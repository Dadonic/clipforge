import json
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import config
from ..auth import current_user
from ..db import Campaign, Clip, Highlight, Job, User, Video, get_db, log_activity, to_dict
from ..services import captions, media, transcription
from ..worker import delete_video_files, finalize_upload, work_dir

router = APIRouter(prefix="/api", tags=["videos"])
CHUNK = 8 * 1024 * 1024


def own_video(db: Session, user: User, vid: int) -> Video:
    v = db.get(Video, vid)
    if not v or v.user_id != user.id:
        raise HTTPException(404, "Recording not found")
    return v


def video_out(db: Session, v: Video) -> dict:
    d = to_dict(v, exclude=("path",))
    job = db.query(Job).filter(Job.video_id == v.id, Job.type.in_(("analyze", "import_url"))).order_by(Job.id.desc()).first()
    d["job"] = to_dict(job) if job else None
    d["highlights"] = db.query(Highlight).filter(Highlight.video_id == v.id, Highlight.status == "suggested").count()
    d["clips"] = db.query(Clip).filter(Clip.video_id == v.id).count()
    d["has_proxy"] = (config.WORK_DIR / str(v.id) / "proxy.mp4").exists()
    return d


class VideoIn(BaseModel):
    filename: str
    size: int
    title: str = ""
    streamer: str = ""
    campaign_id: int | None = None
    authorized: bool = False
    source_note: str = ""


def _safe_name(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext not in media.ALLOWED_EXT:
        raise HTTPException(400, f"Unsupported file type {ext or '(none)'}. Use MP4, MOV, MKV or WebM.")
    return f"{uuid.uuid4().hex}{ext}"


def _campaign_or_none(db, user, cid):
    if cid is None:
        return None
    c = db.get(Campaign, cid)
    if not c or c.user_id != user.id:
        raise HTTPException(400, "Campaign not found")
    return c


@router.post("/videos")
def create_video(body: VideoIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not body.authorized:
        raise HTTPException(400, "Confirm that you are authorised to use this footage before uploading.")
    if body.size <= 0:
        raise HTTPException(400, "File is empty")
    camp = _campaign_or_none(db, user, body.campaign_id)
    free = os.statvfs(config.UPLOAD_DIR)
    if free.f_bavail * free.f_frsize < body.size * 1.3:
        raise HTTPException(507, "Not enough disk space on the server for this recording.")
    v = Video(user_id=user.id, filename=body.filename[:300], title=(body.title or Path(body.filename).stem)[:300],
              streamer=body.streamer or (camp.streamer if camp else ""), campaign_id=body.campaign_id,
              size=body.size, authorized=True, source_note=body.source_note[:500],
              path=str(config.UPLOAD_DIR / _safe_name(body.filename)))
    db.add(v)
    db.commit()
    return {**video_out(db, v), "chunk_size": CHUNK}


@router.put("/videos/{vid}/chunk")
async def upload_chunk(vid: int, offset: int, request: Request, db: Session = Depends(get_db),
                       user: User = Depends(current_user)):
    """Resumable upload: the browser sends the file in 8 MB slices; nothing is buffered in memory."""
    v = own_video(db, user, vid)
    if v.status != "uploading":
        raise HTTPException(409, "Upload already finished")
    if offset < 0 or offset > v.size:
        raise HTTPException(400, "Bad offset")
    mode = "r+b" if os.path.exists(v.path) else "wb"
    written = 0
    with open(v.path, mode) as f:
        f.seek(offset)
        async for chunk in request.stream():
            written += len(chunk)
            if offset + written > v.size:
                raise HTTPException(400, "Chunk exceeds declared file size")
            f.write(chunk)
    v.received = max(v.received or 0, offset + written)
    db.commit()
    return {"received": v.received, "size": v.size}


class CompleteIn(BaseModel):
    analyze: bool = False        # start the analysis straight away
    auto_clips: int = 0          # and make this many clips when it finishes


@router.post("/videos/{vid}/complete")
def complete_upload(vid: int, body: CompleteIn | None = None, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    if v.status != "uploading":
        return video_out(db, v)
    if not os.path.exists(v.path) or os.path.getsize(v.path) != v.size:
        raise HTTPException(400, f"Upload incomplete ({v.received or 0} of {v.size} bytes received). Resume the upload.")
    try:
        finalize_upload(v.id)
    except media.MediaError as e:
        raise HTTPException(422, str(e))
    db.refresh(v)
    if body and body.analyze:
        us = user.settings or {}
        analyze(vid, AnalyzeIn(min_duration=us.get("min_duration") or 15, max_duration=us.get("max_duration") or 60,
                               max_highlights=us.get("max_highlights") or 40, use_llm=us.get("use_llm", True),
                               auto_clips=max(0, min(30, body.auto_clips))), db, user)
        db.refresh(v)
    return video_out(db, v)


class ImportIn(BaseModel):
    url: str
    title: str = ""
    streamer: str = ""
    campaign_id: int | None = None
    authorized: bool = False
    source_note: str = ""


@router.post("/videos/import-url")
def import_url(body: ImportIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not body.authorized:
        raise HTTPException(400, "Confirm that you are authorised to use this footage before importing.")
    if not re.match(r"^https?://", body.url):
        raise HTTPException(400, "Enter a full http(s) link to a video file.")
    name = Path(body.url.split("?")[0]).name or "import.mp4"
    if Path(name).suffix.lower() not in media.ALLOWED_EXT:
        raise HTTPException(400, "The link must point directly to an MP4, MOV, MKV or WebM file (for example a file the "
                                 "streamer shared with you). Links to streaming-site pages are not supported.")
    camp = _campaign_or_none(db, user, body.campaign_id)
    v = Video(user_id=user.id, filename=name[:300], title=(body.title or Path(name).stem)[:300],
              streamer=body.streamer or (camp.streamer if camp else ""), campaign_id=body.campaign_id, authorized=True,
              source_note=(body.source_note or body.url)[:500], path=str(config.UPLOAD_DIR / _safe_name(name)))
    db.add(v)
    db.flush()
    db.add(Job(user_id=user.id, type="import_url", video_id=v.id, payload={"url": body.url},
               max_attempts=config.JOB_MAX_ATTEMPTS))
    db.commit()
    return video_out(db, v)


@router.get("/videos")
def list_videos(db: Session = Depends(get_db), user: User = Depends(current_user)):
    return [video_out(db, v) for v in db.query(Video).filter(Video.user_id == user.id).order_by(Video.id.desc()).all()]


@router.get("/videos/{vid}")
def get_video(vid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    d = video_out(db, v)
    d["cost_estimate"] = transcription.estimate_cost(v.duration or 0)
    return d


class VideoPatch(BaseModel):
    title: str | None = None
    streamer: str | None = None
    campaign_id: int | None = None
    clear_campaign: bool = False
    notes: str | None = None
    facecam: dict | None = None
    clear_facecam: bool = False


@router.patch("/videos/{vid}")
def patch_video(vid: int, body: VideoPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    if body.title is not None:
        v.title = body.title[:300]
    if body.streamer is not None:
        v.streamer = body.streamer[:120]
    if body.notes is not None:
        v.notes = body.notes
    if body.campaign_id is not None:
        _campaign_or_none(db, user, body.campaign_id)
        v.campaign_id = body.campaign_id
    if body.clear_campaign:
        v.campaign_id = None
    if body.facecam is not None:
        try:
            r = {k: min(1.0, max(0.0, float(body.facecam[k]))) for k in ("x", "y", "w", "h")}
        except (KeyError, TypeError, ValueError):
            raise HTTPException(400, "facecam needs x, y, w, h between 0 and 1")
        if r["w"] < 0.02 or r["h"] < 0.02:
            raise HTTPException(400, "Facecam area is too small")
        v.facecam, v.facecam_source = r, "manual"
    if body.clear_facecam:
        v.facecam, v.facecam_source = None, "manual"
    db.commit()
    return video_out(db, v)


@router.delete("/videos/{vid}")
def delete_video(vid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    if db.query(Job).filter(Job.video_id == vid, Job.status == "running").count():
        raise HTTPException(409, "This recording is being processed. Wait for the job to finish, then delete.")
    for c in db.query(Clip).filter(Clip.video_id == vid).all():
        if c.output_path and os.path.exists(c.output_path):
            os.remove(c.output_path)
        db.delete(c)
    db.query(Highlight).filter(Highlight.video_id == vid).delete()
    db.query(Job).filter(Job.video_id == vid).delete()
    delete_video_files(v)
    log_activity(db, user.id, "delete", f"Deleted recording “{v.title}”")
    db.delete(v)
    db.commit()
    return {"ok": True}


class AnalyzeIn(BaseModel):
    min_duration: float = 15
    max_duration: float = 60
    max_highlights: int = 40
    use_llm: bool = True
    auto_clips: int = 0          # after analysis, turn this many top highlights into clips automatically


@router.post("/videos/{vid}/analyze")
def analyze(vid: int, body: AnalyzeIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    if v.status == "uploading":
        raise HTTPException(409, "Finish the upload first")
    if db.query(Job).filter(Job.video_id == vid, Job.type == "analyze", Job.status.in_(("queued", "running"))).count():
        raise HTTPException(409, "Analysis is already running for this recording")
    if not (5 <= body.min_duration <= body.max_duration <= 180):
        raise HTTPException(400, "Durations must satisfy 5 <= min <= max <= 180 seconds")
    if not 0 <= body.auto_clips <= 30:
        raise HTTPException(400, "auto_clips must be between 0 and 30")
    payload = body.model_dump()
    camp = db.get(Campaign, v.campaign_id) if v.campaign_id else None
    if camp:
        payload["extra_hashtags"] = camp.required_hashtags or []
        # campaign length limits win over the defaults, so every suggestion is already postable
        if camp.min_duration and camp.max_duration and body.min_duration == 15 and body.max_duration == 60:
            payload["min_duration"] = max(5.0, min(float(camp.min_duration), 175.0))
            payload["max_duration"] = max(payload["min_duration"], min(float(camp.max_duration), 180.0))
    from ..services import clipmaker
    payload["category_boost"] = clipmaker.insights(db, user.id)["category_boost"]
    job = Job(user_id=user.id, type="analyze", video_id=vid, payload=payload, max_attempts=config.JOB_MAX_ATTEMPTS)
    v.status = "analyzing"
    db.add(job)
    db.commit()
    return video_out(db, v)


@router.post("/videos/{vid}/chat")
async def upload_chat(vid: int, file: UploadFile, db: Session = Depends(get_db), user: User = Depends(current_user)):
    from ..services import chat as chat_svc
    v = own_video(db, user, vid)
    raw = (await file.read(50 * 1024 * 1024)).decode("utf-8", errors="ignore")
    msgs = chat_svc.parse(raw)
    if not msgs:
        raise HTTPException(422, "No timestamped chat messages found. Accepted: lines like “[1:02:33] user: msg”, "
                                 "a Twitch chat JSON export, or CSV with time and message columns.")
    (work_dir(v.id) / "chat.txt").write_text(raw)
    v.has_chat = True
    db.commit()
    return {"messages": len(msgs), "note": "Chat saved. Run the analysis again to include chat spikes."}


@router.get("/videos/{vid}/stream")
def stream(vid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    proxy = config.WORK_DIR / str(v.id) / "proxy.mp4"
    if not proxy.exists():
        raise HTTPException(404, "Preview is not ready yet. It is created during analysis.")
    return FileResponse(proxy, media_type="video/mp4")


@router.get("/videos/{vid}/timeline")
def timeline(vid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    p = config.WORK_DIR / str(v.id) / "timeline.json"
    if not p.exists():
        raise HTTPException(404, "Run the analysis first")
    t = json.loads(p.read_text())
    n = len(t["audio"].get("excite", []))
    step = max(1, n // 600)     # at most ~600 points for the chart

    def ds(a):
        return [round(max(a[i:i + step]), 3) for i in range(0, len(a), step)] if a else []
    return {"step": step, "duration": v.duration, "excite": ds(t["audio"].get("excite", [])),
            "laugh": ds(t["audio"].get("laugh", [])), "chat": ds(t.get("chat", [])), "scenes": t.get("scenes", [])[:3000]}


@router.get("/videos/{vid}/transcript")
def transcript(vid: int, start: float = 0, end: float = 1e9, db: Session = Depends(get_db),
               user: User = Depends(current_user)):
    v = own_video(db, user, vid)
    p = config.WORK_DIR / str(v.id) / "transcript.json"
    if not p.exists():
        return {"provider": "none", "words": []}
    t = json.loads(p.read_text())
    return {"provider": t["provider"], "word_timing": t.get("word_timing"),
            "words": [w for w in t["words"] if w["e"] > start and w["s"] < end]}


@router.get("/videos/{vid}/highlights")
def list_highlights(vid: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    own_video(db, user, vid)
    hs = db.query(Highlight).filter(Highlight.video_id == vid).order_by(Highlight.rank).all()
    return [to_dict(h) for h in hs]


class HighlightPatch(BaseModel):
    status: str


@router.patch("/highlights/{hid}")
def patch_highlight(hid: int, body: HighlightPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    h = db.get(Highlight, hid)
    if not h:
        raise HTTPException(404, "Highlight not found")
    own_video(db, user, h.video_id)
    if body.status not in ("suggested", "dismissed"):
        raise HTTPException(400, "status must be suggested or dismissed")
    h.status = body.status
    db.commit()
    return to_dict(h)


@router.get("/system/captions")
def caption_options(user: User = Depends(current_user)):
    return {"styles": captions.STYLES, "defaults": captions.DEFAULT_PARAMS, "fonts": captions.list_fonts()}
