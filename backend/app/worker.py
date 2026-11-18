"""Background worker. Jobs live in the database (no Redis needed at this scale): the API inserts a row,
a worker claims it, reports progress, and retries on failure. Run it embedded in the API process
(EMBEDDED_WORKER=true, default) or as its own process:  python -m app.worker
"""
import json
import logging
import os
import shutil
import threading
import time
import traceback
from pathlib import Path

from sqlalchemy import update

from . import config
from .db import Clip, Highlight, Job, SessionLocal, Video, init_db, log_activity, now
from .services import audio_analysis, chat, compliance, facecam, highlights, media, offstream, render, semantic, storage, transcription

log = logging.getLogger("clipforge.worker")


def work_dir(video_id: int) -> Path:
    d = config.WORK_DIR / str(video_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


class Reporter:
    """Throttled progress writer for one job."""

    def __init__(self, job_id: int):
        self.job_id, self._last = job_id, 0.0

    def __call__(self, progress: float, stage: str | None = None, force: bool = False):
        t = time.time()
        if not force and t - self._last < 1.0:
            return
        self._last = t
        vals = {"progress": round(max(0.0, min(1.0, progress)), 4)}
        if stage:
            vals["stage"] = stage
        with SessionLocal() as db:
            db.execute(update(Job).where(Job.id == self.job_id).values(**vals))
            db.commit()

    def span(self, lo: float, hi: float, stage: str):
        self(lo, stage, force=True)
        return lambda f: self(lo + (hi - lo) * f, stage)


def _load_json(p: Path, default=None):
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return default


# --------------------------------------------------------------------------- analyze
def run_analyze(job: Job, rep: Reporter) -> None:
    with SessionLocal() as db:
        v = db.get(Video, job.video_id)
        if not v:
            raise RuntimeError("Recording no longer exists")
        v.status, v.error = "analyzing", ""
        db.commit()
        src, dur, vid = v.path, v.duration or 0.0, v.id
        has_audio = bool(v.acodec)
        streamer, fc_source = v.streamer, v.facecam_source
    if not os.path.exists(src):
        raise RuntimeError("Source file is missing from disk")
    wd = work_dir(vid)
    settings = {**(job.payload or {})}
    settings.setdefault("streamer", streamer)
    cost = {}

    wav, proxy = wd / "audio.wav", wd / "proxy.mp4"
    if has_audio and not wav.exists():
        tmp = wd / "audio.tmp.wav"
        media.extract_audio(src, str(tmp), dur, rep.span(0.0, 0.08, "Extracting audio"))
        tmp.rename(wav)
    if not proxy.exists():
        tmp = wd / "proxy.tmp.mp4"
        media.make_proxy(src, str(tmp), dur, rep.span(0.08, 0.30, "Building preview proxy"))
        tmp.rename(proxy)

    words, tr = [], {"provider": "none", "word_timing": "none", "words": []}
    if has_audio:
        tr = transcription.transcribe(str(wav), str(wd / "transcript.json"), rep.span(0.30, 0.72, "Transcribing speech"))
        words = tr["words"]
        if tr["provider"] == "openai":
            cost["transcription_usd"] = round(dur / 60 * config.OPENAI_TRANSCRIBE_PRICE_PER_MIN, 4)

    audio = _load_json(wd / "audio.json")
    if audio is None:
        rep(0.72, "Analysing audio energy", force=True)
        audio = audio_analysis.analyze(str(wav)) if has_audio else {"seconds": int(dur)}
        (wd / "audio.json").write_text(json.dumps(audio))

    scenes = _load_json(wd / "scenes.json")
    if scenes is None:
        scenes = media.detect_scenes(str(proxy), dur, progress=rep.span(0.76, 0.88, "Detecting scene changes"))
        (wd / "scenes.json").write_text(json.dumps(scenes))

    off = _load_json(wd / "offstream.json")
    if off is None:
        try:
            off = offstream.scan(str(proxy), dur, rep.span(0.86, 0.88, "Checking for ads and player controls"))
        except Exception as e:  # a failed scan must not cost the user the analysis
            log.warning("off-stream scan failed: %s", e)
            off = {"step": 1.0, "segments": [], "seconds": 0.0, "error": str(e)[:200]}
        (wd / "offstream.json").write_text(json.dumps(off))
    settings["offstream"] = off.get("segments", [])

    cam = None
    if fc_source != "manual":
        rep(0.88, "Looking for a facecam", force=True)
        try:
            cam = facecam.detect(str(proxy), dur)
        except Exception as e:  # detection is a convenience, never fatal
            log.warning("facecam detection failed: %s", e)

    chat_sig = []
    chat_file = wd / "chat.txt"
    prof = semantic.load_profile(settings.get("profile", "gta6"))
    if chat_file.exists():
        msgs = chat.parse(chat_file.read_text(errors="ignore"))
        chat_sig = chat.signal(msgs, max(int(dur), 1), prof.get("chat_hype_tokens", []))

    rep(0.92, "Ranking highlights", force=True)
    cands, meta = highlights.find_highlights(words, audio, scenes, chat_sig, dur, settings)
    if meta.get("llm"):
        cost["llm_usd"] = meta["llm"].get("usd", 0)
        if meta["llm"].get("error"):
            cost["llm_error"] = meta["llm"]["error"]
    cost["total_usd"] = round(cost.get("transcription_usd", 0) + cost.get("llm_usd", 0), 4)
    (wd / "timeline.json").write_text(json.dumps({
        "audio": {k: audio.get(k, []) for k in ("excite", "laugh", "scream", "db")}, "scenes": scenes, "chat": chat_sig,
        "offstream": off.get("segments", [])}))

    with SessionLocal() as db:
        v = db.get(Video, vid)
        keep = db.query(Highlight).filter(Highlight.video_id == vid, Highlight.status == "clipped").all()
        db.query(Highlight).filter(Highlight.video_id == vid, Highlight.status != "clipped").delete()
        for c in cands:
            if any(min(c["end"], k.end) - max(c["start"], k.start) > 0.5 * (c["end"] - c["start"]) for k in keep):
                continue  # already turned into a clip; do not suggest it again
            db.add(Highlight(video_id=vid, rank=c["rank"], start=c["start"], end=c["end"], peak=c["peak"],
                             title=c["title"], title_options=c["title_options"], description=c["description"],
                             reason=c["reason"], hook=c["hook"], hashtags=c["hashtags"],
                             viral_potential=c["viral_potential"], engagement_score=c["engagement_score"],
                             confidence=c["confidence"], suggested_duration=c["suggested_duration"],
                             transcript=c["transcript"], category=c["category"], flags=c["flags"],
                             signals={**c["signals"], "standalone": c["standalone"]}, scored_by=c["scored_by"]))
        v.status = "ready"
        v.transcribe_provider = tr["provider"] + ("" if tr["word_timing"] != "estimated" else " (estimated word timing)")
        v.semantic_provider = meta["semantic"]
        v.cost = cost
        v.has_chat = bool(chat_sig)
        v.analysis_settings = {k: settings.get(k) for k in ("min_duration", "max_duration", "max_highlights", "use_llm", "auto_clips")}
        if tr.get("warning"):
            v.error = "No transcription provider available: " + tr["warning"]
        if cam and v.facecam_source != "manual":
            v.facecam, v.facecam_source = cam, "auto"
        skipped = f"; skipped {off['seconds']:.0f}s of ads or player controls" if off.get("seconds") else ""
        log_activity(db, v.user_id, "analysis", f"Analysed “{v.title}”: {len(cands)} highlights found{skipped}")
        db.commit()
        n_auto = int(settings.get("auto_clips") or 0)
        if n_auto > 0:
            from .db import User
            from .services import clipmaker
            made = clipmaker.make_top_clips(db, db.get(User, v.user_id), v, n_auto)
            if made:
                log_activity(db, v.user_id, "auto", f"Made {len(made)} clips automatically from this recording")
            db.commit()


# --------------------------------------------------------------------------- render
def run_render(job: Job, rep: Reporter) -> None:
    with SessionLocal() as db:
        c = db.get(Clip, job.clip_id)
        if not c:
            raise RuntimeError("Clip no longer exists")
        v = db.get(Video, c.video_id)
        if not v or not os.path.exists(v.path):
            raise RuntimeError("Source recording is missing from disk")
        rd = compliance.clip_render_dict(c, v)
        h = render.render_hash(rd)
        out = config.CLIP_DIR / f"clip_{c.id}_{h[:10]}.mp4"
        params = dict(src=v.path, out=str(out), start=c.start, end=c.end, src_w=v.width, src_h=v.height,
                      src_fps=v.fps or 30, layout=c.layout, layout_params=c.layout_params or {}, facecam=v.facecam,
                      words=c.words or [], caption_style=c.caption_style, caption_params=c.caption_params or {},
                      watermark=c.watermark_text or "", watermark_pos=c.watermark_pos or "top",
                      gain_db=c.audio_gain_db or 0.0, normalize=bool(c.normalize_audio),
                      quality=c.quality or "standard", has_audio=bool(v.acodec))
        old, cid = c.output_path, c.id
        c.status = "rendering"
        db.commit()
    try:
        meta = render.render_clip(**params, progress=rep.span(0.0, 0.97, "Rendering clip"))
    except Exception:
        with SessionLocal() as db:
            c = db.get(Clip, cid)
            if c:
                c.status = "rendered" if c.output_path and os.path.exists(c.output_path) else "draft"
                db.commit()
        raise
    key = None
    try:
        key = storage.upload(str(out), f"clips/{out.name}")
    except Exception as e:
        log.warning("object storage upload failed (clip kept locally): %s", e)
    with SessionLocal() as db:
        c = db.get(Clip, cid)
        if not c:
            return
        c.output_path, c.rendered_hash = str(out), h
        c.out_meta = {**meta, "s3_key": key}
        c.status = "rendered"
        c.compliance = None       # must be re-checked against the new render
        log_activity(db, c.user_id, "render", f"Rendered clip “{c.title}” ({meta['duration']:.0f}s)")
        db.commit()
    if old and old != str(out) and os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass            # still open in a player (Windows); leave it


# --------------------------------------------------------------------------- import from a direct file URL
def run_import(job: Job, rep: Reporter) -> None:
    import httpx
    url = job.payload["url"]
    with SessionLocal() as db:
        v = db.get(Video, job.video_id)
        dst = v.path
    rep(0.0, "Downloading file", force=True)
    with httpx.stream("GET", url, follow_redirects=True, timeout=60) as r:
        r.raise_for_status()
        ctype = r.headers.get("content-type", "")
        if "text/html" in ctype:
            raise RuntimeError("That link is a web page, not a video file. ClipForge only imports direct file links "
                               "you are authorised to download; it does not rip from streaming sites.")
        total = int(r.headers.get("content-length") or 0)
        got = 0
        with open(dst, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if total:
                    rep(got / total, "Downloading file")
    finalize_upload(job.video_id)


def finalize_upload(video_id: int) -> None:
    with SessionLocal() as db:
        v = db.get(Video, video_id)
        try:
            m = media.probe(v.path)
        except media.MediaError as e:
            v.status, v.error = "failed", str(e)
            db.commit()
            raise
        v.duration, v.width, v.height, v.fps = m["duration"], m["width"], m["height"], m["fps"]
        v.vcodec, v.acodec, v.size = m["vcodec"], m["acodec"], os.path.getsize(v.path)
        v.received = v.size
        v.status, v.error = "uploaded", ""
        log_activity(db, v.user_id, "upload", f"Added recording “{v.title}” ({m['duration'] / 60:.0f} min)")
        db.commit()


HANDLERS = {"analyze": run_analyze, "render": run_render, "import_url": run_import}


def claim_job() -> Job | None:
    with SessionLocal() as db:
        # renders are short and interactive: take them before long analyses
        for types in (("render",), ("import_url", "analyze")):
            job = db.query(Job).filter(Job.status == "queued", Job.type.in_(types)).order_by(Job.id).first()
            if job:
                n = db.execute(update(Job).where(Job.id == job.id, Job.status == "queued")
                               .values(status="running", started_at=now(), attempts=Job.attempts + 1,
                                       progress=0.0, error="")).rowcount
                db.commit()
                if n:
                    db.refresh(job)
                    return job
    return None


def process(job: Job) -> None:
    rep = Reporter(job.id)
    try:
        HANDLERS[job.type](job, rep)
        with SessionLocal() as db:
            db.execute(update(Job).where(Job.id == job.id).values(status="done", progress=1.0, stage="Done", finished_at=now()))
            db.commit()
    except Exception as e:
        log.error("job %s (%s) failed: %s\n%s", job.id, job.type, e, traceback.format_exc())
        with SessionLocal() as db:
            j = db.get(Job, job.id)
            permanent = any(x in str(e) for x in ("could not read", "no longer exists", "missing from disk", "No video stream"))
            retry = j.attempts < j.max_attempts and not permanent
            j.status = "queued" if retry else "failed"
            j.error = str(e)[:1000]
            j.stage = "Retrying" if retry else "Failed"
            j.finished_at = None if retry else now()
            if not retry and j.type == "analyze" and j.video_id:
                v = db.get(Video, j.video_id)
                if v:
                    v.status, v.error = "failed", str(e)[:500]
            db.commit()


def recover_orphans() -> None:
    """Jobs left 'running' by a crash or restart go back to the queue."""
    with SessionLocal() as db:
        db.execute(update(Job).where(Job.status == "running").values(status="queued", stage="Re-queued after restart"))
        db.execute(update(Clip).where(Clip.status == "rendering", Clip.output_path != "").values(status="rendered"))
        db.execute(update(Clip).where(Clip.status == "rendering").values(status="draft"))
        db.commit()


def run_forever(stop: threading.Event | None = None, poll: float = 1.0) -> None:
    recover_orphans()
    log.info("worker started")
    while not (stop and stop.is_set()):
        job = claim_job()
        if job is None:
            time.sleep(poll)
            continue
        process(job)


def delete_video_files(video: Video) -> None:
    shutil.rmtree(config.WORK_DIR / str(video.id), ignore_errors=True)
    if video.path and os.path.exists(video.path):
        try:
            os.remove(video.path)
        except OSError:
            pass


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    init_db()
    run_forever()
