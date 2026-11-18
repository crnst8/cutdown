"""Topics: the talk no segment used, grouped by what it is about.

Runs after assemble. A recording made around the records carries plenty that is
not about any one record - the guest's week, touring, how the album was made -
and a producer often wants it kept, sorted, as material for other things. This
stage asks a model to group what is left into general topics; code does the rest.

Same rules as assemble:

  - The model returns unit IDs, never times. Code measures every duration.
  - Exclusivity: a unit already in a segment is not offered; a unit is placed in
    at most one topic.
  - Admin and unlabelled units never reach a topic file.
  - Units inside a topic play in recorded order. This is unedited material
    sorted by subject, not a second assembly.

Whether it runs at all is the brief's `extras.keep` (drafted by discover from
the producer's instructions), overridden by the context's `extras` setting.
When it is off, topics.json is written empty and no model is called.
"""
from __future__ import annotations

import pathlib

from . import prompts, sections
from .assemble import BLOCKED_ROLES
from .providers import get_provider, request_json, stage_options
from .render import slugify
from .util import log, mmss, note, read_json, read_jsonl, write_json

SCHEMA = {
    "type": "object",
    "required": ["topics"],
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["title", "ids"],
                "properties": {
                    "title": {"type": "string"},
                    "ids": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
            },
        },
        "discard": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
            },
        },
    },
}


def wanted(ctx, brief) -> bool:
    if ctx.extras is not None:
        return bool(ctx.extras)
    extras = (brief.data.get("extras") if brief is not None else None) or {}
    return bool(extras.get("keep")) if isinstance(extras, dict) else bool(extras)


def leftover(units: list[dict], labels: dict, segments: dict) -> list[str]:
    """Unit ids no segment used and that are allowed on air, in recorded order."""
    used = {uid for s in segments.get("segments", [])
            for uid in (s.get("ids") or [])}
    return [u["id"] for u in units
            if u["id"] not in used
            and labels.get(u["id"], {}).get("role", "unlabelled") not in BLOCKED_ROLES]


def sanitize(raw: dict, pool: list[str], timeline: dict, *,
             min_sec: float = 0.0) -> tuple[list[dict], list[str]]:
    """Keep real, available, unclaimed ids; recorded order; measured lengths."""
    rank = {uid: n for n, uid in enumerate(pool)}
    claimed: set[str] = set()
    out, dropped = [], []
    for t in raw.get("topics") or []:
        ids = []
        for uid in t.get("ids") or []:
            if uid not in rank:
                dropped.append(uid)
            elif uid not in claimed:
                ids.append(uid)
                claimed.add(uid)
        if not ids:
            continue
        ids.sort(key=rank.__getitem__)
        sec = round(sum(timeline[i]["dur_cut"] for i in ids), 3)
        if sec < min_sec:
            claimed -= set(ids)
            continue
        out.append({"title": (t.get("title") or "").strip() or "untitled",
                    "summary": (t.get("summary") or "").strip(),
                    "ids": ids, "order": ids, "measured_sec": sec})
    # Name them in the order the talk first comes up, so topic01 is the earliest.
    out.sort(key=lambda t: rank[t["ids"][0]])
    for n, t in enumerate(out, 1):
        t["id"] = f"topic{n:02d}"
        t["anchor"] = {"artist": "", "title": t["title"], "kind": "topic"}
    return out, dropped


def run_topics(build: pathlib.Path, config, ctx, brief, *, run_dir: pathlib.Path,
               dry_run: bool = False, prompts_dir: pathlib.Path | None = None) -> dict:
    out_path = build / "topics.json"
    if not wanted(ctx, brief):
        note("topics: off (brief `extras.keep` is false) - leftover talk not kept")
        doc = {"enabled": False, "topics": []}
        if not dry_run:
            write_json(out_path, doc)
        return doc

    units = read_jsonl(build / "units.jsonl")
    by_id = {u["id"]: u for u in units}
    timeline = read_json(build / "timeline.json")
    lp = build / "labels.jsonl"
    labels = {r["id"]: r for r in read_jsonl(lp)} if lp.exists() else {}
    segments = read_json(build / "segments.json")
    pool = leftover(units, labels, segments)
    if not pool:
        note("topics: nothing left over after assembly")
        doc = {"enabled": True, "topics": []}
        if not dry_run:
            write_json(out_path, doc)
        return doc

    provider, model = get_provider(config, "topics")
    note(f"topics: {len(pool)} leftover units -> {provider.describe()} {model}")
    hints = []
    if brief is not None and isinstance(brief.data.get("extras"), dict):
        hints = [str(h) for h in brief.data["extras"].get("topics") or []]
    lines = []
    for uid in pool:
        u, lab = by_id[uid], labels.get(uid, {})
        tags = ",".join(lab.get("topics") or []) or "-"
        lines.append((uid, f"{uid}  {u['role']}  [{lab.get('role', '?')}; {tags}]  "
                           f"{u['text']}"))
    used = "\n".join(
        f"  {s['id']}  {(s.get('anchor') or {}).get('artist', '?')} - "
        f"{(s.get('anchor') or {}).get('title', '?')}"
        for s in segments.get("segments", []) if s.get("ids")) or "  (none)"
    user = prompts.render(
        "topics.user", prompts_dir, context=ctx.block(), segments=used,
        hints="\n".join(f"  - {h}" for h in hints) or "  (none)",
        count=len(pool), pool=sections.annotate(lines, sections.load(build)),
    )
    system = prompts.load("topics.system", prompts_dir)
    res = request_json(provider, **stage_options(config, "topics"), model=model, system=system, user=user,
                       schema=SCHEMA, run_dir=run_dir, tag="topics", dry_run=dry_run)
    if dry_run:
        return {"dry_run": True}

    min_sec = float(config.get("topics.min_sec", 20) or 0)
    topics, dropped = sanitize(res.data, pool, timeline, min_sec=min_sec)
    if dropped:
        log(f"  dropped {len(dropped)} ids not in the leftover pool: "
            f"{', '.join(dropped[:8])}")
    placed = {uid for t in topics for uid in t["ids"]}
    doc = {"enabled": True, "provider": f"{provider.name}/{model}",
           "topics": topics,
           "discard": [d for d in res.data.get("discard") or []
                       if isinstance(d, dict) and d.get("id") in by_id],
           "unplaced": [uid for uid in pool if uid not in placed]}
    write_json(out_path, doc)
    for t in topics:
        note(f"  {t['id']}  {mmss(t['measured_sec'])}  {t['title']}  "
             f"({len(t['ids'])} units)  -> {slugify(t['title'])}")
    note(f"  {len(topics)} topics, {len(placed)} of {len(pool)} leftover units placed")
    return doc
