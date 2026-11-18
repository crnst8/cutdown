"""Kept audio per unit: the one source for duration, text and cut points.

A unit is kept as one or more spans [in, out] in master seconds. Spans come from
the unit's timeline entry minus: isolated edge fillers, isolated internal
fillers, and any explicit holes (phrase cuts made in the revise pass).

Nothing here is shown to a model. Models see the kept text only.
"""
from __future__ import annotations

import re

FILLERS = {"um", "umm", "uh", "uhh", "uhm", "er", "erm"}
# Removed only inside a sentence: "ah" also opens real replies ("Ah, right").
SOFT_FILLERS = {"ah", "ahh"}
TERMINAL = ".?!…"
EDGE_GAP = 0.15
EDGE_WINDOW = 2.5
MIN_SPAN = 0.05
DEFAULTS = {"remove": True, "min_gap": 0.12, "max_pause": 0.45}
PAUSE_MARK = "‖"


def _norm(word) -> str:
    return re.sub(r"[^a-z]", "", str(word).lower())


def _closes(word) -> bool:
    t = str(word).rstrip('"”’\')')
    return bool(t) and t[-1] in TERMINAL


def _trim_point(words: list, side: str, min_gap: float, window: float) -> float | None:
    """Remove only an isolated edge run of um/uh, never meaningful speech.

    A nearby pause alone is not evidence that the words beyond it are filler.
    Require word text, an all-filler edge and a gap at that exact boundary.
    Internal hesitations and false starts need a producer's listening edit.
    """
    if len(words) < 2 or side not in {"head", "tail"}:
        return None
    if any(len(w) < 3 for w in words):
        return None
    filler = [re.sub(r"[^a-z]", "", w[2].lower()) in {"um", "uh", "umm", "uhh"}
              for w in words]
    if side == "head":
        i = 0
        while i < len(words) and filler[i]:
            i += 1
        if i == 0 or i == len(words) or words[i][0] - words[0][0] > window:
            return None
    else:
        i = len(words)
        while i > 0 and filler[i - 1]:
            i -= 1
        if i == 0 or i == len(words) or words[-1][1] - words[i - 1][1] > window:
            return None
    a, b = words[i - 1][1], words[i][0]
    if b - a < min_gap:
        return None
    return round((a + b) / 2, 3)


def is_filler(words: list, i: int) -> bool:
    t = _norm(words[i][2])
    if t in FILLERS:
        return True
    if t in SOFT_FILLERS:
        return i > 0 and not _closes(words[i - 1][2]) and not _closes(words[i][2])
    return False


def filler_runs(words: list) -> list[tuple[int, int]]:
    """Inclusive (first, last) index pairs of consecutive filler words."""
    runs, i = [], 0
    while i < len(words):
        if is_filler(words, i):
            j = i
            while j + 1 < len(words) and is_filler(words, j + 1):
                j += 1
            runs.append((i, j))
            i = j + 1
        else:
            i += 1
    return runs


def _usable(words: list) -> bool:
    return len(words) >= 2 and all(len(w) >= 3 for w in words)


def _subtract(lo: float, hi: float, holes: list) -> list[list[float]]:
    holes = sorted((max(a, lo), min(b, hi)) for a, b in holes)
    holes = [(a, b) for a, b in holes if b - a > 0]
    for a, b in holes:                      # a hole that starts at the head
        if a - lo < MIN_SPAN and hi - b >= MIN_SPAN:
            lo = max(lo, b)
    for a, b in reversed(holes):            # a hole that ends at the tail
        if hi - b < MIN_SPAN and a - lo >= MIN_SPAN:
            hi = min(hi, a)
    spans, at = [], lo
    for a, b in holes:
        if a - at < MIN_SPAN or hi - b < MIN_SPAN:
            continue
        spans.append([round(at, 3), round(a, 3)])
        at = b
    spans.append([round(at, 3), round(hi, 3)])
    return spans


def unit_spans(uid: str, timeline: dict, unit_words: dict, *, flags: dict | None = None,
               holes: list | None = None, cfg: dict | None = None) -> list[list[float]]:
    """The kept spans of one unit, in master seconds, in order."""
    t = timeline.get(uid)
    if not t:
        return []
    c = {**DEFAULTS, **(cfg or {})}
    flags = flags or {}
    lo, hi = t["cut_in"], t["cut_out"]
    words = unit_words.get(uid) or []
    cut = [tuple(h) for h in (holes or [])]
    if _usable(words):
        if c["remove"] or flags.get("head"):
            p = _trim_point(words, "head", EDGE_GAP, EDGE_WINDOW)
            if p and lo < p < hi:
                lo = p
        if c["remove"] or flags.get("tail"):
            p = _trim_point(words, "tail", EDGE_GAP, EDGE_WINDOW)
            if p and lo < p < hi:
                hi = p
        if c["remove"]:
            for i, j in filler_runs(words):
                if i == 0 or j == len(words) - 1:
                    continue                # edges are handled above
                gl = words[i][0] - words[i - 1][1]
                gr = words[j + 1][0] - words[j][1]
                if gl < c["min_gap"] or gr < c["min_gap"]:
                    continue
                cut.append((words[i - 1][1] + min(gl, c["max_pause"]) / 2,
                            words[j + 1][0] - min(gr, c["max_pause"]) / 2))
    return _subtract(lo, hi, cut)


def group_spans(seg: dict, timeline: dict, unit_words: dict,
                cfg: dict | None = None) -> dict[str, list]:
    """uid -> spans for every unit of a segment or topic, honouring its flags."""
    trim = seg.get("trim") or {}
    holes = seg.get("holes") or {}
    return {uid: unit_spans(uid, timeline, unit_words, flags=trim.get(uid),
                            holes=holes.get(uid), cfg=cfg)
            for uid in (seg.get("order") or seg.get("ids") or [])}


def kept_dur(spans: list) -> float:
    return round(sum(b - a for a, b in spans), 3)


def _kept_words(uid: str, unit_words: dict, spans: list) -> list | None:
    words = unit_words.get(uid) or []
    if not spans or not words or any(len(w) < 3 for w in words):
        return None
    return [w for w in words
            if any(a <= (w[0] + w[1]) / 2 <= b for a, b in spans)]


def kept_text(uid: str, units: dict, unit_words: dict, spans: list) -> str:
    """The words that go to air for this unit."""
    kept = _kept_words(uid, unit_words, spans)
    if kept is None:
        return units.get(uid, {}).get("text", "")
    return " ".join(w[2] for w in kept)


def marked_text(uid: str, units: dict, unit_words: dict, spans: list,
                min_gap: float = DEFAULTS["min_gap"]) -> str:
    """kept_text with PAUSE_MARK wherever a measured pause allows a cut."""
    kept = _kept_words(uid, unit_words, spans)
    if kept is None:
        return units.get(uid, {}).get("text", "")
    out = []
    for n, w in enumerate(kept):
        if n and w[0] - kept[n - 1][1] >= min_gap:
            out.append(PAUSE_MARK)
        out.append(w[2])
    return " ".join(out)


def continuous(prev_uid: str, uid: str, timeline: dict) -> bool:
    """True when only removed audio can lie between the two units' kept audio."""
    if prev_uid == uid:
        return True
    a, b = timeline.get(prev_uid), timeline.get(uid)
    return bool(a and b) and abs(a["cut_out"] - b["cut_in"]) < 1e-6


def clips(order: list[str], spans_by_uid: dict, timeline: dict) -> list[dict]:
    """Merge kept spans into clips. Each: {"in","out","units","join"}.

    join is "start" for the first clip, "tight" when only removed audio lies
    between this clip and the previous one, else "edit".
    """
    out: list[dict] = []
    prev = None
    for uid in order:
        for a, b in spans_by_uid.get(uid) or []:
            if out and abs(out[-1]["out"] - a) < 1e-6:
                out[-1]["out"] = b
                if out[-1]["units"][-1] != uid:
                    out[-1]["units"].append(uid)
            else:
                join = ("start" if not out else
                        "tight" if continuous(prev, uid, timeline) else "edit")
                out.append({"in": a, "out": b, "units": [uid], "join": join})
            prev = uid
    return out


def locate_cut(words: list, text: str, t: dict, cfg: dict | None = None) -> tuple[float, float] | None:
    """The hole [from, to] for a quoted run of words, or None when it is unsafe."""
    c = {**DEFAULTS, **(cfg or {})}
    norm = lambda tok: re.sub(r"[^a-z0-9']", "", tok.lower())
    tokens = [norm(tok) for tok in text.split() if tok != PAUSE_MARK and norm(tok)]
    if not tokens or not words or any(len(w) < 3 for w in words):
        return None
    source = [norm(str(w[2])) for w in words]
    n = len(tokens)
    matches = [i for i in range(len(words)-n+1) if source[i:i+n] == tokens]
    if len(matches) != 1 or len(words)-n < 3:
        return None
    i = matches[0]
    j = i+n-1
    if i == 0:
        a = t['cut_in']
    else:
        gl = words[i][0]-words[i-1][1]
        if gl < c['min_gap']: return None
        a = words[i-1][1]+min(gl,c['max_pause'])/2
    if j == len(words)-1:
        b = t['cut_out']
    else:
        gr = words[j+1][0]-words[j][1]
        if gr < c['min_gap']: return None
        b = words[j+1][0]-min(gr,c['max_pause'])/2
    return round(a,3),round(b,3)
