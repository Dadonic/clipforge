#!/usr/bin/env python3
"""Repeatable end-to-end test against a running ClipForge server.

    python scripts/e2e.py --base http://127.0.0.1:8000 --video path/to/stream.mp4 [--truth stream.truth.json] [--chat chat.txt]

Walks the whole product path through the public API: sign up -> campaign -> chunked upload -> analysis ->
ranked highlights -> clip -> render -> verify the MP4 with ffprobe -> compliance (fail, fix, pass) -> approve ->
record submission -> earnings maths -> CSV import. Exits non-zero on the first failed expectation.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

import httpx

OK = 0


def check(cond, label, extra=""):
    global OK
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  [{extra}]" if extra else ""))
    if not cond:
        sys.exit(1)
    OK += 1


def wait(fn, timeout, what):
    t0 = time.time()
    while time.time() - t0 < timeout:
        r = fn()
        if r:
            return r
        time.sleep(1.5)
    print(f"  FAIL  timed out waiting for {what}")
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--video", required=True)
    ap.add_argument("--truth")
    ap.add_argument("--chat")
    ap.add_argument("--keep", help="copy the exported clip here")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.base + "/api", timeout=120)

    def j(r, code=200):
        if r.status_code != code:
            print(f"  FAIL  {r.request.method} {r.request.url} -> {r.status_code} {r.text[:300]}")
            sys.exit(1)
        return r.json()

    print("Auth")
    email = f"e2e-{uuid.uuid4().hex[:8]}@example.com"
    check(c.get("/videos").status_code == 401, "API rejects anonymous requests")
    j(c.post("/auth/signup", json={"email": email, "password": "correct horse", "name": "E2E"}))
    check(c.post("/auth/login", json={"email": email, "password": "wrong"}).status_code == 401, "wrong password rejected")
    check(j(c.get("/auth/me"))["email"] == email, "session cookie works")
    dash = j(c.get("/dashboard"))
    check(dash["clips_generated"] == 0 and dash["earnings"]["estimated_total"] == 0, "fresh dashboard is all zeros (no demo data)")

    print("Campaign")
    camp = j(c.post("/campaigns", json={"name": "E2E GTA 6 campaign", "streamer": "TestStreamer", "payout_per_1k": 1.5,
                                        "max_budget": 500, "min_duration": 10, "max_duration": 45,
                                        "required_hashtags": ["#gta6", "#teststreamer"], "required_cta": "Watch live on kick",
                                        "approved_platforms": ["tiktok", "youtube_shorts"], "prohibited": ["giveaway"],
                                        "require_watermark": True, "watermark_text": "@TestStreamer",
                                        "min_views": 2000, "min_tier1_pct": 40, "submit_within_min": 30, "keep_live_days": 30,
                                        "cta_onscreen": "Subscribe to @TestStreamer on YouTube"}))
    check(camp["min_views"] == 2000 and camp["auto_hook"] is True, "campaign stores min-views, audience and end-card rules")

    print("Upload")
    size = os.path.getsize(a.video)
    check(c.post("/videos", json={"filename": "x.mp4", "size": size, "authorized": False}).status_code == 400,
          "upload refused without the authorisation tick")
    check(c.post("/videos", json={"filename": "x.exe", "size": size, "authorized": True}).status_code == 400, "non-video extension refused")
    v = j(c.post("/videos", json={"filename": os.path.basename(a.video), "size": size, "authorized": True,
                                  "campaign_id": camp["id"], "title": "E2E stream"}))
    with open(a.video, "rb") as f:
        off = 0
        while off < size:
            blob = f.read(v["chunk_size"])
            j(c.put(f"/videos/{v['id']}/chunk", params={"offset": off}, content=blob))
            off += len(blob)
    vid = v["id"]
    if a.chat:
        r = j(c.post(f"/videos/{vid}/chat", files={"file": ("chat.txt", open(a.chat, "rb"))}))
        check(r["messages"] > 0, "chat log parsed", f"{r['messages']} messages")
    t0 = time.time()
    v = j(c.post(f"/videos/{vid}/complete", json={"analyze": True, "auto_clips": 2}))
    real = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", a.video],
                                     capture_output=True, text=True).stdout)["format"]
    check(v["status"] == "analyzing" and abs(v["duration"] - float(real["duration"])) < 0.5, "metadata extracted matches ffprobe",
          f"{v['duration']:.1f}s {v['width']}x{v['height']} {v['vcodec']}/{v['acodec']}")

    print("Analysis (started automatically by the upload)")
    check(c.post(f"/videos/{vid}/analyze", json={}).status_code == 409, "duplicate analysis request refused")
    v = wait(lambda: (lambda x: x if x["status"] in ("ready", "failed") else None)(j(c.get(f"/videos/{vid}"))), 1800, "analysis")
    check(v["status"] == "ready", "analysis finished", f"{time.time() - t0:.0f}s, speech: {v['transcribe_provider']}, ranking: {v['semantic_provider']}")
    hs = j(c.get(f"/videos/{vid}/highlights"))
    check(len(hs) > 0, "highlights returned", f"{len(hs)}")
    need = ["start", "end", "title", "description", "reason", "viral_potential", "engagement_score", "suggested_duration", "transcript", "confidence"]
    check(all(all(k in h and h[k] is not None for k in need) for h in hs), "every highlight has all required fields")
    check(all(10 - 0.1 <= h["end"] - h["start"] <= 45 + 0.1 for h in hs), "all durations inside the configured 10-45s range")
    check([h["rank"] for h in hs] == sorted(h["rank"] for h in hs) and
          [h["engagement_score"] for h in hs] == sorted((h["engagement_score"] for h in hs), reverse=True), "ranked by score")
    ov = lambda x, y: max(0, min(x["end"], y["end"]) - max(x["start"], y["start"]))
    check(all(ov(x, y) < 0.3 * min(x["end"] - x["start"], y["end"] - y["start"]) for i, x in enumerate(hs) for y in hs[i + 1:]), "no duplicate / overlapping highlights")
    tr = j(c.get(f"/videos/{vid}/transcript"))
    check(len(tr["words"]) > 0 and all(w["e"] >= w["s"] for w in tr["words"]) and
          all(tr["words"][i]["s"] <= tr["words"][i + 1]["s"] + 1e-6 for i in range(len(tr["words"]) - 1)), "word timestamps are monotonic", f"{len(tr['words'])} words")
    if a.truth:
        truth = json.load(open(a.truth))
        top = hs[:len(truth["moments"])]
        hit = [any(ov({"start": s, "end": e}, h) > 0.5 * (e - s) for h in top) for s, e in truth["moments"]]
        check(all(hit), f"top {len(top)} highlights are exactly the {len(truth['moments'])} scripted moments", str(hit))
        d0, d1 = truth["distractor"]
        check(not any(ov({"start": d0, "end": d1}, h) > 0.5 * (d1 - d0) for h in top), "loud-but-empty distractor did not make the top ranks")
        # timestamp accuracy: first word of each scripted moment should be found near its scripted time
        errs = []
        for line in truth["script"]:
            if line["kind"] != "moment":
                continue
            first = line["text"].split()[0].strip(",.!?").lower()
            cand = [w["s"] for w in tr["words"] if w["w"].strip(",.!?").lower() == first and abs(w["s"] - line["t"]) < 3]
            if cand:
                errs.append(min(abs(x - line["t"]) for x in cand))
        check(errs and max(errs) < 1.0, "caption word timing within 1s of the script", f"max error {max(errs):.2f}s over {len(errs)} lines" if errs else "no matches")
        for s, e in truth["moments"]:
            h = next(h for h in top if ov({"start": s, "end": e}, h) > 0.5 * (e - s))
            check(h["start"] <= s + 1.5 and h["end"] >= e - 3.0, f"clip {h['start']:.0f}-{h['end']:.0f}s covers setup and payoff of moment {s}-{e}s")
    print("Automatic clips")
    autos = j(c.get("/clips", params={"video_id": vid}))
    check(len(autos) == 2, "the top 2 highlights were turned into clips without a tap")
    full = [j(c.get(f"/clips/{x['id']}")) for x in autos]
    check(all(x["caption_resolved"]["hook_text"] and "TestStreamer" in x["caption_resolved"]["hook_text"] for x in full),
          "each got an opening hook that names the streamer", full[0]["caption_resolved"]["hook_text"])
    check(all(x["caption_resolved"]["cta_text"] == "Subscribe to @TestStreamer on YouTube" for x in full), "each got the campaign end card")
    check(all(x["campaign_id"] == camp["id"] and x["category"] for x in full), "each carries the campaign and a moment type")
    wait(lambda: all(j(c.get(f"/clips/{x['id']}"))["status"] == "rendered" for x in autos), 900, "auto renders")
    rep0 = j(c.post(f"/clips/{autos[0]['id']}/check"))
    check(rep0["ok"], "an automatic clip passes the campaign's rules untouched (hashtags, CTA, end card, credit, watermark)")
    r = j(c.post(f"/videos/{vid}/clips/top", json={"count": 1}))
    check(r["created"] == 1, "one-tap 'make top N' creates and renders the next best")
    wait(lambda: j(c.get(f"/clips/{r['ids'][0]}"))["status"] == "rendered", 900, "top-N render")
    j(c.delete(f"/clips/{r['ids'][0]}"))
    t1 = time.time()
    j(c.post(f"/videos/{vid}/analyze", json={"min_duration": 15, "max_duration": 30, "use_llm": False}))
    wait(lambda: j(c.get(f"/videos/{vid}"))["status"] == "ready", 300, "re-analysis")
    check(time.time() - t1 < 60, "re-analysis reuses cached transcript/audio (fast)", f"{time.time() - t1:.0f}s")
    hs = j(c.get(f"/videos/{vid}/highlights"))
    check(all(15 - 0.1 <= h["end"] - h["start"] <= 30 + 0.1 for h in hs if h["status"] == "suggested"), "new duration bounds respected after re-run")
    check(sum(h["status"] == "clipped" for h in hs) == 2, "highlights already turned into clips survive a re-run")

    print("Clip + render")
    clip = j(c.post(f"/highlights/{next(h for h in hs if h['status'] == 'suggested')['id']}/clip", json={"render": True}))
    cid = clip["id"]
    clip = wait(lambda: (lambda x: x if x["status"] == "rendered" or (x["job"] and x["job"]["status"] == "failed") else None)(j(c.get(f"/clips/{cid}"))), 900, "render")
    check(clip["status"] == "rendered" and not clip["needs_render"], "clip rendered")
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        with c.stream("GET", f"/clips/{cid}/file", params={"download": 1}) as r:
            for chunk in r.iter_bytes():
                tf.write(chunk)
        out = tf.name
    pr = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", out],
                                   capture_output=True, text=True).stdout)
    vs = next(s for s in pr["streams"] if s["codec_type"] == "video")
    au = next(s for s in pr["streams"] if s["codec_type"] == "audio")
    check((vs["width"], vs["height"]) == (1080, 1920), "exported file is 1080x1920 (9:16)")
    check(vs["codec_name"] == "h264" and vs["pix_fmt"] == "yuv420p" and au["codec_name"] == "aac", "H.264 yuv420p + AAC")
    check(abs(float(pr["format"]["duration"]) - (clip["end"] - clip["start"])) < 0.25, "exported duration matches the trim",
          f"{float(pr['format']['duration']):.2f}s vs {clip['end'] - clip['start']:.2f}s")
    dec = subprocess.run(["ffmpeg", "-v", "error", "-i", out, "-f", "null", "-"], capture_output=True, text=True)
    check(dec.returncode == 0 and not dec.stderr.strip(), "exported file decodes cleanly end to end")
    check(os.path.getsize(out) < 60 * 1024 * 1024, "file size is platform-friendly", f"{os.path.getsize(out) / 1e6:.1f} MB")
    check(len(clip["cues"]) > 0 and clip["cues"][0]["s"] >= 0 and clip["cues"][-1]["e"] <= clip["end"] - clip["start"] + 0.01, "caption cues sit inside the clip", f"{len(clip['cues'])} cues")
    if a.keep:
        import shutil
        shutil.copy(out, a.keep)

    print("Editing")
    h0 = clip["rendered_hash"]
    c2 = j(c.patch(f"/clips/{cid}", json={"title": "New title only"}))
    check(not c2["needs_render"] and c2["rendered_hash"] == h0, "changing the title does NOT require a re-render")
    c2 = j(c.patch(f"/clips/{cid}", json={"caption_style": "karaoke", "layout": "fullscreen"}))
    check(c2["needs_render"], "changing layout/captions DOES require a re-render")
    check(c.patch(f"/clips/{cid}", json={"end": c2["start"] + 0.2}).status_code == 400, "sub-1s trim refused")
    j(c.post(f"/clips/{cid}/render"))
    clip = wait(lambda: (lambda x: x if x["status"] == "rendered" and not x["needs_render"] else None)(j(c.get(f"/clips/{cid}"))), 900, "re-render")
    check(clip["rendered_hash"] != h0, "re-render produced a new output")
    sg = j(c.get(f"/clips/{cid}/suggest"))
    check(len(sg["titles"]) >= 1 and sg["hashtags"], "editing assistant returns titles and hashtags")

    print("Compliance")
    rep = j(c.post(f"/clips/{cid}/check"))
    check(rep["ok"], "a clip made from a campaign recording starts compliant (hashtags, CTA, watermark, credit pre-filled)")
    j(c.patch(f"/clips/{cid}", json={"hashtags": ["#gaming"], "description": "", "title": "untitled", "watermark_text": "", "caption_params": {}}))
    r = c.post(f"/clips/{cid}/approve")
    check(r.status_code == 409, "approval blocked while campaign rules fail")
    fails = {k["code"] for k in r.json()["detail"]["report"]["checks"] if k["status"] == "fail"}
    check({"hashtags", "cta", "cta_card", "watermark", "attribution", "rendered"} <= fails, "each failing rule is named", ",".join(sorted(fails)))
    j(c.patch(f"/clips/{cid}", json={"title": "TestStreamer loses it", "description": "Watch live on kick giveaway",
                                     "hashtags": ["#gta6", "#teststreamer"], "watermark_text": "@TestStreamer",
                                     "caption_params": {"cta_text": "Subscribe to @TestStreamer on YouTube"}}))
    j(c.post(f"/clips/{cid}/render"))
    wait(lambda: (lambda x: x["status"] == "rendered" and not x["needs_render"])(j(c.get(f"/clips/{cid}"))), 900, "render")
    rep = j(c.post(f"/clips/{cid}/check"))
    check([k["code"] for k in rep["checks"] if k["status"] == "fail"] == ["prohibited"], "prohibited term caught", "giveaway")
    j(c.patch(f"/clips/{cid}", json={"description": "Watch live on kick"}))
    clip = j(c.post(f"/clips/{cid}/approve"))
    check(clip["status"] == "approved" and clip["compliance"]["ok"], "clip approved once every rule passes")

    print("Posting kit")
    kit = j(c.get(f"/clips/{cid}/posting"))
    check([p["platform"] for p in kit["platforms"]] == ["tiktok", "youtube_shorts"] and kit["approved"], "posting kit lists only the campaign's platforms")
    yt = next(p for p in kit["platforms"] if p["platform"] == "youtube_shorts")
    check(len(yt["fields"][0]["text"]) <= 100 and "#gta6" in yt["fields"][0]["text"], "YouTube title fits 100 characters with hashtags")
    tk = next(p for p in kit["platforms"] if p["platform"] == "tiktok")["fields"][0]["text"]
    check("Watch live on kick" in tk and "#teststreamer" in tk and "TestStreamer loses it" in tk, "TikTok caption carries title, CTA and hashtags")
    check(any("30 minutes" in r for r in kit["rules"]) and any("2,000" in r for r in kit["rules"]), "after-posting rules are spelled out")

    print("Submission + earnings")
    check(c.post("/submissions", json={"clip_id": cid, "platform": "instagram_reels", "url": "https://x.test/1"}).status_code == 409,
          "submission on a non-approved platform refused")
    check(c.post("/submissions", json={"clip_id": cid, "platform": "tiktok", "url": ""}).status_code == 400, "submission without a link refused")
    url = "https://www.tiktok.com/@e2e/video/" + uuid.uuid4().hex[:10]
    s = j(c.post("/submissions", json={"clip_id": cid, "platform": "tiktok", "url": url, "views": 1000, "approved_views": 0}))
    check(c.post("/submissions", json={"clip_id": cid, "platform": "tiktok", "url": url + "/"}).status_code == 409, "the same link cannot be recorded twice")
    check(s["blocked"] and "2,000" in s["blocked"], "under the campaign's minimum views is called out", s["blocked"])
    check(s["estimated_earnings"] == 0, "no approved views -> $0 estimated (views alone earn nothing)")
    s = j(c.patch(f"/submissions/{s['id']}", json={"views": 250000, "approved_views": 184000}))
    check(s["blocked"] == "Audience share not recorded yet", "missing audience proof is flagged")
    s2 = j(c.patch(f"/submissions/{s['id']}", json={"tier1_pct": 31}))
    check(s2["estimated_earnings"] == 0 and "under the required 40%" in s2["blocked"], "audience below the campaign rule earns $0", s2["blocked"])
    s = j(c.patch(f"/submissions/{s['id']}", json={"tier1_pct": 55}))
    check(s["blocked"] is None, "audience above the rule clears the flag")
    check(abs(s["estimated_earnings"] - 276.0) < 0.005, "estimate = 184,000 / 1,000 x $1.50 = $276.00", f"${s['estimated_earnings']}")
    check(c.patch(f"/submissions/{s['id']}", json={"approved_views": 300000}).status_code == 400, "approved views above total views refused")
    e = j(c.get("/earnings"))
    check(e["estimated_total"] == 276.0 and e["confirmed_total"] == 0 and e["pending_estimated"] == 276.0, "estimated and confirmed are kept separate (confirmed still $0)")
    csv_data = f"URL,Views,Approved Views,Earnings,Status,Paid At\n{url},300000,200000,290.50,paid,2026-11-20\nhttps://unknown.test/x,5,5,1,paid,2026-11-20\n"
    r = j(c.post("/submissions/import-csv", files={"file": ("perf.csv", csv_data.encode())}))
    check(r["updated"] == 1 and r["unmatched"] == ["https://unknown.test/x"], "CSV import updates known links and reports unknown ones (never invents rows)")
    e = j(c.get("/earnings"))
    check(e["estimated_total"] == 300.0 and e["confirmed_total"] == 290.5 and e["pending_estimated"] == 0, "after CSV: estimated $300.00, confirmed $290.50, nothing pending")
    d = j(c.get("/dashboard"))
    check(d["clips_generated"] == 3 and d["clips_approved"] == 1 and d["videos_processed"] == 1 and d["earnings"]["approved_views"] == 200000,
          "dashboard reflects the real rows")
    pc = d["earnings"]["per_campaign"][0]
    check(pc["budget_used_pct"] == 60.0, "campaign budget utilisation 300/500 = 60%")
    check(c.delete(f"/clips/{cid}").status_code == 409, "clip with recorded submissions cannot be deleted by accident")

    ins = j(c.get("/insights"))
    check(ins["posts"] == 1 and ins["avg_views"] == 300000 and ins["category_boost"] == {} and not ins["enough_data"],
          "insights count real posts and do not steer ranking on too little data")

    print("Isolation")
    c2 = httpx.Client(base_url=a.base + "/api", timeout=30)
    j(c2.post("/auth/signup", json={"email": f"other-{uuid.uuid4().hex[:6]}@example.com", "password": "another pass"}))
    check(c2.get(f"/clips/{cid}").status_code == 404 and c2.get(f"/videos/{vid}").status_code == 404 and
          c2.get(f"/clips/{cid}/file").status_code == 404, "a second account cannot see the first account's data or files")
    print(f"\nAll {OK} checks passed.")


if __name__ == "__main__":
    main()
