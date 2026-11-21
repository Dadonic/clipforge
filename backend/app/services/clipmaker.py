"""Creating clips from highlights with the campaign's template applied. Shared by the API and the worker,
so a clip made by one tap, by "make top N", or automatically after analysis comes out identical."""
import json

from .. import config
from ..db import Campaign, Clip, Highlight, Job, Submission, User, Video
from . import offstream, semantic, transcription


class ClipError(ValueError):
    pass


def words_for(video_id: int, start: float, end: float) -> list[dict]:
    p = transcription.active_transcript(config.WORK_DIR / str(video_id))
    if not p.exists():
        return []
    return [w for w in json.loads(p.read_text())["words"] if w["e"] > start and w["s"] < end]


def offstream_flags(video_id: int, start: float, end: float) -> list[dict]:
    """A clip cut by hand can still land on an ad break or on the player's controls. Flag it for review."""
    p = config.WORK_DIR / str(video_id) / "offstream.json"
    if not p.exists():
        return []
    try:
        segs = offstream.overlap(json.loads(p.read_text()).get("segments", []), start, end)
    except (OSError, ValueError):
        return []
    out = []
    for reason in sorted({s["reason"] for s in segs}):
        secs = sum(min(end, s["e"]) - max(start, s["s"]) for s in segs if s["reason"] == reason)
        out.append({"code": "off_stream" if reason == "ad_or_break" else "player_controls", "severity": "review",
                    "detail": f"{offstream.REASONS[reason]} for about {secs:.0f}s of this clip. Trim it out before posting."})
    return out


def default_layout(v: Video) -> tuple[str, dict, dict]:
    """(layout, layout_params, caption overrides) when neither the campaign nor the user chose one."""
    cam = v.facecam
    if not cam:
        return "blurfit", {}, {}
    # facecam on top, gameplay below. Slide the gameplay crop off the webcam if the webcam sits at a side.
    lp = {}
    fw = min(1.0, (config.OUT_W / (config.OUT_H * 0.66)) * (v.height or 9) / (v.width or 16))
    if fw < 1.0:
        centre = (1 - fw) / 2
        if cam["x"] + cam["w"] / 2 > 0.5 and cam["x"] < centre + fw:           # webcam on the right
            lp["focus_x"] = round(max(0.0, min(centre, cam["x"] - fw)) / (1 - fw), 3)
        elif cam["x"] + cam["w"] / 2 <= 0.5 and cam["x"] + cam["w"] > centre:  # webcam on the left
            lp["focus_x"] = round(min(1 - fw, max(centre, cam["x"] + cam["w"])) / (1 - fw), 3)
    return "split", lp, {"hook_y": 0.38}                                       # hook card under the face, not on it


def new_clip(db, user: User, v: Video, start: float, end: float, h: Highlight | None = None,
             campaign_id: int | None = None) -> Clip:
    if not v.duration or v.status not in ("ready", "uploaded", "analyzing"):
        raise ClipError("Recording is not ready")
    start, end = max(0.0, float(start)), min(float(end), v.duration)
    if end - start < 1:
        raise ClipError("Clip must be at least 1 second long")
    camp = db.get(Campaign, campaign_id or v.campaign_id) if (campaign_id or v.campaign_id) else None
    if camp and camp.user_id != user.id:
        camp = None
    preset = (camp.preset if camp else None) or {}
    us = user.settings or {}
    tags = list(h.hashtags or []) if h else list(semantic.load_profile()["default_hashtags"])
    for t in (camp.required_hashtags if camp else []) or []:
        if t.lower() not in [x.lower() for x in tags]:
            tags.append(t)
    cp = dict(preset.get("caption_params") or us.get("default_caption_params") or {})
    layout, lp = preset.get("layout") or us.get("default_layout"), {}
    if not layout:
        layout, lp, cap = default_layout(v)
        cp = {**cap, **cp}
    if preset.get("overlay"):
        lp = {**lp, "overlay": dict(preset["overlay"])}
    flags = (list(h.flags or []) if h else []) + offstream_flags(v.id, start, end)
    auto_hook = camp.auto_hook if camp is not None and camp.auto_hook is not None else us.get("auto_hook", True)
    if auto_hook and h and not cp.get("hook_text"):
        cp["hook_text"] = (h.signals or {}).get("hook_text") or ""
    if camp and (camp.cta_onscreen or "").strip() and not cp.get("cta_text"):
        cp["cta_text"] = camp.cta_onscreen.strip()
    c = Clip(user_id=user.id, video_id=v.id, highlight_id=h.id if h else None, campaign_id=camp.id if camp else None,
             title=(h.title if h else f"{v.title} clip")[:300], description=(camp.required_cta if camp else "") or "",
             hashtags=tags, start=round(start, 2), end=round(end, 2),
             layout=layout, layout_params=lp, caption_style=preset.get("caption_style") or us.get("default_caption_style") or "pop",
             caption_params=cp, words=words_for(v.id, start, end), quality=us.get("quality") or config.DEFAULT_QUALITY,
             watermark_text=(camp.watermark_text if camp and camp.require_watermark else us.get("watermark", "")) or "",
             flags=flags, category=(h.category if h else "") or "")
    db.add(c)
    if h:
        h.status = "clipped"
    db.flush()
    return c


def queue_render(db, user_id: int, c: Clip) -> None:
    if db.query(Job).filter(Job.clip_id == c.id, Job.type == "render", Job.status.in_(("queued", "running"))).count():
        return
    db.add(Job(user_id=user_id, type="render", clip_id=c.id, video_id=c.video_id, max_attempts=config.JOB_MAX_ATTEMPTS))
    c.status = "rendering"


def make_top_clips(db, user: User, v: Video, n: int) -> list[Clip]:
    """Turn the n best remaining highlights into rendering clips."""
    hs = (db.query(Highlight).filter(Highlight.video_id == v.id, Highlight.status == "suggested")
          .order_by(Highlight.rank).limit(max(0, min(int(n), 30))).all())
    out = []
    for h in hs:
        c = new_clip(db, user, v, h.start, h.end, h)
        queue_render(db, user.id, c)
        out.append(c)
    return out


# --------------------------------------------------------------------------- learning from results
BUCKETS = [(0, 20, "under 20s"), (20, 35, "20-35s"), (35, 60, "35-60s"), (60, 1e9, "over 60s")]


def insights(db, user_id: int) -> dict:
    """What has actually performed, from the user's own recorded submissions. No modelling, just grouped averages."""
    subs = db.query(Submission).filter(Submission.user_id == user_id, Submission.clip_id.isnot(None)).all()
    rows = []
    for s in subs:
        c = db.get(Clip, s.clip_id)
        if c:
            rows.append((s, c))
    total = sum(s.views or 0 for s, _ in rows)
    avg = total / len(rows) if rows else 0.0

    def group(keyfn, labels=None):
        g: dict[str, list[int]] = {}
        for s, c in rows:
            g.setdefault(keyfn(s, c), []).append(s.views or 0)
        out = [{"key": k, "label": (labels or {}).get(k, k), "posts": len(v), "views": sum(v),
                "avg_views": round(sum(v) / len(v)), "vs_avg": round((sum(v) / len(v)) / avg, 2) if avg else None}
               for k, v in g.items() if k]
        return sorted(out, key=lambda x: -x["avg_views"])
    cats = {k: v["label"] for k, v in semantic.load_profile()["categories"].items()}
    cats["moment"] = "Notable moment"
    dur = lambda s, c: next(lbl for lo, hi, lbl in BUCKETS if lo <= c.end - c.start < hi)
    by_cat = group(lambda s, c: c.category or "moment", cats)
    # ranking nudge: only for categories with at least 2 posts, and only once there are 5 posts overall
    boost = {}
    if len(rows) >= 5 and avg > 0:
        for g in by_cat:
            if g["posts"] >= 2:
                boost[g["key"]] = round(max(0.85, min(1.15, 0.85 + 0.15 * min(2.0, g["vs_avg"]))), 3)
    return {"posts": len(rows), "avg_views": round(avg), "by_category": by_cat, "by_length": group(dur),
            "by_layout": group(lambda s, c: c.layout), "by_caption_style": group(lambda s, c: c.caption_style),
            "by_hook": group(lambda s, c: "With hook text" if (c.caption_params or {}).get("hook_text") else "No hook text"),
            "by_platform": group(lambda s, c: s.platform), "category_boost": boost,
            "enough_data": len(rows) >= 5,
            "note": "Averages over your own recorded posts. With few posts these swing a lot; treat them as hints."}
