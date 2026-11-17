"""Central configuration. Everything is driven by environment variables (see .env.example)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _load_dotenv() -> None:
    f = ROOT / ".env"
    if not f.exists():
        return
    for line in f.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

DATA_DIR = Path(_env("CLIPFORGE_DATA_DIR", str(ROOT / "data"))).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
WORK_DIR = DATA_DIR / "work"        # per-video caches: audio, proxy, transcript, analysis
CLIP_DIR = DATA_DIR / "clips"       # rendered MP4s
FONTS_DIR = Path(_env("CLIPFORGE_FONTS_DIR", str(ROOT / "fonts"))).resolve()
MODELS_DIR = Path(_env("CLIPFORGE_MODELS_DIR", str(ROOT / "models"))).resolve()
FRONTEND_DIR = ROOT / "frontend"
PROFILES_DIR = Path(__file__).resolve().parent / "profiles"

for d in (DATA_DIR, UPLOAD_DIR, WORK_DIR, CLIP_DIR):
    d.mkdir(parents=True, exist_ok=True)

DATABASE_URL = _env("DATABASE_URL", f"sqlite:///{DATA_DIR / 'clipforge.db'}")
SECRET_KEY = _env("SECRET_KEY", "")
if not SECRET_KEY:
    # Stable per-install secret so sessions survive restarts without the user configuring anything.
    kf = DATA_DIR / ".secret_key"
    if not kf.exists():
        kf.write_text(os.urandom(32).hex())
        kf.chmod(0o600)
    SECRET_KEY = kf.read_text().strip()

ALLOW_SIGNUP = _env("ALLOW_SIGNUP", "true").lower() == "true"
COOKIE_SECURE = _env("COOKIE_SECURE", "false").lower() == "true"

# Worker
EMBEDDED_WORKER = _env("EMBEDDED_WORKER", "true").lower() == "true"
FFMPEG_THREADS = int(_env("FFMPEG_THREADS", "0"))          # 0 = ffmpeg decides
JOB_MAX_ATTEMPTS = int(_env("JOB_MAX_ATTEMPTS", "2"))

# Transcription: auto | faster_whisper | sherpa_onnx | openai | none
TRANSCRIBE_PROVIDER = _env("TRANSCRIBE_PROVIDER", "auto")
WHISPER_MODEL = _env("WHISPER_MODEL", "base.en")            # faster-whisper model name
SHERPA_MODEL_DIR = _env("SHERPA_MODEL_DIR", "")             # folder with *-encoder*.onnx etc.
OPENAI_API_KEY = _env("OPENAI_API_KEY")
OPENAI_TRANSCRIBE_MODEL = _env("OPENAI_TRANSCRIBE_MODEL", "whisper-1")
OPENAI_TRANSCRIBE_PRICE_PER_MIN = float(_env("OPENAI_TRANSCRIBE_PRICE_PER_MIN", "0.006"))

# Semantic analysis (any OpenAI-compatible chat endpoint: OpenAI, OpenRouter, Groq, Moonshot/Kimi, Ollama...)
LLM_BASE_URL = _env("LLM_BASE_URL", "https://api.openai.com/v1")
LLM_API_KEY = _env("LLM_API_KEY")
LLM_MODEL = _env("LLM_MODEL", "gpt-4o-mini")
LLM_PRICE_IN_PER_M = float(_env("LLM_PRICE_IN_PER_M", "0.15"))
LLM_PRICE_OUT_PER_M = float(_env("LLM_PRICE_OUT_PER_M", "0.60"))

# Rendering quality presets: name -> (x264 preset, crf, max bitrate)
QUALITY = {
    "draft": ("ultrafast", 26, "5M"),
    "standard": ("veryfast", 21, "8M"),
    "high": ("medium", 19, "10M"),
}
DEFAULT_QUALITY = _env("RENDER_QUALITY", "standard")
PROXY_HEIGHT = int(_env("PROXY_HEIGHT", "480"))

# Storage: local | s3   (s3 works with Cloudflare R2, Backblaze B2, MinIO, AWS)
STORAGE_BACKEND = _env("STORAGE_BACKEND", "local")
S3_ENDPOINT = _env("S3_ENDPOINT")
S3_BUCKET = _env("S3_BUCKET")
S3_ACCESS_KEY = _env("S3_ACCESS_KEY")
S3_SECRET_KEY = _env("S3_SECRET_KEY")
S3_REGION = _env("S3_REGION", "auto")

OUT_W, OUT_H = 1080, 1920
