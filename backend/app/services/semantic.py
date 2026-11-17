"""Semantic layer: text scoring, content classification, titles/hooks/hashtags.

Two interchangeable back-ends:
  * heuristic (always available, free): keyword/phrase scoring driven by the content profile JSON
  * LLM (optional): any OpenAI-compatible chat endpoint; used to re-rank the top candidates and write copy
Neither is allowed to describe events that are not in the transcript or the measured signals.
"""
import json
import re
from functools import lru_cache

from .. import config


@lru_cache(maxsize=8)
def load_profile(name: str = "gta6") -> dict:
    p = config.PROFILES_DIR / f"{name}.json"
    if not p.exists():
        p = config.PROFILES_DIR / "gta6.json"
    return json.loads(p.read_text())


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower())


def _has(text_norm: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase.lower()) + r"(?![a-z0-9])", text_norm) is not None


def text_score(sentence: str, profile: dict) -> float:
    """0..1: how much this sentence reads like a reaction / joke / notable moment."""
    if not sentence.strip():
        return 0.0
    t = _norm(sentence)
    s = 0.0
    hits = 0
    for cat in profile["categories"].values():
        k = sum(1 for kw in cat["keywords"] if _has(t, kw))
        if k:
            hits += k
            s += min(0.3, 0.14 * k) * cat.get("weight", 1.0)
    s += min(0.3, 0.18 * sum(1 for w in profile["laughter_words"] if _has(t, w)))
    if re.search(r"\b(?:h[ae]){2,}h?\b|\b(?:ha |he ){2,}", t + " "):     # transcribed laughter
        s += 0.25
    s += min(0.15, 0.07 * sum(1 for w in profile["intensity_words"] if _has(t, w)))
    s += min(0.2, 0.1 * sentence.count("!"))
    if "?" in sentence:
        s += 0.08
    caps = [w for w in sentence.split() if len(w) > 2 and w.isupper()]
    s += min(0.12, 0.06 * len(caps))
    if re.search(r"\b(\w+)( \1){2,}\b", t):       # "no no no", "wait wait wait"
        s += 0.15
    return max(0.0, min(1.0, s))


def classify(text: str, signals: dict, profile: dict) -> tuple[str, str]:
    t = _norm(text)
    best, best_score = "", 0.0
    for key, cat in profile["categories"].items():
        sc = sum(1 for kw in cat["keywords"] if _has(t, kw)) * cat.get("weight", 1.0)
        if sc > best_score:
            best, best_score = key, sc
    if not best:
        if signals.get("laugh", 0) > 0.45 or signals.get("scream", 0) > 0.45:
            best = "reaction"
        else:
            return "moment", "Notable moment"
    return best, profile["categories"][best]["label"]


def content_flags(words: list[dict], start: float, signals: dict, profile: dict) -> list[dict]:
    """Policy flags for human review. words carry absolute times; start is the clip start."""
    pol = profile.get("policy", {})
    text = _norm(" ".join(w["w"] for w in words))
    flags = []
    sp = [k for k in pol.get("spoiler_keywords", []) if _has(text, k)]
    if sp:
        flags.append({"code": "possible_spoiler", "severity": "review",
                      "detail": f"Transcript mentions: {', '.join(sp[:4])}. Rockstar removes ending reveals and "
                                f"standalone story cinematics. Check this is gameplay, not story."})
    vi = [k for k in pol.get("violence_keywords", []) if _has(text, k)]
    if vi:
        early = pol.get("violence_early_seconds", 7)
        first = min((w["s"] - start for w in words if any(_has(_norm(w["w"]), k) for k in vi)), default=None)
        det = f"Transcript mentions: {', '.join(vi[:4])}."
        if first is not None and first < early:
            det += f" It occurs {first:.0f}s in. YouTube limits ads when graphic violence is in the first {early}s."
        flags.append({"code": "graphic_violence", "severity": "review", "detail": det})
    sl = [k for k in pol.get("strong_language", []) if _has(text, k)]
    if sl:
        early_sl = [w for w in words if w["s"] - start < 7 and any(_has(_norm(w["w"]), k) for k in sl)]
        flags.append({"code": "strong_language", "severity": "info",
                      "detail": f"Strong language ({len(sl)} term(s))" + (", including in the first 7s" if early_sl else "")
                                + ". May limit ads on some platforms; check the campaign rules."})
    if pol.get("flag_possible_cinematic") and signals.get("scene_cuts", 0) >= pol.get("cinematic_min_scene_cuts", 3) \
            and signals.get("excite", 0) < 0.25 and signals.get("laugh", 0) < 0.2 and signals.get("speech_ratio", 0) > 0.5:
        flags.append({"code": "possible_cinematic", "severity": "review",
                      "detail": "Several camera cuts with continuous dialogue and no streamer reaction - this pattern "
                                "can be a story cutscene. Standalone cutscenes are not allowed. (Heuristic)"})
    return flags


# --------------------------------------------------------------------------- copy (heuristic)
def _best_quote(sentences: list[str], profile: dict, max_len: int = 58) -> str:
    best, bs = "", -1.0
    for s in sentences:
        s = s.strip()
        if not (8 <= len(s) <= max_len):
            continue
        sc = text_score(s, profile) + 0.1
        if sc > bs:
            best, bs = s, sc
    return best.rstrip(".")


def heuristic_copy(sentences: list[str], signals: dict, category: str, label: str, streamer: str,
                   profile: dict, extra_tags: list[str] | None = None) -> dict:
    who = streamer.strip() or "Streamer"
    quote = _best_quote(sentences, profile)
    titles = []
    if quote:
        titles.append(f"“{quote}” — {who} on GTA 6")
    if signals.get("laugh", 0) > 0.4:
        titles.append(f"{who} can't stop laughing at this GTA 6 moment")
    if signals.get("scream", 0) > 0.4 or signals.get("onset", 0) > 0.6:
        titles.append(f"{who} was NOT ready for this in GTA 6")
    titles.append(f"GTA 6: {label.lower()} ({who})")
    if quote and len(titles) < 4:
        titles.append(f"{who}: “{quote}”")
    seen, uniq = set(), []
    for t in titles:
        if t not in seen:
            seen.add(t)
            uniq.append(t[:100])
    first = next((s.strip() for s in sentences if s.strip()), "")
    hook = (f"Opens on: “{first[:90]}”" if first else "No speech in the first seconds - consider opening on the action or adding a text hook.")
    tags = list(profile.get("default_hashtags", []))
    st = re.sub(r"[^a-z0-9]", "", streamer.lower())
    if st:
        tags.append(f"#{st}")
    for t in extra_tags or []:
        if t not in tags:
            tags.append(t)
    return {"title": uniq[0], "title_options": uniq[:4], "hook": hook, "hashtags": tags}


# --------------------------------------------------------------------------- LLM (optional)
def llm_available() -> bool:
    return bool(config.LLM_API_KEY) or "localhost" in config.LLM_BASE_URL or "127.0.0.1" in config.LLM_BASE_URL


_SYSTEM = """You rank short-form clip candidates cut from a GTA 6 livestream.
For each candidate you get: the streamer's transcript for that window and measured signals
(audio excitement, laughter-like sound, scream-like sound, scene cuts, chat spike), each 0-1.
Judge ONLY from that. Never invent events, dialogue or outcomes that are not in the transcript.
If the transcript is empty or unclear, say so and score low rather than guessing.
Return JSON: {"items":[{"id":int,"entertaining":0-100,"standalone":0-100,
"title_options":[3 short titles grounded in the transcript],"description":"one factual sentence",
"why":"one sentence on why a viewer would care","hook":"which line should open the clip",
"category":"npc_interaction|physics|driving|police|discovery|fail|reaction|commentary|moment",
"spoiler_risk":bool,"graphic_violence":bool}]}
standalone = can someone who never saw the stream follow it. Loud is not automatically entertaining."""


def llm_rescore(cands: list[dict], streamer: str) -> tuple[dict[int, dict], dict]:
    """Returns ({candidate_id: result}, usage). Any failure returns ({}, {"error": ...}) and callers fall back."""
    import httpx
    out: dict[int, dict] = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0}
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    try:
        with httpx.Client(timeout=120) as cl:
            for i in range(0, len(cands), 8):
                batch = [{"id": c["id"], "duration": round(c["end"] - c["start"], 1),
                          "transcript": c["transcript"][:1400], "signals": c["signals"]} for c in cands[i:i + 8]]
                body = {"model": config.LLM_MODEL, "temperature": 0.2,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "system", "content": _SYSTEM},
                                     {"role": "user", "content": json.dumps({"streamer": streamer, "candidates": batch})}]}
                r = cl.post(config.LLM_BASE_URL.rstrip("/") + "/chat/completions", headers=headers, json=body)
                r.raise_for_status()
                data = r.json()
                u = data.get("usage") or {}
                usage["prompt_tokens"] += u.get("prompt_tokens", 0)
                usage["completion_tokens"] += u.get("completion_tokens", 0)
                usage["calls"] += 1
                content = data["choices"][0]["message"]["content"]
                m = re.search(r"\{.*\}", content, re.S)
                for it in json.loads(m.group(0) if m else content).get("items", []):
                    if isinstance(it.get("id"), int):
                        out[it["id"]] = it
    except Exception as e:  # network, auth, bad JSON: never fail the analysis because of the optional LLM
        usage["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    usage["usd"] = round(usage["prompt_tokens"] / 1e6 * config.LLM_PRICE_IN_PER_M
                         + usage["completion_tokens"] / 1e6 * config.LLM_PRICE_OUT_PER_M, 5)
    return out, usage
