"""Stage 5: segments + topics + sections + master WAV -> the output folders.

    1-final/    one stitched file per song segment, then one per topic
    2-clips/    the individual clips behind each final file, a folder each
    3-chunks/   the recording, unedited, cut at its sections and named for
                what is in each

No model involved. Kept spans come from spans.py and timeline.json, measured
from inter-word gaps at ingest, so every cut lands in silence. Audio is copied at the
byte level, never decoded, so bit depth and sample rate survive exactly and clip
lengths are sample-exact.
"""
from __future__ import annotations

import csv
import pathlib
import re
import shutil
import unicodedata

from . import bwf, spans
from .spans import _trim_point  # noqa: F401  (tests import it from here)
from .util import CutdownError, hhmmss, log, note, read_json, read_jsonl, write_json
from .wav import WavInfo, write_wav


def slugify(text: str, max_len: int = 40) -> str:
    """Filename-safe, readable, stable. Empty in, empty out."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode()
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rsplit("-", 1)[0] or text[:max_len]
    return text


def _name(template: str, **tokens) -> str:
    """Render a filename template, dropping empty tokens rather than leaving gaps."""
    try:
        out = template.format(**tokens)
    except (KeyError, IndexError, ValueError) as e:
        raise CutdownError(
            f"bad filename template {template!r}: {e}. Available tokens: "
            f"{', '.join(sorted(tokens))}"
        )
    out = re.sub(r"_{2,}", "_", out).strip("_-")
    return _filename(out or "clip")


def _filename(name: str) -> str:
    if not name or name in {".", ".."} or any(c in name for c in "/\\\0"):
        raise CutdownError("render filenames and folder names must be single path components")
    return name


def _contiguous_runs(order: list[str], cuts) -> list[list[str]]:
    """Group units that are adjacent in the master into single clips.

    Two units are adjacent when the second's cut_in is the first's cut_out - i.e.
    ingest split them but no audio sits between them. Rendering such a run as one
    clip means no join, so nothing to hear.

    `cuts` must resolve to the *effective* cut points, after trims. A clip spans
    from its first unit's cut_in to its last unit's cut_out, so a trim on any
    other unit in the run could not be applied to the audio. Comparing effective
    cuts makes a trimmed edge fail the adjacency test, which splits the run at
    exactly the point the trim needs - the trim then lands on a clip boundary
    where it takes effect.
    """
    runs: list[list[str]] = []
    for uid in order:
        if runs:
            prev = runs[-1][-1]
            if abs(cuts(prev)["cut_out"] - cuts(uid)["cut_in"]) < 1e-6:
                runs[-1].append(uid)
                continue
        runs.append([uid])
    return runs



def apply_trims(segments: dict, timeline: dict, unit_words: dict, *,
                min_gap: float = 0.15, window: float = 2.5) -> dict:
    """Resolve legacy head/tail flags into real cut points.

    Returns {unit_id: {"cut_in":…, "cut_out":…, "trimmed":…}} for units that moved.
    """
    effective: dict = {}
    for seg in segments["segments"]:
        for uid, flags in (seg.get("trim") or {}).items():
            if uid not in timeline:
                continue
            words = unit_words.get(uid) or []
            cur = effective.setdefault(uid, dict(timeline[uid]))
            moved = []
            if flags.get("head"):
                p = _trim_point(words, "head", min_gap, window)
                if p and p > cur["cut_in"] and p < cur["cut_out"]:
                    cur["cut_in"] = p
                    moved.append("head")
                else:
                    log(f"  {uid}: no isolated head filler with a safe gap - left alone")
            if flags.get("tail"):
                p = _trim_point(words, "tail", min_gap, window)
                if p and p < cur["cut_out"] and p > cur["cut_in"]:
                    cur["cut_out"] = p
                    moved.append("tail")
                else:
                    log(f"  {uid}: no isolated tail filler with a safe gap - left alone")
            if moved:
                cur["trimmed"] = "+".join(moved)
                cur["dur_cut"] = round(cur["cut_out"] - cur["cut_in"], 3)
            else:
                effective.pop(uid, None)
    return effective


def _stamp_time(seconds: float) -> str:
    """12m40s / 1h02m05s - sorts, reads, and is safe in a filename."""
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else f"{m:02d}m{sec:02d}s"


def retained_text(uid: str, units: dict, unit_words: dict, effective: dict) -> str:
    """The words actually retained by the same trim resolver used for audio."""
    if uid not in effective or not unit_words.get(uid):
        return units.get(uid, {}).get("text", "")
    cut = effective[uid]
    return " ".join(w[2] for w in unit_words[uid]
                    if w[1] > cut["cut_in"] and w[0] < cut["cut_out"])


def chunk_labels(sections: list[dict], groups: list[dict], labels: dict,
                 timeline: dict, max_names: int = 2) -> dict[str, str]:
    """Name each section for what fills it, by measured duration.

    A unit counts toward the segment or topic it was cut into; failing that,
    toward the record its label names; admin toward "production-talk". The
    names holding at least a quarter of the section's talk are used, largest
    first.
    """
    owner: dict[str, str] = {}
    track_title = {}
    for g in groups:
        title = (g.get("anchor") or {}).get("title", "") or g["id"]
        track_title[g["id"]] = title
        for uid in g.get("order") or g.get("ids") or []:
            owner[uid] = title
    out = {}
    for sec in sections:
        weight: dict[str, float] = {}
        total = 0.0
        for uid in sec["ids"]:
            d = timeline.get(uid, {}).get("dur_cut", 0.0)
            total += d
            lab = labels.get(uid, {})
            name = owner.get(uid)
            if name is None:
                if lab.get("role") == "admin":
                    name = "production talk"
                else:
                    tags = [track_title.get(t, t) for t in lab.get("topics") or []]
                    name = next((t for t in tags if t not in ("general",)), "") \
                        or ("own work" if "own_work" in (lab.get("topics") or [])
                            else "general")
                    name = name.replace("own_work", "own work")
            weight[name] = weight.get(name, 0.0) + d
        ranked = [n for n, w in sorted(weight.items(), key=lambda x: -x[1])
                  if total and w / total >= 0.25][:max_names]
        out[sec["id"]] = "+".join(slugify(n, 28) for n in ranked if slugify(n, 28)) \
            or "talk"
    return out


def check_exclusivity(segments: dict) -> None:
    seen: dict[str, str] = {}
    for seg in segments["segments"]:
        for uid in seg.get("order") or seg.get("ids") or []:
            if uid in seen:
                raise CutdownError(
                    f"unit {uid} appears in both {seen[uid]} and {seg['id']}. "
                    f"Duplicated audio across segments is audible; refusing to render."
                )
            seen[uid] = seg["id"]


def check_no_admin(segments: dict, labels_path: pathlib.Path) -> list[str]:
    if not labels_path.exists():
        log("  no labels.jsonl - skipping the admin check")
        return []
    rows = read_jsonl(labels_path)
    admin = {r["id"] for r in rows if r.get("role") == "admin"}
    unlabelled = {r["id"] for r in rows if r.get("role") == "unlabelled"}
    bad, unvetted = [], []
    for seg in segments["segments"]:
        for uid in seg.get("order") or seg.get("ids") or []:
            if uid in admin:
                bad.append(f"{seg['id']}:{uid}")
            elif uid in unlabelled:
                unvetted.append(f"{seg['id']}:{uid}")
    if bad:
        raise CutdownError(
            "admin-tagged units reached a clip: " + ", ".join(bad) +
            ". These are mic checks, level-setting or live direction and cannot go to air."
        )
    if unvetted:
        raise CutdownError(
            "units that never got a label reached a clip: " + ", ".join(unvetted) +
            ". They were never vetted for admin talk. Re-run label, or remove them "
            "from segments.json."
        )
    return sorted(admin)


def readable_master(master: pathlib.Path, build: pathlib.Path,
                    ffmpeg: str | None = None) -> tuple[pathlib.Path, WavInfo]:
    """The master as an uncompressed WAV the byte-level cutter can slice.

    A PCM/float WAV (RIFF, RF64, BW64) is used as it is. Anything else (MP3,
    M4A, FLAC, a compressed WAV) is decoded once with ffmpeg into
    build/master.wav at its own rate, channel count and bit depth, the same
    decoder WhisperX timed the words against, so cut points still line up.
    """
    try:
        return master, WavInfo(master)
    except CutdownError as e:
        why = str(e)
    note(f"master is not an uncompressed WAV ({why.split(': ', 1)[-1][:80]})")
    from .media import to_pcm_wav
    try:
        pcm = to_pcm_wav(master, build / "master.wav", None if ffmpeg == "ffmpeg" else ffmpeg)
    except CutdownError as e:
        raise CutdownError(f"{why}\nDecoding it with ffmpeg also failed: {e}")
    return pcm, WavInfo(pcm)


def _dirs(outdir: pathlib.Path, dirs: dict | None, *protected: pathlib.Path
          ) -> dict[str, pathlib.Path]:
    names = {"final": "1-final", "clips": "2-clips", "chunks": "3-chunks",
             **(dirs or {})}
    paths = {k: outdir / _filename(names[k]) for k in ("final", "clips", "chunks")}
    if len({p.name.casefold() for p in paths.values()}) != len(paths):
        raise CutdownError("render folders must have distinct names")
    protected = [p.resolve() for p in protected]
    for path in paths.values():
        if path.is_symlink():
            raise CutdownError(f"render folder must not be a symlink: {path}")
        resolved = path.resolve()
        if any(p.is_relative_to(resolved) for p in protected):
            raise CutdownError(f"render folder overlaps a source or build path: {path}")
    return paths


def run_render(build: pathlib.Path, outdir: pathlib.Path, master: pathlib.Path, *,
               stringout_gap_ms: int = 500, ffmpeg: str = "ffmpeg",
               skip_admin_check: bool = False,
               clip_template: str = "{seg}_{clip:02d}_{title}_{ids}",
               stringout_template: str = "{seg}_{title}",
               dirs: dict | None = None, chunks: bool = True, fillers: dict | None = None) -> dict:
    segments = read_json(build / "segments.json")
    timeline = read_json(build / "timeline.json")
    units = {r["id"]: r for r in read_jsonl(build / "units.jsonl")}
    unit_words = {}
    uw = build / "unit_words.json"
    if uw.exists():
        unit_words = read_json(uw)
    topics = []
    tp = build / "topics.json"
    if tp.exists():
        topics = read_json(tp).get("topics") or []
    sections = read_json(build / "sections.json") if (build / "sections.json").exists() else []
    lp = build / "labels.jsonl"
    labels = {r["id"]: r for r in read_jsonl(lp)} if lp.exists() else {}
    checks = {}
    cp = build / "check.json"
    if cp.exists():
        for w in read_json(cp).get("warnings", []):
            checks.setdefault(w.get("id") or w.get("segment", ""), []).append(
                f"[{w.get('kind', 'warning')}] {w.get('note', '')}")

    # Segments and topics draw from one pool: both checks run over the union.
    everything = {"segments": list(segments["segments"]) + list(topics)}
    check_exclusivity(everything)
    if not skip_admin_check:
        check_no_admin(everything, lp)

    if not master.exists():
        raise CutdownError(f"master not found: {master}")
    protected = [master, build, build / "transcript", build / "pre-revise", outdir / "runs"]
    d = _dirs(outdir, dirs, *protected)
    master, info = readable_master(master, build, ffmpeg)
    note(f"master: {info.describe()}")
    if info.has_bext:
        note(f"master bext TimeReference {info.bext_time_reference} "
             f"({info.bext_time_reference / info.sample_rate:.3f}s) - clip stamps are offset from it")

    for k in ("final", "clips"):
        if d[k].exists():
            shutil.rmtree(d[k])  # derived output; stale files would read as current
        d[k].mkdir(parents=True, exist_ok=True)

    rows, rejected_rows, seg_summaries, topic_summaries = [], [], [], []
    gap_frames = int(round(stringout_gap_ms / 1000 * info.sample_rate))

    def write_group(seg: dict, kind: str) -> dict | None:
        sid = seg["id"]
        order = seg.get("order") or seg.get("ids") or []
        if not order:
            note(f"{sid}: no units assigned - skipped")
            return None
        anchor = seg.get("anchor") or {}
        tokens = {
            "seg": sid,
            "title": slugify(anchor.get("title", "")),
            "artist": slugify(anchor.get("artist", "")),
            "kind": anchor.get("kind", ""),
            "episode": slugify(segments.get("episode", "")),
            "guest": slugify(segments.get("guest", "")),
        }
        final_name = _name(stringout_template, clip=0, ids="",
                           first_id="", last_id="", **tokens)
        clip_dir = d["clips"] / final_name
        clip_dir.mkdir(parents=True, exist_ok=True)
        for uid in order:
            if uid not in timeline:
                raise CutdownError(f"segment references unknown unit {uid} - segments.json and timeline.json are out of step")
        sp = spans.group_spans(seg, timeline, unit_words, fillers)
        plan = spans.clips(order, sp, timeline)
        seg_frames_total, clip_paths = 0, []
        for n, clip in enumerate(plan, 1):
            run = clip["units"]
            in_s = int(round(clip["in"] * info.sample_rate))
            out_s = int(round(clip["out"] * info.sample_rate))
            n_frames = out_s - in_s
            if n_frames <= 0:
                raise CutdownError(f"{sid} clip {n}: non-positive length "
                                   f"({run[0]}..{run[-1]})")
            ids = run[0] + (f"-{run[-1]}" if len(run) > 1 else "")
            name = _name(clip_template, clip=n, ids=ids,
                         first_id=run[0], last_id=run[-1], **tokens) + ".wav"
            path = clip_dir / name
            frames = info.read_frames(in_s, n_frames)
            got = len(frames) // info.block_align
            if got != n_frames:
                raise CutdownError(f"{name}: wanted {n_frames} frames, master gave {got}")
            tr = info.bext_time_reference + in_s
            # The description shows up in Pro Tools' clip info, so it carries the
            # same context as the filename plus the original master position.
            anchor_text = " - ".join(x for x in (anchor.get("artist", ""),
                                                 anchor.get("title", "")) if x)
            bext = bwf.build_bext(
                time_reference=tr,
                description=f"{sid} clip {n} | {anchor_text or 'no anchor'} | "
                            f"{ids} | master @{hhmmss(clip['in'])}",
                originator="cutdown",
                originator_reference=f"{segments.get('episode', '')} {sid}".strip(),
                coding_history=f"A=PCM,F={info.sample_rate},W={info.bits_per_sample},"
                               f"M={'stereo' if info.channels == 2 else 'mono'},T=cutdown",
            )
            write_wav(path, info, frames, extra_chunks=[(b"bext", bext)])
            bwf.verify(path, expected_time_reference=tr)

            seg_frames_total += n_frames
            clip_paths.append((path, n_frames, clip["join"]))
            declared = clip["out"] - clip["in"]
            covered = sum(max(0, min(b, clip["out"]) - max(a, clip["in"]))
                          for u in run for a, b in sp[u])
            if abs(declared - covered) * info.sample_rate > 1:
                raise CutdownError(f"{name}: samples unaccounted for in planned clip")
            # The clip must be exactly as long as the units it claims to hold.
            # Drift here means audio is present that no unit accounts for - a
            # trim that never landed, or a run merged across a gap.
            drift = n_frames - int(round(declared * info.sample_rate))
            if abs(drift) > 1:
                raise CutdownError(
                    f"{name}: {n_frames} frames but its units declare "
                    f"{declared:.3f}s ({int(round(declared * info.sample_rate))} "
                    f"frames) - {drift} samples unaccounted for "
                    f"({drift / info.sample_rate:+.3f}s). The clip and the "
                    f"manifest would disagree; refusing to write a batch."
                )
            rows.append({
                "kind": kind,
                "segment": sid,
                "clip": n,
                "file": str(path.relative_to(outdir)),
                "unit_ids": " ".join(run),
                "source_in": hhmmss(clip["in"]),
                "source_out": hhmmss(clip["out"]),
                "in_sec": f"{clip['in']:.3f}",
                "out_sec": f"{clip['out']:.3f}",
                "in_samples": in_s,
                "out_samples": out_s,
                "frames": n_frames,
                "dur_sec": f"{n_frames / info.sample_rate:.3f}",
                "declared_dur_sec": f"{declared:.3f}",
                "sample_delta": drift,
                "time_reference": tr,
                "trimmed": " ".join(u for u in run if sp[u] != [[timeline[u]["cut_in"], timeline[u]["cut_out"]]]),
                "join": clip["join"],
                "anchor": anchor_text,
                "anchor_kind": anchor.get("kind", ""),
                "reason": seg.get("reason", "") or seg.get("summary", ""),
                "check_warnings": " | ".join(
                    w for u in run for w in checks.get(u, [])) or "",
                "flags": " ".join(seg.get("flags") or []),
                "text": " ".join(spans.kept_text(u, units, unit_words, [[clip["in"], clip["out"]]])
                                 for u in run if u in units),
            })

        # Only source edits receive the configured gap.
        buf = bytearray()
        for path, nf, join in clip_paths:
            if join == "edit":
                buf += info.silence(gap_frames)
            buf += WavInfo(path).read_frames(0, nf)
        final = d["final"] / f"{final_name}.wav"
        write_wav(final, info, bytes(buf))

        sec = seg_frames_total / info.sample_rate
        lo, hi = (seg.get("target_sec") or [None, None])[:2]
        flags = list(seg.get("flags") or [])
        allowed_hi = hi + max(0, seg.get("completion_grace_sec", 0)) if hi is not None else None
        if lo is not None and hi is not None and not (lo <= sec <= allowed_hi):
            f = "under_target" if sec < lo else "over_target"
            if f not in flags:
                flags.append(f)
        elif hi is not None and sec > hi and "completion_extension" not in flags:
            flags.append("completion_extension")
        rel_final = str(final.relative_to(outdir))
        note(f"{sid}: {len(clip_paths)} clips  {sec/60:.1f} min "
             f"({sec:.1f}s)  -> {rel_final}"
             + (f"  [{', '.join(flags)}]" if flags else ""))
        return {"id": sid, "kind": kind, "clips": len(clip_paths),
                "removed_sec": round(sum(timeline[u]["dur_cut"] - spans.kept_dur(sp[u]) for u in order), 3),
                "sec": round(sec, 3), "target": [lo, hi], "flags": flags,
                "anchor": anchor, "reason": seg.get("reason", ""),
                "summary": seg.get("summary", ""),
                "final": rel_final, "stringout": rel_final,
                "clip_dir": str(clip_dir.relative_to(outdir))}

    for seg in segments["segments"]:
        summary = write_group(seg, "segment")
        if summary is None:
            seg_summaries.append({"id": seg["id"], "kind": "segment", "clips": 0,
                                  "sec": 0.0, "anchor": seg.get("anchor", {}),
                                  "flags": (seg.get("flags") or []) + ["empty"]})
            continue
        seg_summaries.append(summary)
        for r in seg.get("rejected") or []:
            rejected_rows.append({
                "segment": seg["id"], "unit_id": r.get("id", ""), "why": r.get("why", ""),
                "text": units.get(r.get("id", ""), {}).get("text", ""),
                "source_in": hhmmss(timeline[r["id"]]["cut_in"]) if r.get("id") in timeline else "",
                "dur_sec": f"{timeline[r['id']]['dur_cut']:.3f}" if r.get("id") in timeline else "",
            })
    for t in topics:
        summary = write_group(t, "topic")
        if summary is not None:
            topic_summaries.append(summary)

    chunk_summaries = []
    if chunks and sections:
        chunk_summaries = write_chunks(
            sections, d["chunks"], info, outdir=outdir,
            names=chunk_labels(sections, list(segments["segments"]) + list(topics),
                               labels, timeline),
            episode=segments.get("episode", ""), rows=rows, units=units)
    elif chunks:
        log("  no sections.json (ingest predates it) - re-run ingest for chunks")

    if rows:
        with open(outdir / "manifest.csv", "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    with open(outdir / "rejected.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["segment", "unit_id", "why", "dur_sec",
                                          "source_in", "text"])
        w.writeheader()
        w.writerows(rejected_rows)

    cut_rows = [r for r in rows if r["kind"] != "chunk"]
    summary = {
        "master": str(master), "sample_rate": info.sample_rate,
        "bits_per_sample": info.bits_per_sample, "channels": info.channels,
        "clips": len(cut_rows), "segments": seg_summaries,
        "topics": topic_summaries, "chunks": chunk_summaries,
        "dirs": {k: str(v.relative_to(outdir)) for k, v in d.items()},
        "max_sample_delta": max((abs(r["sample_delta"]) for r in cut_rows), default=0),
    }
    write_json(outdir / "render.json", summary)
    note(f"{len(seg_summaries)} segments + {len(topic_summaries)} topics -> "
         f"{d['final'].relative_to(outdir)}/   {len(cut_rows)} clips -> "
         f"{d['clips'].relative_to(outdir)}/"
         + (f"   {len(chunk_summaries)} chunks -> {d['chunks'].relative_to(outdir)}/"
            if chunk_summaries else ""))
    note(f"max sample delta vs declared durations: {summary['max_sample_delta']}")
    return summary


def write_chunks(sections: list[dict], chunk_dir: pathlib.Path, info: WavInfo, *,
                 outdir: pathlib.Path, names: dict[str, str], episode: str,
                 rows: list, units: dict) -> list[dict]:
    """The recording, unedited, one file per section.

    Each runs from its first unit's cut-in to its last unit's cut-out: every
    sample in between is kept, pauses and all. Only the long silences between
    sections (the records played in the room) are left out.
    """
    if chunk_dir.exists():
        shutil.rmtree(chunk_dir)
    chunk_dir.mkdir(parents=True)
    out = []
    for sec in sections:
        in_s = int(round(sec["cut_in"] * info.sample_rate))
        out_s = min(int(round(sec["cut_out"] * info.sample_rate)), info.total_frames)
        n_frames = out_s - in_s
        if n_frames <= 0:
            continue
        label = names.get(sec["id"], "talk")
        name = _filename(f"{sec['id']}_{_stamp_time(sec['cut_in'])}_{label}.wav")
        path = chunk_dir / name
        tr = info.bext_time_reference + in_s
        bext = bwf.build_bext(
            time_reference=tr,
            description=f"{sec['id']} unedited | {label} | {sec['first']}-"
                        f"{sec['last']} | master @{hhmmss(sec['cut_in'])}",
            originator="cutdown",
            originator_reference=f"{episode} {sec['id']}".strip(),
            coding_history=f"A=PCM,F={info.sample_rate},W={info.bits_per_sample},"
                           f"M={'stereo' if info.channels == 2 else 'mono'},T=cutdown",
        )
        write_wav(path, info, info.read_frames(in_s, n_frames),
                  extra_chunks=[(b"bext", bext)])
        bwf.verify(path, expected_time_reference=tr)
        sec_len = n_frames / info.sample_rate
        rows.append({
            "kind": "chunk", "segment": sec["id"], "clip": 1,
            "file": str(path.relative_to(outdir)),
            "unit_ids": f"{sec['first']}-{sec['last']}",
            "source_in": hhmmss(sec["cut_in"]), "source_out": hhmmss(out_s / info.sample_rate),
            "in_sec": f"{sec['cut_in']:.3f}", "out_sec": f"{out_s / info.sample_rate:.3f}",
            "in_samples": in_s, "out_samples": out_s, "frames": n_frames,
            "dur_sec": f"{sec_len:.3f}", "declared_dur_sec": f"{sec_len:.3f}",
            "sample_delta": 0, "time_reference": tr, "trimmed": "", "join": "",
            "anchor": label, "anchor_kind": "chunk", "reason": sec.get("break", ""),
            "check_warnings": "", "flags": "",
            "text": " ".join(units[u]["text"] for u in sec["ids"] if u in units)[:2000],
        })
        out.append({"id": sec["id"], "file": str(path.relative_to(outdir)),
                    "label": label, "sec": round(sec_len, 3),
                    "start": sec["cut_in"], "break": sec.get("break", ""),
                    "units": len(sec["ids"])})
    note(f"{len(out)} unedited chunks -> {chunk_dir.relative_to(outdir)}/")
    return out
