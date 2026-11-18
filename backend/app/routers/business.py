"""Campaigns, submissions, earnings and the command-centre dashboard. Every number is computed from stored rows."""
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import (Activity, Campaign, Clip, Job, Snapshot, Submission, User, Video, get_db, log_activity, now, to_dict)
from ..services.compliance import estimated_earnings

router = APIRouter(prefix="/api", tags=["business"])
PLATFORMS = ["tiktok", "youtube_shorts", "instagram_reels", "x", "facebook_reels", "snapchat", "other"]


def _dt(s: str | None):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
            try:
                return datetime.strptime(s.strip(), fmt)
            except ValueError:
                pass
    raise HTTPException(400, f"Could not read date “{s}”. Use YYYY-MM-DD.")


def own_campaign(db, user, cid) -> Campaign:
    c = db.get(Campaign, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(404, "Campaign not found")
    return c


def sub_block(s: Submission, camp: Campaign | None) -> str | None:
    """Why this submission cannot earn yet under the campaign's own rules (None = nothing blocking)."""
    if not camp:
        return None
    if camp.min_views and (s.views or 0) < camp.min_views:
        return f"Below the {camp.min_views:,}-view minimum"
    if camp.min_tier1_pct:
        if s.tier1_pct is None:
            return "Audience share not recorded yet"
        if s.tier1_pct < camp.min_tier1_pct:
            return f"Audience {s.tier1_pct:g}% is under the required {camp.min_tier1_pct:g}%"
    return None


def sub_est(s: Submission, camp: Campaign | None) -> float:
    if camp and camp.min_tier1_pct and s.tier1_pct is not None and s.tier1_pct < camp.min_tier1_pct:
        return 0.0
    return estimated_earnings(s.approved_views, camp.payout_per_1k if camp else 0, camp.max_payout_per_clip if camp else None,
                              (camp.min_views or 0) if camp else 0)


def campaign_out(db, c: Campaign) -> dict:
    d = to_dict(c)
    subs = db.query(Submission).filter(Submission.campaign_id == c.id).all()
    est = sum(sub_est(s, c) for s in subs)
    d["stats"] = {
        "clips": db.query(Clip).filter(Clip.campaign_id == c.id).count(),
        "approved_clips": db.query(Clip).filter(Clip.campaign_id == c.id, Clip.status == "approved").count(),
        "submissions": len(subs), "views": sum(s.views or 0 for s in subs),
        "approved_views": sum(s.approved_views or 0 for s in subs), "estimated": round(est, 2),
        "confirmed": round(sum(s.confirmed_earnings or 0 for s in subs if s.payment_status == "paid"), 2),
        "budget_used_pct": round(100 * est / c.max_budget, 1) if c.max_budget else None,
    }
    return d


class CampaignIn(BaseModel):
    name: str | None = None
    streamer: str | None = None
    whop_url: str | None = None
    instructions: str | None = None
    payout_per_1k: float | None = None
    max_budget: float | None = None
    max_payout_per_clip: float | None = None
    min_duration: float | None = None
    max_duration: float | None = None
    required_hashtags: list[str] | None = None
    required_cta: str | None = None
    approved_platforms: list[str] | None = None
    prohibited: list[str] | None = None
    submission_instructions: str | None = None
    deadline: str | None = None
    status: str | None = None
    require_watermark: bool | None = None
    watermark_text: str | None = None
    require_attribution: bool | None = None
    notes: str | None = None
    preset: dict | None = None
    min_views: int | None = None
    min_tier1_pct: float | None = None
    submit_within_min: int | None = None
    keep_live_days: int | None = None
    cta_onscreen: str | None = None
    auto_hook: bool | None = None


def _apply_campaign(c: Campaign, body: CampaignIn):
    data = body.model_dump(exclude_unset=True)
    if "status" in data and data["status"] not in ("lead", "active", "paused", "ended"):
        raise HTTPException(400, "status must be lead, active, paused or ended")
    if "deadline" in data:
        data["deadline"] = _dt(data["deadline"])
    for k in ("payout_per_1k", "max_budget", "max_payout_per_clip", "min_duration", "max_duration", "min_views",
              "min_tier1_pct", "submit_within_min", "keep_live_days"):
        if data.get(k) is not None and data[k] < 0:
            raise HTTPException(400, f"{k} cannot be negative")
    if data.get("min_tier1_pct") is not None and data["min_tier1_pct"] > 100:
        raise HTTPException(400, "Audience requirement is a percentage (0-100)")
    if "min_views" in data and data["min_views"] is None:
        data["min_views"] = 0
    if "auto_hook" in data and data["auto_hook"] is None:
        data["auto_hook"] = True
    if "required_hashtags" in data and data["required_hashtags"] is not None:
        data["required_hashtags"] = ["#" + t.strip().lstrip("#") for t in data["required_hashtags"] if t.strip().strip("#")]
    if "approved_platforms" in data and data["approved_platforms"] is not None:
        bad = [p for p in data["approved_platforms"] if p not in PLATFORMS]
        if bad:
            raise HTTPException(400, f"Unknown platform: {', '.join(bad)}")
    for k, v in data.items():
        setattr(c, k, v)
    if (c.min_duration or 0) > (c.max_duration or 1e9):
        raise HTTPException(400, "Minimum duration is longer than maximum duration")


@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [campaign_out(db, c) for c in db.query(Campaign).filter(Campaign.user_id == user.id).order_by(Campaign.id.desc())]


@router.post("/campaigns")
def create_campaign(body: CampaignIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not (body.name or "").strip():
        raise HTTPException(400, "Campaign name is required")
    c = Campaign(user_id=user.id, name=body.name.strip())
    _apply_campaign(c, body)
    db.add(c)
    log_activity(db, user.id, "campaign", f"Added campaign “{c.name}”")
    db.commit()
    return campaign_out(db, c)


@router.patch("/campaigns/{cid}")
def update_campaign(cid: int, body: CampaignIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_campaign(db, user, cid)
    _apply_campaign(c, body)
    # rules changed: stored compliance reports for this campaign's clips are stale
    for clip in db.query(Clip).filter(Clip.campaign_id == cid).all():
        clip.compliance = None
    db.commit()
    return campaign_out(db, c)


@router.delete("/campaigns/{cid}")
def delete_campaign(cid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    c = own_campaign(db, user, cid)
    if db.query(Submission).filter(Submission.campaign_id == cid).count():
        raise HTTPException(409, "This campaign has recorded submissions. Mark it as ended instead, so earnings history stays intact.")
    for clip in db.query(Clip).filter(Clip.campaign_id == cid).all():
        clip.campaign_id, clip.compliance = None, None
    for v in db.query(Video).filter(Video.campaign_id == cid).all():
        v.campaign_id = None
    db.delete(c)
    db.commit()
    return {"ok": True}


# --------------------------------------------------------------------------- submissions
def sub_out(db, s: Submission) -> dict:
    d = to_dict(s)
    camp = db.get(Campaign, s.campaign_id) if s.campaign_id else None
    clip = db.get(Clip, s.clip_id) if s.clip_id else None
    d["campaign_name"] = camp.name if camp else None
    d["clip_title"] = clip.title if clip else None
    d["payout_per_1k"] = camp.payout_per_1k if camp else 0
    d["estimated_earnings"] = sub_est(s, camp)
    d["blocked"] = sub_block(s, camp)
    d["needs_tier1"] = bool(camp and camp.min_tier1_pct)
    return d


class SubIn(BaseModel):
    clip_id: int | None = None
    campaign_id: int | None = None
    platform: str | None = None
    url: str | None = None
    views: int | None = None
    approved_views: int | None = None
    confirmed_earnings: float | None = None
    payment_status: str | None = None
    payment_date: str | None = None
    submitted_at: str | None = None
    notes: str | None = None
    tier1_pct: float | None = None


def _apply_sub(db, user, s: Submission, body: SubIn, creating: bool):
    data = body.model_dump(exclude_unset=True)
    if "payment_status" in data and data["payment_status"] not in ("pending", "approved", "paid", "rejected"):
        raise HTTPException(400, "payment_status must be pending, approved, paid or rejected")
    if "platform" in data and data["platform"] not in PLATFORMS:
        raise HTTPException(400, f"platform must be one of: {', '.join(PLATFORMS)}")
    for k in ("views", "approved_views", "confirmed_earnings", "tier1_pct"):
        if data.get(k) is not None and data[k] < 0:
            raise HTTPException(400, f"{k} cannot be negative")
    if data.get("tier1_pct") is not None and data["tier1_pct"] > 100:
        raise HTTPException(400, "Audience share is a percentage (0-100)")
    for k in ("payment_date", "submitted_at"):
        if k in data:
            data[k] = _dt(data[k])
    if data.get("submitted_at") is None:
        data.pop("submitted_at", None)
    if data.get("clip_id") is not None:
        clip = db.get(Clip, data["clip_id"])
        if not clip or clip.user_id != user.id:
            raise HTTPException(400, "Clip not found")
        if creating and clip.status != "approved":
            raise HTTPException(409, "Only approved clips can be recorded as submitted. Approve the clip first.")
        data.setdefault("campaign_id", clip.campaign_id)
    if data.get("campaign_id") is not None:
        camp = own_campaign(db, user, data["campaign_id"])
        plat = data.get("platform", s.platform)
        if camp.approved_platforms and plat and plat not in camp.approved_platforms:
            raise HTTPException(409, f"{plat} is not an approved platform for “{camp.name}” "
                                     f"(allowed: {', '.join(camp.approved_platforms)}).")
    for k, v in data.items():
        setattr(s, k, v)
    if (s.approved_views or 0) > (s.views or 0):
        raise HTTPException(400, "Approved views cannot be higher than total views")
    if s.payment_status == "paid" and not s.payment_date:
        s.payment_date = now()


def _snapshot(db, s: Submission):
    camp = db.get(Campaign, s.campaign_id) if s.campaign_id else None
    db.add(Snapshot(submission_id=s.id, views=s.views or 0, approved_views=s.approved_views or 0, estimated=sub_est(s, camp)))


@router.get("/submissions")
def list_subs(campaign_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = db.query(Submission).filter(Submission.user_id == user.id)
    if campaign_id:
        q = q.filter(Submission.campaign_id == campaign_id)
    return [sub_out(db, s) for s in q.order_by(Submission.id.desc()).all()]


@router.post("/submissions")
def create_sub(body: SubIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not (body.url or "").strip():
        raise HTTPException(400, "Paste the link to the posted clip. A submission is only recorded once it really exists.")
    if not body.platform:
        raise HTTPException(400, "Choose the platform the clip was posted on")
    key = body.url.strip().rstrip("/").lower()
    if any(x.url.strip().rstrip("/").lower() == key for x in db.query(Submission).filter(Submission.user_id == user.id)):
        raise HTTPException(409, "This link is already recorded. Update its numbers in the submissions table instead.")
    s = Submission(user_id=user.id)
    _apply_sub(db, user, s, body, creating=True)
    db.add(s)
    db.flush()
    _snapshot(db, s)
    log_activity(db, user.id, "submission", f"Recorded submission on {s.platform}")
    db.commit()
    return sub_out(db, s)


@router.patch("/submissions/{sid}")
def update_sub(sid: int, body: SubIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = db.get(Submission, sid)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "Submission not found")
    before = (s.views, s.approved_views)
    _apply_sub(db, user, s, body, creating=False)
    if (s.views, s.approved_views) != before:
        _snapshot(db, s)
    db.commit()
    return sub_out(db, s)


@router.delete("/submissions/{sid}")
def delete_sub(sid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    s = db.get(Submission, sid)
    if not s or s.user_id != user.id:
        raise HTTPException(404, "Submission not found")
    db.query(Snapshot).filter(Snapshot.submission_id == sid).delete()
    db.delete(s)
    db.commit()
    return {"ok": True}


_COLS = {"url": ("url", "submission_url", "link", "post_url", "video_url"),
         "views": ("views", "total_views", "view_count"),
         "approved_views": ("approved_views", "verified_views", "eligible_views", "paid_views"),
         "confirmed_earnings": ("confirmed_earnings", "earnings", "payout", "paid", "amount", "earned"),
         "payment_status": ("payment_status", "status"),
         "payment_date": ("payment_date", "paid_at", "paid_date", "date"),
         "tier1_pct": ("tier1_pct", "tier_1_pct", "tier1", "tier_1", "audience_pct", "tier_1_audience")}


@router.post("/submissions/import-csv")
async def import_csv(file: UploadFile, db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Update existing submissions from a performance export. Rows are matched by post URL; unknown URLs are
    reported back, never silently created, so earnings cannot appear for clips that were not recorded."""
    raw = (await file.read(10 * 1024 * 1024)).decode("utf-8-sig", errors="ignore")
    rd = csv.DictReader(io.StringIO(raw))
    if not rd.fieldnames:
        raise HTTPException(422, "The file has no header row")
    lower = {f.lower().strip().replace(" ", "_"): f for f in rd.fieldnames}
    cmap = {k: next((lower[a] for a in al if a in lower), None) for k, al in _COLS.items()}
    if not cmap["url"]:
        raise HTTPException(422, f"No URL column found. Expected one of: {', '.join(_COLS['url'])}. Found: {', '.join(rd.fieldnames)}")
    subs = {s.url.strip().rstrip("/").lower(): s for s in db.query(Submission).filter(Submission.user_id == user.id)}
    updated, unmatched, errors = 0, [], []

    def num(row, key, cast):
        col = cmap[key]
        if not col or not str(row.get(col, "")).strip():
            return None
        return cast(str(row[col]).replace(",", "").replace("$", "").strip())
    for i, row in enumerate(rd, start=2):
        url = str(row.get(cmap["url"], "")).strip().rstrip("/").lower()
        if not url:
            continue
        s = subs.get(url)
        if not s:
            unmatched.append(url)
            continue
        try:
            body = SubIn()
            v, av, ce = num(row, "views", lambda x: int(float(x))), num(row, "approved_views", lambda x: int(float(x))), \
                num(row, "confirmed_earnings", float)
            patch = {}
            if v is not None:
                patch["views"] = v
            if av is not None:
                patch["approved_views"] = av
            if ce is not None:
                patch["confirmed_earnings"] = ce
            t1 = num(row, "tier1_pct", lambda x: float(x.replace("%", "")))
            if t1 is not None:
                patch["tier1_pct"] = t1
            st = str(row.get(cmap["payment_status"] or "", "")).strip().lower()
            if st in ("pending", "approved", "paid", "rejected"):
                patch["payment_status"] = st
            pdv = str(row.get(cmap["payment_date"] or "", "")).strip()
            if pdv:
                patch["payment_date"] = pdv
            before = (s.views, s.approved_views)
            _apply_sub(db, user, s, SubIn(**patch), creating=False)
            if (s.views, s.approved_views) != before:
                _snapshot(db, s)
            updated += 1
        except (ValueError, HTTPException) as e:
            errors.append(f"Row {i}: {getattr(e, 'detail', e)}")
    db.commit()
    return {"updated": updated, "unmatched": unmatched[:50], "errors": errors[:50],
            "columns_used": {k: v for k, v in cmap.items() if v}}


# --------------------------------------------------------------------------- earnings + dashboard
def _earnings(db, user) -> dict:
    subs = db.query(Submission).filter(Submission.user_id == user.id).all()
    camps = {c.id: c for c in db.query(Campaign).filter(Campaign.user_id == user.id)}
    today = now().date()
    est_total = sum(sub_est(s, camps.get(s.campaign_id)) for s in subs)
    paid = [s for s in subs if s.payment_status == "paid"]
    confirmed = sum(s.confirmed_earnings or 0 for s in paid)
    pending = sum(sub_est(s, camps.get(s.campaign_id)) for s in subs if s.payment_status in ("pending", "approved"))

    # daily series (last 30 days): confirmed by payment date; estimate growth from snapshots
    days = [today - timedelta(days=i) for i in range(29, -1, -1)]
    conf_by_day = defaultdict(float)
    for s in paid:
        if s.payment_date:
            conf_by_day[s.payment_date.date()] += s.confirmed_earnings or 0
    est_by_day = defaultdict(float)
    sub_ids = [s.id for s in subs]
    if sub_ids:
        last = {}
        for sn in db.query(Snapshot).filter(Snapshot.submission_id.in_(sub_ids)).order_by(Snapshot.at).all():
            delta = sn.estimated - last.get(sn.submission_id, 0.0)
            last[sn.submission_id] = sn.estimated
            est_by_day[sn.at.date()] += delta
    series = [{"date": d.isoformat(), "confirmed": round(conf_by_day.get(d, 0), 2),
               "estimated": round(est_by_day.get(d, 0), 2)} for d in days]

    def window(n):
        cut = today - timedelta(days=n - 1)
        return {"confirmed": round(sum(v for d, v in conf_by_day.items() if d >= cut), 2),
                "estimated": round(sum(v for d, v in est_by_day.items() if d >= cut), 2)}
    per_campaign = []
    for c in camps.values():
        cs = [s for s in subs if s.campaign_id == c.id]
        if not cs and c.status != "active":
            continue
        e = sum(sub_est(s, c) for s in cs)
        per_campaign.append({"id": c.id, "name": c.name, "status": c.status, "payout_per_1k": c.payout_per_1k,
                             "submissions": len(cs), "approved_views": sum(s.approved_views or 0 for s in cs),
                             "estimated": round(e, 2),
                             "confirmed": round(sum(s.confirmed_earnings or 0 for s in cs if s.payment_status == "paid"), 2),
                             "max_budget": c.max_budget,
                             "budget_used_pct": round(100 * e / c.max_budget, 1) if c.max_budget else None})
    per_clip = defaultdict(lambda: {"views": 0, "approved_views": 0, "estimated": 0.0, "confirmed": 0.0})
    for s in subs:
        if s.clip_id:
            pc = per_clip[s.clip_id]
            pc["views"] += s.views or 0
            pc["approved_views"] += s.approved_views or 0
            pc["estimated"] += sub_est(s, camps.get(s.campaign_id))
            pc["confirmed"] += (s.confirmed_earnings or 0) if s.payment_status == "paid" else 0
    top = []
    for cid, pc in sorted(per_clip.items(), key=lambda kv: -kv[1]["approved_views"])[:8]:
        clip = db.get(Clip, cid)
        if clip:
            top.append({"id": cid, "title": clip.title, **{k: round(v, 2) if isinstance(v, float) else v for k, v in pc.items()}})
    av = sum(s.approved_views or 0 for s in subs)
    return {"estimated_total": round(est_total, 2), "confirmed_total": round(confirmed, 2), "pending_estimated": round(pending, 2),
            "paid_count": len(paid), "total_views": sum(s.views or 0 for s in subs), "approved_views": av,
            "avg_per_1k_estimated": round(est_total / av * 1000, 3) if av else None,
            "avg_per_1k_confirmed": round(confirmed / sum(s.approved_views or 0 for s in paid) * 1000, 3)
            if paid and sum(s.approved_views or 0 for s in paid) else None,
            "today": window(1), "week": window(7), "month": window(30), "series": series,
            "per_campaign": sorted(per_campaign, key=lambda x: -x["estimated"]), "top_clips": top,
            "submissions": len(subs)}


@router.get("/earnings")
def earnings(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return _earnings(db, user)


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)):
    uid = user.id
    clips = db.query(Clip).filter(Clip.user_id == uid).all()
    today = now().date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    made = defaultdict(int)
    for c in clips:
        made[c.created_at.date()] += 1
    e = _earnings(db, user)
    camps = db.query(Campaign).filter(Campaign.user_id == uid).all()
    # opportunities: active campaigns ranked by payout, with what is ready to post for each
    opps = []
    for c in camps:
        if c.status != "active":
            continue
        ready = sum(1 for cl in clips if cl.campaign_id == c.id and cl.status == "approved"
                    and not db.query(Submission).filter(Submission.clip_id == cl.id).count())
        pc = next((p for p in e["per_campaign"] if p["id"] == c.id), None)
        left = (c.max_budget - pc["estimated"]) if (c.max_budget and pc) else None
        days_left = (c.deadline - now()).days if c.deadline else None
        opps.append({"id": c.id, "name": c.name, "payout_per_1k": c.payout_per_1k, "approved_unposted": ready,
                     "in_review": sum(1 for cl in clips if cl.campaign_id == c.id and cl.status in ("draft", "rendered")),
                     "days_left": days_left, "own_budget_headroom": round(left, 2) if left is not None else None})
    opps.sort(key=lambda o: (-o["approved_unposted"], -(o["payout_per_1k"] or 0)))
    jobs = db.query(Job).filter(Job.user_id == uid, Job.status.in_(("queued", "running"))).order_by(Job.id).all()
    failed = db.query(Job).filter(Job.user_id == uid, Job.status == "failed").order_by(Job.id.desc()).limit(5).all()
    acts = db.query(Activity).filter(Activity.user_id == uid).order_by(Activity.id.desc()).limit(12).all()
    return {
        "videos_processed": db.query(Video).filter(Video.user_id == uid, Video.status == "ready").count(),
        "videos_total": db.query(Video).filter(Video.user_id == uid).count(),
        "clips_generated": len(clips),
        "clips_awaiting_review": sum(1 for c in clips if c.status in ("draft", "rendered", "rendering")),
        "clips_approved": sum(1 for c in clips if c.status == "approved"),
        "clips_rejected": sum(1 for c in clips if c.status == "rejected"),
        "active_campaigns": sum(1 for c in camps if c.status == "active"),
        "production": [{"date": d.isoformat(), "clips": made.get(d, 0)} for d in days],
        "earnings": e, "opportunities": opps,
        "jobs": [_job_out(db, j) for j in jobs], "failed_jobs": [_job_out(db, j) for j in failed],
        "activity": [to_dict(a) for a in acts],
    }


def _job_out(db, j: Job) -> dict:
    d = to_dict(j, exclude=("payload",))
    v = db.get(Video, j.video_id) if j.video_id else None
    c = db.get(Clip, j.clip_id) if j.clip_id else None
    d["label"] = {"analyze": "Analysing", "render": "Rendering", "import_url": "Importing"}.get(j.type, j.type) + \
        " “" + ((c.title if c else v.title if v else "?")[:60]) + "”"
    return d


@router.get("/jobs")
def jobs(db: Session = Depends(get_db), user: User = Depends(current_user)):
    q = db.query(Job).filter(Job.user_id == user.id).order_by(Job.id.desc()).limit(40).all()
    return [_job_out(db, j) for j in q]


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    j = db.get(Job, jid)
    if not j or j.user_id != user.id:
        raise HTTPException(404, "Job not found")
    if j.status != "queued":
        raise HTTPException(409, "Only queued jobs can be cancelled; this one is already " + j.status)
    j.status, j.stage = "cancelled", "Cancelled"
    if j.type == "analyze" and j.video_id:
        v = db.get(Video, j.video_id)
        v.status = "uploaded"
    if j.type == "render" and j.clip_id:
        c = db.get(Clip, j.clip_id)
        if c:
            c.status = "rendered" if c.output_path else "draft"
    db.commit()
    return _job_out(db, j)


@router.post("/jobs/{jid}/retry")
def retry_job(jid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    j = db.get(Job, jid)
    if not j or j.user_id != user.id:
        raise HTTPException(404, "Job not found")
    if j.status != "failed":
        raise HTTPException(409, "Only failed jobs can be retried")
    j.status, j.attempts, j.error, j.stage, j.progress = "queued", 0, "", "Queued", 0
    db.commit()
    return _job_out(db, j)
