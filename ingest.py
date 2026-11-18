#!/usr/bin/env python3
"""Stage 1: WhisperX diarized JSON -> units.jsonl + timeline.json + units.txt

Deterministic. No model involved. Units break on speaker turn, then on long
pause or max duration within a turn.

Outputs:
  units.jsonl   id, speaker, role, text, start, end, dur, gap_before, words
  timeline.json id -> {start, end, cut_in, cut_out, dur_cut}   (model never sees this)
  units.txt     model-facing index: ID + speaker role + text, no timestamps
"""
import json, argparse, pathlib
from collections import Counter

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
            if tok == toks[-1] and i > 0 and joined[i-1][1] == toks[0]:
                hits[spk] += 1
        if hits:
            return hits.most_common(1)[0][0]
    return Counter(w["speaker"] for w in words).most_common(1)[0][0]

def build_units(words, gap_split, max_dur):
    units, cur = [], []
    for i, w in enumerate(words):
        gap = w["start"] - words[i-1]["end"] if i else 0.0
        turn_change = bool(cur) and w["speaker"] != cur[-1]["speaker"]
        too_long = bool(cur) and (w["end"] - cur[0]["start"]) > max_dur
        long_pause = bool(cur) and gap >= gap_split
        if turn_change or too_long or long_pause:
            units.append(cur); cur = []
        cur.append(w)
    if cur:
        units.append(cur)
    return units

def cut_points(units, words, min_gap=0.15, pad=0.08):
    pos = {}
    for n, w in enumerate(words):
        pos[id(w)] = n
    out = []
    for u in units:
        i0, i1 = pos[id(u[0])], pos[id(u[-1])]
        if i0 > 0:
            g = words[i0]["start"] - words[i0-1]["end"]
            back = g/2 if g >= min_gap else max(0.0, min(pad, g/2))
            cut_in = u[0]["start"] - back
        else:
            cut_in = max(0.0, u[0]["start"] - pad)
        if i1 < len(words) - 1:
            g = words[i1+1]["start"] - words[i1]["end"]
            fwd = g/2 if g >= min_gap else max(0.0, min(pad, g/2))
            cut_out = u[-1]["end"] + fwd
        else:
            cut_out = u[-1]["end"] + pad
        out.append((round(cut_in, 3), round(cut_out, 3)))
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("json_in")
    ap.add_argument("-o", "--outdir", default="build")
    ap.add_argument("--guest", default="", help="guest name, used to auto-detect their speaker label")
    ap.add_argument("--gap-split", type=float, default=1.20)
    ap.add_argument("--max-dur", type=float, default=25.0)
    args = ap.parse_args()

    d = json.load(open(args.json_in))
    words = fill_speakers([w for w in d["word_segments"] if "start" in w and "end" in w])
    guest = identify_guest(words, args.guest)

    units = build_units(words, args.gap_split, args.max_dur)
    cuts = cut_points(units, words)

    outdir = pathlib.Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    timeline, jl, txt = {}, [], []
    for n, (u, (ci, co)) in enumerate(zip(units, cuts), 1):
        uid = f"S{n:04d}"
        spk = u[0]["speaker"]
        role = "GUEST" if spk == guest else "HOST"
        text = " ".join(w["word"] for w in u).strip()
        start, end = round(u[0]["start"], 3), round(u[-1]["end"], 3)
        gap_before = round(start - units[n-2][-1]["end"], 3) if n > 1 else None
        jl.append({"id": uid, "speaker": spk, "role": role, "text": text,
                   "start": start, "end": end, "dur": round(end - start, 3),
                   "gap_before": gap_before, "words": len(u)})
        timeline[uid] = {"start": start, "end": end, "cut_in": ci, "cut_out": co,
                         "dur_cut": round(co - ci, 3)}
        txt.append(f"{uid}  {role}: {text}")

    (outdir/"units.jsonl").write_text("\n".join(json.dumps(r) for r in jl) + "\n")
    (outdir/"timeline.json").write_text(json.dumps(timeline, indent=1))
    (outdir/"units.txt").write_text("\n".join(txt) + "\n")

    durs = sorted(r["dur"] for r in jl)
    g = [r for r in jl if r["role"] == "GUEST"]
    print(f"words {len(words)}  units {len(jl)}  guest label {guest}")
    print(f"guest units {len(g)} ({sum(r['dur'] for r in g)/60:.1f} min)  "
          f"host units {len(jl)-len(g)} ({(sum(r['dur'] for r in jl)-sum(r['dur'] for r in g))/60:.1f} min)")
    print(f"unit dur median {durs[len(durs)//2]:.1f}s  max {durs[-1]:.1f}s")
    print(f"units.txt chars {sum(len(t)+1 for t in txt)}")

if __name__ == "__main__":
    main()
