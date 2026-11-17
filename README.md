# ClipForge AI

Turns long livestream recordings into ranked, captioned, vertical clips, checks each clip against the
campaign's rules, and tracks submissions, approved views and earnings. Built first for GTA 6 streamer
campaigns on Whop Content Rewards.

## What it does

1. **Ingest** – resumable chunked upload (MP4/MOV/MKV/WebM) or import from a direct file link. FFprobe reads the metadata.
2. **Analyse** – audio extraction → speech-to-text with word timestamps → audio energy / laughter / shout detection →
   scene-change detection → optional chat-spike analysis → facecam detection.
3. **Rank** – builds an interest curve, picks peaks, snaps each window to sentence boundaries (setup before the peak,
   payoff after it), scores, de-duplicates, flags possible spoilers / cutscenes / graphic violence. Optional LLM re-rank.
4. **Clip** – 1080×1920 H.264 + AAC with six layouts and five animated caption styles, watermark, loudness normalisation.
5. **Review** – live canvas preview of crop and captions, trim, edit words, redraw facecam, editing assistant, bulk actions.
6. **Comply** – per-campaign checklist (duration, hashtags, CTA, watermark, credit, prohibited terms, deadline, flags).
   A clip cannot be approved while any rule fails.
7. **Track** – record each posted link, update views / approved views (inline or CSV import), see estimated vs confirmed earnings.

## Run it

### Docker (recommended on a VPS)

```bash
cp .env.example .env            # optional: set LLM_API_KEY, WHISPER_MODEL, etc.
docker compose up -d --build    # web on :8000, separate worker, PostgreSQL
```

Open `http://<server>:8000`, create your account, then set `ALLOW_SIGNUP=false` in `.env` and restart.
Put it behind HTTPS (Caddy or nginx) and set `COOKIE_SECURE=true` before using it over the internet.
Allow large request bodies / long timeouts on the proxy for uploads (chunks are 8 MB).

### Without Docker

```bash
sudo apt install ffmpeg fontconfig          # ffmpeg 5+ with libass
pip install -r requirements.txt
cd backend && uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Defaults: SQLite database and all files under `./data`, jobs run inside the API process. To run the worker
separately set `EMBEDDED_WORKER=false` and start `python -m app.worker` from `backend/`.

### Hardware

No GPU needed. Measured on a 2-core / 8 GB machine with the `base.en` model: a 4-minute 720p recording analysed
in ~42 s (≈6× real time) and a 17 s clip rendered in ~10 s. Expect a 2-hour stream to take roughly 20–30 minutes to
analyse on similar hardware; clips then render in seconds each. Re-running the analysis with different durations
reuses the cached transcript and takes seconds. Budget ~1.3× the recording size in free disk.

## Credentials and external services

| What | Variable | Needed? |
|---|---|---|
| Speech-to-text (local) | `WHISPER_MODEL` | No key. faster-whisper downloads the model from Hugging Face on first use. |
| Speech-to-text (OpenAI) | `OPENAI_API_KEY`, `TRANSCRIBE_PROVIDER=openai` | Optional, ~$0.006 per audio minute. |
| LLM re-rank | `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | Optional. Any OpenAI-compatible endpoint (OpenAI, OpenRouter, Groq, Moonshot/Kimi, local Ollama). Without it ranking uses signal scoring only. |
| Clip storage | `STORAGE_BACKEND=s3`, `S3_*` | Optional. Cloudflare R2 / S3. Needs `pip install boto3`. |
| Whop | none | See below. |

`TRANSCRIBE_PROVIDER=auto` tries faster-whisper, then sherpa-onnx (`scripts/get_models.sh`, model hosted on GitHub,
word timing estimated from voiced audio), then OpenAI. If none is available the analysis still runs on audio and
scene signals and tells you captions are unavailable.

## Whop integration: what exists and what does not

Researched November 2026. Whop's developer docs describe a **Bounties API** (`POST /bounties`,
`POST /bounty_submissions`, `GET /bounty_submissions`) for flat-fee bounties paid per accepted submission. They do not
document a clipper-side API for **Content Rewards** per-view campaigns: no endpoint to list campaigns, submit a post
to a campaign, or read view-based earnings. Third-party scrapers of the public campaign directory exist; ClipForge does
not use them.

So campaigns are entered by hand (paste the rules from the campaign page), submissions are recorded by you after you
have actually posted and submitted, and views/earnings come from what you type or import by CSV. Nothing is ever
marked as submitted, approved or paid automatically.

CSV import expects a header row with a link column (`url`, `submission_url`, `link`…) and any of `views`,
`approved_views`, `earnings`, `status`, `payment_date`. Rows are matched to existing submissions by link; unknown
links are reported back and never created.

## Content rules (GTA 6 profile)

`backend/app/profiles/gta6.json` holds every keyword list: moment categories, reaction words, spoiler triggers,
violence triggers, strong language, chat hype tokens. Edit the file and restart. Flags never reject a clip on their
own; they block approval until you tick "I watched this clip and these flags are fine".

Upload requires confirming you are authorised to use the footage. The URL importer only accepts direct links to
video files; it does not download from Twitch, Kick or YouTube pages.

## Testing

```bash
cd backend && python -m pytest -q                       # 45 unit tests, ~4 s
python scripts/make_test_stream.py --video any.mp4 --voice voice.onnx --out test_stream.mp4   # needs piper-tts
python scripts/e2e.py --base http://127.0.0.1:8000 --video test_stream.mp4 \
       --truth test_stream.truth.json --chat test_stream.chat.txt                              # 54 checks
```

`e2e.py` works with any real recording too (omit `--truth`); it then checks everything except the ranking ground truth.

### What has been verified, and what has not

Verified here: full pipeline through the API on SQLite (embedded worker) and on PostgreSQL 16 (separate worker);
exported files probed and fully decoded with FFmpeg; UI driven in Chrome at 1280 px and 390 px.

Not verified here, because the build sandbox had no access or no credentials:

- **Real GTA 6 streamer footage.** The ranking test used a synthetic stream (real video, synthesised commentary with
  three scripted moments, one loud distractor, matching chat). Weights in `services/highlights.py` and thresholds in
  `services/audio_analysis.py` will need tuning on real VODs.
- **Facecam detection on a real webcam overlay.** The clustering logic is unit-tested; the face detector itself was
  not run on a real facecam. Manual drawing in the editor always works.
- **faster-whisper** (Hugging Face was unreachable). Tested with the sherpa-onnx fallback instead.
- **OpenAI transcription, LLM re-rank, S3/R2 upload.** Implemented against the documented APIs; never called with real keys.
- **`docker compose up`.** Files are written but no Docker daemon was available to build them.
- **iOS Safari.** Tested in desktop Chrome at phone width only.

Laughter and shout detection are signal-processing heuristics (burst rhythm, brightness, harmonicity), not a trained
sound classifier. The "possible cutscene" flag is likewise a heuristic.

## Layout

```
backend/app/main.py            API, auth, static files
backend/app/worker.py          job queue: analyse, render, import
backend/app/routers/           videos, clips, business (campaigns, submissions, earnings, dashboard)
backend/app/services/          media, transcription, audio_analysis, highlights, semantic, chat, facecam,
                               captions, render, compliance, storage
backend/app/profiles/gta6.json content profile and policy lists
backend/tests/                 unit tests
frontend/                      no-build single-page app (Preact + htm, vendored)
fonts/                         caption fonts; add .ttf/.otf files here to offer more
scripts/                       e2e test, test-stream generator, model fetcher
```

Schema changes: append SQL to `MIGRATIONS` in `backend/app/db.py`; they run once, in order, at startup.
Logs go to stdout (`docker compose logs -f web worker`). Failed jobs keep their error message and can be retried
from the command centre.

## Not built yet

Per the brief, Phase 4 was left out: teams/roles, billing, cloud autoscaling. Also not built: dynamic speaker
tracking (the facecam region is fixed per recording), direct posting to TikTok/YouTube/Instagram, and a trained
sound-event model.

## Free hosting on GitHub Codespaces

Open the repository on GitHub → **Code** → **Codespaces** → **Create codespace**. The first start installs FFmpeg and
the Python packages (a few minutes), then `start.sh` launches the app and port 8000 opens in the browser. If it does
not open, run `bash start.sh` in the terminal and open port 8000 from the **Ports** tab. Stop the codespace when you
finish (free accounts get 120 core-hours and 15 GB-months of storage per month) and delete recordings you are done with.
