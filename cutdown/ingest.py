"""Stage 1: WhisperX diarized JSON -> units.jsonl + timeline.json + units.txt

Deterministic. No model involved. Units break on speaker turn, then on long
pause or max duration within a turn.

Outputs:
  units.jsonl   id, speaker, role, text, start, end, dur, gap_before, words
  timeline.json id -> {start, end, cut_in, cut_out, dur_cut}   (model never sees this)
  units.txt     model-facing index: ID + speaker role + text, no timestamps
  sections.json the recording split at long silences (see sections.py)

Folded in from the standalone ingest.py; unit boundaries and cut points are
unchanged. The one addition is the boundary repair pass (see repair_boundaries).
"""
from __future__ import annotations

import pathlib
from collections import Counter

from . import sections
from .util import CutdownError, log, note, write_json, write_jsonl

TERMINAL = '.?!…'


def fill_speakers(words):
    """Unlabelled words inherit from the previous labelled word, else the next."""
    last = None
    for w in words:
        if w.get("speaker"):
            last = w["speaker"]
        else:
            w["speaker"] = last
    nxt = None
    for w in reversed(words):
        if w.get("speaker"):
            nxt = w["speaker"]
        elif nxt:
            w["speaker"] = nxt
    return words


def identify_guest(words, guest_name):
    """Guest = speaker who utters the station ID, else the higher word count."""
    toks = [t.lower() for t in guest_name.split()]
    if toks:
        joined = [(w["speaker"], w["word"].lower().strip(".,!?")) for w in words]
        hits = Counter()
        for i, (spk, tok) in enumerate(joined):
            if tok == toks[-1] and i > 0 and joined[i - 1][1] == toks[0]:
                hits[spk] += 1
        if hits:
            return hits.most_common(1)[0][0]
    return Counter(w["speaker"] for w in words).most_common(1)[0][0]


def build_units(words, gap_split, max_dur):
    units, cur = [], []
    for i, w in enumerate(words):
        gap = w["start"] - words[i - 1]["end"] if i else 0.0
        turn_change = bool(cur) and w["speaker"] != cur[-1]["speaker"]
        too_long = bool(cur) and (w["end"] - cur[0]["start"]) > max_dur
        long_pause = bool(cur) and gap >= gap_split
        if turn_change or too_long or long_pause:
            units.append(cur)
            cur = []
        cur.append(w)
    if cur:
        units.append(cur)
    return units


def _ends_open(text: str) -> bool:
    """True when the text does not close a sentence, i.e. it is still running."""
    t = text.rstrip().rstrip('"”’\')')
    return bool(t) and t[-1] not in TERMINAL


def repair_boundaries(units, max_words=2, max_gap=1.20):
    """Pull back diarization boundaries that land one word late.

    Fast interjections make WhisperX flip the speaker one word early on the next
    turn: a HOST unit ending mid-sentence, then a one- or two-word unit under a
    different label that is plainly the tail of the HOST's sentence.

    A unit is repaired when all of:
      - it is <= max_words words
      - the previous unit's text has no terminal punctuation (still running)
      - the previous unit is a different speaker
      - the gap between them is under max_gap (a fast interjection, not a pause)

    Repaired words are relabelled to the previous unit's speaker; the caller
    rebuilds units from the corrected word labels, which also coalesces any
    same-speaker units the relabelling has made adjacent.

    Returns a list of repair records for the log.
    """
    repairs = []
    for i in range(1, len(units)):
        u, prev = units[i], units[i - 1]
        if len(u) > max_words:
            continue
        if u[0]["speaker"] == prev[-1]["speaker"]:
            continue
        prev_text = " ".join(w["word"] for w in prev).strip()
        if not _ends_open(prev_text):
            continue
        gap = u[0]["start"] - prev[-1]["end"]
        if gap >= max_gap:
            continue
        text = " ".join(w["word"] for w in u).strip()
        repairs.append({
            "at": round(u[0]["start"], 3),
            "gap": round(gap, 3),
            "from": u[0]["speaker"],
            "to": prev[-1]["speaker"],
            "text": text,
            "after": prev_text[-60:],
        })
        target = prev[-1]["speaker"]
        for w in u:
            w["speaker"] = target
    return repairs


def cut_points(units, words, min_gap=0.15, pad=0.08):
    pos = {}
    for n, w in enumerate(words):
        pos[id(w)] = n
    out = []
    for u in units:
        i0, i1 = pos[id(u[0])], pos[id(u[-1])]
        if i0 > 0:
            g = words[i0]["start"] - words[i0 - 1]["end"]
            back = g / 2 if g >= min_gap else max(0.0, min(pad, g / 2))
            cut_in = u[0]["start"] - back
        else:
            cut_in = max(0.0, u[0]["start"] - pad)
        if i1 < len(words) - 1:
            g = words[i1 + 1]["start"] - words[i1]["end"]
            fwd = g / 2 if g >= min_gap else max(0.0, min(pad, g / 2))
            cut_out = u[-1]["end"] + fwd
        else:
            cut_out = u[-1]["end"] + pad
        out.append((round(cut_in, 3), round(cut_out, 3)))
    return out


def question_stats(units) -> dict:
    """speaker -> word and question counts over word-list units."""
    stats = {}
    for unit in units:
        if not unit: continue
        row = stats.setdefault(unit[0]["speaker"], {"words":0,"questions":0})
        row["words"] += len(unit)
        row["questions"] += sum(str(w["word"]).rstrip('"”’\')]').endswith('?') for w in unit)
    return stats


def guest_by_questions(units) -> str | None:
    """Pick the lower question rate when exactly two speakers are decisive."""
    stats = question_stats(units)
    if len(stats) != 2: return None
    low, high = sorted(stats, key=lambda s: stats[s]["questions"] / max(1,stats[s]["words"]))
    a,b = stats[low],stats[high]
    if b["questions"] >= 5 and b["questions"] / max(1,b["words"]) >= 2 * a["questions"] / max(1,a["words"]):
        return low
    return None


def run_ingest(json_in: pathlib.Path, outdir: pathlib.Path, *, guest: str = "",
               gap_split: float = 1.20, max_dur: float = 25.0, min_gap: float = 0.15,
               pad: float = 0.08, repair: bool = True, repair_max_words: int = 2,
               section_gap: float = sections.DEFAULT_GAP_SEC,
               section_max: float = sections.DEFAULT_MAX_SEC,
               guest_speaker: str | None = None) -> dict:
    from .util import read_json

    d = read_json(json_in)
    if "word_segments" not in d:
        raise CutdownError(f"{json_in} has no 'word_segments' - is this diarized WhisperX output?")
    words = fill_speakers([w for w in d["word_segments"] if "start" in w and "end" in w])
    dropped = len(d["word_segments"]) - len(words)
    if dropped:
        log(f"dropped {dropped} words with no timestamp")
    if not words:
        raise CutdownError(f"{json_in} contains no timestamped words")

    # Split-channel recordings say who the guest is; otherwise work it out.
    speakers = {w["speaker"] for w in words}
    units = build_units(words, gap_split, max_dur)

    repairs = []
    if repair:
        repairs = repair_boundaries(units, max_words=repair_max_words, max_gap=gap_split)
        if repairs:
            units = build_units(words, gap_split, max_dur)
            for r in repairs:
                log(f"  repair @{r['at']:.3f}s gap {r['gap']:.3f}s "
                    f"{r['from']}->{r['to']}: ...{r['after']!r} + {r['text']!r}")

    by_questions = guest_by_questions(units)
    guest_label = guest_speaker if guest_speaker in speakers else (by_questions or identify_guest(words, guest))
    if guest_speaker not in speakers and by_questions:
        log("  guest picked by question rate")
    cuts = cut_points(units, words, min_gap=min_gap, pad=pad)

    outdir.mkdir(parents=True, exist_ok=True)
    timeline, jl, txt, unit_words = {}, [], [], {}
    for n, (u, (ci, co)) in enumerate(zip(units, cuts), 1):
        uid = f"S{n:04d}"
        spk = u[0]["speaker"]
        role = "GUEST" if spk == guest_label else "HOST"
        text = " ".join(w["word"] for w in u).strip()
        start, end = round(u[0]["start"], 3), round(u[-1]["end"], 3)
        gap_before = round(start - units[n - 2][-1]["end"], 3) if n > 1 else None
        jl.append({"id": uid, "speaker": spk, "role": role, "text": text,
                   "start": start, "end": end, "dur": round(end - start, 3),
                   "gap_before": gap_before, "words": len(u)})
        timeline[uid] = {"start": start, "end": end, "cut_in": ci, "cut_out": co,
                         "dur_cut": round(co - ci, 3)}
        txt.append(f"{uid}  {role}: {text}")
        # Word timings for stage 5 trim resolution. Never shown to a model.
        unit_words[uid] = [[round(w["start"], 3), round(w["end"], 3), w["word"]] for w in u]

    qs = question_stats(units)
    roles = {}
    for role in ("HOST", "GUEST"):
        selected = [r for r in jl if r["role"] == role]
        n = sum(r["words"] for r in selected)
        questions = sum(qs[s]["questions"] for s in qs if (s == guest_label) == (role == "GUEST"))
        roles[role.lower()] = {"words":n,"questions":questions,"minutes":round(sum(r["dur"] for r in selected)/60,3),"rate":100*questions/max(1,n)}
    g,h = roles["guest"],roles["host"]
    verdict = "swapped" if g["questions"] >= 5 and g["rate"] >= 2*h["rate"] else "ok" if h["questions"] >= 5 and h["rate"] >= 2*g["rate"] else "unclear"
    write_json(outdir / "roles.json", {"mode":"split" if guest_speaker else "mixed", "verdict":verdict, **roles})
    if verdict == "swapped":
        note(f"  WARNING the side labelled GUEST asks {g['rate']:.2f} questions per 100 words and the side labelled HOST asks {h['rate']:.2f}: host and guest look swapped")
    write_jsonl(outdir / "units.jsonl", jl)
    write_json(outdir / "timeline.json", timeline)
    write_json(outdir / "unit_words.json", unit_words, indent=0)
    (outdir / "units.txt").write_text("\n".join(txt) + "\n")
    if repairs:
        write_json(outdir / "repairs.json", repairs)
    secs = sections.detect_sections(jl, timeline, gap_sec=section_gap,
                                    max_sec=section_max)
    write_json(outdir / "sections.json", secs)

    guest_units = [r for r in jl if r["role"] == "GUEST"]
    guest_sec = sum(r["dur"] for r in guest_units)
    total_sec = sum(r["dur"] for r in jl)
    durs = sorted(r["dur"] for r in jl)
    stats = {
        "roles": verdict,
        "words": len(words), "units": len(jl), "guest_label": guest_label,
        "guest_units": len(guest_units), "guest_min": round(guest_sec / 60, 1),
        "host_units": len(jl) - len(guest_units),
        "host_min": round((total_sec - guest_sec) / 60, 1),
        "median_dur": durs[len(durs) // 2], "max_dur": durs[-1],
        "units_txt_chars": sum(len(t) + 1 for t in txt),
        "repairs": len(repairs),
        "speech_starts": jl[0]["start"] if jl else None,
        "sections": len(secs),
        "silences": sum(1 for x in secs if x["break"] == "silence"),
    }
    note(f"words {stats['words']}  units {stats['units']}  guest label {guest_label}")
    note(f"guest units {stats['guest_units']} ({stats['guest_min']:.1f} min)  "
         f"host units {stats['host_units']} ({stats['host_min']:.1f} min)")
    note(f"unit dur median {stats['median_dur']:.1f}s  max {stats['max_dur']:.1f}s")
    note(f"units.txt chars {stats['units_txt_chars']}  boundary repairs {len(repairs)}")
    note(f"sections {stats['sections']} ({stats['silences']} after a silence of "
         f"{section_gap:g}s+): {sections.describe(secs)}")
    return stats
