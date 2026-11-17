"""Pluggable speech-to-text. Every provider returns the same shape:

    {"provider": str, "word_timing": "exact"|"estimated", "words": [{"s": float, "e": float, "w": str}, ...]}

Providers are tried in order when TRANSCRIBE_PROVIDER=auto: faster-whisper (local, exact word timestamps),
sherpa-onnx Whisper (local, word times estimated from voiced audio), OpenAI API (paid, exact).
"""
import glob
import json
import os
import re
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from .. import config

SR = 16000


class TranscriptionUnavailable(RuntimeError):
    pass


def read_wav(path: str, start: float = 0.0, dur: Optional[float] = None) -> np.ndarray:
    with wave.open(path, "rb") as w:
        assert w.getframerate() == SR and w.getnchannels() == 1, "expected 16 kHz mono wav"
        w.setpos(min(int(start * SR), w.getnframes()))
        n = w.getnframes() - w.tell() if dur is None else int(dur * SR)
        raw = w.readframes(max(0, n))
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def wav_duration(path: str) -> float:
    with wave.open(path, "rb") as w:
        return w.getnframes() / w.getframerate()


# --------------------------------------------------------------------------- VAD chunking
def speech_chunks(path: str, max_len: float = 12.0, min_sil: float = 0.28) -> list[tuple[float, float]]:
    """Split audio into speech chunks at pauses. Energy-based; adaptive to the stream's noise floor
    (game audio is always present under a streamer's voice, so a fixed threshold would never fire)."""
    hop = 0.02
    n_hop = int(SR * hop)
    env_parts = []
    with wave.open(path, "rb") as w:
        while True:
            raw = w.readframes(SR * 60)
            if not raw:
                break
            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            k = len(a) // n_hop
            if k:
                env_parts.append(np.sqrt((a[:k * n_hop].reshape(k, n_hop) ** 2).mean(axis=1) + 1e-10))
    if not env_parts:
        return []
    env = np.concatenate(env_parts)
    db = 20 * np.log10(env + 1e-6)
    floor, loud = np.percentile(db, 10), np.percentile(db, 90)
    thr = floor + max(4.0, 0.35 * (loud - floor))
    voiced = db > thr
    # close gaps shorter than min_sil
    gap = int(min_sil / hop)
    chunks, i, n = [], 0, len(voiced)
    while i < n:
        if not voiced[i]:
            i += 1
            continue
        j, last = i, i
        while j < n and (voiced[j] or j - last <= gap):
            if voiced[j]:
                last = j
            j += 1
        chunks.append((i * hop, (last + 1) * hop))
        i = j
    # pad, merge tiny, split long
    out: list[tuple[float, float]] = []
    total = len(env) * hop
    for s, e in chunks:
        s, e = max(0.0, s - 0.12), min(total, e + 0.2)
        if e - s < 0.25:
            continue
        if out and s - out[-1][1] < 0.15 and e - out[-1][0] <= max_len:
            out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    final = []
    for s, e in out:
        while e - s > max_len:
            # cut at the quietest point in the back half of the allowed window
            a, b = int((s + max_len * 0.5) / hop), int((s + max_len) / hop)
            cut = (a + int(np.argmin(db[a:b]))) * hop
            final.append((s, cut))
            s = cut
        final.append((s, e))
    return final


def spread_words(text: str, audio: np.ndarray, t0: float) -> list[dict]:
    """Estimate word times inside a short chunk: distribute words over *voiced* time,
    weighted by word length. Used only when the model gives no timestamps."""
    words = text.split()
    if not words:
        return []
    hop = int(SR * 0.02)
    k = max(1, len(audio) // hop)
    env = np.sqrt((audio[:k * hop].reshape(k, hop) ** 2).mean(axis=1) + 1e-10) if len(audio) >= hop else np.ones(1)
    thr = max(np.percentile(env, 25), env.max() * 0.08)
    weight = np.where(env > thr, 1.0, 0.0)
    if weight.sum() == 0:
        weight[:] = 1.0
    cum = np.concatenate([[0.0], np.cumsum(weight)])
    cum /= cum[-1]
    lens = np.array([len(w.strip(".,!?;:\"'")) + 1.5 for w in words], dtype=float)
    edges = np.concatenate([[0.0], np.cumsum(lens) / lens.sum()])
    grid = np.arange(len(cum)) * 0.02
    # a word starts where voiced audio resumes (skip silence before it) and ends where its share of voiced audio ends
    starts = grid[np.clip(np.searchsorted(cum, edges[:-1], side="right") - 1, 0, len(grid) - 1)]
    ends = grid[np.clip(np.searchsorted(cum, edges[1:] - 1e-9, side="left"), 0, len(grid) - 1)]
    out = []
    for i, w in enumerate(words):
        s0 = float(starts[i])
        e0 = max(float(ends[i]), s0 + 0.04)
        if i + 1 < len(words):
            e0 = max(s0, min(e0, float(starts[i + 1])))
        out.append({"s": round(t0 + s0, 3), "e": round(t0 + e0, 3), "w": w})
    return out


# --------------------------------------------------------------------------- providers
class FasterWhisperProvider:
    name = "faster_whisper"
    word_timing = "exact"

    def __init__(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise TranscriptionUnavailable("faster-whisper is not installed") from e
        try:
            self.model = WhisperModel(config.WHISPER_MODEL, device="cpu", compute_type="int8",
                                      cpu_threads=os.cpu_count() or 2,
                                      download_root=str(config.MODELS_DIR / "faster-whisper"))
        except Exception as e:  # model download blocked / not cached
            raise TranscriptionUnavailable(f"faster-whisper model '{config.WHISPER_MODEL}' unavailable: {e}") from e

    def transcribe(self, wav: str, progress: Optional[Callable[[float], None]] = None) -> list[dict]:
        total = wav_duration(wav)
        segs, _ = self.model.transcribe(wav, word_timestamps=True, vad_filter=True, beam_size=1,
                                        condition_on_previous_text=False)
        words = []
        for seg in segs:
            for w in seg.words or []:
                words.append({"s": round(w.start, 3), "e": round(w.end, 3), "w": w.word.strip()})
            if progress:
                progress(min(1.0, seg.end / max(total, 1)))
        return words


class SherpaWhisperProvider:
    name = "sherpa_onnx"
    word_timing = "estimated"

    def __init__(self):
        try:
            import sherpa_onnx
        except ImportError as e:
            raise TranscriptionUnavailable("sherpa-onnx is not installed") from e
        d = config.SHERPA_MODEL_DIR
        if not d:
            found = sorted(glob.glob(str(config.MODELS_DIR / "sherpa-onnx-whisper-*")))
            d = found[0] if found else ""
        if not d or not Path(d).is_dir():
            raise TranscriptionUnavailable("no sherpa-onnx Whisper model folder found in models/")

        def pick(pat):
            hits = sorted(glob.glob(os.path.join(d, pat)))
            int8 = [h for h in hits if "int8" in h]
            return (int8 or hits or [None])[0]
        enc, dec, tok = pick("*encoder*.onnx"), pick("*decoder*.onnx"), pick("*tokens.txt")
        if not (enc and dec and tok):
            raise TranscriptionUnavailable(f"incomplete sherpa-onnx model in {d}")
        self.rec = sherpa_onnx.OfflineRecognizer.from_whisper(
            encoder=enc, decoder=dec, tokens=tok, num_threads=os.cpu_count() or 2)

    def transcribe(self, wav: str, progress=None) -> list[dict]:
        chunks = speech_chunks(wav)
        words: list[dict] = []
        total = max(1e-6, sum(e - s for s, e in chunks))
        done = 0.0
        for s, e in chunks:
            audio = read_wav(wav, s, e - s)
            st = self.rec.create_stream()
            st.accept_waveform(SR, audio)
            self.rec.decode_stream(st)
            text = (st.result.text or "").strip()
            if text and not _is_junk(text):
                words.extend(spread_words(text, audio, s))
            done += e - s
            if progress:
                progress(done / total)
        return words


class OpenAIProvider:
    """OpenAI /audio/transcriptions. Audio is sent as 32 kbps mono MP3 in 10-minute pieces (API limit 25 MB)."""
    name = "openai"
    word_timing = "exact"

    def __init__(self):
        if not config.OPENAI_API_KEY:
            raise TranscriptionUnavailable("OPENAI_API_KEY is not set")

    def transcribe(self, wav: str, progress=None) -> list[dict]:
        import httpx
        total = wav_duration(wav)
        piece = 600.0
        words: list[dict] = []
        t = 0.0
        with tempfile.TemporaryDirectory() as td, httpx.Client(timeout=600) as cl:
            while t < total:
                mp3 = os.path.join(td, "p.mp3")
                subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", str(t), "-t", str(piece), "-i", wav,
                                "-ac", "1", "-b:a", "32k", mp3], capture_output=True, check=True)
                with open(mp3, "rb") as f:
                    r = cl.post("https://api.openai.com/v1/audio/transcriptions",
                                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                                data={"model": config.OPENAI_TRANSCRIBE_MODEL, "response_format": "verbose_json",
                                      "timestamp_granularities[]": "word"},
                                files={"file": ("audio.mp3", f, "audio/mpeg")})
                r.raise_for_status()
                for w in r.json().get("words", []):
                    words.append({"s": round(t + w["start"], 3), "e": round(t + w["end"], 3), "w": w["word"].strip()})
                t += piece
                if progress:
                    progress(min(1.0, t / total))
        return words


_JUNK = {"[music]", "(music)", "[blank_audio]", "[ silence ]", "[silence]", "you", "thank you.", "thanks for watching!",
         ".", "[applause]", "[laughter]", "(laughing)"}


def _is_junk(text: str) -> bool:
    """Whisper hallucinates stock phrases on music/noise; drop the well-known ones."""
    return text.strip().lower() in _JUNK


_NONSPEECH = re.compile(r"^[\(\[\*].*[\)\]\*][.,!?]*$")
_LAUGH = re.compile(r"^(?:h[ae]|ah|eh){3,}h?[.,!?]*$", re.I)


def clean_words(words: list[dict]) -> list[dict]:
    """Drop non-speech tags such as (static) / [MUSIC]; normalise long laughter strings to a caption-sized word."""
    out = []
    for w in words:
        t = w["w"].strip()
        if not t or _NONSPEECH.match(t):
            continue
        if _LAUGH.match(t):
            t = "Hahaha!"
        out.append({"s": w["s"], "e": w["e"], "w": t})
    return out


PROVIDERS = {"faster_whisper": FasterWhisperProvider, "sherpa_onnx": SherpaWhisperProvider, "openai": OpenAIProvider}
_cache: dict[str, object] = {}


def get_provider(name: Optional[str] = None):
    name = name or config.TRANSCRIBE_PROVIDER
    if name == "none":
        raise TranscriptionUnavailable("transcription disabled (TRANSCRIBE_PROVIDER=none)")
    order = ["faster_whisper", "sherpa_onnx", "openai"] if name == "auto" else [name]
    errors = []
    for n in order:
        if n in _cache:
            return _cache[n]
        try:
            _cache[n] = PROVIDERS[n]()
            return _cache[n]
        except TranscriptionUnavailable as e:
            errors.append(f"{n}: {e}")
        except KeyError:
            errors.append(f"{n}: unknown provider")
    raise TranscriptionUnavailable("; ".join(errors))


def provider_status() -> list[dict]:
    """Cheap availability check for the settings page: never loads a model."""
    import importlib.util
    out = []
    fw = importlib.util.find_spec("faster_whisper") is not None
    out.append({"name": "faster_whisper", "available": fw,
                "detail": f"local, free, exact word timing; model {config.WHISPER_MODEL} downloads on first use" if fw
                else "not installed (pip install faster-whisper)"})
    sh = importlib.util.find_spec("sherpa_onnx") is not None
    has_model = bool(config.SHERPA_MODEL_DIR) or bool(glob.glob(str(config.MODELS_DIR / "sherpa-onnx-whisper-*")))
    out.append({"name": "sherpa_onnx", "available": sh and has_model,
                "detail": "local, free, estimated word timing" if sh and has_model
                else "optional fallback; run scripts/get_models.sh to install"})
    out.append({"name": "openai", "available": bool(config.OPENAI_API_KEY),
                "detail": "paid API, exact word timing" if config.OPENAI_API_KEY else "set OPENAI_API_KEY to enable"})
    return out


def transcribe(wav: str, cache_path: str, progress=None) -> dict:
    """Transcribe with caching: a finished transcript is never recomputed."""
    if os.path.exists(cache_path):
        return json.load(open(cache_path))
    try:
        p = get_provider()
    except TranscriptionUnavailable as e:
        return {"provider": "none", "word_timing": "none", "words": [], "warning": str(e)}
    words = p.transcribe(wav, progress)
    words = clean_words(words)
    out = {"provider": p.name, "word_timing": p.word_timing, "words": words}
    tmp = cache_path + ".tmp"
    json.dump(out, open(tmp, "w"))
    os.replace(tmp, cache_path)
    return out


def estimate_cost(duration_s: float) -> dict:
    """Cost before running: only the OpenAI route costs money. Does not load any model."""
    name = config.TRANSCRIBE_PROVIDER
    usd = duration_s / 60 * config.OPENAI_TRANSCRIBE_PRICE_PER_MIN if name == "openai" else 0.0
    return {"provider": name, "usd": round(usd, 4)}
