"""Highlight detection and ranking.

Pipeline:  per-second signals -> combined interest curve -> peaks -> clip windows snapped to
sentence boundaries (setup before the peak, payoff after it) -> scoring -> de-duplication -> optional LLM re-rank.

Design rules taken from the product brief:
  * loud is not automatically entertaining: a loud peak with no corroborating signal is penalised
  * a clip must be understandable alone: starts at a sentence start, has setup before the peak
  * never end before the payoff: the window runs until the excitement has decayed and the sentence finished
  * no duplicates: overlapping windows and near-identical transcripts are collapsed
"""
import numpy as np
from scipy.ndimage import uniform_filter1d
from scipy.signal import find_peaks

from . import semantic

DEFAULTS = {"min_duration": 15.0, "max_duration": 60.0, "max_highlights": 40, "streamer": "", "profile": "gta6",
            "use_llm": True, "extra_hashtags": [], "min_score": 30.0}


def build_sentences(words: list[dict], max_gap: float = 0.8, max_len: float = 14.0) -> list[dict]:
    sents, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        end_punct = w["w"].rstrip('"\')').endswith((".", "?", "!"))
        gap = (nxt["s"] - w["e"]) if nxt else 99
        if end_punct or gap > max_gap or (w["e"] - cur[0]["s"]) > max_len:
            sents.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": " ".join(x["w"] for x in cur)})
            cur = []
    if cur:
        sents.append({"s": cur[0]["s"], "e": cur[-1]["e"], "text": " ".join(x["w"] for x in cur)})
    return sents


def _arr(x, n):
    a = np.zeros(n)
    if x:
        m = min(n, len(x))
        a[:m] = np.asarray(x[:m], dtype=float)
    return a


def interest_curve(n: int, audio: dict, scenes: list[float], chat: list[float], sentences: list[dict],
                   profile: dict) -> dict:
    excite, onset = _arr(audio.get("excite"), n), _arr(audio.get("onset"), n)
    laugh, scream = _arr(audio.get("laugh"), n), _arr(audio.get("scream"), n)
    text = np.zeros(n)
    speech = np.zeros(n)
    for s in sentences:
        sc = semantic.text_score(s["text"], profile)
        a, b = int(s["s"]), min(n - 1, int(s["e"]))
        if a < n:
            text[a:b + 1] = np.maximum(text[a:b + 1], sc)
            speech[a:b + 1] = 1
    cuts = np.zeros(n)
    for t in scenes:
        if 0 <= int(t) < n:
            cuts[int(t)] += 1
    scene = np.clip(uniform_filter1d(cuts, size=7) * 7 / 3.0, 0, 1)   # 3+ cuts within 7 s -> 1.0
    chat_a = _arr(chat, n)
    has_chat = bool(chat) and chat_a.max() > 0

    w = {"excite": 0.30, "onset": 0.10, "laugh": 0.16, "scream": 0.08, "text": 0.22, "scene": 0.06, "chat": 0.18}
    if not has_chat:
        w["chat"] = 0.0
    tot = sum(w.values())
    c = (w["excite"] * excite + w["onset"] * onset + w["laugh"] * laugh + w["scream"] * scream
         + w["text"] * text + w["scene"] * scene + w["chat"] * chat_a) / tot
    # corroboration: loudness alone (no laugh / reaction words / chat / cuts nearby) is discounted
    support = np.maximum.reduce([uniform_filter1d(laugh, 5), uniform_filter1d(text, 5),
                                 uniform_filter1d(chat_a, 5), scene * 0.6])
    c = c * np.where(support < 0.12, 0.55, 1.0)
    return {"curve": uniform_filter1d(c, size=5), "excite": excite, "onset": onset, "laugh": laugh, "scream": scream,
            "text": text, "scene": scene, "cuts": cuts, "chat": chat_a, "speech": speech, "has_chat": has_chat}


def _snap_start(t: float, sentences: list[dict], lo: float, hi: float) -> tuple[float, bool]:
    """Nearest sentence start to t inside [lo, hi]; prefers starting slightly early over late."""
    best, bd = None, 1e9
    for s in sentences:
        if lo <= s["s"] <= hi:
            d = abs(s["s"] - t) + (0.6 if s["s"] > t else 0.0)
            if d < bd:
                best, bd = s["s"], d
    return (best, True) if best is not None else (t, False)


def _sentence_at(t: float, sentences: list[dict]):
    for s in sentences:
        if s["s"] <= t <= s["e"]:
            return s
    return None


def build_window(p: int, sig: dict, sentences: list[dict], duration: float, mn: float, mx: float) -> dict:
    curve = sig["curve"]
    n = len(curve)
    peak_v = curve[p]
    # --- payoff: run forward until the curve has decayed for 2 s
    e = p + 1
    limit = min(n - 1, p + int(mx * 0.5))
    while e < limit and not (curve[e] < 0.4 * peak_v and curve[min(n - 1, e + 1)] < 0.4 * peak_v):
        e += 1
    end = float(min(duration, e + 1.0))
    s_at = _sentence_at(end, sentences)
    ends_clean = False
    if s_at:                                   # never cut a sentence in half
        if s_at["e"] - p <= mx * 0.6:
            end, ends_clean = s_at["e"] + 0.35, True
        elif s_at["s"] - 0.1 > p + 1.5:
            end, ends_clean = s_at["s"] - 0.1, True
    else:
        prev = [s for s in sentences if p < s["e"] <= end]
        ends_clean = True                      # ends in a gap between sentences
        if prev:
            end = max(end, prev[-1]["e"] + 0.35)
    end = min(duration, end)

    # --- setup: aim for a clip long enough to carry context, never starting right on the peak
    target = float(np.clip((end - p) + 10.0, mn, mx))
    start0 = max(0.0, min(end - target, p - 4.0))
    start, starts_clean = _snap_start(start0, sentences, max(0.0, end - mx), min(start0 + 6.0, p - 2.0))
    if starts_clean:
        start = max(0.0, start - 0.2)
    # trim dead air at the head (quiet, no speech) as long as setup survives
    first_word = next((s["s"] for s in sentences if s["s"] >= start), None)
    if first_word is not None and first_word - start > 2.0 and p - first_word >= 3.0 \
            and sig["excite"][int(start):int(first_word)].max(initial=0) < 0.3:
        start = first_word - 0.5
    # --- enforce duration bounds
    if end - start < mn:
        need = mn - (end - start)
        ns, ok = _snap_start(start - need, sentences, max(0.0, start - need - 4.0), start - need + 0.5)
        start = max(0.0, ns - (0.2 if ok else 0.0))
        starts_clean = ok or starts_clean
        if end - start < mn:
            end = min(duration, start + mn)
            s2 = _sentence_at(end, sentences)
            if s2 and s2["e"] + 0.35 - start <= mx:
                end = s2["e"] + 0.35
    if end - start > mx:
        start = end - mx
        starts_clean = False
    return {"start": round(max(0.0, start), 2), "end": round(min(duration, end), 2), "peak": float(p),
            "starts_clean": starts_clean, "ends_clean": ends_clean}


def _jaccard(a: str, b: str) -> float:
    A, B = set(a.lower().split()), set(b.lower().split())
    return len(A & B) / max(1, len(A | B))


def _iou(a, b) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(1e-6, max(a["end"], b["end"]) - min(a["start"], b["start"]))


def dedupe(cands: list[dict]) -> list[dict]:
    kept = []
    for c in sorted(cands, key=lambda x: -x["engagement_score"]):
        dup = False
        for k in kept:
            if _iou(c, k) > 0.25 or (len(c["transcript"].split()) >= 6 and _jaccard(c["transcript"], k["transcript"]) > 0.6):
                dup = True
                break
        if not dup:
            kept.append(c)
    return kept


def find_highlights(words: list[dict], audio: dict, scenes: list[float], chat: list[float], duration: float,
                    settings: dict | None = None) -> tuple[list[dict], dict]:
    st = {**DEFAULTS, **(settings or {})}
    mn, mx = float(st["min_duration"]), float(st["max_duration"])
    mx = max(mx, mn)
    profile = semantic.load_profile(st["profile"])
    n = max(int(duration), int(audio.get("seconds", 0)), 1)
    sentences = build_sentences(words)
    sig = interest_curve(n, audio, scenes, chat, sentences, profile)
    curve = sig["curve"]
    meta = {"sentences": len(sentences), "has_chat": bool(sig["has_chat"]), "semantic": "heuristic", "llm": None}
    if duration < mn or curve.max() <= 0:
        return [], meta

    thr = max(float(np.percentile(curve, 70)), 0.04)
    peaks, props = find_peaks(curve, distance=max(6, int(mn * 0.6)), height=thr, prominence=max(0.015, curve.std() * 0.25))
    if len(peaks) == 0 and curve.max() > 0.04:
        peaks = np.array([int(np.argmax(curve))])
    budget = int(np.clip(duration / 60 * 2 + 4, 6, st["max_highlights"] * 2))
    peaks = sorted(peaks, key=lambda p: -curve[p])[:budget]
    cmax = float(curve.max())

    cands = []
    for p in peaks:
        w = build_window(int(p), sig, sentences, duration, mn, mx)
        a, b = int(w["start"]), max(int(w["start"]) + 1, int(np.ceil(w["end"])))
        b = min(b, n)
        cw = [x for x in words if x["s"] >= w["start"] - 0.05 and x["e"] <= w["end"] + 0.3]
        transcript = " ".join(x["w"] for x in cw)
        sents = [s for s in sentences if s["s"] >= w["start"] - 0.3 and s["e"] <= w["end"] + 0.4]
        dur = w["end"] - w["start"]
        top = lambda arr: float(np.sort(arr[a:b])[-max(1, min(3, b - a)):].mean())
        signals = {"excite": round(top(sig["excite"]), 2), "onset": round(float(sig["onset"][a:b].max()), 2),
                   "laugh": round(top(sig["laugh"]), 2), "scream": round(top(sig["scream"]), 2),
                   "text": round(float(sig["text"][a:b].max()), 2), "scene_cuts": int(sig["cuts"][a:b].sum()),
                   "chat": round(float(sig["chat"][a:b].max()), 2) if sig["has_chat"] else None,
                   "speech_ratio": round(float(sig["speech"][a:b].mean()), 2),
                   "words_per_sec": round(len(cw) / max(dur, 1), 2)}
        first = cw[0]["w"].lower().strip(",.") if cw else ""
        pre, post = w["peak"] - w["start"], w["end"] - w["peak"]
        standalone = (0.30 * w["starts_clean"] + 0.20 * w["ends_clean"] + 0.20 * min(1.0, pre / 5.0)
                      + 0.10 * min(1.0, post / 2.0) + 0.20 * float(1.0 <= signals["words_per_sec"] <= 4.5)
                      - (0.15 if first in profile["weak_openers"] else 0.0)
                      - (0.25 if len(cw) < 4 else 0.0))
        standalone = float(np.clip(standalone, 0, 1))
        hook = max(float(sig["text"][a:min(b, a + 4)].max(initial=0)), float(sig["onset"][a:min(b, a + 3)].max(initial=0)),
                   0.4 if cw and cw[0]["s"] - w["start"] < 1.2 else 0.0)
        chat_v = signals["chat"] or 0.0
        eng = (0.30 * curve[int(p)] / cmax + 0.18 * signals["text"] + 0.14 * signals["laugh"] + 0.06 * signals["scream"]
               + 0.08 * chat_v + 0.04 * min(1, signals["scene_cuts"] / 3) + 0.20 * standalone)
        eng = float(np.clip(eng / (0.92 if sig["has_chat"] else 0.84), 0, 1))
        corroborated = signals["text"] >= 0.2 or signals["laugh"] >= 0.35 or chat_v >= 0.4
        if not corroborated:          # activity without a reason a viewer would care
            eng *= 0.7
        agree = sum([signals["excite"] > 0.4, max(signals["laugh"], signals["scream"]) > 0.35, signals["text"] > 0.3,
                     chat_v > 0.4, signals["scene_cuts"] >= 2])
        conf = float(np.clip(0.2 + 0.13 * agree + (0.15 if len(cw) >= 6 else 0.0) + 0.1 * standalone, 0.05, 0.95))
        if not (signals["text"] >= 0.2 or signals["laugh"] >= 0.35 or chat_v >= 0.4):
            conf *= 0.6
        cat, label = semantic.classify(transcript, signals, profile)
        copy = semantic.heuristic_copy([s["text"] for s in sents], signals, cat, label, st["streamer"], profile,
                                       st.get("extra_hashtags"))
        parts = []
        if signals["excite"] > 0.4:
            parts.append(f"audio jumps well above the stream's baseline ({signals['excite']:.2f})")
        if signals["laugh"] > 0.35:
            parts.append("laughter-like sound")
        if signals["scream"] > 0.35:
            parts.append("shout/scream-like sound")
        if signals["text"] > 0.3:
            parts.append("reaction language in the commentary")
        if chat_v > 0.4:
            parts.append("chat activity spike")
        if signals["scene_cuts"] >= 2:
            parts.append(f"{signals['scene_cuts']} quick scene changes")
        reason = ("Flagged because of " + ", ".join(parts) + ".") if parts else \
            "Above-average activity for this stream, but no strong single signal. Review before using."
        if standalone >= 0.7:
            reason += " Starts on a full sentence with setup before the peak, so it should make sense on its own."
        elif standalone < 0.4:
            reason += " Context is thin - check it makes sense without the rest of the stream."
        quote = next((s["text"] for s in sorted(sents, key=lambda s: -semantic.text_score(s["text"], profile))
                      if len(s["text"].split()) >= 3), "")
        desc = (f"{label}. " + (f"Streamer says: “{quote[:140]}”" if quote else "No clear speech in this window."))
        cands.append({**w, "transcript": transcript, "signals": signals, "standalone": round(standalone, 2),
                      "engagement_score": round(eng * 100, 1),
                      "viral_potential": round(float(np.clip(eng * (0.75 + 0.25 * hook), 0, 1)) * 100, 1),
                      "confidence": round(conf, 2), "suggested_duration": round(dur, 1),
                      "category": cat, "description": desc, "reason": reason, "scored_by": "heuristic",
                      "flags": semantic.content_flags(cw, w["start"], signals, profile), **copy})

    cands = dedupe(cands)[: st["max_highlights"]]
    # quality over quantity: keep the clear winners, drop the long tail of merely-above-average moments
    if cands:
        floor = max(float(st.get("min_score", 30.0)), 0.4 * cands[0]["engagement_score"])
        cands = [c for i, c in enumerate(cands) if c["engagement_score"] >= floor]

    # --- optional LLM re-rank of what survived (one cheap call per 8 candidates)
    if st["use_llm"] and semantic.llm_available() and cands:
        for i, c in enumerate(cands):
            c["id"] = i
        res, usage = semantic.llm_rescore([c for c in cands if len(c["transcript"].split()) >= 4], st["streamer"])
        meta["llm"] = usage
        if res:
            meta["semantic"] = "llm+heuristic"
            for c in cands:
                r = res.get(c["id"])
                if not r:
                    continue
                try:
                    ent, sa = float(r.get("entertaining", 0)), float(r.get("standalone", 0))
                except (TypeError, ValueError):
                    continue
                c["engagement_score"] = round(0.5 * c["engagement_score"] + 0.5 * (0.65 * ent + 0.35 * sa), 1)
                c["viral_potential"] = round(0.5 * c["viral_potential"] + 0.5 * ent, 1)
                opts = [str(t)[:100] for t in (r.get("title_options") or []) if t][:3]
                if opts:
                    c["title_options"] = opts + c["title_options"][:1]
                    c["title"] = opts[0]
                if r.get("description"):
                    c["description"] = str(r["description"])[:400]
                if r.get("why"):
                    c["reason"] = str(r["why"])[:400] + " " + c["reason"]
                if r.get("hook"):
                    c["hook"] = str(r["hook"])[:300]
                if r.get("category") in profile["categories"]:
                    c["category"] = r["category"]
                codes = {f["code"] for f in c["flags"]}
                if r.get("spoiler_risk") and "possible_spoiler" not in codes:
                    c["flags"].append({"code": "possible_spoiler", "severity": "review",
                                       "detail": "The language model judged this may reveal story content."})
                if r.get("graphic_violence") and "graphic_violence" not in codes:
                    c["flags"].append({"code": "graphic_violence", "severity": "review",
                                       "detail": "The language model judged this may be graphic."})
                c["scored_by"] = "llm+heuristic"
        for c in cands:
            c.pop("id", None)

    cands.sort(key=lambda x: -x["engagement_score"])
    for i, c in enumerate(cands):
        c["rank"] = i + 1
    return cands, meta
