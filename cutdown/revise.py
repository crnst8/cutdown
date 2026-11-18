"""One optional revision pass, with rollback and a partial re-check."""
from __future__ import annotations

import shutil
from . import assemble, check, prompts, sections, spans
from .providers import get_named_provider, request_json, stage_options
from .render import slugify
from .util import CutdownError, log, mmss, note, read_json, read_jsonl, write_json

SCHEMA = {**assemble.SCHEMA, "properties": {**assemble.SCHEMA["properties"],
          "fixed": {"type": "array"}, "left": {"type": "array"},
          "cuts": {"type": "array", "items": {"type": "object", "required": ["id", "text"], "properties": {"id": {"type": "string"}, "text": {"type": "string"}}}}}}


def counts(warnings):
    return {**{s: sum(w.get("severity", "low") == s for w in warnings)
               for s in ("high", "medium", "low")}, "total": len(warnings)}


def run_revise(build, config, ctx, brief, *, run_dir, prompts_dir=None) -> dict:
    doc = read_json(build / "segments.json")
    had_topics = (build / "topics.json").exists()
    tdoc = read_json(build / "topics.json") if had_topics else {"enabled": False, "topics": []}
    chk = read_json(build / "check.json")
    if chk.get("revision"):
        raise CutdownError("already revised; force assemble to start again")
    all_units = read_jsonl(build / "units.jsonl")
    units = {u["id"]: u for u in all_units}
    lp = build / "labels.jsonl"
    labels = {u["id"]: u for u in read_jsonl(lp)} if lp.exists() else {}
    timeline = read_json(build / "timeline.json")
    wp = build / "unit_words.json"
    words = read_json(wp) if wp.exists() else {}
    secs = sections.load(build)
    fcfg = config.get("fillers", {})
    backup = build / "pre-revise"
    backup.mkdir(exist_ok=True)
    names = ["segments.json", "check.json"] + (["topics.json"] if had_topics else [])
    for name in names:
        shutil.copy2(build / name, backup / name)
    try:
        rank = {u["id"]: n for n, u in enumerate(all_units)}
        blocked = {u for u in units if labels.get(u, {}).get("role") in assemble.BLOCKED_ROLES
                   or (labels and u not in labels)}
        warnings = chk.get("warnings", [])
        warn_by = {g: [w for w in warnings if w.get("segment") == g]
                   for g in {w.get("segment") for w in warnings}}
        verdict_by = {s["id"]: s.get("verdict", "") for s in chk.get("segments", [])}
        routing = config.get("stages.revise") or config.stage("assemble")
        provider = get_named_provider(config, routing["provider"])
        model = routing["model"]
        fallback_used = False
        system = prompts.load("assemble.system", prompts_dir) + "\n\n" + prompts.load("revise.system", prompts_dir)

        def call(user, tag):
            nonlocal provider, model, fallback_used
            try:
                stage = "revise" if config.get("stages.revise") else "assemble"
                options = stage_options(config, stage) if not fallback_used else {}
                artifacts = run_dir / f"fallback-{slugify(provider.name)}" if fallback_used else run_dir
                return request_json(provider, **options, model=model, system=system,
                                    user=user, schema=SCHEMA, run_dir=artifacts, tag=tag)
            except CutdownError:
                if (fallback_used or not routing.get("fallback_provider") or not routing.get("fallback_model")):
                    raise
                if (provider.name, model) == (routing["fallback_provider"], routing["fallback_model"]):
                    raise
                provider = get_named_provider(config, routing["fallback_provider"])
                model = routing["fallback_model"]
                fallback_used = True
                return call(user, tag)

        changed, touched = set(), set()
        min_sec = config.get("topics.min_sec", 20)
        neighbours = config.get("revise.neighbours", 8)

        def revise_group(seg, topic=False):
            gid = seg["id"]
            original_order = seg.get("order") or seg.get("ids") or []
            if not original_order or not warn_by.get(gid):
                return
            own = set(original_order)
            groups = doc["segments"] + (tdoc["topics"] if topic else [])
            taken = {u for g in groups if g is not seg for u in g.get("ids", [])}
            free = set(units) - blocked - taken
            near = {u for u in units if any(abs(rank[u] - rank[v]) <= neighbours for v in own)}
            tagged = {u for u in units if gid in (labels.get(u, {}).get("topics") or [])} if not topic else set()
            considered = {r.get("id") for r in seg.get("rejected", [])} if not topic else set()
            cand = [u["id"] for u in all_units if u["id"] in own or u["id"] in free & (near | tagged | considered)]
            tags = {u: f"in:{t['id']}" for t in tdoc["topics"] for u in t.get("ids", [])}
            tags.update({u: "in current edit" for u in own})
            old_measured = seg.get("measured_sec", assemble.measure(original_order, timeline, words, seg.get("trim", {}), cfg=fcfg))
            lo, hi = (min_sec, float("inf")) if topic else seg["target_sec"]
            allowed_hi = hi if topic else hi + seg.get("completion_grace_sec", config.get("assemble.completion_grace_sec", 20))
            anchor = seg.get("anchor") or {}
            piece = (f"Topic file: {gid} - {anchor.get('title', seg.get('title', ''))}" if topic else
                     f"Segment: {gid} - plays before {anchor.get('artist', '?')} - {anchor.get('title', '?')} ({anchor.get('kind', 'guest_pick')})")
            current = check._render_segments({"segments": [seg]}, units, timeline, words, fcfg, marks=True)
            user = prompts.render("revise.user", prompts_dir, context=ctx.block(), piece=piece,
                target=f"at least {lo} seconds" if topic else f"{lo} to {allowed_hi} seconds",
                measured=f"{old_measured:.1f}", current="\n".join(current.splitlines()[1:]),
                verdict=verdict_by.get(gid) or "(none)",
                warnings="\n".join(f"{i}. [{w.get('severity', 'low')}] {w.get('kind', 'other')} at {w.get('id') or '(no unit)'}: {w.get('note', '')}" for i,w in enumerate(warn_by[gid],1)),
                pool=assemble._pool_lines(cand, units, labels, secs, timeline, words, cfg=fcfg, extra_tags=tags), segment_id=gid)
            res = call(user, f"revise.{gid}")
            holes, applied, refused = {}, [], []
            def resolve(res):
                holes.clear()
                applied.clear()
                refused.clear()
                ids, order, _, rejected, dropped = assemble._sanitize(res.data, set(cand))
                if topic:
                    ids = sorted(ids, key=rank.get)
                    order = ids[:]
                for entry in res.data.get("cuts") or []:
                    uid, text = entry["id"], entry["text"]
                    if uid not in ids: continue
                    hole = spans.locate_cut(words.get(uid) or [], text, timeline[uid], fcfg)
                    if hole:
                        holes.setdefault(uid, []).append(list(hole))
                        applied.append({"id":uid,"text":text})
                    else:
                        refused.append({"id":uid,"text":text})
                        log(f"  {gid}: cut refused in {uid} (not between pauses, not found, or leaves too little)")
                return ids, order, assemble.measure(ids, timeline, words, {}, cfg=fcfg, holes=holes)
            ids, order, measured = resolve(res)
            n = 0
            while not topic and ids and not lo <= measured <= allowed_hi and n < config.get("assemble.max_iterations", 2):
                n += 1
                retry = prompts.render("assemble.retry", prompts_dir, segment_id=gid, measured=f"{measured:.1f}", target_lo=lo, target_hi=allowed_hi,
                    delta=round(lo-measured if measured<lo else measured-allowed_hi,1), direction="short" if measured<lo else "long",
                    selection="\n".join(
                        f"{u} {assemble.effective_dur(u,timeline,words,{},cfg=fcfg,holes=holes):.1f}s "
                        f"{spans.kept_text(u, units, words, spans.unit_spans(u,timeline,words,cfg=fcfg,holes=holes.get(u)))}"
                        for u in order),
                    advice=assemble._advice(measured,lo,hi,len(ids)))
                res = call(user + "\n\n---\n\n" + retry, f"revise.{gid}.iter{n}")
                ids, order, measured = resolve(res)
            distance = lambda d: max(lo-d, 0, d-allowed_hi)
            reject = not ids or (topic and measured < lo) or (not topic and distance(measured)>0 and distance(measured)>=distance(old_measured))
            if reject:
                flags = seg.setdefault("flags", [])
                if "revision_rejected" not in flags: flags.append("revision_rejected")
                note(f"  {gid}: revision rejected")
                return
            if order == original_order and not applied:
                seg["revision"] = {"changed": False, "left": res.data.get("left") or []}
                return
            added = [u for u in ids if u not in own]
            removed = [u for u in original_order if u not in ids]
            seg.update(ids=ids, order=order, trim={}, reason=res.data.get("reason", ""), measured_sec=measured,
                revision={"changed":True,"added":added,"removed":removed,"fixed":res.data.get("fixed") or [],"left":res.data.get("left") or [],"measured_before":old_measured,"cuts":applied[:],"cuts_refused":refused[:]})
            if holes: seg["holes"] = {u:h[:] for u,h in holes.items()}
            else: seg.pop("holes", None)
            seg.setdefault("rejected", []).extend({"id":u,"why":"removed in revision"} for u in removed)
            if not topic:
                seg["flags"] = [f for f in seg.get("flags", []) if f not in {"under_target","over_target","needs_review","completion_extension"}]
                if measured < lo: seg["flags"] += ["under_target","needs_review"]
                elif measured > allowed_hi: seg["flags"] += ["over_target","needs_review"]
                elif measured > hi: seg["flags"].append("completion_extension")
                for t in tdoc["topics"]:
                    if set(added) & set(t.get("ids", [])):
                        t["ids"] = [u for u in t["ids"] if u not in added]
                        t["order"] = [u for u in t.get("order", t["ids"]) if u not in added]
                        touched.add(t["id"])
            changed.add(gid)

        for seg in doc["segments"]: revise_group(seg)
        dropped = set()
        for t in tdoc["topics"][:]:
            if t["id"] not in touched: continue
            t["measured_sec"] = round(sum(timeline[u]["dur_cut"] for u in t["ids"]),3)
            if not t["ids"] or t["measured_sec"] < min_sec:
                tdoc["topics"].remove(t)
                tdoc.setdefault("unplaced", []).extend(t["ids"])
                dropped.add(t["id"])
                note(f"  {t['id']}: dropped after units moved into segments")
            else: changed.add(t["id"])
        for t in tdoc["topics"]: revise_group(t, True)
        assemble._assert_exclusive({"segments":doc["segments"]+tdoc["topics"]})
        def save():
            write_json(build / "segments.json", doc)
            if had_topics: write_json(build / "topics.json", tdoc)
        save()
        chk["warnings"] = [w for w in chk.get("warnings",[]) if w.get("segment") not in dropped]
        chk["segments"] = [s for s in chk.get("segments",[]) if s.get("id") not in dropped]
        write_json(build / "check.json", chk)
        if changed:
            check.run_check(build, config, ctx, run_dir=run_dir / "check", prompts_dir=prompts_dir, only=changed)
            chk = read_json(build / "check.json")
        before = [w for w in warnings if w.get("segment") in changed]
        after = [w for w in chk.get("warnings",[]) if w.get("segment") in changed]
        for g in doc["segments"] + tdoc["topics"]:
            gid = g["id"]
            if gid in changed and counts([w for w in after if w.get("segment")==gid])["high"] > counts([w for w in before if w.get("segment")==gid])["high"]:
                g.setdefault("flags",[]).append("revision_regressed")
            if gid in changed:
                r = g.get("revision",{})
                b = sum(w.get("segment") == gid for w in before)
                a = sum(w.get("segment") == gid for w in after)
                note(f"  {gid}: revised  +{len(r.get('added',[]))} -{len(r.get('removed',[]))} units  {mmss(r.get('measured_before',0))} -> {mmss(g['measured_sec'])}  warnings {b} -> {a}")
        save()
        result = {"groups":sorted(changed),"before":counts(before),"after":counts(after),"warnings_before":before}
        chk["revision"] = result
        write_json(build / "check.json", chk)
        note(f"revise: {len(changed)} pieces, warnings {len(before)} -> {len(after)}")
        return result
    except Exception as e:
        for name in names: shutil.copy2(backup / name, build / name)
        if not had_topics: (build / "topics.json").unlink(missing_ok=True)
        if isinstance(e, CutdownError): raise
        raise CutdownError(f"revise failed and was rolled back: {e}") from e
