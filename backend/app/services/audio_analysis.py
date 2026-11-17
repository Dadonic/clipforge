"""Audio signal analysis on the 16 kHz mono track. Produces one value per second for each signal.

Signals (all 0..1 unless noted):
  excite  - loudness above the *local* baseline (rolling 90 s median), so a constantly loud stream scores 0
  onset   - sudden jumps in loudness (second-to-second rise)
  laugh   - laughter-like sound: voiced, bright, with 3-9 Hz burst rhythm ("ha-ha-ha")   [heuristic]
  scream  - scream/shout-like: very loud relative to baseline and spectrally bright       [heuristic]
  silence - fraction of the second that is quiet
  db      - raw loudness in dBFS (for display)
The laugh/scream detectors are signal-processing heuristics, not a trained sound classifier.
"""
import wave

import numpy as np
from scipy.ndimage import median_filter, uniform_filter1d

SR = 16000
HOP = 320          # 20 ms
PER_SEC = SR // HOP


def _frames(path: str):
    """Yield (rms, centroid_hz) arrays per minute of audio, streaming from disk."""
    win = np.hanning(HOP * 2).astype(np.float32)
    freqs = np.fft.rfftfreq(HOP * 2, 1 / SR)
    with wave.open(path, "rb") as w:
        if w.getframerate() != SR or w.getnchannels() != 1:
            raise ValueError("expected 16 kHz mono wav")
        carry = np.zeros(0, dtype=np.float32)
        while True:
            raw = w.readframes(SR * 60)
            if not raw:
                break
            a = np.concatenate([carry, np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0])
            k = len(a) // HOP
            carry = a[k * HOP:]
            if k == 0:
                continue
            x = a[:k * HOP].reshape(k, HOP)
            rms = np.sqrt((x ** 2).mean(axis=1) + 1e-10)
            # spectral centroid on 40 ms windows (frame + next frame)
            nxt = np.vstack([x[1:], x[-1:]])
            spec = np.abs(np.fft.rfft(np.hstack([x, nxt]) * win, axis=1))
            cent = (spec * freqs).sum(axis=1) / (spec.sum(axis=1) + 1e-9)
            # spectral flatness: ~1 for noise/static, low for voiced (harmonic) sound such as a shout or laugh
            ps = spec[:, 4:] ** 2 + 1e-12
            flat = np.exp(np.log(ps).mean(axis=1)) / ps.mean(axis=1)
            yield rms, cent, flat


def analyze(path: str) -> dict:
    rms_parts, cent_parts, flat_parts = [], [], []
    for r, c, f in _frames(path):
        rms_parts.append(r)
        cent_parts.append(c)
        flat_parts.append(f)
    if not rms_parts:
        return {"seconds": 0, "excite": [], "onset": [], "laugh": [], "scream": [], "silence": [], "db": []}
    rms = np.concatenate(rms_parts)
    cent = np.concatenate(cent_parts)
    flat = np.concatenate(flat_parts)
    n_sec = len(rms) // PER_SEC
    if n_sec == 0:
        n_sec = 1
        rms = np.pad(rms, (0, PER_SEC - len(rms)), constant_values=1e-5)
        cent = np.pad(cent, (0, PER_SEC - len(cent)))
        flat = np.pad(flat, (0, PER_SEC - len(flat)), constant_values=1.0)
    rms, cent, flat = rms[:n_sec * PER_SEC], cent[:n_sec * PER_SEC], flat[:n_sec * PER_SEC]
    voiced = flat < 0.30
    db_f = 20 * np.log10(rms + 1e-6)

    sec_rms = np.sqrt((rms.reshape(n_sec, PER_SEC) ** 2).mean(axis=1))
    db = 20 * np.log10(sec_rms + 1e-6)

    # local baseline: rolling median over ~90 s, so sustained loud sections (music, shootouts) become "normal"
    base = median_filter(db, size=min(91, max(3, n_sec // 2 * 2 + 1)), mode="nearest")
    spread = max(3.0, float(np.percentile(db, 90) - np.percentile(db, 50)))
    excite = np.clip((db - base) / (spread * 1.6), 0, 1)

    # sudden jumps: only rises that are unusual *for this stream* count (top few percent)
    rise = np.clip(np.diff(db, prepend=db[0]), 0, None)
    p95, p995 = np.percentile(rise, 95), np.percentile(rise, 99.5)
    onset = np.clip((rise - p95) / max(p995 - p95, 3.0), 0, 1)

    floor = np.percentile(db_f, 10)
    quiet_thr = floor + max(4.0, 0.3 * (np.percentile(db_f, 90) - floor))
    silence = (db_f < quiet_thr).reshape(n_sec, PER_SEC).mean(axis=1)

    # --- laughter-like: burst rhythm 3-9 Hz in the loudness envelope + bright spectrum + not silent
    env = db_f - uniform_filter1d(db_f, size=PER_SEC)            # remove slow trend
    laugh = np.zeros(n_sec)
    fr = np.fft.rfftfreq(PER_SEC * 2, 1 / PER_SEC)
    band = (fr >= 3) & (fr <= 9)
    for i in range(n_sec):
        seg = env[max(0, (i - 1) * PER_SEC + PER_SEC // 2):(i + 1) * PER_SEC + PER_SEC // 2]
        if len(seg) < PER_SEC * 2:
            seg = np.pad(seg, (0, PER_SEC * 2 - len(seg)))
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
        rhythm = sp[band].sum() / (sp[1:].sum() + 1e-9)
        depth = np.clip(seg.std() / 6.0, 0, 1)                   # bursts need real amplitude swing
        bright = np.clip((cent[i * PER_SEC:(i + 1) * PER_SEC].mean() - 900) / 1500, 0, 1)
        tonal = voiced[i * PER_SEC:(i + 1) * PER_SEC].mean()
        laugh[i] = np.clip((rhythm - 0.45) / 0.4, 0, 1) * depth * (0.4 + 0.6 * bright) * (1 - silence[i]) * min(1.0, tonal * 2)
    laugh = np.clip(uniform_filter1d(laugh, size=3) * 1.6, 0, 1)

    # --- scream/shout-like: well above baseline and bright for a sustained part of the second
    hot = (db_f > np.repeat(base, PER_SEC) + spread * 0.9) & (cent > 1500) & voiced
    scream = np.clip(hot.reshape(n_sec, PER_SEC).mean(axis=1) * 1.8, 0, 1)

    r = lambda a: [round(float(v), 3) for v in a]
    return {"seconds": n_sec, "excite": r(excite), "onset": r(onset), "laugh": r(laugh), "scream": r(scream),
            "silence": r(silence), "db": [round(float(v), 1) for v in db]}
