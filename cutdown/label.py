"""Stage 2: tag every unit with topics and role.

Multi-label is the point. It is what lets own-work chat embedded in another
track's discussion be claimed by the right segment later.

The model sees ID-tagged text and returns IDs. It never sees a timestamp.
"""
from __future__ import annotations

import pathlib

from . import prompts, sections
from .providers import get_provider, request_json, stage_options
from .util import CutdownError, log, note, read_jsonl, write_jsonl

ROLES = ["setup", "payoff", "anecdote", "aside", "admin"]

SCHEMA = {
    "type": "object",
    "required": ["labels"],
    "properties": {
        "labels": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["id", "topics", "role", "guest_led"],
                "properties": {
                    "id": {"type": "string"},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "role": {"type": "string", "enum": ROLES},
                    "guest_led": {"type": "boolean"},
                },
            },
        }
    },
}


def _windows(units: list, size: int, overlap: int) -> list[list]:
    if size <= 0 or size >= len(units):
        return [units]
    step = max(1, size - overlap)
    return [units[i:i + size] for i in range(0, len(units), step)]


def _index(units: list, secs: list | None = None) -> str:
    return sections.index(units, secs)


def run_label(build: pathlib.Path, config, ctx, *, brief=None, run_dir: pathlib.Path,
              dry_run: bool = False, prompts_dir: pathlib.Path | None = None) -> dict:
    units = read_jsonl(build / "units.jsonl")
    if not units:
        raise CutdownError("units.jsonl is empty - run ingest first")

    provider, model = get_provider(config, "label")
    size = int(config.get("label.window_units", 0) or 0)
    overlap = int(config.get("label.window_overlap", 0) or 0)
    wins = _windows(units, size, overlap)
    note(f"label: {len(units)} units in {len(wins)} window(s) "
         f"-> {provider.describe()} {model}")

    system = prompts.load("label.system", prompts_dir)
    tracks = brief.track_lines() if brief else "  (no brief yet)"
    secs = sections.load(build)

    got: dict[str, dict] = {}
    for wi, win in enumerate(wins, 1):
        user = prompts.render(
            "label.user", prompts_dir,
            context=ctx.block(), tracks=tracks, units=_index(win, secs),
            count=len(win), first_id=win[0]["id"], last_id=win[-1]["id"],
        )
        res = request_json(provider, **stage_options(config, "label"), model=model, system=system, user=user,
                           schema=SCHEMA, run_dir=run_dir,
                           tag=f"label.w{wi:02d}", dry_run=dry_run)
        if dry_run:
            continue
        for row in res.data["labels"]:
            got[row["id"]] = row
        log(f"  window {wi}/{len(wins)}: {len(res.data['labels'])} labels "
            f"in {res.attempts} attempt(s)")

    if dry_run:
        return {"dry_run": True, "windows": len(wins)}

    wanted = [u["id"] for u in units]
    missing = [i for i in wanted if i not in got]
    if missing:
        log(f"  {len(missing)} units came back unlabelled; asking again for those only")
        by_id = {u["id"]: u for u in units}
        user = prompts.render(
            "label.user", prompts_dir,
            context=ctx.block(), tracks=tracks,
            units=_index([by_id[i] for i in missing]),
            count=len(missing), first_id=missing[0], last_id=missing[-1],
        )
        try:
            res = request_json(provider, **stage_options(config, "label"), model=model, system=system, user=user,
                               schema=SCHEMA, run_dir=run_dir, tag="label.missing")
            for row in res.data["labels"]:
                got[row["id"]] = row
        except CutdownError as e:
            log(f"  follow-up failed: {e}")

    rows, unlabelled = [], []
    for u in units:
        r = got.get(u["id"])
        if r is None:
            unlabelled.append(u["id"])
            # Conservative: unlabelled units are held out of assembly rather than
            # silently treated as usable content.
            rows.append({"id": u["id"], "topics": [], "role": "unlabelled",
                         "guest_led": False, "unlabelled": True})
            continue
        extra = {k: v for k, v in r.items()
                 if k not in ("id", "topics", "role", "guest_led")}
        rows.append({"id": u["id"], "topics": list(r["topics"]), "role": r["role"],
                     "guest_led": bool(r["guest_led"]), **extra})

    stray = sorted(set(got) - set(wanted))
    if stray:
        log(f"  ignored {len(stray)} labels for IDs that do not exist: "
            f"{', '.join(stray[:8])}")

    write_jsonl(build / "labels.jsonl", rows)

    counts: dict[str, int] = {}
    topics: dict[str, int] = {}
    for r in rows:
        counts[r["role"]] = counts.get(r["role"], 0) + 1
        for t in r["topics"]:
            topics[t] = topics.get(t, 0) + 1
    note("  roles: " + "  ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    note("  topics: " + "  ".join(f"{k} {v}" for k, v in
                                  sorted(topics.items(), key=lambda x: -x[1])[:10]))
    if unlabelled:
        note(f"  WARNING {len(unlabelled)} units never got a label and are held out "
             f"of assembly: {', '.join(unlabelled[:10])}")
    return {"units": len(rows), "roles": counts, "topics": topics,
            "unlabelled": unlabelled}
