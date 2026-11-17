"""Unit tests for the pure logic. Run:  cd backend && python -m pytest -q
The full pipeline on a real file is covered by scripts/e2e.py (needs a running server)."""
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS

import pytest

os.environ.setdefault("CLIPFORGE_DATA_DIR", tempfile.mkdtemp(prefix="cf_test_"))
os.environ["EMBEDDED_WORKER"] = "false"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.services import audio_analysis, captions, chat, compliance, facecam, highlights, media, render, semantic, transcription  # noqa: E402


def W(*triples):
    return [{"s": s, "e": e, "w": w} for s, e, w in triples]


# ---------------------------------------------------------------- earnings
def test_earnings_formula():
    assert compliance.estimated_earnings(184_000, 1.5) == 276.0
    assert compliance.estimated_earnings(0, 5) == 0
    assert compliance.estimated_earnings(999, 1.0) == 1.0          # rounds to cents
    assert compliance.estimated_earnings(1_000_000, 2.0, cap=500) == 500
    assert compliance.estimated_earnings(-5, 2.0) == 0


# ---------------------------------------------------------------- chat
def test_chat_formats():
    assert chat.parse("[0:01:05] bob: LUL\n1:02:03 amy: hi\n90 x: y") == [(65.0, "bob: LUL"), (3723.0, "amy: hi"), (90.0, "x: y")]
    assert chat.parse('{"comments":[{"content_offset_seconds":12.5,"message":{"body":"KEKW"}}]}') == [(12.5, "KEKW")]
    assert chat.parse("time,user,message\n0:10,a,hello\n75,b,clip it") == [(10.0, "hello"), (75.0, "clip it")]
    assert chat.parse("no timestamps here") == []


def test_chat_spike():
    msgs = [(t, "hi") for t in range(0, 300, 10)] + [(150 + i * 0.2, "LUL") for i in range(40)]
    sig = chat.signal(msgs, 300, ["lul"])
    assert max(range(300), key=lambda i: sig[i]) in range(148, 160)
    assert sig[20] < 0.2


# ---------------------------------------------------------------- transcription helpers
def test_clean_words_and_spread():
    out = transcription.clean_words(W((0, 1, "(static)"), (1, 2, "Hello"), (2, 3, "[MUSIC]"), (3, 4, "Hehehehehehe"), (4, 5, "")))
    assert [w["w"] for w in out] == ["Hello", "Hahaha!"]
    import numpy as np
    audio = np.concatenate([np.zeros(8000), np.random.default_rng(0).normal(0, .3, 16000), np.zeros(8000)]).astype("float32")
    ws = transcription.spread_words("one two three", audio, 10.0)
    assert [w["w"] for w in ws] == ["one", "two", "three"]
    assert all(a["e"] <= b["s"] + 1e-6 for a, b in zip(ws, ws[1:]))
    assert 10.4 < ws[0]["s"] < 10.7 and ws[-1]["e"] < 11.7       # words land on the voiced part, not the silence


# ---------------------------------------------------------------- highlights
def test_sentences_and_dedupe():
    s = highlights.build_sentences(W((0, .4, "No"), (.4, .9, "way!"), (1, 1.5, "Look"), (1.5, 2, "at"), (4, 4.5, "that")))
    assert [x["text"] for x in s] == ["No way!", "Look at", "that"]
    a = {"start": 10, "end": 30, "engagement_score": 80, "transcript": "the car flew over the bridge wow"}
    b = {"start": 14, "end": 34, "engagement_score": 70, "transcript": "something else entirely different here"}
    c = {"start": 100, "end": 120, "engagement_score": 60, "transcript": "the car flew over the bridge wow"}
    d = {"start": 200, "end": 220, "engagement_score": 50, "transcript": "a brand new unrelated set of words"}
    assert highlights.dedupe([a, b, c, d]) == [a, d]


def _synthetic():
    n = 300
    audio = {"seconds": n, "excite": [0.05] * n, "onset": [0.0] * n, "laugh": [0.0] * n, "scream": [0.0] * n, "silence": [0.5] * n}
    for t in range(100, 108):                    # real moment: loud + laughter + reaction words
        audio["excite"][t] = 0.8
        audio["laugh"][t] = 0.6
    for t in range(200, 212):                    # loud only: nothing else going on
        audio["excite"][t] = 0.9
    words = W((92, 93, "Okay"), (93, 94, "watch"), (94, 95, "this."), (100, 101, "No"), (101, 102, "way!"),
              (102, 103, "Did"), (103, 104, "you"), (104, 105, "see"), (105, 106, "that?"))
    return words, audio


def test_real_moment_outranks_loud_noise():
    words, audio = _synthetic()
    hs, meta = highlights.find_highlights(words, audio, [], [], 300.0, {"min_duration": 10, "max_duration": 40, "use_llm": False})
    assert hs and hs[0]["start"] <= 100 <= hs[0]["end"]
    assert hs[0]["start"] <= 93 and hs[0]["end"] >= 106, "window must include the setup sentence and the payoff"
    assert all(10 <= h["end"] - h["start"] <= 40 for h in hs)
    loud = [h for h in hs if h["start"] <= 205 <= h["end"]]
    assert not loud or loud[0]["engagement_score"] < hs[0]["engagement_score"] * 0.75
    assert meta["semantic"] == "heuristic"


def test_no_signal_no_highlights():
    hs, _ = highlights.find_highlights([], {"seconds": 120, "excite": [0.0] * 120}, [], [], 120.0, {"use_llm": False})
    assert hs == []


def test_flags():
    p = semantic.load_profile()
    f = semantic.content_flags(W((10, 11, "that"), (11, 12, "headshot"), (12, 13, "was"), (13, 14, "crazy")), 9.0, {}, p)
    assert [x["code"] for x in f] == ["graphic_violence"] and "first 7s" in f[0]["detail"]
    f = semantic.content_flags(W((0, 1, "this"), (1, 2, "is"), (2, 3, "the"), (3, 4, "final"), (4, 5, "mission")), 0, {}, p)
    assert f[0]["code"] == "possible_spoiler" and f[0]["severity"] == "review"
    assert semantic.content_flags(W((0, 1, "what"), (1, 2, "time"), (2, 3, "am"), (3, 4, "I"), (4, 5, "ending"), (5, 6, "today")), 0, {}, p) == []


# ---------------------------------------------------------------- captions
def test_cues_sync_and_ass():
    words = W((10.0, 10.3, "No"), (10.3, 10.8, "way,"), (10.8, 11.2, "he"), (11.2, 11.6, "jumped"), (13.0, 13.5, "Bro"), (99, 100, "outside"))
    p = captions.resolve("pop", {})
    cues = captions.build_cues(words, 10.0, 15.0, p)
    assert [[w["w"] for w in c["words"]] for c in cues] == [["NO", "WAY,", "HE"], ["JUMPED"], ["BRO"]]
    assert cues[0]["s"] == 0.0 and cues[0]["words"][1]["s"] == pytest.approx(0.3)
    assert all(c["e"] <= 5.0 for c in cues) and all(a["e"] <= b["s"] + 1e-6 for a, b in zip(cues, cues[1:]))
    ass = captions.build_ass(words, 10.0, 15.0, "pop", {}, watermark="@me {x}")
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass and "@me (x)" in ass
    assert ass.count("Dialogue:") == 5 + 1          # one event per active word + watermark
    assert "Dialogue" not in captions.build_ass(words, 10.0, 15.0, "pop", {"enabled": False})
    for style in captions.STYLES:
        assert captions.build_ass(words, 10.0, 15.0, style, {}).count("Dialogue:") >= 2


# ---------------------------------------------------------------- layouts
@pytest.mark.parametrize("layout", list(render.LAYOUTS))
@pytest.mark.parametrize("size", [(1920, 1080), (1280, 720), (2560, 1440), (1080, 1920), (1366, 768)])
def test_panels_fill_canvas_and_stay_in_frame(layout, size):
    sw, sh = size
    lay = render.build_panels(layout, {"zoom": 1.4, "focus_x": 0.9, "top": {"x": .5, "y": 0, "w": .5, "h": .5}}, sw, sh, {"x": 0.7, "y": 0.6, "w": 0.3, "h": 0.4})
    for pn in lay["panels"]:
        s, d = pn["src"], pn["dst"]
        assert s["x"] >= 0 and s["y"] >= 0 and s["x"] + s["w"] <= sw and s["y"] + s["h"] <= sh
        assert s["w"] % 2 == 0 and s["h"] % 2 == 0 and d["w"] % 2 == 0 and d["h"] % 2 == 0
        assert 0 <= d["y"] and d["y"] + d["h"] <= 1920 and d["w"] == 1080
        assert abs(s["w"] / s["h"] - d["w"] / d["h"]) < 0.02, "no stretching"
    if not lay["blur_bg"]:
        assert sum(p["dst"]["h"] for p in lay["panels"]) == 1920


def test_render_hash_ignores_metadata():
    base = {"start": 1, "end": 20, "layout": "split", "layout_params": {}, "caption_style": "pop", "caption_params": {}, "words": [],
            "audio_gain_db": 0, "normalize_audio": True, "watermark_text": "", "watermark_pos": "top", "quality": "standard", "facecam": None}
    h = render.render_hash(base)
    assert render.render_hash({**base, "title": "x", "description": "y", "hashtags": ["#a"]}) == h
    assert render.render_hash({**base, "end": 21}) != h and render.render_hash({**base, "watermark_text": "w"}) != h


# ---------------------------------------------------------------- facecam
def test_facecam_cluster():
    stable = [[(0.12, 0.2, 0.08, 0.14)] for _ in range(10)]
    noisy = [[((i * 37 % 10) / 10 + 0.05, (i * 53 % 10) / 10 + 0.05, 0.1, 0.15)] for i in range(10)]   # cutscene faces: all over the frame
    f = facecam.cluster_faces(stable)
    assert f and abs(f["cx"] - 0.12) < 0.01
    assert facecam.cluster_faces(noisy) is None and facecam.cluster_faces([[] for _ in range(10)]) is None
    r = facecam.face_to_cam_rect(f, 16 / 9)
    assert 0 <= r["x"] and r["x"] + r["w"] <= 1 and 0 <= r["y"] and r["y"] + r["h"] <= 1 and r["w"] > f["w"]


# ---------------------------------------------------------------- compliance
def _clip(**kw):
    base = dict(start=0, end=30, layout="blurfit", layout_params={}, caption_style="pop", caption_params={}, words=[], audio_gain_db=0,
                normalize_audio=True, watermark_text="@S", watermark_pos="top", quality="standard", title="S goes wild", description="Watch live",
                hashtags=["#gta6"], flags=[], flags_cleared=False, output_path="/x.mp4",
                out_meta={"width": 1080, "height": 1920, "vcodec": "h264", "acodec": "aac"})
    base.update(kw)
    c = NS(**base)
    c.rendered_hash = kw.get("rendered_hash") or render.render_hash(compliance.clip_render_dict(c, NS(facecam=None)))
    return c


def _camp(**kw):
    base = dict(min_duration=15, max_duration=60, required_hashtags=["#GTA6"], required_cta="watch live", require_watermark=True, watermark_text="@s",
                require_attribution=True, streamer="S", prohibited=["casino"], status="active", deadline=None, max_budget=None, name="C")
    base.update(kw)
    return NS(**base)


def test_compliance_pass_and_each_failure():
    v = NS(authorized=True, facecam=None)
    assert compliance.check_clip(_clip(), v, _camp())["ok"]
    fails = lambda rep: {c["code"] for c in rep["checks"] if c["status"] == "fail"}
    assert fails(compliance.check_clip(_clip(end=10), v, _camp())) == {"duration"}
    assert fails(compliance.check_clip(_clip(hashtags=[]), v, _camp())) == {"hashtags"}
    assert fails(compliance.check_clip(_clip(description=""), v, _camp())) == {"cta"}
    assert fails(compliance.check_clip(_clip(watermark_text=""), v, _camp(streamer=""))) == {"watermark"}
    assert fails(compliance.check_clip(_clip(description="Watch live casino"), v, _camp())) == {"prohibited"}
    assert fails(compliance.check_clip(_clip(rendered_hash="stale"), v, _camp())) == {"rendered"}
    assert fails(compliance.check_clip(_clip(out_meta={"width": 1920, "height": 1080, "vcodec": "h264", "acodec": "aac"}), v, _camp())) == {"aspect"}
    assert fails(compliance.check_clip(_clip(), NS(authorized=False, facecam=None), _camp())) == {"authorized"}
    assert fails(compliance.check_clip(_clip(), v, _camp(status="ended"))) == {"status"}
    flagged = _clip(flags=[{"code": "possible_spoiler", "severity": "review", "detail": ""}])
    assert fails(compliance.check_clip(flagged, v, _camp())) == {"flags"}
    flagged.flags_cleared = True
    assert compliance.check_clip(flagged, v, _camp())["ok"]
    rep = compliance.check_clip(_clip(), v, None)
    assert rep["ok"] and rep["warnings"] == 1


# ---------------------------------------------------------------- media + render on a generated file
@pytest.fixture(scope="module")
def sample(tmp_path_factory):
    p = str(tmp_path_factory.mktemp("m") / "s.mkv")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=30:d=6", "-f", "lavfi", "-i",
                    "sine=f=440:d=6", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", p], check=True)
    return p


def test_probe_and_bad_file(sample, tmp_path):
    m = media.probe(sample)
    assert (m["width"], m["height"]) == (1280, 720) and abs(m["duration"] - 6) < 0.2 and m["vcodec"] == "h264" and m["has_audio"]
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(media.MediaError):
        media.probe(str(bad))


def test_trim_and_vertical_render(sample, tmp_path):
    out = str(tmp_path / "o.mp4")
    meta = render.render_clip(sample, out, start=1.0, end=4.5, src_w=1280, src_h=720, src_fps=30, layout="stacked", layout_params={}, facecam=None,
                              words=W((1.2, 1.6, "Hello"), (1.6, 2.2, "world")), caption_style="pop", caption_params={}, watermark="@t", quality="draft")
    assert (meta["width"], meta["height"]) == (1080, 1920) and meta["vcodec"] == "h264" and meta["acodec"] == "aac"
    assert abs(meta["duration"] - 3.5) < 0.15
    a = str(tmp_path / "a.wav")
    media.extract_audio(sample, a, 6)
    an = audio_analysis.analyze(a)
    assert an["seconds"] == 6 and len(an["excite"]) == 6


def test_job_failure_is_recorded_not_swallowed(tmp_path):
    from app import worker
    from app.db import Job, SessionLocal, User, Video, init_db
    init_db()
    bad = tmp_path / "gone.mp4"
    with SessionLocal() as db:
        u = User(email=f"t{os.getpid()}@x.test", password_hash="x")
        db.add(u); db.flush()
        v = Video(user_id=u.id, title="t", path=str(bad), duration=10, width=1280, height=720, status="uploaded", acodec="aac")
        db.add(v); db.flush()
        db.add(Job(user_id=u.id, type="analyze", video_id=v.id, payload={}, max_attempts=2))
        db.commit()
        vid = v.id
    job = worker.claim_job()
    assert job and job.status == "running"
    worker.process(job)
    with SessionLocal() as db:
        j = db.get(Job, job.id)
        assert j.status == "failed" and "missing" in j.error       # permanent error: not retried
        assert db.get(Video, vid).status == "failed"
    assert worker.claim_job() is None


def test_hook_text_card():
    ass = captions.build_ass([], 0, 20, "pop", {"hook_text": "He did NOT expect the NPC to do this", "hook_seconds": 4})
    line = [l for l in ass.splitlines() if ",Hook," in l and l.startswith("Dialogue")]
    assert len(line) == 1 and "0:00:04.00" in line[0] and "\\N" in line[0]
    assert ",Hook," not in captions.build_ass([], 0, 20, "pop", {}).split("[Events]")[1]
    a = {"start": 0, "end": 9, "caption_params": {"hook_text": "x"}}
    assert render.render_hash(a) != render.render_hash({**a, "caption_params": {}})
