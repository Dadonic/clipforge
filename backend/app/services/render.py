"""Vertical clip renderer: layouts -> panels -> one ffmpeg filter graph. Output is always
1080x1920 H.264 (yuv420p, +faststart) with AAC audio.

A layout is reduced to "panels": a source rectangle (pixels in the original frame) drawn into a
destination rectangle on the 1080x1920 canvas. The browser preview draws the very same panels.
"""
import hashlib
import json
import os
from pathlib import Path

from .. import config
from . import captions, media

W, H = config.OUT_W, config.OUT_H
LAYOUTS = {
    "blurfit": "Full gameplay on blurred background (nothing cropped)",
    "fullscreen": "Full-screen gameplay (centre crop)",
    "split": "Facecam on top, gameplay below",
    "facecam_focus": "Facecam-focused (large facecam, gameplay strip)",
    "stacked": "Stacked gameplay (full frame on top, zoomed action below)",
    "custom": "Custom (choose both regions yourself)",
}
DEFAULT_FACECAM = {"x": 0.0, "y": 0.0, "w": 0.26, "h": 0.32}


def _even(v: float) -> int:
    return max(2, int(round(v / 2)) * 2)


def _fit(rect: dict, sw: int, sh: int, dw: int, dh: int) -> dict:
    """Normalised source rect -> pixel rect shrunk around its centre to the destination aspect (cover)."""
    x, y, w, h = rect["x"] * sw, rect["y"] * sh, rect["w"] * sw, rect["h"] * sh
    target = dw / dh
    if w / h > target:
        nw = h * target
        x += (w - nw) / 2
        w = nw
    else:
        nh = w / target
        y += (h - nh) / 2
        h = nh
    w, h = min(_even(w), sw - sw % 2), min(_even(h), sh - sh % 2)
    x, y = int(max(0, min(x, sw - w))), int(max(0, min(y, sh - h)))
    return {"x": x, "y": y, "w": w, "h": h}


def _clampf(v, lo, hi, default):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return default


def _rect(r, default):
    try:
        x, y = _clampf(r["x"], 0, 0.98, 0), _clampf(r["y"], 0, 0.98, 0)
        return {"x": x, "y": y, "w": _clampf(r["w"], 0.02, 1 - x, 1 - x), "h": _clampf(r["h"], 0.02, 1 - y, 1 - y)}
    except (TypeError, KeyError):
        return dict(default)


def build_panels(layout: str, params: dict | None, sw: int, sh: int, facecam: dict | None) -> dict:
    p = params or {}
    fx, fy = _clampf(p.get("focus_x"), 0, 1, 0.5), _clampf(p.get("focus_y"), 0, 1, 0.5)
    zoom = _clampf(p.get("zoom"), 1, 3, 1.0)
    cam = _rect(p.get("facecam") or facecam, DEFAULT_FACECAM)

    def gameplay(dw, dh):
        """Zoomable crop of the frame at the destination aspect, positioned by focus_x / focus_y."""
        target = dw / dh
        fw = min(1.0, target * sh / sw) / zoom
        fh = min(1.0, sw / (target * sh)) / zoom
        return _fit({"x": (1 - fw) * fx, "y": (1 - fh) * fy, "w": fw, "h": fh}, sw, sh, dw, dh)

    panels, blur = [], False
    if layout == "fullscreen":
        panels.append({"src": gameplay(W, H), "dst": {"x": 0, "y": 0, "w": W, "h": H}})
    elif layout in ("split", "facecam_focus", "custom"):
        ratio = _clampf(p.get("ratio"), 0.15, 0.8, 0.55 if layout == "facecam_focus" else 0.34)
        th = _even(H * ratio)
        top_src = _rect(p.get("top"), cam) if layout == "custom" else cam
        panels.append({"src": _fit(top_src, sw, sh, W, th), "dst": {"x": 0, "y": 0, "w": W, "h": th}})
        if layout == "custom" and p.get("bottom"):
            bsrc = _fit(_rect(p["bottom"], {"x": 0, "y": 0, "w": 1, "h": 1}), sw, sh, W, H - th)
        else:
            bsrc = gameplay(W, H - th)
        panels.append({"src": bsrc, "dst": {"x": 0, "y": th, "w": W, "h": H - th}})
    elif layout == "stacked":
        th = min(_even(W * sh / sw), _even(H * 0.45))        # portrait sources: cap the top panel
        panels.append({"src": _fit({"x": 0, "y": 0, "w": 1, "h": 1}, sw, sh, W, th), "dst": {"x": 0, "y": 0, "w": W, "h": th}})
        panels.append({"src": gameplay(W, H - th), "dst": {"x": 0, "y": th, "w": W, "h": H - th}})
    else:  # blurfit
        blur = True
        fw, fh = 1.0 / zoom, 1.0
        dh = min(H, _even(W * (fh * sh) / (fw * sw)))
        src = _fit({"x": (1 - fw) * fx, "y": 0, "w": fw, "h": fh}, sw, sh, W, dh)
        y = min(H - dh, int((H - dh) * _clampf(p.get("offset_y"), 0, 1, 0.42)) // 2 * 2)
        panels.append({"src": src, "dst": {"x": 0, "y": y, "w": W, "h": dh}})
    return {"blur_bg": blur, "panels": panels, "canvas": {"w": W, "h": H}, "source": {"w": sw, "h": sh}}


def render_hash(clip: dict) -> str:
    """Hash of everything that changes pixels or audio. Title/description/hashtags are deliberately excluded,
    so editing metadata never forces a re-render."""
    keys = ["start", "end", "layout", "layout_params", "caption_style", "caption_params", "words",
            "audio_gain_db", "normalize_audio", "watermark_text", "watermark_pos", "quality", "facecam"]
    blob = json.dumps({k: clip.get(k) for k in keys}, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


def _fesc(path: str) -> str:
    """Escape a file path for use inside a quoted ffmpeg filter option. Windows paths are turned into
    forward-slash form first (C:\\clips\\a.ass -> C\\:/clips/a.ass), which is what ffmpeg expects there."""
    return path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


def render_clip(src: str, out: str, *, start: float, end: float, src_w: int, src_h: int, src_fps: float,
                layout: str, layout_params: dict, facecam: dict | None, words: list[dict], caption_style: str,
                caption_params: dict, watermark: str = "", watermark_pos: str = "top", gain_db: float = 0.0,
                normalize: bool = True, quality: str = "standard", has_audio: bool = True, progress=None) -> dict:
    dur = max(0.1, end - start)
    lay = build_panels(layout, layout_params, src_w, src_h, facecam)
    work = Path(out).with_suffix("")
    ass_path = str(work) + ".ass"
    Path(ass_path).write_text(captions.build_ass(words, start, end, caption_style, caption_params, watermark, watermark_pos),
                              encoding="utf-8")
    n = len(lay["panels"]) + (1 if lay["blur_bg"] else 0)
    fc = [f"[0:v]setpts=PTS-STARTPTS,split={n}" + "".join(f"[v{i}]" for i in range(n))]
    idx = 0
    if lay["blur_bg"]:
        fc.append(f"[v0]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
                  f"boxblur=12:2,eq=brightness=-0.12:saturation=1.1,scale={W}:{H}:flags=bilinear,setsar=1[base]")
        idx = 1
    else:
        fc.append(f"color=c=black:s={W}x{H}:d={dur + 1:.2f}[base]")
    cur = "base"
    for j, pn in enumerate(lay["panels"]):
        s, d = pn["src"], pn["dst"]
        fc.append(f"[v{idx + j}]crop={s['w']}:{s['h']}:{s['x']}:{s['y']},scale={d['w']}:{d['h']}:flags=lanczos,setsar=1[p{j}]")
        fc.append(f"[{cur}][p{j}]overlay={d['x']}:{d['y']}:shortest=1[o{j}]")
        cur = f"o{j}"
    fps = 30 if not src_fps or src_fps < 40 else 60
    fc.append(f"[{cur}]ass='{_fesc(ass_path)}':fontsdir='{_fesc(str(config.FONTS_DIR))}',fps={fps},format=yuv420p[vout]")
    args = ["-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", src]
    maps = ["-map", "[vout]"]
    if has_audio:
        af = [f"volume={gain_db:.1f}dB"] if abs(gain_db) > 0.05 else []
        if normalize:
            af.append("loudnorm=I=-14:TP=-1.5:LRA=11")
        af.append("aresample=48000")
        fc.append(f"[0:a]{','.join(af)}[aout]")
        maps += ["-map", "[aout]", "-c:a", "aac", "-b:a", "160k", "-ac", "2"]
    preset, crf, maxrate = config.QUALITY.get(quality, config.QUALITY["standard"])
    args += ["-filter_complex", ";".join(fc), *maps, "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-maxrate", maxrate, "-bufsize", "16M", "-profile:v", "high", "-level", "4.2", "-g", str(fps * 2),
             "-t", f"{dur:.3f}", "-movflags", "+faststart", out]
    media.run_ffmpeg(args, dur, progress)
    try:
        os.remove(ass_path)
    except OSError:
        pass
    m = media.probe(out)
    return {"width": m["width"], "height": m["height"], "duration": round(m["duration"], 2), "vcodec": m["vcodec"],
            "acodec": m["acodec"], "size": os.path.getsize(out), "fps": m["fps"]}
