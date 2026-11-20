"""Campaign compliance engine. Produces a transparent checklist; a clip can only be approved when no check fails.
Nothing here guarantees a platform or campaign owner will accept a clip - it only catches rule breaks we can measure.
"""
import re
from ..db import now
from . import render


def _norm_tag(t: str) -> str:
    return "#" + re.sub(r"[^a-z0-9_]", "", t.lower())


def estimated_earnings(approved_views: int, rate_per_1k: float, cap: float | None = None, min_views: int = 0) -> float:
    if min_views and (approved_views or 0) < min_views:
        return 0.0                      # below the campaign's minimum a clip earns nothing
    v = (max(0, approved_views or 0) / 1000.0) * (rate_per_1k or 0.0)
    if cap:
        v = min(v, cap)
    return round(v, 2)


def check_clip(clip, video, campaign, campaign_estimated_total: float = 0.0) -> dict:
    checks = []

    def add(code, label, status, detail=""):
        checks.append({"code": code, "label": label, "status": status, "detail": detail})

    cur_hash = render.render_hash(clip_render_dict(clip, video))
    meta = clip.out_meta or {}
    if not clip.output_path or not meta:
        add("rendered", "Clip is rendered", "fail", "Render the clip first.")
    elif clip.rendered_hash != cur_hash:
        add("rendered", "Render is up to date", "fail", "Edits were made after the last render. Render again.")
    else:
        add("rendered", "Clip is rendered and up to date", "pass")
        ok = meta.get("width") == 1080 and meta.get("height") == 1920
        add("aspect", "9:16 at 1080x1920", "pass" if ok else "fail", f"{meta.get('width')}x{meta.get('height')}")
        ok = meta.get("vcodec") == "h264" and meta.get("acodec") in ("aac", None)
        add("codec", "H.264 video / AAC audio", "pass" if ok else "fail", f"{meta.get('vcodec')}/{meta.get('acodec')}")

    add("authorized", "Footage marked as authorised", "pass" if video.authorized else "fail",
        "" if video.authorized else "Confirm on the recording that you are allowed to use this footage.")

    review = [f for f in (clip.flags or []) if f.get("severity") == "review"]
    if review and not clip.flags_cleared:
        add("flags", "Content flags reviewed", "fail",
            "Unreviewed: " + ", ".join(f["code"] for f in review) + ". Watch the clip, then mark the flags as reviewed.")
    elif review:
        add("flags", "Content flags reviewed by you", "pass", ", ".join(f["code"] for f in review))
    else:
        add("flags", "No content flags raised", "pass")

    dur = clip.end - clip.start
    if campaign is None:
        add("campaign", "Assigned to a campaign", "warn", "No campaign assigned - campaign rules were not checked.")
        return _finish(checks)

    lo, hi = campaign.min_duration or 0, campaign.max_duration or 1e9
    add("duration", f"Duration within {lo:g}-{hi:g}s", "pass" if lo - 0.05 <= dur <= hi + 0.05 else "fail", f"{dur:.1f}s")

    have = {_norm_tag(t) for t in (clip.hashtags or [])}
    missing = [t for t in (campaign.required_hashtags or []) if _norm_tag(t) not in have]
    if campaign.required_hashtags:
        add("hashtags", "Required hashtags present", "fail" if missing else "pass",
            ("Missing: " + " ".join(missing)) if missing else " ".join(campaign.required_hashtags))

    cp = clip.caption_params or {}
    onscreen = f"{cp.get('hook_text') or ''} {cp.get('cta_text') or ''}".lower()
    text = f"{clip.title} {clip.description} {onscreen}".lower()
    if (campaign.required_cta or "").strip():
        ok = campaign.required_cta.strip().lower() in text
        add("cta", "Required caption / CTA included", "pass" if ok else "fail",
            "" if ok else f"Add \u201c{campaign.required_cta.strip()}\u201d to the description or the on-screen text.")
    want_card = (getattr(campaign, "cta_onscreen", "") or "").strip()
    if want_card:
        ok = want_card.lower() in onscreen
        add("cta_card", "Call to action shown on screen", "pass" if ok else "fail",
            "" if ok else f"Put \u201c{want_card}\u201d in the end card or hook text.")

    want_logo = ((getattr(campaign, "preset", None) or {}).get("overlay") or {}).get("file")
    if want_logo:
        have = ((clip.layout_params or {}).get("overlay") or {}).get("file")
        ok = have == want_logo and render.overlay_spec(clip.layout_params) is not None
        add("overlay", "Campaign logo overlay on the whole clip", "pass" if ok else "fail",
            "" if ok else "This campaign requires its logo on every clip. Turn the logo overlay on in the Layout tab.")
    if campaign.require_watermark:
        wm = (clip.watermark_text or "").strip()
        want = (campaign.watermark_text or "").strip()
        ok = bool(wm) and (not want or want.lower() in wm.lower())
        add("watermark", "Required watermark on video", "pass" if ok else "fail",
            "" if ok else (f"Watermark must contain “{want}”." if want else "Add a watermark."))

    if campaign.require_attribution and (campaign.streamer or "").strip():
        s = campaign.streamer.strip().lower()
        hay = text + " " + " ".join(clip.hashtags or []).lower() + " " + (clip.watermark_text or "").lower()
        ok = s in hay or re.sub(r"[^a-z0-9]", "", s) in re.sub(r"[^a-z0-9#]", "", hay)
        add("attribution", "Streamer credited", "pass" if ok else "fail",
            "" if ok else f"Mention {campaign.streamer} in the title, description, hashtags or watermark.")

    transcript = " ".join(w["w"] for w in (clip.words or []) if clip.start <= w["s"] <= clip.end).lower()
    bad = [t for t in (campaign.prohibited or []) if t.strip() and t.strip().lower() in f"{text} {transcript}"]
    if campaign.prohibited:
        add("prohibited", "No prohibited terms", "fail" if bad else "pass",
            ("Found: " + ", ".join(bad)) if bad else "Checked title, description and transcript.")

    if campaign.status == "ended":
        add("status", "Campaign is open", "fail", "Campaign is marked as ended.")
    elif campaign.status != "active":
        add("status", "Campaign is open", "warn", f"Campaign status is “{campaign.status}”.")
    else:
        add("status", "Campaign is open", "pass")

    if campaign.deadline:
        late = campaign.deadline < now()
        add("deadline", "Before campaign deadline", "fail" if late else "pass", campaign.deadline.strftime("%d %b %Y %H:%M"))

    if campaign.max_budget and campaign_estimated_total >= campaign.max_budget:
        add("budget", "Campaign budget remaining", "warn",
            "Your own estimated earnings already reach the campaign budget; it may be exhausted.")
    return _finish(checks)


def _finish(checks: list[dict]) -> dict:
    return {"ok": not any(c["status"] == "fail" for c in checks),
            "fails": sum(c["status"] == "fail" for c in checks),
            "warnings": sum(c["status"] == "warn" for c in checks),
            "checks": checks, "checked_at": now().isoformat() + "Z"}


def clip_render_dict(clip, video) -> dict:
    return {"start": clip.start, "end": clip.end, "layout": clip.layout, "layout_params": clip.layout_params or {},
            "caption_style": clip.caption_style, "caption_params": clip.caption_params or {}, "words": clip.words or [],
            "audio_gain_db": clip.audio_gain_db or 0.0, "normalize_audio": bool(clip.normalize_audio),
            "watermark_text": clip.watermark_text or "", "watermark_pos": clip.watermark_pos or "top",
            "quality": clip.quality or "standard", "facecam": video.facecam}
