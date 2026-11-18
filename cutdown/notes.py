"""Notes mode: a folder of exported clips -> a readable, shareable document.

The other half of the job. `cutdown` cuts an interview down; `cutdown notes`
reads an edit that has already been made in the DAW and hands back something you
can send to someone: a clean transcript per clip, with the anecdotes worth
knowing about called out and timestamped.

The rules are the pipeline's rules.

  - The transcript is verbatim ASR. Nothing is rewritten, reordered or tidied.
    Only the summaries and headings are generated, and the document says so.
  - The model reads ID-tagged paragraphs and returns IDs. Code owns every
    timestamp that reaches the page, measured from the audio.
  - A pull quote that is not in the transcript word for word is dropped, not
    printed. Checked here, not requested in the prompt.
  - Transcription is cached per clip, so re-running to iterate on the summaries
    costs a model call, not another hour of WhisperX.

Outputs, under <outdir>:
  notes.md notes.txt notes.json    the document
  transcripts/<clip>.txt           one clip each, to paste on its own
  build/notes/                     per-clip whisper JSON, read JSON, summaries
  runs/                            raw model I/O
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from datetime import datetime

from . import prompts
from .ingest import fill_speakers
from .providers import get_provider, request_json, stage_options
from .util import CutdownError, clock, log, note, read_json, write_json

AUDIO_EXT = [".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a", ".mp4", ".mov"]

# Removed only when --drop-fillers is given, and only as whole words. Anything
# that could be a real word in a real sentence stays.
FILLERS = {"um", "umm", "ummm", "uh", "uhh", "uhhh", "erm", "mm", "mmm", "hm", "hmm"}

TERMINAL = ".?!…"

SUMMARY_SCHEMA = {
    "type": "object",
    "required": ["headline", "summary", "anecdotes"],
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        # diarization letter -> "guest" | "host" | "other"
        "speakers": {"type": "object"},
        "anecdotes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "what", "ids"],
                "properties": {
                    "title": {"type": "string"},
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                    "strength": {"type": "string",
                                 "enum": ["strong", "good", "thin"]},
                    "ids": {"type": "array", "minItems": 1,
                            "items": {"type": "string"}},
                },
            },
        },
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "text"],
                "properties": {
                    "id": {"type": "string"},
                    "text": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
    },
}

DIGEST_SCHEMA = {
    "type": "object",
    "required": ["summary", "highlights"],
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "themes": {"type": "array", "items": {"type": "string"}},
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["clip", "what"],
                "properties": {
                    "clip": {"type": "string"},
                    "what": {"type": "string"},
                    "why": {"type": "string"},
                },
            },
        },
    },
}


# ---------------------------------------------------------------- clips in

def find_clips(paths: list[pathlib.Path],
               extensions: list[str] | None = None) -> list[pathlib.Path]:
    """Resolve files and directories to an ordered, de-duplicated clip list.

    Directories are read one level deep: a bounce folder is flat, and walking
    into `_previous/` or a nested session would quietly transcribe the wrong
    audio for an hour before anyone noticed.
    """
    exts = {("." + e.lstrip(".")).lower() for e in (extensions or AUDIO_EXT)}
    found: list[pathlib.Path] = []
    for raw in paths:
        p = pathlib.Path(raw).expanduser()
        if p.is_dir():
            hits = sorted(q for q in p.iterdir()
                          if q.is_file() and not q.name.startswith(".")
                          and q.suffix.lower() in exts)
            if not hits:
                raise CutdownError(
                    f"{p} holds no audio files. Looked for "
                    f"{', '.join(sorted(exts))} in that directory only, not "
                    f"below it.")
            found += hits
        elif p.is_file():
            found.append(p)
        else:
            raise CutdownError(f"not found: {p}")

    seen: set[pathlib.Path] = set()
    clips: list[pathlib.Path] = []
    for q in found:
        r = q.resolve()
        if r not in seen:
            seen.add(r)
            clips.append(r)
    if not clips:
        raise CutdownError("no clips to read")

    by_stem: dict[str, list[pathlib.Path]] = {}
    for c in clips:
        by_stem.setdefault(c.stem, []).append(c)
    clash = {k: v for k, v in by_stem.items() if len(v) > 1}
    if clash:
        first = next(iter(clash))
        raise CutdownError(
            f"two clips share the filename {first!r}, and every artifact here is "
            f"named after the clip:\n  "
            + "\n  ".join(str(p) for p in clash[first])
            + "\nRename one, or pass the folders separately.")
    return clips


def clip_facts(path: pathlib.Path) -> dict:
    """Duration, format and the BWF stamp, read straight off the file.

    The stamp is the clip's position in the session the edit was made in, so it
    is what lets the document say where in the interview each clip came from.
    Absent for a plain export, and that is not an error.
    """
    from .wav import WavInfo
    blank = {"sample_rate": None, "channels": None, "bits_per_sample": None,
             "duration": None, "time_reference": None, "master_sec": None}
    try:
        w = WavInfo(path)
    except CutdownError as e:
        log(f"  {path.name}: no WAV header ({e}); timings come from the transcript")
        return blank
    tr = w.bext_time_reference if w.has_bext else None
    return {
        "sample_rate": w.sample_rate,
        "channels": w.channels,
        "bits_per_sample": w.bits_per_sample,
        "duration": round(w.duration, 3),
        "time_reference": tr,
        "master_sec": (round(tr / w.sample_rate, 3)
                       if tr is not None and w.sample_rate else None),
    }


# ---------------------------------------------------------------- reading

def _words_from(doc: dict) -> list[dict]:
    """Word-level timings if WhisperX produced them, segment-level if not."""
    ws = [dict(w) for w in doc.get("word_segments", [])
          if "start" in w and "end" in w]
    if ws:
        return fill_speakers(ws)
    out = []
    for s in doc.get("segments", []):
        if "start" not in s or "end" not in s:
            continue
        text = str(s.get("text", "")).strip()
        if text:
            out.append({"word": text, "start": s["start"], "end": s["end"],
                        "speaker": s.get("speaker")})
    return fill_speakers(out)


def _is_filler(word: str) -> bool:
    return re.sub(r"[^\w']", "", word).lower() in FILLERS


def _ends_sentence(word: str) -> bool:
    t = word.rstrip().rstrip('"”’\')')
    return bool(t) and t[-1] in TERMINAL


def split_paragraphs(words: list[dict], *, gap: float = 1.60,
                     max_sec: float = 40.0, min_words: int = 12) -> list[list[dict]]:
    """Group words into paragraphs a person can read.

    Break on a speaker change always; on a pause once the paragraph has enough
    in it to stand as one; and on length, preferring a sentence end but taking a
    hard break at twice the limit so a monologue never arrives as a wall.
    """
    paras: list[list[dict]] = []
    cur: list[dict] = []
    for i, w in enumerate(words):
        if cur:
            gap_before = w["start"] - words[i - 1]["end"]
            dur = words[i - 1]["end"] - cur[0]["start"]
            if (w["speaker"] != cur[-1]["speaker"]
                    or (gap_before >= gap and len(cur) >= min_words)
                    or (dur >= max_sec and _ends_sentence(cur[-1]["word"]))
                    or dur >= max_sec * 2):
                paras.append(cur)
                cur = []
        cur.append(w)
    if cur:
        paras.append(cur)
    return paras


def _letter(n: int) -> str:
    return chr(ord("A") + n) if n < 26 else f"S{n + 1}"


def read_clip(path: pathlib.Path, whisper_json: pathlib.Path, *, gap: float,
              max_sec: float, drop_fillers: bool = False) -> dict:
    """WhisperX JSON + the clip itself -> the readable, ID-tagged clip record."""
    words = _words_from(read_json(whisper_json))
    if drop_fillers:
        kept = [w for w in words if not _is_filler(str(w.get("word", "")))]
        if len(kept) != len(words):
            log(f"  {path.name}: dropped {len(words) - len(kept)} filler words")
        words = kept

    facts = clip_facts(path)
    # Diarization labels are per clip and meaningless across clips, so they are
    # shown to the model as bare letters and named later.
    order: list[str] = []
    for w in words:
        spk = w.get("speaker") or "SPEAKER_00"
        w["speaker"] = spk
        if spk not in order:
            order.append(spk)
    letters = {spk: _letter(i) for i, spk in enumerate(order)}

    paragraphs = []
    for n, para in enumerate(split_paragraphs(words, gap=gap, max_sec=max_sec), 1):
        paragraphs.append({
            "id": f"P{n:02d}",
            "speaker": letters[para[0]["speaker"]],
            "start": round(para[0]["start"], 3),
            "end": round(para[-1]["end"], 3),
            "text": " ".join(str(w["word"]).strip() for w in para).strip(),
            "words": [[round(w["start"], 3), round(w["end"], 3), str(w["word"])]
                      for w in para],
        })

    speakers: dict[str, dict] = {}
    for spk, letter in letters.items():
        mine = [w for w in words if w["speaker"] == spk]
        speakers[letter] = {
            "raw": spk,
            "words": len(mine),
            "sec": round(sum(w["end"] - w["start"] for w in mine), 3),
        }

    duration = facts["duration"]
    if duration is None:
        duration = round(paragraphs[-1]["end"], 3) if paragraphs else 0.0

    return {
        "clip": path.name,
        "stem": path.stem,
        "path": str(path),
        **facts,
        "duration": duration,
        "words": len(words),
        "speakers": speakers,
        "paragraphs": paragraphs,
    }


def order_clips(clips: list[dict], how: str = "time") -> list[dict]:
    """Session order when every clip carries its BWF stamp, filename order else."""
    by_name = sorted(clips, key=lambda c: c["stem"].lower())
    if how == "name":
        return by_name
    if clips and all(c.get("master_sec") is not None for c in clips):
        return sorted(clips, key=lambda c: c["master_sec"])
    if clips:
        log("  not every clip carries a BWF timestamp; ordering by filename")
    return by_name


# ---------------------------------------------------------------- verbatim guard

def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s']", " ", str(text).lower())).strip()


def locate(words: list[list], quote: str) -> tuple[float, float] | None:
    """Word-accurate (start, end) of a verbatim quote inside a paragraph.

    Matching is on normalised text, so ASR punctuation and case do not decide
    whether a real quote survives - but the words themselves must be there, in
    that order. Returns None when they are not.
    """
    want = _norm(quote)
    if not want:
        return None
    toks, spans = [], []
    for i, w in enumerate(words):
        for t in _norm(w[2]).split():
            toks.append(t)
            spans.append(i)
    if not toks:
        return None
    hay = " ".join(toks)
    at = hay.find(want)
    if at == -1:
        return None
    before = hay[:at].count(" ") + (1 if at else 0)
    length = want.count(" ") + 1
    a, b = spans[before], spans[min(before + length - 1, len(spans) - 1)]
    return (words[a][0], words[b][1])


def resolve_summary(raw: dict, clip: dict) -> dict:
    """Turn the model's IDs into measured times, and drop what cannot be checked.

    Anecdotes keep only paragraph IDs that exist; a quote survives only if its
    words are in the paragraph it was attributed to.
    """
    paras = {p["id"]: p for p in clip["paragraphs"]}
    out = {
        "headline": str(raw.get("headline", "")).strip(),
        "summary": str(raw.get("summary", "")).strip(),
        "topics": [str(t) for t in (raw.get("topics") or [])],
        "anecdotes": [],
        "quotes": [],
        "dropped": {"anecdotes": 0, "ids": 0, "quotes": 0},
    }

    for a in raw.get("anecdotes") or []:
        ids = [i for i in (a.get("ids") or []) if i in paras]
        out["dropped"]["ids"] += len(a.get("ids") or []) - len(ids)
        if not ids:
            out["dropped"]["anecdotes"] += 1
            continue
        out["anecdotes"].append({
            "title": str(a.get("title", "")).strip(),
            "what": str(a.get("what", "")).strip(),
            "why": str(a.get("why", "")).strip(),
            "strength": a.get("strength", ""),
            "ids": ids,
            "start": min(paras[i]["start"] for i in ids),
            "end": max(paras[i]["end"] for i in ids),
        })
    out["anecdotes"].sort(key=lambda a: a["start"])

    for q in raw.get("quotes") or []:
        text = str(q.get("text", "")).strip().strip('"“”')
        para = paras.get(q.get("id", ""))
        span = locate(para["words"], text) if para else None
        if span is None:
            # It may be a real quote filed under the wrong paragraph.
            for p in clip["paragraphs"]:
                span = locate(p["words"], text)
                if span:
                    para = p
                    break
        if span is None or para is None:
            out["dropped"]["quotes"] += 1
            continue
        out["quotes"].append({"id": para["id"], "text": text, "why":
                              str(q.get("why", "")).strip(),
                              "start": round(span[0], 3), "end": round(span[1], 3)})
    out["quotes"].sort(key=lambda q: q["start"])

    roles = {}
    given = raw.get("speakers") if isinstance(raw.get("speakers"), dict) else {}
    for letter in clip["speakers"]:
        role = str(given.get(letter, "")).strip().lower()
        roles[letter] = role if role in ("guest", "host", "other") else ""
    out["speakers"] = roles
    return out


def name_speakers(clip: dict, guest: str, host: str) -> dict:
    """Letter -> display name, from the model's reading, falling back to volume.

    In an edited interview clip the person being interviewed is almost always
    the one who says the most, which is the fallback when there is no summary to
    read the roles from.
    """
    roles = dict((clip.get("summary") or {}).get("speakers") or {})
    if not any(roles.get(k) for k in clip["speakers"]):
        loudest = max(clip["speakers"], key=lambda k: clip["speakers"][k]["words"],
                      default=None)
        roles = {k: ("guest" if k == loudest else "host") for k in clip["speakers"]}
    names = {}
    for letter in clip["speakers"]:
        role = roles.get(letter) or "other"
        if role == "guest":
            names[letter] = guest or "Guest"
        elif role == "host":
            names[letter] = host or "Host"
        else:
            names[letter] = f"Speaker {letter}"
    return names


# ---------------------------------------------------------------- model stages

def _index(clip: dict) -> str:
    return "\n".join(f"{p['id']}  {p['speaker']}: {p['text']}"
                     for p in clip["paragraphs"])


def _read_hash(clip: dict) -> str:
    blob = "\n".join(f"{p['id']}|{p['speaker']}|{p['text']}"
                     for p in clip["paragraphs"])
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def summarise_clip(clip: dict, *, provider, model, ctx, run_dir: pathlib.Path,
                   tag: str, dry_run: bool = False,
                   prompts_dir: pathlib.Path | None = None, extra_body=None) -> dict | None:
    system = prompts.load("notes.system", prompts_dir)
    user = prompts.render("notes.user", prompts_dir, context=ctx.block(),
                          clip=clip["clip"], count=len(clip["paragraphs"]),
                          length=clock(clip["duration"]), paragraphs=_index(clip))
    res = request_json(provider, model=model, system=system, user=user,
                       extra_body=extra_body, schema=SUMMARY_SCHEMA, run_dir=run_dir, tag=tag,
                       dry_run=dry_run)
    if dry_run:
        return None
    return resolve_summary(res.data, clip)


def _digest_index(clips: list[dict]) -> str:
    blocks = []
    for c in clips:
        s = c.get("summary") or {}
        lines = [f"{c['cid']}  {c['clip']}  ({clock(c['duration'])})",
                 f"  headline: {s.get('headline', '(none)')}",
                 f"  summary: {s.get('summary', '')}"]
        for a in s.get("anecdotes") or []:
            lines.append(f"  anecdote [{a.get('strength', '?')}]: "
                         f"{a['title']} - {a['what']}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def run_digest(clips: list[dict], *, config, ctx, run_dir: pathlib.Path,
               cache: pathlib.Path | None = None, force: bool = False,
               dry_run: bool = False,
               prompts_dir: pathlib.Path | None = None) -> dict | None:
    """One pass over every clip note, for the top of the document."""
    usable = [c for c in clips if c.get("summary")]
    if len(usable) < 2:
        return None
    provider, model = get_provider(config, "digest")
    key = hashlib.sha256(_digest_index(usable).encode()).hexdigest()[:16]
    if cache is not None and cache.exists() and not force:
        try:
            prev = read_json(cache)
        except CutdownError:
            prev = {}
        if prev.get("key") == key and prev.get("digest", {}).get(
                "provider") == f"{provider.name}/{model}":
            log("  digest: up to date")
            return prev["digest"]
    note(f"  digest: {len(usable)} clips -> {provider.describe()} {model}")
    system = prompts.load("digest.system", prompts_dir)
    user = prompts.render("digest.user", prompts_dir, context=ctx.block(),
                          count=len(usable), clips=_digest_index(usable))
    res = request_json(provider, model=model, system=system, user=user,
                       **stage_options(config, "digest"), schema=DIGEST_SCHEMA, run_dir=run_dir, tag="digest",
                       dry_run=dry_run)
    if dry_run:
        return None
    by_cid = {c["cid"]: c for c in usable}
    highlights = []
    for h in res.data.get("highlights") or []:
        c = by_cid.get(str(h.get("clip", "")).strip())
        if c is None:
            continue
        highlights.append({"cid": c["cid"], "clip": c["clip"], "n": c["n"],
                           "what": str(h.get("what", "")).strip(),
                           "why": str(h.get("why", "")).strip()})
    out = {
        "title": str(res.data.get("title", "")).strip(),
        "summary": str(res.data.get("summary", "")).strip(),
        "themes": [str(t) for t in (res.data.get("themes") or [])],
        "highlights": highlights,
        "provider": f"{provider.name}/{model}",
    }
    if cache is not None:
        write_json(cache, {"key": key, "digest": out})
    return out


# ---------------------------------------------------------------- the run

def run_notes(clips_in: list[pathlib.Path], build: pathlib.Path,
              outdir: pathlib.Path, config, ctx, *, run_dir: pathlib.Path,
              host: str = "", order: str = "time", drop_fillers: bool = False,
              summarise: bool = True, digest: bool = True, force: bool = False,
              resummarise: bool = False, dry_run: bool = False,
              prompts_dir: pathlib.Path | None = None) -> dict:
    from .notesdoc import render_clip_text, render_markdown, render_text
    from .transcribe import run_transcribe

    cfg = config.get("notes", {}) or {}
    paths = find_clips(clips_in, cfg.get("extensions"))
    work = build / "notes"
    work.mkdir(parents=True, exist_ok=True)

    # Clips are short and often one voice. WhisperX's interview defaults will
    # split a solo answer into two speakers, so the notes overlay relaxes them.
    tcfg = dict(config.get("transcribe", {}) or {})
    tcfg.update(cfg.get("transcribe", {}) or {})

    note(f"notes: {len(paths)} clip(s) -> {outdir}")
    entries: list[dict] = []
    for i, path in enumerate(paths, 1):
        note(f"[{i}/{len(paths)}] {path.name}")
        wj = run_transcribe(path, work, tcfg, dry_run=dry_run, recover=not force)
        if dry_run and not wj.exists():
            continue
        clip = read_clip(path, wj, gap=float(cfg.get("para_gap", 1.60)),
                         max_sec=float(cfg.get("para_max_sec", 40.0)),
                         drop_fillers=drop_fillers or bool(cfg.get("drop_fillers")))
        write_json(work / f"{path.stem}.read.json", clip, indent=0)
        note(f"      {clock(clip['duration'])}  {clip['words']} words  "
             f"{len(clip['paragraphs'])} paragraphs  "
             f"{len(clip['speakers'])} speaker(s)"
             + (f"  master {clock(clip['master_sec'])}"
                if clip["master_sec"] is not None else ""))
        entries.append(clip)

    if not entries:
        if dry_run:
            raise CutdownError(
                "--dry-run has nothing to write prompts from: no clip has been "
                "transcribed yet, and dry-run does not transcribe. Run it once "
                "for real, then iterate on the prompts with --dry-run.")
        raise CutdownError("no clip produced a transcript")

    entries = order_clips(entries, cfg.get("order", order))
    for n, c in enumerate(entries, 1):
        c["n"] = n
        c["cid"] = f"C{n:02d}"

    provider_label = ""
    if summarise:
        provider, model = get_provider(config, "notes")
        provider_label = f"{provider.name}/{model}"
        note(f"  summaries -> {provider.describe()} {model}")
        for c in entries:
            cache = work / f"{c['stem']}.summary.json"
            rh = _read_hash(c)
            if not c["paragraphs"]:
                note(f"  {c['cid']} {c['clip']}: no speech, nothing to summarise")
                c["summary"] = None
                continue
            if cache.exists() and not resummarise and not force:
                try:
                    prev = read_json(cache)
                except CutdownError:
                    prev = {}
                if (prev.get("read_hash") == rh
                        and prev.get("provider") == provider_label):
                    c["summary"] = prev["summary"]
                    log(f"  {c['cid']} {c['clip']}: summary up to date")
                    continue
            s = summarise_clip(c, provider=provider, model=model, ctx=ctx,
                               run_dir=run_dir, tag=f"notes.{c['cid']}", **stage_options(config, "notes"),
                               dry_run=dry_run, prompts_dir=prompts_dir)
            c["summary"] = s
            if s is None:
                continue
            write_json(cache, {"read_hash": rh, "provider": provider_label,
                               "summary": s})
            d = s["dropped"]
            note(f"  {c['cid']} {c['clip']}: {len(s['anecdotes'])} anecdote(s), "
                 f"{len(s['quotes'])} quote(s)"
                 + (f"  [dropped {d['quotes']} unverifiable quote(s)]"
                    if d["quotes"] else "")
                 + (f"  [dropped {d['anecdotes']} anecdote(s) with no real IDs]"
                    if d["anecdotes"] else ""))
    else:
        for c in entries:
            c["summary"] = None

    for c in entries:
        c["names"] = name_speakers(c, ctx.guest, host)

    doc = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "context": {"guest": ctx.guest, "show": ctx.show, "episode": ctx.episode,
                    "host": host},
        "transcriber": f"whisperx {tcfg.get('model', 'large-v3')}",
        "provider": provider_label,
        "clips": entries,
        "digest": None,
        "totals": {
            "clips": len(entries),
            "sec": round(sum(c["duration"] for c in entries), 3),
            "words": sum(c["words"] for c in entries),
            "anecdotes": sum(len((c.get("summary") or {}).get("anecdotes") or [])
                             for c in entries),
        },
    }

    if summarise and digest:
        doc["digest"] = run_digest(entries, config=config, ctx=ctx, run_dir=run_dir,
                                   cache=work / "digest.json",
                                   # --dry-run exists to see the prompt; a cache
                                   # hit would write nothing to look at.
                                   force=force or resummarise or dry_run,
                                   dry_run=dry_run,
                                   prompts_dir=prompts_dir)

    if dry_run:
        note("--dry-run: prompts written, nothing sent, no document written.")
        return doc

    outdir.mkdir(parents=True, exist_ok=True)
    # Word timings are how a quote gets its position; they are build detail, and
    # they would be most of notes.json by weight. They stay in the read JSON.
    write_json(outdir / "notes.json", {
        **doc,
        "clips": [{**c, "paragraphs": [{k: v for k, v in p.items() if k != "words"}
                                       for p in c["paragraphs"]]}
                  for c in entries],
    })
    (outdir / "notes.md").write_text(render_markdown(doc))
    (outdir / "notes.txt").write_text(render_text(doc))
    tdir = outdir / "transcripts"
    tdir.mkdir(parents=True, exist_ok=True)
    for c in entries:
        (tdir / f"{c['stem']}.txt").write_text(render_clip_text(c, doc))

    note(f"  {doc['totals']['clips']} clips  {clock(doc['totals']['sec'])}  "
         f"{doc['totals']['words']} words  {doc['totals']['anecdotes']} anecdotes")
    note(f"  -> {outdir / 'notes.md'}")
    note(f"  -> {outdir / 'notes.txt'}   (paste this one anywhere)")
    note(f"  -> {tdir}/  one txt per clip")
    return doc
