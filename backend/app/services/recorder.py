"""Stream recorder: ClipForge drives a portable copy of OBS Studio (tools/obs) to record a stream for you.

How it works
  * OBS runs minimised in the tray with its own ClipForge profile and scene collection, written here on
    first use. Two scenes:
      - "Browser": OBS plays the stream page itself (an embedded YouTube player, or any URL) at 1920x1080.
        Nothing on your screen is involved, so the mouse, other windows and notifications never show up.
      - "Edge window": records the Edge window and only Edge's sound, for pages that cannot be embedded.
  * ClipForge talks to OBS over its built-in websocket (localhost only, random password) to switch
    scene, load the URL, start and stop recording.
  * When a recording stops, the file is imported as a recording, ready to analyse.

Only record streams you are allowed to clip (the campaign's own streamer and footage).
"""
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import config

log = logging.getLogger("clipforge.recorder")

OBS_DIR = config.ROOT / "tools" / "obs"
OBS_EXE = OBS_DIR / "bin" / "64bit" / "obs64.exe"
OBS_CFG = OBS_DIR / "config" / "obs-studio"
REC_DIR = config.DATA_DIR / "recordings"
SECRET_FILE = config.DATA_DIR / "obs_ws_secret"
PORT = int(os.environ.get("OBS_WS_PORT", "4455"))
APP_URL = os.environ.get("CLIPFORGE_LOCAL_URL", "http://localhost:8000")   # serves the player page OBS opens
CFG_VERSION = "2"
SCENE_BROWSER, SCENE_EDGE = "Browser", "Edge window"
BROWSER_SOURCE = "Stream page"
EDGE_WINDOW = ":Chrome_WidgetWin_1:msedge.exe"

_lock = threading.Lock()
_state: dict = {"status": "idle"}       # idle | starting | recording | stopping | error
_timer: threading.Timer | None = None


class RecorderError(RuntimeError):
    pass


# --------------------------------------------------------------------------- OBS configuration
def available() -> bool:
    return OBS_EXE.exists()


def _password() -> str:
    if SECRET_FILE.exists():
        return SECRET_FILE.read_text().strip()
    pw = secrets.token_urlsafe(18)
    SECRET_FILE.write_text(pw)
    return pw


def _ini(sections: dict) -> str:
    out = []
    for sec, kv in sections.items():
        out.append(f"[{sec}]")
        out += [f"{k}={v}" for k, v in kv.items()]
        out.append("")
    return "\n".join(out)


def _source(sid: str, name: str, settings: dict, **extra) -> dict:
    return {"id": sid, "versioned_id": sid, "name": name, "uuid": str(uuid.uuid4()), "settings": settings,
            "enabled": True, "muted": False, "volume": 1.0, "mixers": 255, "sync": 0, "flags": 0,
            "monitoring_type": 0, "push-to-talk": False, "push-to-mute": False, "filters": [], **extra}


def _scene(name: str, children: list[dict]) -> dict:
    items = []
    for i, ch in enumerate(children, start=1):
        items.append({"name": ch["name"], "source_uuid": ch["uuid"], "visible": True, "locked": True, "id": i,
                      "rot": 0.0, "pos": {"x": 0.0, "y": 0.0}, "scale": {"x": 1.0, "y": 1.0}, "align": 5,
                      "bounds_type": 2, "bounds_align": 0, "bounds": {"x": 1920.0, "y": 1080.0},
                      "crop_left": 0, "crop_top": 0, "crop_right": 0, "crop_bottom": 0, "scale_filter": "disable",
                      "blend_method": "default", "blend_type": "normal"})
    return _source("scene", name, {"id_counter": len(items), "custom_size": False, "items": items})


def ensure_obs_config(force: bool = False) -> None:
    """Write OBS's portable config: profile, scene collection and websocket settings. Idempotent."""
    if not available():
        raise RecorderError("OBS is not installed in tools/obs")
    REC_DIR.mkdir(parents=True, exist_ok=True)
    marker = OBS_CFG / "clipforge_config_version"
    if not force and marker.exists() and marker.read_text().strip() == CFG_VERSION:
        return
    (OBS_DIR / "portable_mode.txt").write_text("ClipForge runs OBS in portable mode\n")
    for d in ("basic/profiles/ClipForge", "basic/scenes", "plugin_config/obs-websocket"):
        (OBS_CFG / d).mkdir(parents=True, exist_ok=True)
    general = {"General": {"FirstRun": "false", "EnableAutoUpdates": "false", "LastVersion": "553648130",
                           "ConfirmOnExit": "false", "WarnBeforeStartingRecord": "false",
                           "WarnBeforeStoppingRecord": "false"},
               "Basic": {"Profile": "ClipForge", "ProfileDir": "ClipForge", "SceneCollection": "ClipForge",
                         "SceneCollectionFile": "ClipForge", "ConfigOnNewProfile": "false"},
               "BasicWindow": {"SysTrayEnabled": "true", "SysTrayWhenStarted": "true", "SysTrayMinimizeToTray": "true",
                               "RecordWhenStreaming": "false", "ShowTransitions": "false"}}
    for f in ("global.ini", "user.ini"):
        (OBS_CFG / f).write_text(_ini(general), encoding="utf-8")
    # forward slashes in paths: OBS's ini reader turns "\\r" in "...data\\recordings" into a line break
    profile = {"General": {"Name": "ClipForge"},
               "Video": {"BaseCX": "1920", "BaseCY": "1080", "OutputCX": "1920", "OutputCY": "1080",
                         "FPSType": "0", "FPSCommon": "30", "ScaleType": "bicubic", "ColorFormat": "NV12",
                         "ColorSpace": "709", "ColorRange": "Partial"},
               "Audio": {"SampleRate": "48000", "ChannelSetup": "Stereo"},
               "Output": {"Mode": "Simple"},
               "SimpleOutput": {"FilePath": REC_DIR.as_posix(), "RecFormat2": "hybrid_mp4", "RecQuality": "Small",
                                "RecEncoder": "x264", "RecAudioEncoder": "aac", "FileNameWithoutSpace": "true",
                                "RecRB": "false", "Preset": "veryfast", "ABitrate": "160"}}
    (OBS_CFG / "basic/profiles/ClipForge/basic.ini").write_text(_ini(profile), encoding="utf-8")
    browser = _source("browser_source", BROWSER_SOURCE,
                      {"url": "about:blank", "width": 1920, "height": 1080, "fps": 30, "fps_custom": False,
                       "reroute_audio": True, "restart_when_active": False, "shutdown": False,
                       "css": "body{background:#000;margin:0;overflow:hidden}"})
    edge = _source("window_capture", "Edge", {"window": EDGE_WINDOW, "priority": 2, "cursor": False,
                                             "method": 0, "client_area": True})
    edge_audio = _source("wasapi_process_output_capture", "Edge sound", {"window": EDGE_WINDOW, "priority": 2})
    scenes = [_scene(SCENE_BROWSER, [browser]), _scene(SCENE_EDGE, [edge, edge_audio])]
    collection = {"name": "ClipForge", "current_scene": SCENE_BROWSER, "current_program_scene": SCENE_BROWSER,
                  "scene_order": [{"name": SCENE_BROWSER}, {"name": SCENE_EDGE}],
                  "sources": [browser, edge, edge_audio] + scenes, "groups": [], "transitions": [],
                  "current_transition": "Fade", "transition_duration": 300, "quick_transitions": [],
                  "modules": {}, "preview_locked": False, "scaling_enabled": False}
    (OBS_CFG / "basic/scenes/ClipForge.json").write_text(json.dumps(collection, indent=1), encoding="utf-8")
    ws = {"alerts_enabled": False, "auth_required": True, "first_load": False, "server_enabled": True,
          "server_password": _password(), "server_port": PORT}
    (OBS_CFG / "plugin_config/obs-websocket/config.json").write_text(json.dumps(ws), encoding="utf-8")
    marker.write_text(CFG_VERSION)


# --------------------------------------------------------------------------- websocket (obs-websocket v5)
class _OBS:
    def __init__(self, timeout: float = 5.0):
        from websockets.sync.client import connect
        self.ws = connect(f"ws://127.0.0.1:{PORT}", open_timeout=timeout, close_timeout=2,
                          subprotocols=["obswebsocket.json"])
        hello = json.loads(self.ws.recv(timeout=timeout))["d"]
        ident = {"rpcVersion": 1, "eventSubscriptions": 0}
        auth = hello.get("authentication")
        if auth:
            secret = base64.b64encode(hashlib.sha256((_password() + auth["salt"]).encode()).digest()).decode()
            ident["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()).decode()
        self.ws.send(json.dumps({"op": 1, "d": ident}))
        msg = json.loads(self.ws.recv(timeout=timeout))
        if msg.get("op") != 2:
            raise RecorderError("OBS refused the connection (password mismatch)")

    def call(self, req: str, data: dict | None = None, timeout: float = 10.0) -> dict:
        rid = uuid.uuid4().hex
        self.ws.send(json.dumps({"op": 6, "d": {"requestType": req, "requestId": rid, "requestData": data or {}}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = json.loads(self.ws.recv(timeout=max(0.1, end - time.time())))
            if msg.get("op") == 7 and msg["d"].get("requestId") == rid:
                st = msg["d"].get("requestStatus", {})
                if not st.get("result"):
                    raise RecorderError(f"OBS {req} failed: {st.get('comment') or st.get('code')}")
                return msg["d"].get("responseData") or {}
        raise RecorderError(f"OBS did not answer {req}")

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def _connect(wait: float = 0.0) -> _OBS:
    end = time.time() + wait
    last = None
    while True:
        try:
            return _OBS()
        except Exception as e:  # not running yet, or still starting
            last = e
            if time.time() >= end:
                raise RecorderError(f"Cannot reach OBS: {last}") from last
            time.sleep(1.0)


def _obs_running() -> bool:
    if os.name != "nt":
        return False
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq obs64.exe"], capture_output=True).stdout
    return b"obs64.exe" in out


def _kill_obs() -> None:
    if os.name != "nt":
        return
    subprocess.run(["taskkill", "/IM", "obs64.exe", "/T"], capture_output=True)
    for _ in range(10):                    # a tray-minimised or stuck OBS can ignore the polite close
        time.sleep(1.0)
        if not _obs_running():
            return
    subprocess.run(["taskkill", "/IM", "obs64.exe", "/T", "/F"], capture_output=True)
    time.sleep(1.0)


def _clear_crash_flag() -> None:
    """OBS 33 asks "Launch in Safe Mode?" after an unclean exit (ClipForge being stopped kills it too).
    Nobody is there to answer, and safe mode turns off the websocket, so start it as a clean run."""
    for _ in range(10):                    # a just-killed OBS may hold the file open for a moment
        left = []
        for f in (OBS_CFG / ".sentinel").glob("*"):
            try:
                f.unlink()
            except OSError:
                left.append(f)
        if not left:
            return
        time.sleep(0.5)
    log.warning("could not clear OBS crash flag: %s", left)


def launch_obs() -> None:
    ensure_obs_config()
    try:
        obs = _connect()
    except RecorderError:
        obs = None
    if obs:                                      # already running (maybe still loading)
        _wait_ready(obs)
        return
    if _obs_running():                           # running but not answering: stuck on a dialog
        _kill_obs()
    _clear_crash_flag()
    flags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW for the launcher itself
    subprocess.Popen([str(OBS_EXE), "--portable", "--minimize-to-tray", "--disable-updater", "--disable-missing-files-check", "--profile", "ClipForge",
                      "--collection", "ClipForge", "--scene", SCENE_BROWSER],
                     cwd=str(OBS_EXE.parent), creationflags=flags)
    _wait_ready(_connect(wait=45.0))


def _wait_ready(obs: "_OBS", wait: float = 45.0) -> None:
    """OBS accepts the websocket before its scenes have loaded; wait until it answers requests."""
    end = time.time() + wait
    try:
        while True:
            try:
                obs.call("GetRecordStatus")
                return
            except RecorderError as e:
                if "not ready" not in str(e).lower() or time.time() >= end:
                    raise
                time.sleep(1.0)
    finally:
        obs.close()


# --------------------------------------------------------------------------- URLs
def youtube_id(url: str) -> str | None:
    u = urlparse(url.strip())
    host = (u.hostname or "").lower()
    if host.endswith("youtu.be"):
        return u.path.strip("/").split("/")[0] or None
    if "youtube.com" in host:
        q = parse_qs(u.query)
        if q.get("v"):
            return q["v"][0]
        m = re.match(r"^/(?:live|shorts|embed)/([A-Za-z0-9_-]{6,})", u.path)
        if m:
            return m.group(1)
    return None


def start_seconds(url: str) -> int:
    q = parse_qs(urlparse(url).query)
    t = (q.get("t") or q.get("start") or ["0"])[0]
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?", t)
    if not m:
        return 0
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def player_url(url: str) -> str:
    """What OBS opens. YouTube links go through ClipForge's own player page: YouTube refuses an embed
    opened as a bare page (Error 153), it has to sit inside a page from a real site."""
    vid = youtube_id(url)
    if not vid:
        return url
    s = start_seconds(url)
    return f"{APP_URL}/recorder/player?v={vid}" + (f"&start={s}" if s else "")


def player_page(vid: str, start: int = 0) -> str:
    """Full-frame YouTube embed, no controls, autoplay (OBS's browser allows sound without a click)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", vid or ""):
        raise RecorderError("bad video id")
    src = (f"https://www.youtube.com/embed/{vid}?autoplay=1&controls=0&rel=0&playsinline=1&iv_load_policy=3"
           f"&disablekb=1&fs=0&modestbranding=1&enablejsapi=1&vq=hd1080&origin={APP_URL}"
           + (f"&start={int(start)}" if start else ""))
    return ("<!doctype html><html><head><meta charset=utf-8><meta name=referrer content=strict-origin-when-cross-origin>"
            "<style>html,body{margin:0;height:100%;background:#000;overflow:hidden}"
            "iframe{position:fixed;inset:0;width:100%;height:100%;border:0}</style></head><body>"
            f'<iframe src="{src}" allow="autoplay; encrypted-media" referrerpolicy="strict-origin-when-cross-origin">'
            "</iframe></body></html>")


def embeddable(url: str) -> bool | None:
    """True/False for YouTube links (from YouTube's oEmbed answer), None when it cannot be told."""
    vid = youtube_id(url)
    if not vid:
        return None
    try:
        import httpx
        r = httpx.get("https://www.youtube.com/oembed", params={"url": f"https://www.youtube.com/watch?v={vid}",
                                                                "format": "json"}, timeout=10)
        return r.status_code == 200
    except Exception:
        return None


# --------------------------------------------------------------------------- recording control
def status() -> dict:
    with _lock:
        st = {k: v for k, v in _state.items() if not k.startswith("_")}
    st["available"] = available()
    if st.get("status") == "recording" and st.get("started_at"):
        st["elapsed"] = round(time.time() - st["started_at"], 1)
    return st


def start(url: str, minutes: float, mode: str, on_done) -> dict:
    """Begin recording. on_done(path, state) runs in a background thread when the recording ends."""
    global _timer
    with _lock:
        if _state.get("status") in ("starting", "recording", "stopping"):
            raise RecorderError("A recording is already in progress")
        _state.clear()
        _state.update({"status": "starting", "url": url, "mode": mode, "minutes": minutes})
    try:
        launch_obs()
        obs = _connect(wait=10)
        try:
            if obs.call("GetRecordStatus").get("outputActive"):
                raise RecorderError("OBS is already recording")
            if mode == "browser":
                obs.call("SetInputSettings", {"inputName": BROWSER_SOURCE,
                                              "inputSettings": {"url": player_url(url)}, "overlay": True})
                obs.call("SetCurrentProgramScene", {"sceneName": SCENE_BROWSER})
                time.sleep(6)                      # let the player load and start before the first frame
            else:
                obs.call("SetCurrentProgramScene", {"sceneName": SCENE_EDGE})
            obs.call("StartRecord")
            for _ in range(8):                     # OBS can accept the request and then refuse to write
                time.sleep(0.5)
                if obs.call("GetRecordStatus").get("outputActive"):
                    break
            else:
                raise RecorderError("OBS could not start writing the recording (check the recordings folder)")
        finally:
            obs.close()
    except Exception as e:
        with _lock:
            _state.update({"status": "error", "error": str(e)[:300]})
        raise
    with _lock:
        _state.update({"status": "recording", "started_at": time.time(), "_on_done": on_done})
    if minutes and minutes > 0:
        _timer = threading.Timer(minutes * 60, lambda: _safe_stop(on_done))
        _timer.daemon = True
        _timer.start()
    return status()


def _safe_stop(on_done) -> None:
    try:
        stop(on_done)
    except Exception as e:
        with _lock:
            _state.update({"status": "error", "error": str(e)[:300]})


def stop(on_done=None) -> dict:
    global _timer
    with _lock:
        if _state.get("status") != "recording":
            raise RecorderError("Nothing is being recorded")
        _state["status"] = "stopping"
        on_done = on_done or _state.get("_on_done")
    if _timer:
        _timer.cancel()
        _timer = None
    obs = _connect(wait=5)
    try:
        path = obs.call("StopRecord", timeout=60).get("outputPath")
        for _ in range(120):                   # StopRecord answers before OBS has finished the file
            if not obs.call("GetRecordStatus").get("outputActive"):
                break
            time.sleep(0.5)
        if obs.call("GetInputSettings", {"inputName": BROWSER_SOURCE}).get("inputSettings", {}).get("url") != "about:blank":
            obs.call("SetInputSettings", {"inputName": BROWSER_SOURCE, "inputSettings": {"url": "about:blank"},
                                          "overlay": True})          # stop the player using bandwidth
    finally:
        obs.close()
    if not path:
        raise RecorderError("OBS did not report where it saved the recording")
    for _ in range(30):                        # then the size settles once the last write lands
        try:
            size = os.path.getsize(path)
            time.sleep(1.0)
            if size and os.path.getsize(path) == size:
                break
        except OSError:
            time.sleep(1.0)
    with _lock:
        snap = {k: v for k, v in _state.items() if not k.startswith("_")}
        _state.clear()
        _state.update({"status": "idle", "last_file": path})
    if on_done:
        threading.Thread(target=on_done, args=(path, snap), daemon=True).start()
    return {"path": path}


def quit_obs() -> None:
    """Close OBS (only when it is not recording)."""
    try:
        obs = _connect()
    except RecorderError:
        _kill_obs()                              # not answering: nothing ClipForge is recording
        return
    try:
        recording = obs.call("GetRecordStatus").get("outputActive")
    except RecorderError:
        recording = False
    finally:
        obs.close()
    if not recording:
        _kill_obs()


def import_path(path: str) -> Path:
    """Move a finished recording into ClipForge's uploads folder. On Windows a rename fails while OBS
    still has the file open, so keep trying for a while instead of copying a half-written file."""
    src = Path(path)
    dst = config.UPLOAD_DIR / f"{uuid.uuid4().hex}{src.suffix.lower() or '.mp4'}"
    for _ in range(120):
        try:
            os.replace(src, dst)
            return dst
        except PermissionError:
            time.sleep(1.0)
        except OSError:
            break                              # a different drive: fall back to copying
    shutil.copy2(src, dst)
    if dst.stat().st_size != src.stat().st_size:
        dst.unlink(missing_ok=True)
        raise OSError("the recording was still being written")
    try:
        src.unlink()
    except OSError:
        pass
    return dst
