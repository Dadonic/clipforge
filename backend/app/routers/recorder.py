"""Record a stream with OBS from inside ClipForge, then import and analyse it automatically."""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..auth import current_user
from ..db import Campaign, SessionLocal, User, Video, get_db, log_activity
from ..services import recorder
from ..worker import finalize_upload

router = APIRouter(prefix="/api/recorder", tags=["recorder"])
log = logging.getLogger("clipforge.recorder")


class StartIn(BaseModel):
    url: str = ""
    minutes: float = 30
    mode: str = "browser"            # browser: OBS plays the page itself; edge: records the Edge window
    title: str = ""
    streamer: str = ""
    campaign_id: int | None = None
    authorized: bool = False
    analyze: bool = True
    auto_clips: int = 5


player_router = APIRouter(tags=["recorder"])


@player_router.get("/recorder/player", include_in_schema=False)
def player(v: str, start: int = 0):
    """The page OBS's browser source opens (no sign-in: it only wraps a public YouTube embed)."""
    try:
        return HTMLResponse(recorder.player_page(v, start))
    except recorder.RecorderError:
        raise HTTPException(400, "bad video id")


@router.get("/status")
def get_status(user: User = Depends(current_user)):
    return recorder.status()


@router.post("/check")
def check_url(body: StartIn, user: User = Depends(current_user)):
    ok = recorder.embeddable(body.url) if body.url else None
    return {"youtube_id": recorder.youtube_id(body.url or ""), "embeddable": ok,
            "player_url": recorder.player_url(body.url) if body.url else ""}


@router.post("/start")
def start(body: StartIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    if not recorder.available():
        raise HTTPException(400, "OBS is not set up on this computer (tools/obs is missing).")
    if not body.authorized:
        raise HTTPException(400, "Confirm that you are allowed to clip this stream before recording.")
    if body.mode not in ("browser", "edge"):
        raise HTTPException(400, "mode must be browser or edge")
    if body.mode == "browser" and not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "Paste the full link to the stream or video.")
    if not 1 <= body.minutes <= 240:
        raise HTTPException(400, "Record between 1 and 240 minutes.")
    camp = None
    if body.campaign_id:
        camp = db.get(Campaign, body.campaign_id)
        if not camp or camp.user_id != user.id:
            raise HTTPException(404, "Campaign not found")
    if body.mode == "browser" and recorder.youtube_id(body.url) and recorder.embeddable(body.url) is False:
        raise HTTPException(400, "This YouTube video cannot be played outside YouTube. Open it in Edge, press F, "
                                 "and use the Edge window option instead.")
    uid = user.id
    meta = {"title": body.title or (f"{camp.streamer or camp.name} stream" if camp else "Stream recording"),
            "streamer": body.streamer or (camp.streamer if camp else ""), "campaign_id": body.campaign_id,
            "analyze": body.analyze, "auto_clips": max(0, min(30, body.auto_clips)), "url": body.url}

    def done(path: str, snap: dict) -> None:
        _import(uid, path, meta)

    try:
        st = recorder.start(body.url, body.minutes, body.mode, done)
    except recorder.RecorderError as e:
        raise HTTPException(502, str(e))
    log_activity(db, uid, "recording", f"Started recording “{meta['title']}” for {body.minutes:.0f} min")
    db.commit()
    return st


@router.post("/stop")
def stop(user: User = Depends(current_user)):
    try:
        return recorder.stop()
    except recorder.RecorderError as e:
        raise HTTPException(409, str(e))


@router.post("/quit")
def quit_obs(user: User = Depends(current_user)):
    recorder.quit_obs()
    return {"ok": True}


def _import(user_id: int, path: str, meta: dict) -> None:
    """Runs after a recording stops: move the file in, probe it, and queue analysis."""
    from .videos import AnalyzeIn, analyze
    try:
        dst = recorder.import_path(path)
    except OSError as e:
        log.error("could not import %s: %s", path, e)
        return
    with SessionLocal() as db:
        v = Video(user_id=user_id, filename=Path(path).name[:300], title=meta["title"][:300], streamer=meta["streamer"],
                  campaign_id=meta["campaign_id"], size=dst.stat().st_size, authorized=True,
                  source_note=(meta.get("url") or "Recorded with OBS")[:500], path=str(dst))
        db.add(v)
        db.commit()
        vid = v.id
    try:
        finalize_upload(vid)
    except Exception as e:
        log.error("recorded file could not be read: %s", e)
        return
    if not meta.get("analyze"):
        return
    with SessionLocal() as db:
        user = db.get(User, user_id)
        us = user.settings or {}
        try:
            analyze(vid, AnalyzeIn(min_duration=us.get("min_duration") or 15, max_duration=us.get("max_duration") or 60,
                                   max_highlights=us.get("max_highlights") or 40, use_llm=us.get("use_llm", True),
                                   voice_isolation=bool(us.get("voice_isolation", False)),
                                   auto_clips=meta.get("auto_clips", 5)), db, user)
        except HTTPException as e:
            log.error("could not queue analysis for recording %s: %s", vid, e.detail)
