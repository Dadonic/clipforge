"""Off-stream detection: parts of a recording that are not the stream itself.

People record streams off a video player, so a recording picks up ad breaks, buffering screens and
the player's own controls. None of that may end up in a clip. Two measured signals, no guessing:

  * layout break - a stream has fixed overlays (webcam, counters, chat box). The pixels that barely
    change across the whole recording are its "anchors". When most anchors change at once for
    several seconds, the picture is no longer the stream (ad, buffering, "be right back" card).
  * player bar - a thin red progress line near the bottom edge means the player's controls are
    showing on top of the picture.

A recording with no fixed overlays simply yields no layout breaks; detection is conservative.
"""
import subprocess

import numpy as np

from .. import config

SW, SH = 576, 324          # analysis frame; 6x6 blocks reduce it to GW x GH
GW, GH = 96, 54
REASONS = {"ad_or_break": "Ad break, buffering or off-stream screen",
           "player_controls": "Video player controls visible on screen"}


def player_bar(frame: np.ndarray) -> bool:
    """frame: SH x SW x 3 uint8 (RGB). True if a thin red line runs along the bottom of the picture."""
    strip = frame[int(frame.shape[0] * 0.85):].astype(np.int16)
    red = (strip[..., 0] > 150) & (strip[..., 1] < 80) & (strip[..., 2] < 80)
    frac = red.mean(axis=1)
    r = int(frac.argmax())
    if frac[r] < 0.10:
        return False
    away = [frac[i] for i in (r - 3, r + 3) if 0 <= i < len(frac)]
    if not away or max(away) > 0.03:                      # a thick red area is game content, not a progress line
        return False
    left = red[r, : max(4, int(red.shape[1] * 0.02))].mean()
    return bool(frac[r] >= 0.5 or left > 0.5)             # live streams fill the bar; recordings start at the left


def _runs(mask: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    out, s = [], None
    for i, m in enumerate(mask):
        if m and s is None:
            s = i
        elif not m and s is not None:
            if i - s >= min_len:
                out.append((s, i))
            s = None
    if s is not None and len(mask) - s >= min_len:
        out.append((s, len(mask)))
    return out


def layout_breaks(gray: np.ndarray) -> np.ndarray:
    """gray: frames x GH x GW. Boolean per frame: the fixed overlays are gone."""
    n = len(gray)
    if n < 30:
        return np.zeros(n, dtype=bool)
    g = gray.reshape(n, -1).astype(np.float32)
    med = np.median(g, axis=0)
    mad = np.median(np.abs(g - med), axis=0)
    idx = np.argsort(mad)[: max(50, int(0.08 * g.shape[1]))]
    if float(np.median(mad[idx])) > 10.0:                 # nothing on screen stays put: no overlays to anchor on
        return np.zeros(n, dtype=bool)
    d = np.abs(g[:, idx] - med[idx]).mean(axis=1)
    return d > max(30.0, 4.0 * float(np.median(d)))


def segments(gray: np.ndarray, bar: np.ndarray, step: float, duration: float) -> list[dict]:
    segs = []
    for a, b in _runs(layout_breaks(gray), max(1, int(round(3.0 / step)))):
        segs.append({"s": a * step - 1.0, "e": b * step + 1.0, "reason": "ad_or_break"})
    for a, b in _runs(np.asarray(bar, dtype=bool), max(1, int(round(2.0 / step)))):
        segs.append({"s": a * step - 0.5, "e": b * step + 0.5, "reason": "player_controls"})
    segs.sort(key=lambda x: (x["s"], x["reason"]))
    merged: list[dict] = []
    for sg in segs:
        sg["s"], sg["e"] = max(0.0, sg["s"]), min(duration, sg["e"])
        if merged and sg["s"] <= merged[-1]["e"] + 1.0:
            m = merged[-1]
            m["e"] = max(m["e"], sg["e"])
            if sg["reason"] == "ad_or_break":
                m["reason"] = "ad_or_break"
        else:
            merged.append(dict(sg))
    total = sum(m["e"] - m["s"] for m in merged)
    if duration > 0 and total / duration > 0.4:           # "most of the recording is off-stream" means the read is wrong
        return []
    return [{"s": round(m["s"], 2), "e": round(m["e"], 2), "reason": m["reason"]} for m in merged]


def scan(src: str, duration: float, progress=None) -> dict:
    """One decode pass over the preview proxy. Returns {"step", "segments", "seconds"}."""
    step = max(1.0, duration / 3600.0)
    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error"]
    if config.FFMPEG_THREADS:
        cmd += ["-threads", str(config.FFMPEG_THREADS)]
    cmd += ["-i", str(src), "-an", "-vf", f"fps={1.0 / step:.6f},scale={SW}:{SH}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    size, grays, bars = SW * SH * 3, [], []
    expect = max(1, int(duration / step))
    try:
        while True:
            buf = p.stdout.read(size)
            if len(buf) < size:
                break
            f = np.frombuffer(buf, dtype=np.uint8).reshape(SH, SW, 3)
            grays.append(f.reshape(GH, SH // GH, GW, SW // GW, 3).mean(axis=(1, 3, 4)).astype(np.uint8))
            bars.append(player_bar(f))
            if progress and len(grays) % 20 == 0:
                progress(min(1.0, len(grays) / expect))
    finally:
        p.stdout.close()
        p.wait()
    if not grays:
        return {"step": step, "segments": [], "seconds": 0.0}
    segs = segments(np.stack(grays), np.array(bars), step, duration)
    return {"step": step, "segments": segs, "seconds": round(sum(s["e"] - s["s"] for s in segs), 1)}


def overlap(segs: list[dict], start: float, end: float) -> list[dict]:
    return [s for s in segs or [] if s["e"] > start and s["s"] < end]
