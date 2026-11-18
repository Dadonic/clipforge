import csv
import io
import json
import os
import re
import tempfile
import zipfile

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from .. import config
from ..auth import current_user
from ..db import Campaign, Clip, Highlight, Job, Submission, User, Video, get_db, log_activity, now, to_dict
from ..services import captions, clipmaker, compliance, render, semantic, storage

router = APIRouter(prefix="/api", tags=["clips"])


def own_clip(db: Session, user: User, cid: int) -> Clip:
    c = db.get(Clip, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Clip not found")
    return c


def clip_out(db: Session, c: Clip, full: bool = False) -> dict:
    v = db.get(Video, c.video_id)
    d = to_dict(c, exclude=("output_path", "words") if not full else ("output_path",))
    d["duration"] = round(c.end - c.start, 2)
    d["has_output"] = bool(c.output_path and os.path.exists(c.output_path))
    d["needs_render"] = (not d["has_output"]) or c.rendered_hash != render.render_hash(compliance.clip_render_dict(c, v))
    d["video_title"] = v.title if v else ""
    camp = db.get(Campaign, c.campaign_id) if c.campaign_id else None
    d["campaign_name"] = camp.name if camp else None
    job = db.query(Job).filter(Job.clip_id == c.id, Job.type == "render").order_by(Job.id.desc()).first()
    d["job"] = to_dict(job) if job and job.status in ("queued", "running", "failed") else None
    d["submissions"] = db.query(Submission).filter(Submission.clip_id == c.id).count()
    if full and v:
        d["panels"] = render.build_panels(c.layout, c.layout_params, v.width, v.height, v.facecam)
        cp = captions.resolve(c.caption_style, c.caption_params)
        d["caption_resolved"] = cp
        d["cues"] = captions.build_cues(c.words or [], c.start, c.end, cp) if cp["enabled"] else []
        d["video"] = {"id": v.id, "duration": v.duration, "width": v.width, "height": v.height,
                      "facecam": v.facecam, "facecam_source": v.facecam_source, "streamer": v.streamer}
    return d


def _new_clip(db, user, v, start, end, h, campaign_id):
    try:
        return clipmaker.new_clip(db, user, v, start, end, h, campaign_id)
    except clipmaker.ClipError as e:
        raise HTTPException(409, str(e))


def queue_render(db, user, c: Clip) -> None:
    clipmaker.queue_render(db, user.id, c)


class FromHighlight(BaseModel):
    campaign_id: int | None = None
    render: bool = True


@router.post("/highlights/{hid}/clip")
def clip_from_highlight(hid: int, body: FromHighlight, db: Session = Depends(get_db), user: User = Depends(current_user)):
    h = db.get(Highlight, hid)
    v = db.get(Video, h.video_id) if h else None
    if not h or not v or v.user_id != user.id:
        raise HTTPException(404, "Highlight not found")
    c = _new_clip(db, user, v, h.start, h.end, h, body.campaign_id)
    if body.render:
        queue_render(db, user, c)
    db.commit()
    return clip_out(db, c)


class TopIn(BaseModel):
    count: int = 5


@router.post("/videos/{vid}/clips/top")
def top_clips(vid: int, body: TopIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """One tap: turn the best remaining highlights into clips with the campaign template, and start rendering."""
    v = db.get(Video, vid)
    if not v or v.user_id != user.id:
        raise HTTPException(404, "Recording not found")
    if not 1 <= body.count <= 30:
        raise HTTPException(400, "count must be between 1 and 30")
    made = clipmaker.make_top_clips(db, user, v, body.count)
    db.commit()
    return {"created": len(made), "ids": [c.id for c in made]}


class ManualClip(BaseModel):
    start: float
    end: float
    campaign_id: int | None = None


@router.post("/videos/{vid}/clips")
def manual_clip(vid: int, body: ManualClip, db: Session = Depends(get_db), user: User = Depends(current_user)):
    v = db.get(Video, vid)
    if not v or v.user_id != user.id:
        raise HTTPException(404, "Recording not found")
    c = _new_clip(db, user, v, body.start, body.end, None, body.campaign_id)
    db.commit()
    return clip_out(db, c)


@router.get("/clips")
def list_clips(status: str | None = None, video_id: int | None = None, campaign_id: int | None = None,
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = db.query(Clip).filter(Clip.user_id == user.id)
    if status:
        q = q.filter(Clip.status.in_(status.split(",")))
    if video_id:
        q = q.filter(Clip.video_id == video_id)
    if campaign_id:
        q = q.filter(Clip.campaign_id == campaign_id)
    return [clip_out(db, c) for c in q.order_by(Clip.id.desc()).all()]


@router.get("/clips/{cid}")
def get_clip(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return clip_out(db, own_clip(db, user, cid), full=True)


class ClipPatch(BaseModel):
    title: str | None = None
    description: str | None = None
    hashtags: list[str] | None = None
    start: float | None = None
    end: float | None = None
    layout: str | None = None
    layout_params: dict | None = None
    caption_style: str | None = None
    caption_params: dict | None = None
    words: list[dict] | None = None
    audio_gain_db: float | None = None
    normalize_audio: bool | None = None
    watermark_text: str | None = None
    watermark_pos: str | None = None
    quality: str | None = None
    campaign_id: int | None = None
    clear_campaign: bool = False
    flags_cleared: bool | None = None


@router.patch("/clips/{cid}")
def patch_clip(cid: int, body: ClipPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_clip(db, user, cid)
    v = db.get(Video, c.video_id)
    data = body.model_dump(exclude_unset=True)
    if "layout" in data and data["layout"] not in render.LAYOUTS:
        raise HTTPException(400, "Unknown layout")
    if "caption_style" in data and data["caption_style"] not in captions.STYLES:
        raise HTTPException(400, "Unknown caption style")
    if "quality" in data and data["quality"] not in config.QUALITY:
        raise HTTPException(400, "Unknown quality")
    if "watermark_pos" in data and data["watermark_pos"] not in ("top", "middle", "bottom"):
        raise HTTPException(400, "Unknown watermark position")
    if "audio_gain_db" in data:
        data["audio_gain_db"] = max(-20.0, min(20.0, float(data["audio_gain_db"] or 0)))
    if "hashtags" in data:
        data["hashtags"] = [("#" + re.sub(r"^#+", "", t.strip()))[:60] for t in data["hashtags"] if t.strip().strip("#")]
    if "words" in data:
        try:
            data["words"] = [{"s": float(w["s"]), "e": float(w["e"]), "w": str(w["w"])[:80]} for w in data["words"]]
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "Each word needs s, e and w")
    ns, ne = data.get("start", c.start), data.get("end", c.end)
    if "start" in data or "end" in data:
        ns, ne = max(0.0, float(ns)), min(float(ne), v.duration or 1e9)
        if ne - ns < 1:
            raise HTTPException(400, "Clip must be at least 1 second long")
        if ne - ns > 600:
            raise HTTPException(400, "Clip is longer than 10 minutes")
        data["start"], data["end"] = round(ns, 2), round(ne, 2)
        if "words" not in data:
            # widening the range pulls in transcript words for the new part; edits inside the old range are kept
            have = c.words or []
            lo = min((w["s"] for w in have), default=1e18)
            hi = max((w["e"] for w in have), default=-1.0)
            extra = [w for w in clipmaker.words_for(v.id, ns, ne) if w["e"] <= lo or w["s"] >= hi]
            if extra:
                data["words"] = sorted(have + extra, key=lambda w: w["s"])
        # moving the cut can move it onto, or off, an ad break or the player's controls
        own = ("off_stream", "player_controls")
        fresh = clipmaker.offstream_flags(v.id, ns, ne)
        had = {f["code"] for f in (c.flags or []) if f.get("code") in own}
        c.flags = [f for f in (c.flags or []) if f.get("code") not in own] + fresh
        if {f["code"] for f in fresh} - had:
            c.flags_cleared = False
    if data.pop("clear_campaign", False):
        c.campaign_id = None
    if data.get("campaign_id") is not None:
        camp = db.get(Campaign, data["campaign_id"])
        if not camp or camp.user_id != user.id:
            raise HTTPException(400, "Campaign not found")
    else:
        data.pop("campaign_id", None)
    was_approved = c.status == "approved"
    before = render.render_hash(compliance.clip_render_dict(c, v))
    for k, val in data.items():
        setattr(c, k, val)
    after = render.render_hash(compliance.clip_render_dict(c, v))
    if before != after or "campaign_id" in data or body.clear_campaign:
        c.compliance = None
        if was_approved:
            c.status = "rendered"     # approval applied to a different cut / different rules
    db.commit()
    return clip_out(db, c, full=True)


@router.post("/clips/{cid}/recaption")
def recaption(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Discard caption edits and reload words from the cached transcript (no re-transcription cost)."""
    c = own_clip(db, user, cid)
    c.words = clipmaker.words_for(c.video_id, c.start, c.end)
    c.compliance = None
    db.commit()
    return clip_out(db, c, full=True)


@router.post("/clips/{cid}/render")
def render_clip(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_clip(db, user, cid)
    queue_render(db, user, c)
    db.commit()
    return clip_out(db, c, full=True)


def _run_check(db, c: Clip) -> dict:
    v = db.get(Video, c.video_id)
    camp = db.get(Campaign, c.campaign_id) if c.campaign_id else None
    total = 0.0
    if camp:
        for s in db.query(Submission).filter(Submission.campaign_id == camp.id).all():
            total += compliance.estimated_earnings(s.approved_views, camp.payout_per_1k, camp.max_payout_per_clip)
    rep = compliance.check_clip(c, v, camp, total)
    c.compliance = rep
    return rep


@router.post("/clips/{cid}/check")
def check(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_clip(db, user, cid)
    rep = _run_check(db, c)
    db.commit()
    return rep


def _approve(db, user, c: Clip) -> dict:
    rep = _run_check(db, c)
    if rep["ok"]:
        c.status = "approved"
        log_activity(db, user.id, "approve", f"Approved clip “{c.title}”")
    return rep


@router.post("/clips/{cid}/approve")
def approve(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_clip(db, user, cid)
    rep = _approve(db, user, c)
    db.commit()
    if not rep["ok"]:
        raise HTTPException(409, detail={"message": f"{rep['fails']} compliance check(s) failed", "report": rep})
    return clip_out(db, c, full=True)


class RejectIn(BaseModel):
    reason: str = ""


@router.post("/clips/{cid}/reject")
def reject(cid: int, body: RejectIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_clip(db, user, cid)
    c.status, c.reject_reason = "rejected", body.reason[:300]
    db.commit()
    return clip_out(db, c, full=True)


def _delete_clip(db, c: Clip) -> None:
    if db.query(Submission).filter(Submission.clip_id == c.id).count():
        raise HTTPException(409, f"“{c.title}” has recorded submissions. Delete those first to keep earnings history consistent.")
    if c.output_path and os.path.exists(c.output_path):
        try:
            os.remove(c.output_path)
        except OSError:
            pass
    if c.highlight_id:
        h = db.get(Highlight, c.highlight_id)
        if h:
            h.status = "suggested"
    db.query(Job).filter(Job.clip_id == c.id).delete()
    db.delete(c)


@router.delete("/clips/{cid}")
def delete_clip(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    _delete_clip(db, own_clip(db, user, cid))
    db.commit()
    return {"ok": True}


class BulkIn(BaseModel):
    ids: list[int]
    action: str
    campaign_id: int | None = None


@router.post("/clips/bulk")
def bulk(body: BulkIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if body.action not in ("render", "approve", "reject", "delete", "assign"):
        raise HTTPException(400, "Unknown action")
    done, skipped = [], []
    for cid in body.ids[:200]:
        c = db.get(Clip, cid)
        if not c or c.user_id != user.id:
            continue
        try:
            if body.action == "render":
                queue_render(db, user, c)
            elif body.action == "approve":
                rep = _approve(db, user, c)
                if not rep["ok"]:
                    bad = [k["label"] for k in rep["checks"] if k["status"] == "fail"]
                    skipped.append({"id": cid, "title": c.title, "why": "; ".join(bad)})
                    continue
            elif body.action == "reject":
                c.status = "rejected"
            elif body.action == "assign":
                camp = db.get(Campaign, body.campaign_id) if body.campaign_id else None
                if not camp or camp.user_id != user.id:
                    raise HTTPException(400, "Campaign not found")
                c.campaign_id, c.compliance = camp.id, None
                if c.status == "approved":
                    c.status = "rendered"
            elif body.action == "delete":
                _delete_clip(db, c)
            done.append(cid)
        except HTTPException as e:
            if body.action == "assign":
                raise
            skipped.append({"id": cid, "title": c.title, "why": str(e.detail)})
    db.commit()
    return {"done": done, "skipped": skipped}


def _fname(c: Clip) -> str:
    base = re.sub(r"[^A-Za-z0-9 _-]+", "", c.title).strip().replace(" ", "_")[:60] or "clip"
    return f"{base}_{c.id}.mp4"


@router.get("/clips/{cid}/file")
def clip_file(cid: int, download: int = 0, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_clip(db, user, cid)
    if not c.output_path or not os.path.exists(c.output_path):
        key = (c.out_meta or {}).get("s3_key")
        if key and storage.enabled():
            return RedirectResponse(storage.presigned_url(key, _fname(c)))
        raise HTTPException(404, "This clip has not been rendered yet")
    if download:
        c.exported_at = now()
        db.commit()
        return FileResponse(c.output_path, media_type="video/mp4", filename=_fname(c))
    return FileResponse(c.output_path, media_type="video/mp4")


@router.get("/export.zip")
def export_zip(ids: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Bundle rendered clips with a posting sheet (title, description, hashtags, campaign) per clip."""
    try:
        want = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated numbers")
    clips = [c for c in (db.get(Clip, i) for i in want[:100]) if c and c.user_id == user.id
             and c.output_path and os.path.exists(c.output_path)]
    if not clips:
        raise HTTPException(404, "None of the selected clips are rendered")
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=config.DATA_DIR)
    tmp.close()
    sheet = io.StringIO()
    wr = csv.writer(sheet)
    wr.writerow(["file", "title", "description", "hashtags", "campaign", "duration_s", "status"])
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as z:
        for c in clips:
            camp = db.get(Campaign, c.campaign_id) if c.campaign_id else None
            z.write(c.output_path, _fname(c))
            wr.writerow([_fname(c), c.title, c.description, " ".join(c.hashtags or []), camp.name if camp else "",
                         f"{c.end - c.start:.1f}", c.status])
            c.exported_at = now()
        z.writestr("posting_sheet.csv", sheet.getvalue())
    db.commit()
    return FileResponse(tmp.name, media_type="application/zip", filename="clipforge_export.zip",
                        background=BackgroundTask(os.remove, tmp.name))


UPLOAD_LINKS = {"tiktok": "https://www.tiktok.com/upload", "youtube_shorts": "https://studio.youtube.com",
                "x": "https://x.com/compose/post"}
PLATFORM_NAMES = {"tiktok": "TikTok", "youtube_shorts": "YouTube Shorts", "instagram_reels": "Instagram Reels", "x": "X",
                  "facebook_reels": "Facebook Reels", "snapchat": "Snapchat", "other": "Other"}


@router.get("/clips/{cid}/posting")
def posting_kit(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Ready-to-paste text for each platform the campaign allows, plus the campaign's after-posting rules."""
    c = own_clip(db, user, cid)
    camp = db.get(Campaign, c.campaign_id) if c.campaign_id else None
    tags = " ".join(c.hashtags or [])
    desc = (c.description or "").strip()
    plats = (camp.approved_platforms if camp and camp.approved_platforms else ["tiktok", "instagram_reels", "youtube_shorts"])
    posted = {s.platform for s in db.query(Submission).filter(Submission.clip_id == c.id)}
    out = []
    for p in plats:
        if p == "youtube_shorts":
            head = " ".join((c.hashtags or [])[:3])
            title = c.title if len(c.title) + len(head) + 1 <= 100 else c.title[:99 - len(head)].rstrip()
            fields = [{"label": "Title", "text": f"{title} {head}".strip()},
                      {"label": "Description", "text": " ".join(x for x in [desc, tags] if x)}]
        elif p == "x":
            body = " ".join(x for x in [c.title, desc] if x)
            room = 280 - len(tags) - 1
            fields = [{"label": "Post text", "text": f"{body[:max(0, room)].rstrip()} {tags}".strip()}]
        else:
            fields = [{"label": "Caption", "text": " ".join(x for x in [c.title, desc, tags] if x)}]
        out.append({"platform": p, "name": PLATFORM_NAMES.get(p, p), "fields": fields, "upload_url": UPLOAD_LINKS.get(p),
                    "posted": p in posted})
    rules = []
    if camp:
        if camp.submit_within_min:
            rules.append(f"Submit the link on the campaign page within {camp.submit_within_min} minutes of posting.")
        if camp.min_views:
            rules.append(f"A clip earns only after {camp.min_views:,} views.")
        if camp.min_tier1_pct:
            rules.append(f"At least {camp.min_tier1_pct:g}% of viewers must be in the campaign's required countries. "
                         f"Record the figure from your analytics on the submission.")
        if camp.keep_live_days:
            rules.append(f"Keep the clip live for at least {camp.keep_live_days} days.")
        if (camp.submission_instructions or "").strip():
            rules.append(camp.submission_instructions.strip())
    return {"approved": c.status == "approved", "platforms": out, "rules": rules,
            "campaign_url": camp.whop_url if camp else "", "campaign_name": camp.name if camp else None}


@router.get("/insights")
def insights(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return clipmaker.insights(db, user.id)


@router.get("/clips/{cid}/suggest")
def suggest(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Editing assistant. Everything is derived from the transcript and measured audio; nothing is invented."""
    from ..services import highlights as hl
    c = own_clip(db, user, cid)
    v = db.get(Video, c.video_id)
    camp = db.get(Campaign, c.campaign_id) if c.campaign_id else None
    prof = semantic.load_profile()
    words = [w for w in (c.words or []) if w["e"] > c.start and w["s"] < c.end]
    sents = hl.build_sentences(words)
    dur = c.end - c.start
    tips = []
    audio = None
    ap = config.WORK_DIR / str(v.id) / "audio.json"
    if ap.exists():
        audio = json.loads(ap.read_text())
    dead = []
    if audio and audio.get("silence"):
        sil = audio["silence"]
        t = int(c.start)
        run = None
        while t < min(int(c.end), len(sil)):
            quiet = sil[t] > 0.85 and not any(w["s"] < t + 1 and w["e"] > t for w in words)
            if quiet and run is None:
                run = t
            if (not quiet or t == int(c.end) - 1) and run is not None:
                if t - run >= 2:
                    dead.append({"start": run, "end": t, "seconds": t - run})
                run = None
            t += 1
    for d in dead:
        tips.append({"type": "dead_time", "text": f"{d['seconds']}s of quiet with no speech at "
                     f"{d['start'] - c.start:.0f}s into the clip. Consider trimming around it."})
    trim = {}
    if words:
        if words[0]["s"] - c.start > 1.5:
            trim["start"] = round(max(0.0, words[0]["s"] - 0.4), 2)
            tips.append({"type": "start", "text": f"First word is {words[0]['s'] - c.start:.1f}s in. "
                         f"Start at {trim['start']:.1f}s so the clip opens on speech."})
        if sents and c.end - sents[-1]["e"] > 2.5:
            trim["end"] = round(sents[-1]["e"] + 0.5, 2)
            tips.append({"type": "end", "text": f"{c.end - sents[-1]['e']:.1f}s of tail after the last sentence. "
                         f"End at {trim['end']:.1f}s unless the reaction is visual."})
        first = words[0]["w"].lower().strip(",.")
        if first in prof["weak_openers"]:
            tips.append({"type": "hook", "text": f"Clip opens mid-thought (“{first}…”). Move the start back "
                         "to the beginning of that sentence or forward to the next one."})
    else:
        tips.append({"type": "captions", "text": "No speech found in this range, so captions will be empty. "
                     "Consider a text hook in the title instead."})
    best = max(sents, key=lambda s: semantic.text_score(s["text"], prof), default=None)
    if best and best["s"] - c.start > 6 and semantic.text_score(best["text"], prof) > 0.25:
        tips.append({"type": "hook", "text": f"Strongest line is {best['s'] - c.start:.0f}s in: “{best['text'][:80]}”. "
                     "Viewers decide in the first 2-3 seconds; consider a tighter start."})
    lo, hi = (camp.min_duration, camp.max_duration) if camp else (15, 60)
    ideal = min(max(dur, lo), hi)
    if dur > hi or dur < lo:
        tips.append({"type": "duration", "text": f"Clip is {dur:.0f}s; {'campaign' if camp else 'default'} range is {lo:g}-{hi:g}s."})
    wps = len(words) / max(dur, 1)
    cap = "pop" if wps >= 2.2 else ("oneword" if 0 < wps < 1.2 else "karaoke")
    layout = "split" if v.facecam else "blurfit"
    sig = {}
    h = db.get(Highlight, c.highlight_id) if c.highlight_id else None
    if h:
        sig = h.signals or {}
    cat, label = semantic.classify(" ".join(w["w"] for w in words), sig, prof)
    copy = semantic.heuristic_copy([s["text"] for s in sents], sig, cat, label, (camp.streamer if camp else "") or v.streamer,
                                   prof, (camp.required_hashtags if camp else None))
    desc = " ".join(x for x in [copy["title_options"][0], (camp.required_cta if camp else ""), " ".join(copy["hashtags"])] if x)
    return {"titles": copy["title_options"], "hook": copy["hook"], "hashtags": copy["hashtags"], "description": desc,
            "tips": tips, "trim": trim, "ideal_duration": round(ideal, 1),
            "caption_style": {"value": cap, "why": f"{wps:.1f} words/sec in this clip"},
            "layout": {"value": layout, "why": "Facecam detected on this recording" if v.facecam else
                       "No facecam found; this layout keeps the whole game HUD visible"},
            "source": "heuristic (transcript + audio measurements)"}
