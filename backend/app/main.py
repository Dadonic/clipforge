import logging
import threading
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import auth, config, worker
from .db import User, get_db, init_db
from .routers import business, clips, recorder, videos
from .services import captions, render, semantic, storage, transcription
from .services import recorder as recorder_svc

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
_stop = threading.Event()


def _file_password_reset() -> None:
    """Forgotten password on a self-hosted copy: put a file named reset-password.txt in the program folder with
    the new password on the first line (or "email new-password" when there are several accounts) and restart.
    Only someone with access to the machine's files can do this. The file is removed after use."""
    from .db import SessionLocal
    f = config.ROOT / "reset-password.txt"
    if not f.exists():
        return
    try:
        parts = f.read_text(encoding="utf-8-sig").strip().splitlines()[0].split()
    except (OSError, IndexError):
        parts = []
    db = SessionLocal()
    try:
        users = db.query(User).all()
        u = None
        if len(parts) == 2:
            u = next((x for x in users if x.email == parts[0].lower()), None)
        elif len(parts) == 1 and len(users) == 1:
            u = users[0]
        if u and len(parts[-1]) >= 8:
            u.password_hash = auth.hash_password(parts[-1])
            db.commit()
    finally:
        db.close()
    try:
        f.unlink()
    except OSError:
        try:
            f.write_text("")            # could not delete: at least do not leave the password on disk
        except OSError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _file_password_reset()
    if config.EMBEDDED_WORKER:
        threading.Thread(target=worker.run_forever, args=(_stop,), daemon=True, name="clipforge-worker").start()
    yield
    _stop.set()


app = FastAPI(title="ClipForge AI", version="1.0.0", lifespan=lifespan)
app.include_router(videos.router)
app.include_router(clips.router)
app.include_router(business.router)
app.include_router(recorder.router)


class Creds(BaseModel):
    email: str
    password: str
    name: str = ""


def _user_out(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "settings": u.settings or {}}


def _set_cookie(resp: Response, uid: int):
    resp.set_cookie(auth.COOKIE, auth.make_token(uid), max_age=auth.TTL, httponly=True, samesite="lax",
                    secure=config.COOKIE_SECURE, path="/")


@app.post("/api/auth/signup")
def signup(body: Creds, resp: Response, db: Session = Depends(get_db)):
    first = db.query(User).count() == 0
    if not first and not config.ALLOW_SIGNUP:
        raise HTTPException(403, "Sign-ups are closed on this server")
    email = body.email.strip().lower()
    if "@" not in email or len(email) < 5:
        raise HTTPException(400, "Enter a valid email address")
    if len(body.password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(409, "An account with this email already exists")
    u = User(email=email, name=body.name.strip()[:120], password_hash=auth.hash_password(body.password), settings={})
    db.add(u)
    db.commit()
    _set_cookie(resp, u.id)
    return _user_out(u)


@app.post("/api/auth/login")
def login(body: Creds, resp: Response, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.email == body.email.strip().lower()).first()
    if not u or not auth.verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Wrong email or password")
    _set_cookie(resp, u.id)
    return _user_out(u)


@app.post("/api/auth/logout")
def logout(resp: Response):
    resp.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/api/auth/me")
def me(user: User = Depends(auth.current_user)):
    return _user_out(user)


class MePatch(BaseModel):
    name: str | None = None
    settings: dict | None = None
    current_password: str | None = None
    new_password: str | None = None


SETTING_KEYS = {"default_layout", "default_caption_style", "default_caption_params", "quality", "watermark",
                "min_duration", "max_duration", "max_highlights", "use_llm", "auto_clips", "auto_hook",
                "voice_isolation"}


@app.patch("/api/auth/me")
def update_me(body: MePatch, db: Session = Depends(get_db), user: User = Depends(auth.current_user)):
    if body.name is not None:
        user.name = body.name.strip()[:120]
    if body.settings is not None:
        s = {**(user.settings or {}), **{k: v for k, v in body.settings.items() if k in SETTING_KEYS}}
        if s.get("default_layout") and s["default_layout"] not in render.LAYOUTS:
            raise HTTPException(400, "Unknown layout")
        if s.get("default_caption_style") and s["default_caption_style"] not in captions.STYLES:
            raise HTTPException(400, "Unknown caption style")
        if s.get("quality") and s["quality"] not in config.QUALITY:
            raise HTTPException(400, "Unknown quality")
        user.settings = s
    if body.new_password:
        if not auth.verify_password(body.current_password or "", user.password_hash):
            raise HTTPException(403, "Current password is wrong")
        if len(body.new_password) < 8:
            raise HTTPException(400, "New password must be at least 8 characters")
        user.password_hash = auth.hash_password(body.new_password)
    db.merge(user)
    db.commit()
    return _user_out(user)


@app.get("/api/system")
def system(user: User = Depends(auth.current_user)):
    """What is actually wired up on this server. Keys are never returned, only whether they are set."""
    prof = semantic.load_profile()
    return {
        "transcription": {"selected": config.TRANSCRIBE_PROVIDER, "providers": transcription.provider_status(),
                          "whisper_model": config.WHISPER_MODEL},
        "semantic": {"llm_configured": semantic.llm_available(), "llm_model": config.LLM_MODEL if semantic.llm_available() else None,
                     "llm_base_url": config.LLM_BASE_URL if semantic.llm_available() else None,
                     "fallback": "heuristic scoring from the content profile"},
        "storage": storage.status(),
        "layouts": render.LAYOUTS, "caption_styles": {k: v["label"] for k, v in captions.STYLES.items()},
        "fonts": captions.list_fonts(), "quality": list(config.QUALITY), "platforms": business.PLATFORMS,
        "profile": {"name": prof["name"], "categories": {k: v["label"] for k, v in prof["categories"].items()},
                    "policy": prof["policy"]},
        "whop": {"api_integration": False,
                 "detail": "Whop documents no clipper-side API for Content Rewards (finding per-view campaigns, submitting "
                           "posts or reading view-based earnings). Its Bounties API covers flat-fee bounties only. "
                           "ClipForge therefore tracks campaigns, submissions and earnings from what you enter or import by CSV."},
        "embedded_worker": config.EMBEDDED_WORKER,
        "recorder": {"available": recorder_svc.available()},
        "phone_access": {"enabled": config.PHONE_ACCESS,
                         "url": f"http://{config.lan_ip()}:8000" if config.PHONE_ACCESS and config.lan_ip() else None},
    }


@app.get("/api/health")
def health():
    return {"ok": True}


@app.exception_handler(Exception)
async def unhandled(request, exc):
    logging.getLogger("clipforge").exception("unhandled error on %s", request.url.path)
    return JSONResponse({"detail": "Something went wrong on the server. Check the server log for details."}, status_code=500)


app.mount("/fonts", StaticFiles(directory=config.FONTS_DIR), name="fonts")
app.mount("/static", StaticFiles(directory=config.FRONTEND_DIR), name="static")


@app.get("/{path:path}", include_in_schema=False)
def spa(path: str):
    if path.startswith("api/"):
        raise HTTPException(404, "Not found")
    return FileResponse(config.FRONTEND_DIR / "index.html", headers={"Cache-Control": "no-cache"})
