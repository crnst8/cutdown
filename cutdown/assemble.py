"""Stage 3: choose and order the units for each segment.

Kept text and durations come from spans.py. Three rules are enforced here
rather than asked for:

  - The model returns IDs. Code resolves them to positions and measures the real
    duration from the master timeline.
  - If the measured duration misses the target, the model is re-prompted with the
    measured figure. Capped, then flagged for a human.
  - A unit assigned to one segment leaves the pool before the next segment is
    assembled. Duplicated audio across segments is audible and unacceptable.
"""
from __future__ import annotations

import pathlib

from . import prompts, sections
from .providers import get_named_provider, get_provider, request_json, stage_options
from .render import slugify
from . import spans
from .util import CutdownError, log, mmss, note, read_json, read_jsonl, write_json

SCHEMA = {
    "type": "object",
    "required": ["ids", "order", "reason"],
    "properties": {
        "ids": {"type": "array", "items": {"type": "string"}},
        "order": {"type": "array", "items": {"type": "string"}},
        "trim": {"type": "object"},
        "rejected": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "why"],
                "properties": {"id": {"type": "string"}, "why": {"type": "string"}},
            },
        },
        "reason": {"type": "string"},
    },
}

# Roles that can never reach a clip.
BLOCKED_ROLES = {"admin", "unlabelled"}


def effective_dur(uid: str, timeline: dict, unit_words: dict, trim: dict,
                  min_gap: float = 0.15, window: float = 2.5, *, cfg: dict | None = None, holes: dict | None = None) -> float:
    """Measured duration of a unit, honouring any trim the model flagged."""
    return spans.kept_dur(spans.unit_spans(uid, timeline, unit_words,
        flags=(trim or {}).get(uid), cfg=cfg, holes=(holes or {}).get(uid)))


def measure(ids: list[str], timeline: dict, unit_words: dict, trim: dict, cfg=None, *, holes: dict | None = None) -> float:
    return round(sum(effective_dur(i, timeline, unit_words, trim, cfg=cfg, holes=holes) for i in ids), 3)


def _pool_lines(pool_ids: list[str], units: dict, labels: dict,
                secs: list | None = None, timeline: dict | None = None, unit_words=None, cfg=None, extra_tags=None) -> str:
    out = []
    for uid in pool_ids:
        u = units[uid]
        lab = labels.get(uid, {})
        tags = ",".join(lab.get("topics") or []) or "-"
        role = lab.get("role", "?")
        led = "guest-led" if lab.get("guest_led") else "host-led"
        kept = spans.unit_spans(uid, timeline, unit_words or {}, cfg=cfg) if timeline else []
        text = spans.kept_text(uid, units, unit_words or {}, kept) if timeline else u['text']
        extra = f"; {extra_tags[uid]}" if extra_tags and uid in extra_tags else ""
        duration = f"  ({spans.kept_dur(kept):.1f}s measured)" if timeline else ""
        out.append((uid, f"{uid}  {u['role']}  [{role}; {tags}; {led}{extra}]{duration}  {text}"))
    return sections.annotate(out, secs)


def _segment_note(seg: dict, pool=None) -> str:
    parts = [seg.get("note", "") or ""]
    evidence = [u for u in seg.get("evidence", []) if pool is not None and u in pool]
    if evidence:
        parts.append("The run sheet found this record named or introduced in: " + ", ".join(evidence) + ".")
    if seg.get("sections"):
        parts.append("The run sheet places most of this record's talk in section(s) "
                     f"{', '.join(seg['sections'])}; material elsewhere still counts.")
    return "\n".join(p for p in parts if p)


def _sanitize(data: dict, pool: set[str]) -> tuple[list[str], list[str], dict, list[dict], list[str]]:
    """Keep only real, still-available IDs; make order a permutation of ids."""
    dropped = []
    ids, seen = [], set()
    for i in data.get("ids", []):
        if i in pool and i not in seen:
            ids.append(i)
            seen.add(i)
        elif i not in pool:
            dropped.append(i)
    order = list(dict.fromkeys(i for i in data.get("order", []) if i in seen))
    for i in ids:
        if i not in order:
            order.append(i)
    trim = {k: v for k, v in (data.get("trim") or {}).items()
            if k in seen and isinstance(v, dict)}
    rejected = [r for r in (data.get("rejected") or []) if isinstance(r, dict)]
    return ids, order, trim, rejected, dropped


def _advice(measured: float, lo: float, hi: float, n: int) -> str:
    if measured < lo:
        return ("Add more material. Look for units you rejected, and for material "
                "about this record that sits far from where the record is "
                "introduced. Do not pad with host questions or backchannel.")
    return ("Cut material. Keep complete thoughts and their necessary continuations. "
            "Drop a whole weaker idea rather than cutting off an answer: the "
            "weakest anecdote, the repeated point, the host scaffolding. "
            f"You have {n} units selected; keep all units needed to finish each retained idea.")


def run_assemble(build: pathlib.Path, config, ctx, brief, *, run_dir: pathlib.Path,
                 dry_run: bool = False, prompts_dir: pathlib.Path | None = None) -> dict:
    provider, model = get_provider(config, "assemble")
    try:
        return _run_assemble(build, config, ctx, brief, run_dir=run_dir,
                             dry_run=dry_run, prompts_dir=prompts_dir,
                             provider=provider, model=model)
    except CutdownError as original:
        routing = config.stage("assemble")
        fallback_name = routing.get("fallback_provider")
        fallback_model = routing.get("fallback_model")
        # Fallback requires explicit routing and is disabled during dry runs.
        if dry_run or not fallback_name or not fallback_model:
            raise
        fallback = get_named_provider(config, fallback_name)
        if fallback.name == provider.name and fallback_model == model:
            raise
        note(f"assemble: primary route failed ({str(original).splitlines()[0][:180]}); "
             f"restarting via {fallback.describe()} {fallback_model}")
        return _run_assemble(build, config, ctx, brief,
                             run_dir=run_dir / f"fallback-{slugify(fallback.name)}",
                             dry_run=False, prompts_dir=prompts_dir,
                             provider=fallback, model=fallback_model)


def _run_assemble(build: pathlib.Path, config, ctx, brief, *, run_dir: pathlib.Path,
                  dry_run: bool, prompts_dir: pathlib.Path | None, provider, model) -> dict:
    units = {u["id"]: u for u in read_jsonl(build / "units.jsonl")}
    timeline = read_json(build / "timeline.json")
    unit_words = read_json(build / "unit_words.json") if (build / "unit_words.json").exists() else {}
    labels_path = build / "labels.jsonl"
    labels = {r["id"]: r for r in read_jsonl(labels_path)} if labels_path.exists() else {}
    if not labels:
        log("  no labels.jsonl - assembling from the raw index (admin talk is NOT "
            "filtered; run label first)")

    options = stage_options(config, "assemble") if (provider.name, model) == (config.stage("assemble")["provider"], config.stage("assemble")["model"]) else {}
    fcfg = config.get("fillers", {})
    max_iter = int(config.get("assemble.max_iterations", 2))
    completion_grace = max(0.0, float(config.get("assemble.completion_grace_sec", 20)))
    note(f"assemble: {len(brief.segments)} segments -> {provider.describe()} {model}")

    pool = {uid for uid in units
            if labels.get(uid, {}).get("role") not in BLOCKED_ROLES}
    excluded = len(units) - len(pool)
    if excluded:
        note(f"  {excluded} units held out (admin/unlabelled); pool is {len(pool)}")

    system = prompts.load("assemble.system", prompts_dir)
    secs = sections.load(build)
    out_segments, already = [], []

    for seg in brief.segments:
        sid = seg["id"]
        anchor = seg.get("anchor") or {}
        lo, hi = seg["target_sec"]
        allowed_hi = hi + completion_grace
        pool_ids = sorted(pool)
        user = prompts.render(
            "assemble.user", prompts_dir,
            context=ctx.block(), segment_id=sid,
            anchor=f"{anchor.get('artist', '?')} - {anchor.get('title', '?')}",
            anchor_kind=anchor.get("kind", "guest_pick"),
            target_lo=lo, target_hi=hi,
            segment_note=_segment_note(seg, pool),
            already="\n".join(already) if already else "  (this is the first segment)",
            pool=_pool_lines(pool_ids, units, labels, secs, timeline, unit_words, cfg=fcfg),
        )
        user += (f"\n\nCompletion allowance: up to {completion_grace:g} seconds beyond "
                 f"the target maximum ({allowed_hi:g} seconds total) only to finish "
                 "the guest's current thought. Do not add a new tangent to fill it. "
                 "Explain any extension in reason. A complete short segment is "
                 "better than repeated ideas or host padding.")
        res = request_json(provider, **options, model=model, system=system, user=user,
                           schema=SCHEMA, run_dir=run_dir,
                           tag=f"assemble.{sid}", dry_run=dry_run)
        if dry_run:
            out_segments.append({"id": sid, "anchor": anchor, "target_sec": [lo, hi]})
            continue

        ids, order, trim, rejected, dropped = _sanitize(res.data, pool)
        if dropped:
            log(f"  {sid}: dropped {len(dropped)} IDs that are not in the pool: "
                f"{', '.join(dropped[:8])}")
        measured = measure(ids, timeline, unit_words, trim, cfg=fcfg)
        reason = res.data.get("reason", "")
        flags, iterations = [], 0

        while ids and not (lo <= measured <= allowed_hi) and iterations < max_iter:
            iterations += 1
            delta = round(lo - measured if measured < lo else measured - allowed_hi, 1)
            selection = "\n".join(
                f"  {i}  {effective_dur(i, timeline, unit_words, trim, cfg=fcfg):6.1f}s  "
                f"{spans.kept_text(i, units, unit_words, spans.unit_spans(i, timeline, unit_words, flags=trim.get(i), cfg=fcfg))}" for i in order)
            retry = prompts.render(
                "assemble.retry", prompts_dir, segment_id=sid,
                measured=f"{measured:.1f}", target_lo=lo, target_hi=allowed_hi,
                delta=delta, direction="short" if measured < lo else "long",
                selection=selection, advice=_advice(measured, lo, hi, len(ids)),
            )
            log(f"  {sid}: {measured:.1f}s vs target [{lo}, {hi}] - "
                f"re-prompting ({iterations}/{max_iter})")
            res = request_json(provider, **options, model=model, system=system,
                               user=user + "\n\n---\n\n" + retry, schema=SCHEMA,
                               run_dir=run_dir, tag=f"assemble.{sid}.iter{iterations}")
            ids, order, trim, rejected2, dropped = _sanitize(res.data, pool)
            rejected = (rejected2 or rejected)
            reason = res.data.get("reason", reason)
            measured = measure(ids, timeline, unit_words, trim, cfg=fcfg)

        if not ids:
            flags.append("empty")
            note(f"  {sid}: no units selected - FLAGGED for review")
        elif not (lo <= measured <= allowed_hi):
            flags.append("under_target" if measured < lo else "over_target")
            flags.append("needs_review")
            note(f"  {sid}: {mmss(measured)} ({measured:.1f}s) outside [{lo}, {hi}] "
                 f"after {iterations} iteration(s) - FLAGGED for review")
        else:
            if measured > hi:
                flags.append("completion_extension")
            note(f"  {sid}: {mmss(measured)} ({measured:.1f}s) "
                 f"{'using completion allowance' if measured > hi else 'within target'}  "
                 f"{len(ids)} units")

        pool -= set(ids)          # exclusivity, enforced in code
        already.append(f"  {sid}  {anchor.get('artist', '?')} - "
                       f"{anchor.get('title', '?')}  ({len(ids)} units, {mmss(measured)})")
        out_segments.append({
            "id": sid, "anchor": anchor, "target_sec": [lo, hi],
            "ids": ids, "order": order, "trim": trim,
            "rejected": rejected, "reason": reason,
            "measured_sec": measured, "iterations": iterations, "flags": flags,
            "completion_grace_sec": completion_grace,
        })

    doc = {
        "guest": ctx.guest or brief.guest, "show": ctx.show or brief.show,
        "episode": ctx.episode or brief.episode,
        "provider": f"{provider.name}/{model}",
        "segments": out_segments,
    }
    if not dry_run:
        _assert_exclusive(doc)
        write_json(build / "segments.json", doc)
    return doc


def _assert_exclusive(doc: dict) -> None:
    seen: dict[str, str] = {}
    for seg in doc["segments"]:
        for uid in seg.get("ids", []):
            if uid in seen:
                raise CutdownError(
                    f"internal error: {uid} was assigned to both {seen[uid]} and "
                    f"{seg['id']}. Refusing to write segments.json."
                )
            seen[uid] = seg["id"]
