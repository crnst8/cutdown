"""report.md - what the producer reads before opening Pro Tools."""
from __future__ import annotations

import pathlib

from . import spans
from .util import mmss, read_json, read_jsonl

SEV = {"high": "HIGH", "medium": "med", "low": "low"}


def write_report(build: pathlib.Path, outdir: pathlib.Path, ctx, fillers=None) -> pathlib.Path:
    render = read_json(outdir / "render.json")
    segments = {s["id"]: s for s in read_json(build / "segments.json")["segments"]}
    units = {u["id"]: u for u in read_jsonl(build / "units.jsonl")}
    wp = build / "unit_words.json"
    words = read_json(wp) if wp.exists() else {}
    tp = build / "timeline.json"
    timeline = read_json(tp) if tp.exists() else {}
    check = read_json(build / "check.json") if (build / "check.json").exists() else {}
    revision = check.get("revision", {})
    verdicts = {s["id"]: s for s in check.get("segments", [])}
    warnings: dict[str, list] = {}
    for w in check.get("warnings", []):
        warnings.setdefault(w.get("segment", ""), []).append(w)

    L: list[str] = []
    title = " / ".join(x for x in (ctx.show, ctx.guest, ctx.episode) if x) or "cutdown"
    L.append(f"# {title}")
    L.append("")
    L.append(f"{render['clips']} clips, {render['sample_rate']} Hz "
             f"{render['bits_per_sample']}-bit {render['channels']} ch, "
             f"BWF-stamped to their original master positions.")
    L.append("")
    if revision.get("groups"):
        before, after = revision["before"], revision["after"]
        L.append(f"One revision pass ran on {len(revision['groups'])} pieces: warnings {before['total']} → {after['total']} (high {before['high']} → {after['high']}). Originals are in build/pre-revise/.")
        L.append("")
    dirs = render.get("dirs") or {}
    if dirs:
        L.append("## Where things are")
        L.append("")
        L.append(f"- `{dirs.get('final', '1-final')}/` — one finished file per song "
                 f"segment" + (", then one per topic" if render.get("topics") else "")
                 + ". Clips joined with a short gap, ready to audition.")
        L.append(f"- `{dirs.get('clips', '2-clips')}/` — the clips behind each finished "
                 f"file, one folder each, BWF-stamped for Spot → Timestamp.")
        if render.get("chunks"):
            L.append(f"- `{dirs.get('chunks', '3-chunks')}/` — the whole recording, "
                     f"unedited, cut where the long silences fell and named for what "
                     f"is in each piece.")
        L.append("")

    flagged = [s for s in render["segments"]
               if set(s.get("flags") or []) - {"completion_extension"}]
    if flagged:
        L.append("## Needs a human")
        L.append("")
        for s in flagged:
            L.append(f"- **{s['id']}** — {', '.join(s['flags'])} "
                     f"({mmss(s['sec'])}, target "
                     f"{s.get('target', ['?', '?'])[0]}–{s.get('target', ['?', '?'])[1]}s)")
        L.append("")

    L.append("## Segments")
    L.append("")
    L.append("| segment | anchor | clips | length | target | flags | warnings |")
    L.append("|---|---|---:|---:|---:|---|---:|")
    for s in render["segments"]:
        a = s.get("anchor") or {}
        t = s.get("target") or [None, None]
        L.append(
            f"| {s['id']} | {a.get('artist', '')} — {a.get('title', '')} | "
            f"{s['clips']} | {mmss(s['sec'])} | "
            f"{t[0]}–{t[1]}s | {', '.join(s.get('flags') or []) or '—'} | "
            f"{len(warnings.get(s['id'], []))} |")
    L.append("")

    for s in render["segments"]:
        sid = s["id"]
        seg = segments.get(sid, {})
        a = s.get("anchor") or {}
        L.append(f"### {sid} — {a.get('artist', '')} — {a.get('title', '')}")
        L.append("")
        L.append(f"`{s.get('final') or s.get('stringout', '')}` · {mmss(s['sec'])} · "
                 f"{s['clips']} clips · {len(seg.get('ids') or [])} units"
                 + (f" · {seg.get('iterations')} duration iteration(s)"
                    if seg.get("iterations") else "")
                 + (f" · {s['removed_sec']:.1f}s of filler removed"
                    if s.get("removed_sec", 0) >= 0.05 else ""))
        L.append("")
        if seg.get("reason"):
            L.append(f"> {seg['reason']}")
            L.append("")
        v = verdicts.get(sid)
        if v:
            marks = []
            if v.get("stands_alone") is not None:
                marks.append(("stands alone" if v["stands_alone"] else "DOES NOT stand alone"))
            if v.get("lands") is not None:
                marks.append(("lands" if v["lands"] else "DOES NOT land"))
            L.append(f"**Check ({check.get('provider', '')}):** "
                     f"{'; '.join(marks)}. {v.get('verdict', '')}")
            L.append("")
        ws = warnings.get(sid, [])
        if ws:
            L.append("| severity | unit | kind | note |")
            L.append("|---|---|---|---|")
            for w in ws:
                L.append(f"| {SEV.get(w.get('severity', 'low'), '?')} | "
                         f"{w.get('id', '—')} | {w.get('kind', 'other')} | "
                         f"{w.get('note', '')} |")
            L.append("")
        rev = seg.get("revision", {})
        if rev.get("changed"):
            L.append(f"**Revised:** +{len(rev.get('added', []))} −{len(rev.get('removed', []))} units, {mmss(rev.get('measured_before', 0))} → {mmss(seg.get('measured_sec', 0))}.")
            L.extend(f'cut in {x["id"]}: "{x["text"]}"' for x in rev.get("cuts", []))
            L.extend(f"- {x.get('how', '')}" for x in rev.get("fixed", []))
            L.extend(f"- left: {x.get('why', '')}" for x in rev.get("left", []))
        order = seg.get("order") or seg.get("ids") or []
        if order:
            kept = spans.group_spans(seg, timeline, words, fillers)
            L.append("<details><summary>Assembled text</summary>")
            L.append("")
            for uid in order:
                L.append(f"**{uid}** {spans.kept_text(uid, units, words, kept.get(uid, []))}")
                L.append("")
            L.append("</details>")
            L.append("")
        rej = seg.get("rejected") or []
        if rej:
            L.append(f"<details><summary>Considered and rejected ({len(rej)})</summary>")
            L.append("")
            for r in rej:
                uid = r.get("id", "")
                L.append(f"- **{uid}** — {r.get('why', '')}  \n  "
                         f"_{units.get(uid, {}).get('text', '')[:200]}_")
            L.append("")
            L.append("</details>")
            L.append("")

    topics = render.get("topics") or []
    if topics:
        tdoc = read_json(build / "topics.json") if (build / "topics.json").exists() else {}
        tby = {t["id"]: t for t in tdoc.get("topics", [])}
        L.append("## Topics")
        L.append("")
        L.append("The talk no segment used, grouped by subject, in recorded order.")
        L.append("")
        L.append("| topic | title | clips | length | file |")
        L.append("|---|---|---:|---:|---|")
        for t in topics:
            L.append(f"| {t['id']} | {(t.get('anchor') or {}).get('title', '')} | "
                     f"{t['clips']} | {mmss(t['sec'])} | `{t.get('final', '')}` |")
        L.append("")
        for t in topics:
            info = tby.get(t["id"], {})
            if info.get("summary"):
                L.append(f"- **{t['id']}** — {info['summary']}")
            verdict = verdicts.get(t["id"], {})
            if verdict:
                L.append(f"- **{t['id']} check:** {verdict.get('verdict', '')}")
            for w in warnings.get(t["id"], []):
                L.append(f"- **{t['id']} {SEV.get(w.get('severity', 'low'), '?')}** "
                         f"{w.get('id', '—')} [{w.get('kind', 'other')}]: "
                         f"{w.get('note', '')}")
        L.append("")

    chunks = render.get("chunks") or []
    if chunks:
        L.append("## Chunks (unedited)")
        L.append("")
        L.append("| chunk | starts | length | after | file |")
        L.append("|---|---:|---:|---|---|")
        for c in chunks:
            after = {"silence": "silence", "length": "pause", "start": "—"}.get(
                c.get("break", ""), "")
            L.append(f"| {c['id']} | {mmss(c['start'])} | {mmss(c['sec'])} | {after} | "
                     f"`{c['file']}` |")
        L.append("")

    L.append("## Provenance")
    L.append("")
    L.append("- `manifest.csv` — every clip and chunk (`kind` column): source "
             "in/out, unit IDs, samples, BWF time reference, the model's reason, "
             "check warnings.")
    L.append("- `rejected.csv` — units the model considered and did not use, per "
             "segment, with its reasons. Read this when a cut feels thin.")
    L.append("- `build/` — every intermediate. `runs/` — raw model I/O.")
    L.append("")
    L.append("Spot clips in Pro Tools with **Spot → Timestamp**; each carries its "
             "original master position.")

    path = outdir / "report.md"
    path.write_text("\n".join(L) + "\n")
    return path
