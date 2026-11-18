"""FFmpeg / FFprobe helpers. All heavy work is streamed by ffmpeg; nothing loads a whole video in memory."""
import json
import re
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .. import config

ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".webm"}


class MediaError(RuntimeError):
    pass


def probe(path: str) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True)
    except FileNotFoundError:
        raise MediaError("FFmpeg is not installed on this computer (or the server window was opened before it was "
                         "installed). Install FFmpeg, close the server window, and start ClipForge again.")
    if r.returncode != 0:
        raise MediaError(f"ffprobe could not read this file: {r.stderr.strip()[:300]}")
    data = json.loads(r.stdout)
    v = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        raise MediaError("No video stream found in this file.")
    fps = 0.0
    try:
        n, d = (v.get("avg_frame_rate") or "0/1").split("/")
        fps = float(n) / float(d) if float(d) else 0.0
    except Exception:
        pass
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        w, h = h, w
    dur = float(data.get("format", {}).get("duration") or v.get("duration") or 0)
    return {
        "duration": dur, "width": w, "height": h, "fps": round(fps, 3),
        "vcodec": v.get("codec_name"), "acodec": a.get("codec_name") if a else None,
        "has_audio": a is not None, "size": int(data.get("format", {}).get("size") or 0),
        "bitrate": int(data.get("format", {}).get("bit_rate") or 0),
    }


_TIME = re.compile(r"out_time_us=(\d+)")


def run_ffmpeg(args: list[str], total: Optional[float] = None,
               progress: Optional[Callable[[float], None]] = None) -> str:
    """Run ffmpeg, report 0..1 progress via -progress pipe, return stderr text. Raises MediaError on failure."""
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats"]
    if config.FFMPEG_THREADS:
        cmd += ["-threads", str(config.FFMPEG_THREADS)]
    cmd += args
    errf = subprocess.PIPE
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True)
    # stderr can be large (metadata=print); drain it in a thread to avoid pipe deadlock
    import threading
    err_chunks: list[str] = []
    t = threading.Thread(target=lambda: err_chunks.append(p.stderr.read()), daemon=True)
    t.start()
    for line in p.stdout:
        if progress and total:
            m = _TIME.search(line)
            if m:
                progress(min(1.0, int(m.group(1)) / 1e6 / max(total, 0.01)))
    p.wait()
    t.join()
    err = "".join(err_chunks)
    if p.returncode != 0:
        tail = "\n".join(err.strip().splitlines()[-6:])
        raise MediaError(f"ffmpeg failed: {tail[:800]}")
    return err


def extract_audio(src: str, dst: str, duration: float, progress=None) -> None:
    """16 kHz mono PCM used by transcription and audio analysis."""
    run_ffmpeg(["-i", src, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst], duration, progress)


def make_proxy(src: str, dst: str, duration: float, progress=None) -> None:
    """Small H.264 proxy that every browser can play and seek (source may be MKV/HEVC/etc)."""
    h = config.PROXY_HEIGHT
    run_ffmpeg(["-i", src, "-vf", f"scale=-2:'min({h},ih)'", "-r", "30", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "30", "-g", "60", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                "-movflags", "+faststart", dst], duration, progress)


def detect_scenes(src: str, duration: float, threshold: float = 0.32, progress=None) -> list[float]:
    """Scene-change timestamps (seconds), computed on a tiny down-scaled stream so it stays cheap."""
    err = run_ffmpeg(["-i", src, "-an", "-vf",
                      f"fps=4,scale=160:-2,select='gt(scene,{threshold})',showinfo",
                      "-f", "null", "-"], duration, progress)
    return sorted({round(float(x), 2) for x in re.findall(r"pts_time:\s*([\d.]+)", err)})


def grab_frame(src: str, t: float, dst: str, width: int = 640) -> bool:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", f"{t:.2f}", "-i", src, "-frames:v", "1",
                        "-vf", f"scale={width}:-2", "-q:v", "4", dst], capture_output=True)
    return r.returncode == 0 and Path(dst).exists()
