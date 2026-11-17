"""Optional chat-log analysis. Accepts the common export shapes:

  * text lines:  [1:02:33] user: message     /  01:02:33 user: message  /  3753 user: message
  * JSON:        Twitch chat downloaders ({"comments":[{"content_offset_seconds":..,"message":{"body":..}}]})
                 or a plain list of {"time"|"time_in_seconds"|"offset": seconds, "message"|"text": str}
  * CSV:         a column named time/offset/seconds (seconds or h:mm:ss) and a message/text column
Returns a per-second activity signal (0..1), spike = message rate far above the stream's own average.
"""
import csv
import io
import json
import re

import numpy as np
from scipy.ndimage import uniform_filter1d

_LINE = re.compile(r"^\s*\[?(\d{1,2}:\d{2}(?::\d{2})?|\d+(?:\.\d+)?)\]?\s+(.*)$")


def _to_sec(v) -> float | None:
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return float(s)
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?(\.\d+)?", s):
        t = 0.0
        for part in s.split(":"):
            t = t * 60 + float(part)
        return t
    return None


def parse(raw: str) -> list[tuple[float, str]]:
    raw = raw.strip()
    msgs: list[tuple[float, str]] = []
    if raw[:1] in "[{":
        try:
            data = json.loads(raw)
            items = data.get("comments", data.get("messages", [])) if isinstance(data, dict) else data
            for it in items:
                if not isinstance(it, dict):
                    continue
                t = next((it[k] for k in ("content_offset_seconds", "time_in_seconds", "time", "offset", "seconds")
                          if k in it), None)
                m = it.get("message", it.get("text", it.get("body", "")))
                if isinstance(m, dict):
                    m = m.get("body", "")
                t = _to_sec(t) if t is not None else None
                if t is not None:
                    msgs.append((t, str(m)))
            if msgs:
                return msgs
        except json.JSONDecodeError:
            pass
    head = raw.splitlines()[0].lower() if raw else ""
    if "," in head and any(k in head for k in ("time", "offset", "seconds")):
        rd = csv.DictReader(io.StringIO(raw))
        tk = next((f for f in rd.fieldnames or [] if f.lower().strip() in ("time", "offset", "seconds", "time_in_seconds")), None)
        mk = next((f for f in rd.fieldnames or [] if f.lower().strip() in ("message", "text", "body", "content")), None)
        if tk:
            for row in rd:
                t = _to_sec(row.get(tk, ""))
                if t is not None:
                    msgs.append((t, row.get(mk, "") if mk else ""))
            return msgs
    for line in raw.splitlines():
        m = _LINE.match(line)
        if m:
            t = _to_sec(m.group(1))
            if t is not None:
                msgs.append((t, m.group(2)))
    return msgs


def signal(msgs: list[tuple[float, str]], n_sec: int, hype_tokens: list[str]) -> list[float]:
    if not msgs or n_sec <= 0:
        return []
    rate = np.zeros(n_sec)
    hype = np.zeros(n_sec)
    toks = [t.lower() for t in hype_tokens]
    for t, m in msgs:
        i = int(t)
        if 0 <= i < n_sec:
            rate[i] += 1
            low = m.lower()
            if any(tok in low.split() or (len(tok) > 3 and tok in low) for tok in toks):
                hype[i] += 1
    sm = uniform_filter1d(rate + 1.5 * hype, size=5)
    base = uniform_filter1d(sm, size=min(n_sec, 120))
    sd = max(float(sm.std()), 0.3)
    z = (sm - base) / sd
    return [round(float(v), 3) for v in np.clip(z / 3.0, 0, 1)]
