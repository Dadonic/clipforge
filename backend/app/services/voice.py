"""Voice separation: pull the streamer's voice out of the game audio before transcription.

Loud gameplay (gunfire, engines, music) buries speech, so the speech model mishears or skips lines and
the captions and hook text suffer. A vocal-separation model (UVR MDX-Net "Voc_FT", run on onnxruntime,
no GPU needed) keeps the voice and drops most of the rest. The cleaned track is used only for
transcription; clips keep the original audio.

The model file is downloaded once into the models folder. If it is missing or anything fails, the
analysis carries on with the original audio.
"""
import os
import subprocess
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .. import config

MODEL_NAME = "UVR-MDX-NET-Voc_FT.onnx"
MODEL_URL = "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/" + MODEL_NAME
SR = 44100
N_FFT, HOP, DIM_F, DIM_T = 7680, 1024, 3072, 256       # Voc_FT's training geometry
CHUNK = HOP * (DIM_T - 1)                               # samples per model window (~5.8 s)
TRIM = N_FFT // 2
COMPENSATE = 1.021


class SeparationUnavailable(RuntimeError):
    pass


def model_path() -> Path:
    return config.MODELS_DIR / "uvr" / MODEL_NAME


def ensure_model(timeout: float = 300.0) -> Path:
    p = model_path()
    if p.exists() and p.stat().st_size > 10_000_000:
        return p
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        import httpx
        tmp = p.with_suffix(".part")
        with httpx.stream("GET", MODEL_URL, follow_redirects=True, timeout=timeout) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for b in r.iter_bytes(1 << 20):
                    f.write(b)
        os.replace(tmp, p)
    except Exception as e:
        raise SeparationUnavailable(f"voice model download failed: {e}") from e
    return p


def _session(path: Path):
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise SeparationUnavailable("onnxruntime is not installed") from e
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
    return ort.InferenceSession(str(path), sess_options=so, providers=["CPUExecutionProvider"])


_window = np.hanning(N_FFT + 1)[:-1].astype(np.float32)  # periodic Hann, as torch.hann_window


def _stft(x: np.ndarray) -> np.ndarray:
    """x: (2, n) -> (2, N_FFT//2+1, frames) complex. Centred, reflect-padded (torch.stft defaults)."""
    pad = N_FFT // 2
    x = np.pad(x, ((0, 0), (pad, pad)), mode="reflect")
    frames = 1 + (x.shape[1] - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(frames)[:, None]
    seg = x[:, idx] * _window                               # (2, frames, N_FFT)
    return np.fft.rfft(seg, axis=-1).transpose(0, 2, 1)     # (2, bins, frames)


def _istft(spec: np.ndarray, length: int) -> np.ndarray:
    """spec: (2, bins, frames) complex -> (2, length). Overlap-add with window-square normalisation."""
    frames = spec.shape[2]
    seg = np.fft.irfft(spec.transpose(0, 2, 1), n=N_FFT, axis=-1) * _window   # (2, frames, N_FFT)
    total = N_FFT + HOP * (frames - 1)
    out = np.zeros((2, total), dtype=np.float32)
    norm = np.zeros(total, dtype=np.float32)
    w2 = _window ** 2
    for i in range(frames):
        s = i * HOP
        out[:, s:s + N_FFT] += seg[:, i]
        norm[s:s + N_FFT] += w2
    out /= np.maximum(norm, 1e-8)
    pad = N_FFT // 2
    return out[:, pad:pad + length]


def _run_chunk(sess, x: np.ndarray) -> np.ndarray:
    """x: (2, CHUNK + 2*TRIM) -> vocals for the same span."""
    spec = _stft(x)[:, :DIM_F, :DIM_T]
    inp = np.concatenate([spec.real, spec.imag], axis=0)[None].astype(np.float32)        # (1, 4, F, T)
    out = sess.run(None, {"input": inp})[0][0]
    full = np.zeros((2, N_FFT // 2 + 1, DIM_T), dtype=np.complex64)
    full[:, :DIM_F, :] = out[:2] + 1j * out[2:]
    return _istft(full, x.shape[1])


def separate(mix: np.ndarray, sess, progress: Optional[Callable[[float], None]] = None) -> np.ndarray:
    """mix: (2, n) float32 at 44.1 kHz -> vocals (2, n). Neighbouring windows overlap slightly and are cross-faded."""
    n = mix.shape[1]
    ov = SR // 2                                         # half a second of overlap, cross-faded
    step = CHUNK - ov
    padded = np.pad(mix, ((0, 0), (TRIM, TRIM + CHUNK)))
    out = np.zeros((2, n + CHUNK), dtype=np.float32)
    weight = np.zeros(n + CHUNK, dtype=np.float32)
    fade = np.ones(CHUNK, dtype=np.float32)
    ramp = np.linspace(1e-3, 1.0, ov, dtype=np.float32)
    fade[:ov], fade[-ov:] = ramp, ramp[::-1]
    starts = list(range(0, n, step))
    for k, s in enumerate(starts):
        seg = padded[:, s:s + CHUNK + 2 * TRIM]
        if seg.shape[1] < CHUNK + 2 * TRIM:
            seg = np.pad(seg, ((0, 0), (0, CHUNK + 2 * TRIM - seg.shape[1])))
        voc = _run_chunk(sess, seg)[:, TRIM:TRIM + CHUNK]
        out[:, s:s + CHUNK] += voc * fade
        weight[s:s + CHUNK] += fade
        if progress and k % 4 == 0:
            progress(min(1.0, (k + 1) / len(starts)))
    return (out[:, :n] / np.maximum(weight[:n], 1e-6)) * COMPENSATE


def _read_pcm(path: str) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 2, "expected 44.1 kHz stereo"
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).reshape(-1, 2).T.astype(np.float32) / 32768.0


def isolate_voice(src: str, dst_16k: str, duration: float, progress=None) -> dict:
    """Decode src to 44.1 kHz stereo, separate the voice and write it as 16 kHz mono (the speech model's format).
    Works in ~10-minute blocks so a long recording never has to sit in memory whole."""
    sess = _session(ensure_model())
    tmp = Path(dst_16k).with_suffix(".mix.wav")
    voc_tmp = Path(dst_16k).with_suffix(".voc44.wav")
    block = 600.0
    try:
        with wave.open(str(voc_tmp), "wb") as wv:
            wv.setnchannels(1)
            wv.setsampwidth(2)
            wv.setframerate(SR)
            t, total = 0.0, max(duration, 1.0)
            while t < total:
                subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.3f}",
                                "-t", f"{block:.3f}", "-i", src, "-vn", "-ac", "2", "-ar", str(SR),
                                "-c:a", "pcm_s16le", str(tmp)], check=True, capture_output=True)
                mix = _read_pcm(str(tmp))
                if mix.shape[1] == 0:
                    break
                sub = None
                if progress:
                    t0 = t
                    sub = lambda f, t0=t0: progress(min(1.0, (t0 + f * block) / total))
                voc = separate(mix, sess, sub).mean(axis=0)
                wv.writeframes((np.clip(voc, -1, 1) * 32767).astype(np.int16).tobytes())
                t += block
        subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-i", str(voc_tmp),
                        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", dst_16k], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise SeparationUnavailable(f"ffmpeg failed: {(e.stderr or b'').decode(errors='ignore')[-300:]}") from e
    finally:
        for f in (tmp, voc_tmp):
            try:
                os.remove(f)
            except OSError:
                pass
    return {"model": MODEL_NAME}
