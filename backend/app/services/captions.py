"""Caption engine: word-level cues -> animated ASS subtitles (burned in by libass).

The same cue grouping is served to the browser so the Studio preview shows exactly what will be rendered.
"""
import re
import subprocess
from functools import lru_cache

from .. import config
from . import semantic

STYLES = {
    "pop":     {"label": "Pop (active word highlighted)", "size": 96, "max_words": 3, "uppercase": True, "bg": False},
    "karaoke": {"label": "Karaoke fill", "size": 80, "max_words": 4, "uppercase": True, "bg": False},
    "oneword": {"label": "One word at a time", "size": 120, "max_words": 1, "uppercase": True, "bg": False},
    "boxed":   {"label": "Boxed", "size": 72, "max_words": 4, "uppercase": False, "bg": True},
    "clean":   {"label": "Clean subtitle", "size": 62, "max_words": 6, "uppercase": False, "bg": False},
}
DEFAULT_PARAMS = {"enabled": True, "font": "Poppins Bold", "size": None, "y": 0.70, "color": "#FFFFFF",
                  "accent": "#FFE600", "keyword_color": "#3DFF8B", "outline": 7, "outline_color": "#000000",
                  "bg": None, "uppercase": None, "max_words": None, "highlight_keywords": True,
                  "hook_text": "", "hook_seconds": 3.0, "hook_y": 0.16,
                  "cta_text": "", "cta_seconds": 3.0, "cta_y": 0.84}


@lru_cache(maxsize=1)
def list_fonts() -> list[dict]:
    out = []
    try:
        files = sorted(p for p in config.FONTS_DIR.iterdir() if p.suffix.lower() in (".ttf", ".otf"))
    except FileNotFoundError:
        files = []
    for f in files:
        r = subprocess.run(["fc-scan", "--format", "%{family}|%{style}", str(f)], capture_output=True, text=True)
        if r.returncode != 0 or "|" not in r.stdout:
            continue
        fam, style = r.stdout.strip().split("|", 1)
        ass_name = fam.split(",")[-1].strip()
        st = style.split(",")[0].strip()
        bold = "bold" in st.lower() and "bold" not in ass_name.lower()
        label = ass_name if st.lower() in ass_name.lower() or st.lower() == "regular" else f"{ass_name} {st}"
        out.append({"label": label, "ass_name": ass_name, "bold": bold, "file": f.name})
    return out


def resolve(style: str, params: dict | None) -> dict:
    s = STYLES.get(style, STYLES["pop"])
    p = {**DEFAULT_PARAMS, **{k: v for k, v in (params or {}).items() if v is not None}}
    for k in ("size", "max_words", "uppercase", "bg"):
        if p.get(k) is None:
            p[k] = s[k]
    p["style"] = style if style in STYLES else "pop"
    return p


def _kw_set(profile: dict) -> set[str]:
    kws = set(profile.get("intensity_words", [])) | set(profile.get("laughter_words", []))
    for c in profile["categories"].values():
        kws |= {k for k in c["keywords"] if " " not in k}
    return {k.lower() for k in kws}


def build_cues(words: list[dict], start: float, end: float, p: dict, profile_name: str = "gta6") -> list[dict]:
    """Group words into on-screen chunks. Times are relative to the clip start."""
    kw = _kw_set(semantic.load_profile(profile_name)) if p.get("highlight_keywords") else set()
    ws = []
    for w in words:
        if w["e"] <= start or w["s"] >= end or not str(w["w"]).strip():
            continue
        s, e = max(0.0, w["s"] - start), min(end - start, w["e"] - start)
        if e - s < 0.04:
            e = s + 0.04
        txt = str(w["w"]).strip()
        bare = re.sub(r"[^a-z0-9']", "", txt.lower())
        ws.append({"s": round(s, 3), "e": round(e, 3), "w": txt.upper() if p["uppercase"] else txt,
                   "kw": bare in kw or (len(bare) > 2 and txt.isupper() and not p["uppercase"])})
    cues, cur = [], []
    limit_chars = 9 * max(1, p["max_words"]) if p["max_words"] <= 4 else 34

    def flush():
        if cur:
            cues.append({"s": cur[0]["s"], "e": cur[-1]["e"], "words": list(cur)})
            cur.clear()
    for i, w in enumerate(ws):
        if cur:
            gap = w["s"] - cur[-1]["e"]
            chars = sum(len(x["w"]) + 1 for x in cur) + len(w["w"])
            if len(cur) >= p["max_words"] or chars > limit_chars or gap > 0.6 \
                    or cur[-1]["w"].rstrip('"\')').endswith((".", "?", "!")):
                flush()
        cur.append(w)
    flush()
    for i, c in enumerate(cues):           # hold each chunk until the next one (no flicker), max +0.5 s
        nxt = cues[i + 1]["s"] if i + 1 < len(cues) else end - start
        c["e"] = round(min(nxt, c["e"] + 0.5) if nxt - c["e"] > 0.02 else nxt, 3)
    return cues


def _ass_color(hex_color: str, alpha: int = 0) -> str:
    h = hex_color.lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _esc(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _wrap2(txt: str, limit: int) -> str:
    """Break a long line into two balanced lines at the space nearest the middle."""
    if len(txt) <= limit:
        return txt
    mid, sp = len(txt) // 2, [i for i, ch in enumerate(txt) if ch == " "]
    if not sp:
        return txt
    cut = min(sp, key=lambda i: abs(i - mid))
    return txt[:cut] + "\\N" + txt[cut + 1:]


def build_ass(words: list[dict], start: float, end: float, style: str, params: dict | None,
              watermark: str = "", watermark_pos: str = "top") -> str:
    p = resolve(style, params)
    W, H = config.OUT_W, config.OUT_H
    fonts = {f["label"]: f for f in list_fonts()}
    f = fonts.get(p["font"]) or (next(iter(fonts.values())) if fonts else {"ass_name": "Sans", "bold": True})
    primary, accent, kwc = _ass_color(p["color"]), _ass_color(p["accent"]), _ass_color(p["keyword_color"])
    outline_c = _ass_color(p["outline_color"])
    border_style = 3 if p["bg"] else 1
    outline = max(0, int(p["outline"])) if not p["bg"] else 14
    back = _ass_color("#000000", 0x50)
    cy = int(H * float(p["y"]))
    bold = -1 if f["bold"] else 0
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{f['ass_name']},{int(p['size'])},{primary},{primary},{outline_c if not p['bg'] else back},{back},{bold},0,0,0,100,100,0,0,{border_style},{outline},{0 if p['bg'] else 2},5,60,60,0,1
Style: Hook,{f['ass_name']},64,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,{bold},0,0,0,100,100,0,0,3,18,0,5,70,70,0,1
Style: Cta,{f['ass_name']},56,&H00000000,&H00000000,&H0000E6FF,&H0000E6FF,{bold},0,0,0,100,100,0,0,3,16,0,5,70,70,0,1
Style: WM,{f['ass_name']},44,&H59FFFFFF,&H59FFFFFF,&H73000000,&H00000000,{bold},0,0,0,100,100,1,0,1,2,0,8,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    pos = f"\\an5\\pos({W // 2},{cy})"
    if p["enabled"]:
        for c in build_cues(words, start, end, p):
            ws = c["words"]
            if p["style"] == "karaoke":
                txt = "".join(f"{{\\kf{max(1, int(round((w['e'] - w['s']) * 100)))}}}{_esc(w['w'])} " for w in ws).strip()
                lines.append(f"Dialogue: 1,{_ts(c['s'])},{_ts(c['e'])},Cap,,0,0,0,,{{{pos}\\1c{accent}\\2c{primary}}}{txt}")
            elif p["style"] == "clean":
                txt = " ".join((f"{{\\1c{kwc}}}{_esc(w['w'])}{{\\1c{primary}}}" if w["kw"] else _esc(w["w"])) for w in ws)
                lines.append(f"Dialogue: 1,{_ts(c['s'])},{_ts(c['e'])},Cap,,0,0,0,,{{{pos}\\fad(60,40)}}{txt}")
            elif p["style"] == "oneword":
                w = ws[0]
                col = kwc if w["kw"] else primary
                lines.append(f"Dialogue: 1,{_ts(c['s'])},{_ts(c['e'])},Cap,,0,0,0,,"
                             f"{{{pos}\\1c{col}\\fscx70\\fscy70\\t(0,90,\\fscx108\\fscy108)\\t(90,160,\\fscx100\\fscy100)}}{_esc(w['w'])}")
            else:  # pop / boxed: one event per active word so the highlight moves with speech
                for i, w in enumerate(ws):
                    t0 = c["s"] if i == 0 else w["s"]
                    t1 = c["e"] if i == len(ws) - 1 else ws[i + 1]["s"]
                    if t1 - t0 < 0.02:
                        continue
                    parts = []
                    for j, x in enumerate(ws):
                        if j == i:
                            parts.append(f"{{\\1c{accent}\\fscx112\\fscy112}}{_esc(x['w'])}{{\\1c{primary}\\fscx100\\fscy100}}")
                        elif x["kw"]:
                            parts.append(f"{{\\1c{kwc}}}{_esc(x['w'])}{{\\1c{primary}}}")
                        else:
                            parts.append(_esc(x["w"]))
                    intro = "\\fscx92\\fscy92\\t(0,70,\\fscx100\\fscy100)" if i == 0 else ""
                    lines.append(f"Dialogue: 1,{_ts(t0)},{_ts(t1)},Cap,,0,0,0,,{{{pos}{intro}}}{' '.join(parts)}")
    hook = str(p.get("hook_text") or "").strip()
    if hook:
        # big text card over the opening seconds: gives viewers a reason to stay before anyone speaks
        hs = max(1.0, min(float(p.get("hook_seconds") or 3.0), end - start))
        hy = int(H * float(p.get("hook_y") or 0.16))
        txt = _esc(hook[:90])
        if len(txt) > 24:                      # wrap long hooks onto two balanced lines
            mid, sp = len(txt) // 2, [i for i, ch in enumerate(txt) if ch == " "]
            if sp:
                cut = min(sp, key=lambda i: abs(i - mid))
                txt = txt[:cut] + "\\N" + txt[cut + 1:]
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(hs)},Hook,,0,0,0,,{{\\an5\\pos({W // 2},{hy})\\fad(120,200)}}{txt}")
    cta = str(p.get("cta_text") or "").strip()
    if cta:
        # end card: tells the viewer where to go next, over the last seconds of the clip
        dur = end - start
        cs = max(1.0, min(float(p.get("cta_seconds") or 3.0), dur))
        cy2 = int(H * float(p.get("cta_y") or 0.84))
        ctxt = _wrap2(_esc(cta[:80]), 26)
        lines.append(f"Dialogue: 2,{_ts(dur - cs)},{_ts(dur)},Cta,,0,0,0,,{{\\an5\\pos({W // 2},{cy2})\\fad(150,0)}}{ctxt}")
    if watermark.strip():
        an, y = {"top": (8, 90), "bottom": (2, H - 90), "middle": (5, H // 2)}.get(watermark_pos, (8, 90))
        lines.append(f"Dialogue: 0,{_ts(0)},{_ts(end - start + 1)},WM,,0,0,0,,{{\\an{an}\\pos({W // 2},{y})}}{_esc(watermark.strip())}")
    return head + "\n".join(lines) + "\n"
