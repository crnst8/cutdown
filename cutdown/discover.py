"""Propose a draft brief from the transcript, so nobody hand-writes a run sheet.

Track anchors announce themselves in this format - "Why this one?", "This is why
I picked this record", "And I chose <title>". The draft is for the producer to
correct, not to trust.
"""
from __future__ import annotations

import os
import pathlib

from . import prompts, sections
from .brief import dump_brief
from .providers import get_provider, request_json, stage_options
from .util import CutdownError, note, read_json, read_jsonl

SCHEMA = {
    "type": "object",
    "required": ["segments"],
    "properties": {
        "guest": {"type": "string"},
        "show": {"type": "string"},
        "segments": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "anchor", "target_sec"],
                "properties": {
                    "id": {"type": "string"},
                    "anchor": {
                        "type": "object",
                        "required": ["title", "kind"],
                        "properties": {
                            "artist": {"type": "string"},
                            "title": {"type": "string"},
                            "kind": {"type": "string",
                                     "enum": ["guest_pick", "own_work", "other"]},
                        },
                    },
                    "target_sec": {"type": "array", "minItems": 2,
                                   "items": {"type": "number"}},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "sections": {"type": "array", "items": {"type": "string"}},
                    "note": {"type": "string"},
                },
            },
        },
        # What to do with the talk no segment uses, read from the instructions.
        "extras": {
            "type": "object",
            "required": ["keep"],
            "properties": {
                "keep": {"type": "boolean"},
                "topics": {"type": "array", "items": {"type": "string"}},
                "why": {"type": "string"},
            },
        },
    },
}


def _master_path(build: pathlib.Path, brief_dir: pathlib.Path,
                 audio: pathlib.Path | None) -> str:
    """The source recording, as a path relative to the brief.

    `--master` (else `--audio`) wins, then the studio's `.source.json` marker,
    else a placeholder to correct. Written relative to the brief's directory so render can resolve
    it wherever the brief lives.
    """
    src = audio
    if src is None:
        marker = build.parent / ".source.json"
        if marker.exists():
            try:
                src = pathlib.Path((read_json(marker) or {}).get("audio", ""))
            except CutdownError:
                src = None
    if not src or not str(src):
        return "audio/master.wav"
    return os.path.relpath(str(src), brief_dir)


def run_discover(build: pathlib.Path, config, ctx, *, out: pathlib.Path,
                 run_dir: pathlib.Path, dry_run: bool = False,
                 prompts_dir: pathlib.Path | None = None,
                 audio: pathlib.Path | None = None) -> dict:
    units = read_jsonl(build / "units.jsonl")
    if not units:
        raise CutdownError("units.jsonl is empty - run ingest first")

    provider, model = get_provider(config, "discover")
    note(f"discover: reading {len(units)} units -> {provider.describe()} {model}")

    secs = sections.load(build)
    system = prompts.load("discover.system", prompts_dir)
    user = prompts.render("discover.user", prompts_dir, context=ctx.block(),
                          units=sections.index(units, secs), count=len(units),
                          sections=sections.outline(secs),
                          targets=_targets_text(ctx))
    res = request_json(provider, **stage_options(config, "discover"), model=model, system=system, user=user,
                       schema=SCHEMA, run_dir=run_dir, tag="discover", dry_run=dry_run)
    if dry_run:
        return {"dry_run": True}

    valid_ids = {u["id"] for u in units}
    valid_secs = {x["id"] for x in secs}
    segs, seen = [], set()
    for i, s in enumerate(res.data["segments"], 1):
        a = s["anchor"]
        kind = a.get("kind", "guest_pick")
        ev = [e for e in (s.get("evidence") or []) if e in valid_ids]
        where = [c for c in (s.get("sections") or []) if c in valid_secs]
        # Ids are the brief's keys; a model repeating one would make the brief
        # invalid, so number them here.
        sid = s.get("id") or f"seg{i:02d}"
        if sid in seen:
            sid = f"seg{i:02d}"
        seen.add(sid)
        lo, hi = (ctx.target_for(kind)
                  or [int(s["target_sec"][0]), int(s["target_sec"][1])])
        segs.append({
            "id": sid,
            "anchor": {"artist": a.get("artist", "") or "",
                       "title": a.get("title", "") or "",
                       "kind": kind},
            "target_sec": [min(lo, hi), max(lo, hi)],
            **({"note": s["note"]} if s.get("note") else {}),
            **({"evidence": ev} if ev else {}),
            **({"sections": where} if where else {}),
        })
    extras = res.data.get("extras") or {}
    keep = ctx.extras if ctx.extras is not None else bool(extras.get("keep"))

    draft = {
        "guest": ctx.guest or res.data.get("guest", ""),
        "show": ctx.show or res.data.get("show", ""),
        "episode": ctx.episode or "",
        "master": _master_path(build, out.parent, audio),
        "segments": segs,
        # Leftover talk: kept as topic files when true. `topics` are hints for
        # the topics stage, not a closed list.
        "extras": {"keep": keep,
                   "topics": [str(t) for t in (extras.get("topics") or [])][:12]},
    }
    if ctx.songs:
        draft["songs"] = ctx.songs
    if ctx.names:
        draft["names"] = ctx.names
    if ctx.notes:
        draft["notes"] = ctx.notes

    header = (
        "# Draft brief proposed by `cutdown discover`. CORRECT IT BEFORE USE.\n"
        "# Artists and titles came from an ASR transcript and the model's reading\n"
        "# of it; both get things wrong. `evidence` lists the units each pick was\n"
        "# found in, so you can check fast. Delete `evidence` once you are happy.\n"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(header + dump_brief(draft))
    note(f"  {len(segs)} segments proposed -> {out}")
    for s in segs:
        a = s["anchor"]
        note(f"    {s['id']}  {a['artist'] or '?'} - {a['title']}  [{a['kind']}]")
    if keep:
        hints = draft["extras"]["topics"]
        note("  leftover talk: kept as topic files"
             + (f" ({', '.join(hints)})" if hints else ""))
    note(f"  Check artists and titles in {out.name}; they came from ASR.")
    return draft


def _targets_text(ctx) -> str:
    gp = ctx.target_for("guest_pick") or [90, 240]
    ow = ctx.target_for("own_work") or [60, 180]
    return (f"Use [{gp[0]}, {gp[1]}] as target_sec for guest picks and "
            f"[{ow[0]}, {ow[1]}] for own_work unless the instructions or the "
            f"interview clearly suggest otherwise.")
