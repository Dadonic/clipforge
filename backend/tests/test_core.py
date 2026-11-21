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


# ---------------------------------------------------------------- v2: hooks, end card, campaign rules, learning
def test_hook_is_grounded_and_names_streamer():
    p = semantic.load_profile()
    h = semantic.make_hook(["Okay, driving now.", "No way! He just jumped on the car!"], {"laugh": 0.2}, "reaction", "Tomo", p)
    assert h.startswith("Tomo: ") and "jumped on the car" in h
    assert semantic.make_hook([], {"laugh": 0.9}, "moment", "Tomo", p) == "Tomo can't stop laughing"
    assert semantic.make_hook([], {}, "police", "Tomo", p) == "Tomo vs the cops in GTA 6"
    assert "Streamer" in semantic.make_hook([], {}, "moment", "", p)


def test_end_card_timing_and_wrap():
    ass = captions.build_ass([], 10, 30, "pop", {"cta_text": "Subscribe to @Tomovids on YouTube", "cta_seconds": 4})
    line = [l for l in ass.splitlines() if l.startswith("Dialogue") and ",Cta," in l]
    assert len(line) == 1 and "0:00:16.00,0:00:20.00" in line[0] and "\\N" in line[0] and "@Tomovids" in line[0]
    assert not [l for l in captions.build_ass([], 10, 30, "pop", {}).splitlines() if l.startswith("Dialogue") and ",Cta," in l]
    a = {"start": 0, "end": 9, "caption_params": {"cta_text": "x"}}
    assert render.render_hash(a) != render.render_hash({**a, "caption_params": {}})


def test_minimum_views_rule():
    assert compliance.estimated_earnings(1999, 2.0, 150, min_views=2000) == 0
    assert compliance.estimated_earnings(2000, 2.0, 150, min_views=2000) == 4.0
    assert compliance.estimated_earnings(500_000, 2.0, 150, min_views=2000) == 150


def test_onscreen_text_counts_for_cta_and_credit():
    v = NS(authorized=True, facecam=None)
    camp = _camp(required_cta="@Tomovids", cta_onscreen="Subscribe to @Tomovids on YouTube", streamer="Tomo", require_watermark=False, watermark_text="")
    bare = _clip(title="wild moment", description="", watermark_text="", caption_params={})
    fails = {c["code"] for c in compliance.check_clip(bare, v, camp)["checks"] if c["status"] == "fail"}
    assert {"cta", "cta_card", "attribution"} <= fails
    good = _clip(title="wild moment", description="", watermark_text="",
                 caption_params={"hook_text": "Tomo was NOT ready", "cta_text": "Subscribe to @Tomovids on YouTube"})
    assert compliance.check_clip(good, v, camp)["ok"]


def test_learned_boost_reorders_and_is_bounded():
    words, audio = _synthetic()
    base, _ = highlights.find_highlights(words, audio, [], [], 300.0, {"min_duration": 10, "max_duration": 40, "use_llm": False})
    cat = base[0]["category"]
    up, _ = highlights.find_highlights(words, audio, [], [], 300.0, {"min_duration": 10, "max_duration": 40, "use_llm": False, "category_boost": {cat: 1.15}})
    dn, _ = highlights.find_highlights(words, audio, [], [], 300.0, {"min_duration": 10, "max_duration": 40, "use_llm": False, "category_boost": {cat: 0.85}})
    assert dn[0]["engagement_score"] < base[0]["engagement_score"] <= up[0]["engagement_score"]
    assert up[0]["signals"].get("learned_boost") in (1.15, None) and dn[0]["signals"]["learned_boost"] == 0.85
    assert base[0]["signals"]["hook_text"]


def test_migrations_upgrade_old_database_in_place(tmp_path):
    import sqlite3
    from sqlalchemy import create_engine, inspect, text
    from app import db as dbm
    old = tmp_path / "old.db"
    con = sqlite3.connect(old)
    con.execute("CREATE TABLE campaigns (id INTEGER PRIMARY KEY, user_id INTEGER, name VARCHAR(200))")
    con.execute("CREATE TABLE clips (id INTEGER PRIMARY KEY, title VARCHAR(300))")
    con.execute("CREATE TABLE submissions (id INTEGER PRIMARY KEY, url VARCHAR(600))")
    con.execute("INSERT INTO campaigns (id, user_id, name) VALUES (1, 1, 'kept')")
    con.commit(); con.close()
    eng = create_engine(f"sqlite:///{old}")
    for _ in range(2):          # second pass must be a no-op
        insp = inspect(eng)
        with eng.begin() as c:
            for table, col, ddl in dbm.COLUMN_MIGRATIONS:
                if col not in {x["name"] for x in insp.get_columns(table)}:
                    c.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
    with eng.connect() as c:
        row = c.execute(text("SELECT name, min_views, auto_hook, cta_onscreen FROM campaigns")).fetchone()
    assert tuple(row) == ("kept", 0, 1, "")
    assert {m[0] for m in dbm.COLUMN_MIGRATIONS} <= {"campaigns", "clips", "submissions"}


# ---------------------------------------------------------------- Windows support
def test_font_names_same_with_and_without_fontconfig():
    from app import config
    for f in sorted(config.FONTS_DIR.iterdir()):
        if f.suffix.lower() in (".ttf", ".otf"):
            assert captions._font_names_pillow(f) == captions._font_names(f), f.name
    assert {x["label"] for x in captions.list_fonts()} >= {"Poppins Bold"}


def test_windows_paths_are_escaped_for_ffmpeg():
    assert render._fesc(r"C:\Users\Jack\ClipForgeData\clips\clip_1.ass") == r"C\:/Users/Jack/ClipForgeData/clips/clip_1.ass"
    assert render._fesc("/data/clips/it's.ass") == "/data/clips/it\\'s.ass"


# ---------------------------------------------------------------- off-stream detection (ads, player controls)
import numpy as np  # noqa: E402
from app.services import clipmaker, offstream  # noqa: E402


def _stream_frames(n=120, breaks=((40, 52),)):
    """Synthetic recording: moving 'gameplay' noise with a fixed overlay; during a break the whole frame changes."""
    rng = np.random.default_rng(3)
    g = rng.integers(0, 255, size=(n, offstream.GH, offstream.GW)).astype(np.uint8)
    g[:, 2:10, 70:94] = 240          # counter overlay
    g[:, 34:54, 68:96] = 60          # webcam area
    for a, b in breaks:
        g[a:b] = rng.integers(0, 40, size=(b - a, offstream.GH, offstream.GW))
    return g


def test_offstream_layout_break_found():
    g = _stream_frames()
    segs = offstream.segments(g, np.zeros(len(g), dtype=bool), 1.0, 120.0)
    assert len(segs) == 1 and segs[0]["reason"] == "ad_or_break"
    assert 38 <= segs[0]["s"] <= 40 and 52 <= segs[0]["e"] <= 54


def test_offstream_quiet_without_overlays_or_breaks():
    rng = np.random.default_rng(4)
    noise = rng.integers(0, 255, size=(120, offstream.GH, offstream.GW)).astype(np.uint8)
    assert offstream.segments(noise, np.zeros(120, dtype=bool), 1.0, 120.0) == []        # nothing fixed to anchor on
    assert offstream.segments(_stream_frames(breaks=()), np.zeros(120, dtype=bool), 1.0, 120.0) == []
    # a one-second glitch is not an ad break
    assert offstream.segments(_stream_frames(breaks=((40, 41),)), np.zeros(120, dtype=bool), 1.0, 120.0) == []


def test_offstream_unreliable_read_is_discarded():
    g = _stream_frames(breaks=((10, 45), (60, 100)))
    assert offstream.segments(g, np.zeros(len(g), dtype=bool), 1.0, 120.0) == []


def test_player_bar():
    f = np.full((offstream.SH, offstream.SW, 3), 90, dtype=np.uint8)
    assert not offstream.player_bar(f)
    live = f.copy()
    live[300:302, :, :] = (230, 20, 20)                      # full-width red line: live stream
    assert offstream.player_bar(live)
    vod = f.copy()
    vod[300:302, :120, :] = (230, 20, 20)                    # played part from the left edge
    assert offstream.player_bar(vod)
    car = f.copy()
    car[285:320, 100:400, :] = (230, 20, 20)                 # a big red object is game content
    assert not offstream.player_bar(car)
    mid = f.copy()
    mid[300:302, 200:320, :] = (230, 20, 20)                 # short red run away from the left edge
    assert not offstream.player_bar(mid)
    bars = np.zeros(60, dtype=bool)
    bars[20:26] = True
    segs = offstream.segments(_stream_frames(n=60, breaks=()), bars, 1.0, 60.0)
    assert [s["reason"] for s in segs] == ["player_controls"]


def test_clear_of_trims_or_drops():
    w = {"start": 10.0, "end": 40.0, "peak": 25.0, "starts_clean": True, "ends_clean": True}
    assert highlights.clear_of(w, [], 15) is w
    assert highlights.clear_of(w, [{"s": 50, "e": 60}], 15) is w
    t = highlights.clear_of(w, [{"s": 36, "e": 45}], 15)                # ad after the peak: end early
    assert t["end"] < 36 and t["start"] == 10.0 and not t["ends_clean"] and t["starts_clean"]
    t = highlights.clear_of(w, [{"s": 5, "e": 14}], 15)                 # ad before the peak: start late
    assert t["start"] > 14 and t["end"] == 40.0
    assert highlights.clear_of(w, [{"s": 20, "e": 30}], 15) is None     # the peak itself is the ad
    assert highlights.clear_of(w, [{"s": 12, "e": 22}, {"s": 30, "e": 45}], 15) is None   # nothing long enough left


def test_highlights_never_touch_offstream():
    # a small stream: a loud, wordy moment at 60 s (an "ad") and another at 150 s
    n = 240
    ex = np.zeros(n)
    ex[55:70] = 1.0
    ex[145:160] = 0.9
    audio = {"seconds": n, "excite": ex.tolist(), "onset": (ex * 0.5).tolist(), "laugh": (ex * 0.6).tolist(),
             "scream": [0.0] * n}
    words = []
    for base in (52, 142):
        for i, t in enumerate("oh my god no way that is insane what just happened wow".split()):
            words.append({"s": base + i * 1.2, "e": base + i * 1.2 + 0.5, "w": t})
    scenes, dur = [], float(n)
    base, _ = highlights.find_highlights(words, audio, scenes, [], dur, {"use_llm": False, "min_score": 0})
    assert any(c["start"] < 70 and c["end"] > 55 for c in base)
    off = [{"s": 50.0, "e": 72.0, "reason": "ad_or_break"}]
    got, meta = highlights.find_highlights(words, audio, scenes, [], dur, {"use_llm": False, "min_score": 0, "offstream": off})
    assert got and all(c["end"] <= 50.0 or c["start"] >= 72.0 for c in got)
    assert all("insane" not in c["transcript"] or c["start"] >= 72 for c in got)
    assert meta["offstream_seconds"] == 22.0


def test_hooks_and_titles_differ_across_one_recording():
    n = 400
    ex = np.zeros(n)
    words = []
    for k, base in enumerate((40, 120, 200, 280, 360)):
        ex[base:base + 8] = 1.0
        said = ["look at that truck", "why is he running", "the boat just sank", "who parked this here", "that jump was close"][k]
        for i, t in enumerate((said + " ha ha").split()):
            words.append({"s": base + i * 0.8, "e": base + i * 0.8 + 0.4, "w": t})
    audio = {"seconds": n, "excite": ex.tolist(), "onset": (ex * 0.4).tolist(), "laugh": ex.tolist(), "scream": [0.0] * n}
    got, _ = highlights.find_highlights(words, audio, [], [], float(n), {"use_llm": False, "min_score": 0, "streamer": "Tomo"})
    assert len(got) >= 3
    hooks = [c["signals"]["hook_text"] for c in got[:3]]
    assert len(set(hooks)) == 3 and all("Tomo" in h for h in hooks)
    assert len({c["title"] for c in got[:3]}) >= 2


def test_make_hook_variants():
    prof = semantic.load_profile("gta6")
    a = semantic.make_hook([], {"laugh": 0.9}, "police", "Tomo", prof)
    b = semantic.make_hook([], {"laugh": 0.9}, "police", "Tomo", prof, variant=1)
    assert a != b and "Tomo" in a and "Tomo" in b
    assert semantic.make_hook([], {}, "nonsense", "", prof).startswith("Streamer")


def test_facecam_rect_is_clipped_not_shifted():
    # a webcam in the bottom-right corner: the rect must stay around the face and stop at the picture edge
    r = facecam.face_to_cam_rect({"cx": 0.863, "cy": 0.834, "w": 0.105, "h": 0.23}, 16 / 9)
    assert abs(r["x"] + r["w"] - 1.0) < 1e-6 and abs(r["y"] + r["h"] - 1.0) < 1e-6
    assert 0.68 < r["x"] < 0.74 and 0.55 < r["y"] < 0.62


def test_default_layout_slides_gameplay_off_the_webcam():
    V = NS
    assert clipmaker.default_layout(V(facecam=None, width=1920, height=1080))[0] == "blurfit"
    lay, lp, cap = clipmaker.default_layout(V(facecam={"x": 0.705, "y": 0.583, "w": 0.295, "h": 0.417}, width=1920, height=1080))
    assert lay == "split" and 0.40 < lp["focus_x"] < 0.46 and cap["hook_y"] > 0.34
    p = render.build_panels(lay, lp, 1920, 1080, {"x": 0.705, "y": 0.583, "w": 0.295, "h": 0.417})
    game = p["panels"][1]["src"]
    assert game["x"] + game["w"] <= 0.705 * 1920 + 2          # gameplay crop ends where the webcam starts
    lay, lp, _ = clipmaker.default_layout(V(facecam={"x": 0.0, "y": 0.6, "w": 0.28, "h": 0.4}, width=1920, height=1080))
    game = render.build_panels(lay, lp, 1920, 1080, {"x": 0.0, "y": 0.6, "w": 0.28, "h": 0.4})["panels"][1]["src"]
    assert game["x"] >= 0.28 * 1920 - 2
    # a webcam in the middle top (not at a side edge of the crop) leaves the crop centred
    lay, lp, _ = clipmaker.default_layout(V(facecam={"x": 0.8, "y": 0.0, "w": 0.2, "h": 0.25}, width=1920, height=1080))
    assert "focus_x" not in lp


def test_llm_rescore_waits_out_rate_limits(monkeypatch):
    import httpx
    calls = {"n": 0, "slept": []}

    class Resp:
        def __init__(self, code, body=None, headers=None):
            self.status_code, self._b, self.headers = code, body or {}, headers or {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise httpx.HTTPStatusError("err", request=None, response=None)

        def json(self):
            return self._b

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, headers=None, json=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return Resp(429, headers={"retry-after": "7"})
            ids = [c["id"] for c in __import__("json").loads(json["messages"][1]["content"])["candidates"]]
            return Resp(200, {"usage": {"prompt_tokens": 10, "completion_tokens": 5}, "choices": [{"message": {
                "content": __import__("json").dumps({"items": [{"id": i, "entertaining": 80} for i in ids]})}}]})

    monkeypatch.setattr(httpx, "Client", Client)
    monkeypatch.setattr(semantic, "_sleep", lambda s: calls["slept"].append(s))
    monkeypatch.setattr(semantic.config, "LLM_BATCH", 2)
    cands = [{"id": i, "start": 0, "end": 20, "transcript": "a b c d e", "signals": {}} for i in range(5)]
    out, usage = semantic.llm_rescore(cands, "Tomo")
    assert sorted(out) == [0, 1, 2, 3, 4] and "error" not in usage
    assert usage["calls"] == 3 and calls["n"] == 4 and calls["slept"] == [7.0] and usage["waited_s"] == 7.0


def test_overlay_spec_and_compliance(tmp_path, monkeypatch):
    from PIL import Image
    monkeypatch.setattr(render.config, "OVERLAY_DIR", tmp_path)
    assert render.overlay_spec({}) is None
    assert render.overlay_spec({"overlay": {"file": "missing.png"}}) is None
    Image.new("RGBA", (300, 100), (255, 0, 0, 255)).save(tmp_path / "logo.png")
    s = render.overlay_spec({"overlay": {"file": "../../logo.png", "x": 0.5, "y": 0.03, "w": 0.7}})   # basename only
    assert s and s["w"] == 756 and s["x"] == 162 and s["y"] == 57 and s["path"].endswith("logo.png")
    edge = render.overlay_spec({"overlay": {"file": "logo.png", "x": 1.0, "y": 0.5, "w": 0.5}})
    assert edge["x"] + edge["w"] <= 1080
    # a logo changes the render hash, so adding or moving it forces a new render
    base = dict(start=0, end=20, layout="blurfit", layout_params={}, facecam=None, words=[], caption_style="pop",
                caption_params={}, watermark_text="", watermark_pos="top", audio_gain_db=0, normalize_audio=True, quality="standard")
    assert render.render_hash(base) != render.render_hash({**base, "layout_params": {"overlay": {"file": "logo.png"}}})


# ---------------------------------------------------------------- OBS recorder and voice separation
import json  # noqa: E402
from app.services import recorder, voice  # noqa: E402


def test_recorder_urls():
    assert recorder.youtube_id("https://www.youtube.com/watch?v=cq7OxWhmgHA&t=10") == "cq7OxWhmgHA"
    assert recorder.youtube_id("https://youtu.be/cq7OxWhmgHA?si=x") == "cq7OxWhmgHA"
    assert recorder.youtube_id("https://www.youtube.com/live/cq7OxWhmgHA") == "cq7OxWhmgHA"
    assert recorder.youtube_id("https://www.twitch.tv/kreekcraft") is None
    assert recorder.start_seconds("https://youtu.be/x?t=1h2m3s") == 3723
    assert recorder.start_seconds("https://youtu.be/x?t=95") == 95
    u = recorder.player_url("https://www.youtube.com/watch?v=abcdefghijk&t=90")
    assert u.startswith("https://www.youtube.com/embed/abcdefghijk?autoplay=1") and "controls=0" in u and "start=90" in u
    assert recorder.player_url("https://example.com/stream") == "https://example.com/stream"


def test_recorder_writes_obs_config(tmp_path, monkeypatch):
    import configparser
    exe = tmp_path / "obs" / "bin" / "64bit" / "obs64.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setattr(recorder, "OBS_DIR", tmp_path / "obs")
    monkeypatch.setattr(recorder, "OBS_EXE", exe)
    monkeypatch.setattr(recorder, "OBS_CFG", tmp_path / "obs" / "config" / "obs-studio")
    monkeypatch.setattr(recorder, "REC_DIR", tmp_path / "rec")
    monkeypatch.setattr(recorder, "SECRET_FILE", tmp_path / "secret")
    recorder.ensure_obs_config()
    cfg = tmp_path / "obs" / "config" / "obs-studio"
    assert (tmp_path / "obs" / "portable_mode.txt").exists()
    prof = configparser.ConfigParser()
    prof.read(cfg / "basic/profiles/ClipForge/basic.ini")
    assert prof["SimpleOutput"]["FilePath"] == str(tmp_path / "rec") and prof["Video"]["OutputCX"] == "1920"
    col = json.loads((cfg / "basic/scenes/ClipForge.json").read_text())
    names = {s["name"]: s for s in col["sources"]}
    assert {recorder.SCENE_BROWSER, recorder.SCENE_EDGE, recorder.BROWSER_SOURCE} <= set(names)
    uuids = {s["uuid"] for s in col["sources"]}
    for sc in (recorder.SCENE_BROWSER, recorder.SCENE_EDGE):
        assert all(it["source_uuid"] in uuids for it in names[sc]["settings"]["items"])
    ws = json.loads((cfg / "plugin_config/obs-websocket/config.json").read_text())
    assert ws["server_enabled"] and ws["auth_required"] and ws["server_password"] == (tmp_path / "secret").read_text()
    before = (cfg / "basic/scenes/ClipForge.json").read_text()
    recorder.ensure_obs_config()            # idempotent: an existing setup is left alone
    assert (cfg / "basic/scenes/ClipForge.json").read_text() == before


def test_voice_stft_round_trip():
    rng = np.random.default_rng(1)
    x = (rng.standard_normal((2, 100_000)) * 0.1).astype(np.float32)
    y = voice._istft(voice._stft(x), x.shape[1])
    assert float(np.abs(x - y).max()) < 1e-4


def test_voice_separate_overlap_add_is_seamless():
    class Echo:                       # a fake model that returns its input: output must equal the mix
        def run(self, _, feeds):
            return [feeds["input"]]
    sr = voice.SR
    t = np.arange(int(sr * 14)) / sr
    mix = np.stack([np.sin(2 * np.pi * 220 * t), np.sin(2 * np.pi * 330 * t)]).astype(np.float32) * 0.3
    out = voice.separate(mix, Echo()) / voice.COMPENSATE
    # the model only sees the lowest DIM_F bins (up to ~17.6 kHz), so low tones must come back intact
    assert float(np.abs(out - mix)[:, sr:-sr].max()) < 2e-3
