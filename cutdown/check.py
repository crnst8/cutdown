"""Stage 4: a craft check on the assembled text.

Runs on a different model family from stage 3 so it reads the segment cold, the
way a listener hears it, with no memory of the conversation it was cut from.

Warnings feed one optional revision pass and appear in the manifest and report.
"""
from __future__ import annotations

import pathlib

from . import prompts, spans
from .providers import get_provider, request_json, stage_options
from .util import CutdownError, note, read_json, read_jsonl, write_json

SCHEMA = {
    "type": "object",
    "required": ["segments", "warnings"],
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "verdict"],
                "properties": {
                    "id": {"type": "string"},
                    "stands_alone": {"type": "boolean"},
                    "lands": {"type": "boolean"},
                    "verdict": {"type": "string"},
                },
            },
        },
        "warnings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["segment", "note"],
                "properties": {
                    "segment": {"type": "string"},
                    "id": {"type": "string"},
                    "kind": {"type": "string", "enum": [
                        "dangling_reference", "join", "cold_open", "ending",
                        "repetition", "instruction", "other"]},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "note": {"type": "string"},
                },
            },
        },
    },
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _render_segments(doc: dict, units: dict, timeline: dict | None = None,
                     unit_words: dict | None = None, cfg=None, marks=False) -> str:
    timeline, unit_words = timeline or {}, unit_words or {}
    blocks = []
    for seg in doc["segments"]:
        order = seg.get("order") or seg.get("ids") or []
        if not order:
            continue
        a = seg.get("anchor") or {}
        heading = (f"topic: {a.get('title', '?')}" if a.get("kind") == "topic" else
                   f"plays before {a.get('artist', '?')} - {a.get('title', '?')}")
        lines = [f"### {seg['id']} - {heading}", ""]
        prev = None
        sp = spans.group_spans(seg, timeline, unit_words, cfg)
        for uid in order:
            if prev is not None and not spans.continuous(prev, uid, timeline):
                lines.append("[EDIT JOIN]")
            text = (spans.marked_text if marks else spans.kept_text)(uid, units, unit_words, sp.get(uid, []))
            lines.append(f"[{uid}] {units.get(uid, {}).get('role', '?')}: {text}")
            prev = uid
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def run_check(build: pathlib.Path, config, ctx, *, run_dir: pathlib.Path,
              dry_run: bool = False, prompts_dir: pathlib.Path | None = None, only: set[str] | None = None) -> dict:
    doc = read_json(build / "segments.json")
    if (build / "topics.json").exists():
        doc["segments"] = doc["segments"] + read_json(build / "topics.json").get("topics", [])
    units = {u["id"]: u for u in read_jsonl(build / "units.jsonl")}
    timeline = read_json(build / "timeline.json")
    wp = build / "unit_words.json"
    unit_words = read_json(wp) if wp.exists() else {}
    playable = [s for s in doc["segments"] if (s.get("order") or s.get("ids"))]
    valid_all = {s["id"] for s in doc["segments"]}
    if only is not None:
        playable = [s for s in playable if s["id"] in only]
    if not playable:
        raise CutdownError("segments.json has no segments with units - run assemble first")

    provider, model = get_provider(config, "check")
    assemble_provider = str(doc.get("provider", ""))
    if assemble_provider.startswith(provider.name + "/"):
        note(f"  NOTE check and assemble are both on {provider.name}. The check is "
             f"meant to come from a different family; consider re-routing "
             f"[stages.check] in cutdown.toml.")
    note(f"check: {len(playable)} segments -> {provider.describe()} {model}")

    system = prompts.load("check.system", prompts_dir)
    user = prompts.render("check.user", prompts_dir, context=ctx.block(),
                          segments=_render_segments({"segments": playable}, units, timeline, unit_words, cfg=config.get("fillers", {})),
                          count=len(playable))
    res = request_json(provider, **stage_options(config, "check"), model=model, system=system, user=user,
                       schema=SCHEMA, run_dir=run_dir, tag="check", dry_run=dry_run)
    if dry_run:
        return {"dry_run": True}

    out = dict(res.data)
    out["provider"] = f"{provider.name}/{model}"
    valid = {s["id"] for s in playable}
    kept = [w for w in out["warnings"] if w.get("segment") in valid]
    dropped = len(out["warnings"]) - len(kept)
    kept.sort(key=lambda w: (SEVERITY_ORDER.get(w.get("severity", "low"), 3),
                             w.get("segment", "")))
    if only is not None and (build / "check.json").exists():
        old = read_json(build / "check.json")
        kept += [w for w in old.get("warnings", []) if w.get("segment") in valid_all - only]
        out["segments"] = [s for s in old.get("segments", []) if s.get("id") in valid_all - only] + out["segments"]
        kept.sort(key=lambda w: (SEVERITY_ORDER.get(w.get("severity", "low"), 3), w.get("segment", "")))
    out["warnings"] = kept
    write_json(build / "check.json", out)

    by_sev: dict[str, int] = {}
    for w in kept:
        by_sev[w.get("severity", "low")] = by_sev.get(w.get("severity", "low"), 0) + 1
    if kept:
        note("  warnings: " + "  ".join(f"{k} {v}" for k, v in sorted(
            by_sev.items(), key=lambda x: SEVERITY_ORDER.get(x[0], 3))))
        for w in kept[:8]:
            note(f"    [{w.get('severity', '?')}] {w.get('segment')} "
                 f"{w.get('id', '-')}: {w.get('note', '')[:100]}")
    else:
        note("  no warnings")
    if dropped:
        note(f"  ignored {dropped} warnings against unknown segments")
    return out
