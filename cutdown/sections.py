"""Sections: the recording's own structure, found from silence.

A music interview recorded around the records has a shape before any model reads
it. When the room listens to a song that was not recorded, the transcript has a
long hole in it; that hole is the strongest signal there is that one piece of
talk ended and the next began. Sections are cut at those holes, and any section
still too long to scan is split at its widest pause.

Sections do three jobs:

  - They are printed into every model-facing index as headers, so discover,
    label, assemble and topics see where the song breaks fell.
  - They become the unedited chunk files in the output, so the raw material is
    browsable in pieces rather than as one long master.
  - Their ids (C01, C02, ...) are how a producer refers to "the bit after the
    third song" in a note.

Deterministic, no model involved. Timestamps stay here and in timeline.json; the
model-facing headers carry only the section id and the length of the silence,
never a position in the recording.
"""
from __future__ import annotations

from .util import clock

DEFAULT_GAP_SEC = 20.0
DEFAULT_MAX_SEC = 360.0
# A length split never lands on a pause shorter than this, and never leaves a
# piece shorter than MIN_PIECE of the section it came from.
MIN_SPLIT_GAP = 0.8
MIN_PIECE = 0.2


def _gap(units: list[dict], i: int) -> float:
    return float(units[i].get("gap_before") or 0.0) if i else 0.0


def _split_long(units: list[dict], lo: int, hi: int, max_sec: float) -> list[int]:
    """Indices inside [lo, hi) where a too-long run should break."""
    span = units[hi - 1]["end"] - units[lo]["start"]
    if span <= max_sec or hi - lo < 2:
        return []
    t0 = units[lo]["start"]
    best, best_gap = None, MIN_SPLIT_GAP
    for i in range(lo + 1, hi):
        at = (units[i]["start"] - t0) / span
        if not (MIN_PIECE <= at <= 1 - MIN_PIECE):
            continue
        g = _gap(units, i)
        if g > best_gap:
            best, best_gap = i, g
    if best is None:
        return []
    return (_split_long(units, lo, best, max_sec) + [best]
            + _split_long(units, best, hi, max_sec))


def detect_sections(units: list[dict], timeline: dict, *,
                    gap_sec: float = DEFAULT_GAP_SEC,
                    max_sec: float = DEFAULT_MAX_SEC) -> list[dict]:
    """Split the unit list into sections at long silences, then by length."""
    if not units:
        return []
    silences = [i for i in range(1, len(units)) if _gap(units, i) >= gap_sec]
    bounds = [0, *silences, len(units)]
    breaks: list[tuple[int, str]] = [(0, "start")]
    for lo, hi in zip(bounds, bounds[1:]):
        if lo:
            breaks.append((lo, "silence"))
        breaks += [(i, "length") for i in _split_long(units, lo, hi, max_sec)]
    breaks.sort()
    out = []
    for n, (lo, why) in enumerate(breaks):
        hi = breaks[n + 1][0] if n + 1 < len(breaks) else len(units)
        ids = [u["id"] for u in units[lo:hi]]
        first, last = units[lo], units[hi - 1]
        out.append({
            "id": f"C{n + 1:02d}",
            "ids": ids,
            "first": ids[0], "last": ids[-1],
            "start": first["start"], "end": last["end"],
            "cut_in": timeline[ids[0]]["cut_in"],
            "cut_out": timeline[ids[-1]]["cut_out"],
            "break": why,
            "gap_before": round(_gap(units, lo), 3),
        })
    return out


def section_of(sections: list[dict]) -> dict[str, str]:
    """unit id -> section id."""
    return {uid: s["id"] for s in sections for uid in s["ids"]}


def header(section: dict) -> str:
    """The line a model sees above a section. No positions, only the break."""
    if section["break"] == "silence":
        why = (f"after {round(section['gap_before'])}s of silence - likely a record "
               f"played in the room")
    elif section["break"] == "length":
        why = "continues the previous section after a pause"
    else:
        why = "start of the recording"
    return f"--- {section['id']} ({len(section['ids'])} units; {why}) ---"


def annotate(lines: list[tuple[str, str]], sections: list[dict] | None) -> str:
    """Join (unit id, line) pairs, with a section header wherever one starts.

    Only headers for sections whose first unit is present are printed, so a
    windowed or filtered index stays coherent.
    """
    starts = {s["first"]: s for s in sections or []}
    out = []
    for uid, line in lines:
        s = starts.get(uid)
        if s is not None:
            if out:
                out.append("")
            out.append(header(s))
        out.append(line)
    return "\n".join(out)


def outline(sections: list[dict]) -> str:
    """One line per section, for the prompts that plan rather than tag."""
    if not sections:
        return "  (no sections)"
    silences = sum(1 for s in sections if s["break"] == "silence")
    head = (f"  {len(sections)} sections; {silences} begin after a long silence "
            f"(a record played in the room, not recorded)." if silences else
            f"  {len(sections)} sections, split by length only: no long silences, "
            f"so the records were not played in the room during recording.")
    rows = [f"  {s['id']}  {s['first']}-{s['last']}  {len(s['ids'])} units"
            + ("  [after silence]" if s["break"] == "silence" else "")
            for s in sections]
    return "\n".join([head, *rows])


def describe(sections: list[dict]) -> str:
    """For the terminal: where the sections fell."""
    return "  ".join(f"{s['id']}@{clock(s['start'])}" for s in sections)


def load(build) -> list[dict]:
    """sections.json, or none for a build made before sections existed."""
    from .util import read_json
    p = build / "sections.json"
    return read_json(p) if p.exists() else []


def index(units: list[dict], sections: list[dict] | None,
          fmt=lambda u: f"{u['id']}  {u['role']}: {u['text']}") -> str:
    """The model-facing unit index, with section headers where they fall."""
    return annotate([(u["id"], fmt(u)) for u in units], sections)
