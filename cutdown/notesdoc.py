"""notes.md, notes.txt and the per-clip transcripts.

Pure functions over the document that `notes.py` builds, so the wording of the
output can be changed without touching the pipeline, and so the writers can be
tested without a model or an audio file.

Two renderings of the same content, because the destination decides the format:
Markdown for anywhere that renders it, and a wrapped plain-text version for
pasting into a message or an email where Markdown syntax is just noise.

Every timestamp on the page was measured from the audio. `[1:04]` is the
position inside the clip; `master 9:44` is where that clip sits in the session
the edit was made in, and only appears when the clip carries a BWF stamp.
"""
from __future__ import annotations

import textwrap

from .util import clock

WIDTH = 78
STRENGTH = {"strong": "strong", "good": "good", "thin": "thin"}


def _title(doc: dict) -> str:
    c = doc.get("context") or {}
    parts = [x for x in (c.get("show"), c.get("guest"), c.get("episode")) if x]
    return " / ".join(parts) or "Clip notes"


def _at(clip: dict) -> str:
    """Where this clip came from, as far as the file will say."""
    if clip.get("master_sec") is None:
        return ""
    return f"master {clock(clip['master_sec'])}"


def _wrap(text: str, indent: str = "") -> str:
    if not text:
        return ""
    return "\n".join(textwrap.fill(line, width=WIDTH, initial_indent=indent,
                                   subsequent_indent=indent) or indent.rstrip()
                     for line in text.splitlines())


def _provenance(doc: dict) -> tuple[str, str]:
    n = doc["totals"]["clips"]
    line = " · ".join([f"{n} clip" + ("" if n == 1 else "s"),
                       clock(doc["totals"]["sec"]),
                       f"{doc['totals']['words']} words"])
    src = doc.get("transcriber", "")
    prov = doc.get("provider", "")
    tail = f"Transcribed with {src}." if src else ""
    tail += (f" Summaries and headings written by {prov}." if prov
             else " No model was used: this is the transcript only.")
    return line, tail.strip()


def _preamble(doc: dict) -> str:
    if not doc.get("provider"):
        return ("Every word here is verbatim ASR output, mistakes included, and "
                "every timestamp was measured from the audio.")
    return ("Transcript text is verbatim ASR output, mistakes included. Only the "
            "summaries, headings and anecdote descriptions are written by a "
            "model. Every timestamp was measured from the audio.")


# ---------------------------------------------------------------- markdown

def render_markdown(doc: dict) -> str:
    L: list[str] = []
    line, tail = _provenance(doc)
    L += [f"# {_title(doc)} — clip notes", "", f"*{line}*", ""]
    L += [f"> {_preamble(doc)}", ""]
    if tail:
        L += [f"*{tail}*", ""]

    d = doc.get("digest")
    if d:
        L += ["## At a glance", ""]
        if d.get("summary"):
            L += [d["summary"], ""]
        if d.get("themes"):
            L += ["**Themes:** " + " · ".join(d["themes"]), ""]
        if d.get("highlights"):
            L += ["**Worth using**", ""]
            for h in d["highlights"]:
                why = f" — {h['why']}" if h.get("why") else ""
                L.append(f"{h['n']}. **{h['clip']}** — {h['what']}{why}")
            L.append("")

    L += ["## Clips", "",
          "| # | clip | length | in session | what it is |",
          "|---:|---|---:|---:|---|"]
    for c in doc["clips"]:
        s = c.get("summary") or {}
        L.append(f"| {c['n']} | `{c['clip']}` | {clock(c['duration'])} | "
                 f"{clock(c['master_sec']) if c.get('master_sec') is not None else '—'} | "
                 f"{s.get('headline', '') or '—'} |")
    L.append("")

    for c in doc["clips"]:
        s = c.get("summary") or {}
        L += ["---", "", f"## {c['n']}. {s.get('headline') or c['clip']}", ""]
        meta = [f"`{c['clip']}`", clock(c["duration"])]
        if _at(c):
            meta.append(_at(c))
        meta.append(f"{c['words']} words")
        L += [" · ".join(meta), ""]
        if s.get("summary"):
            L += [s["summary"], ""]
        if s.get("topics"):
            L += ["*" + " · ".join(s["topics"]) + "*", ""]

        if s.get("anecdotes"):
            L += ["**Anecdotes**", ""]
        for a in s.get("anecdotes") or []:
            L.append(f"**[{clock(a['start'])}–{clock(a['end'])}] {a['title']}**"
                     + (f"  `{STRENGTH.get(a.get('strength', ''), '')}`"
                        if a.get("strength") else ""))
            L.append("")
            if a.get("what"):
                L += [a["what"], ""]
            if a.get("why"):
                L += [f"*Why: {a['why']}*", ""]

        for q in s.get("quotes") or []:
            L.append(f"> [{clock(q['start'])}] “{q['text']}”")
            if q.get("why"):
                L.append(f">\n> *{q['why']}*")
            L.append("")

        L += ["**Transcript**", ""]
        for p in c["paragraphs"]:
            who = c.get("names", {}).get(p["speaker"], p["speaker"])
            L += [f"**[{clock(p['start'])}] {who}:** {p['text']}", ""]
        if not c["paragraphs"]:
            L += ["*No speech in this clip.*", ""]

    return "\n".join(L).rstrip() + "\n"


# ---------------------------------------------------------------- plain text

def render_text(doc: dict) -> str:
    L: list[str] = []
    title = f"{_title(doc)} — clip notes"
    line, tail = _provenance(doc)
    L += [title, "=" * len(title), line, ""]
    L += [_wrap(_preamble(doc)), ""]
    if tail:
        L += [_wrap(tail), ""]

    d = doc.get("digest")
    if d:
        L += ["AT A GLANCE", "-" * 11, ""]
        if d.get("summary"):
            L += [_wrap(d["summary"]), ""]
        if d.get("themes"):
            L += [_wrap("Themes: " + ", ".join(d["themes"])), ""]
        for h in d.get("highlights") or []:
            why = f" ({h['why']})" if h.get("why") else ""
            L += [_wrap(f"{h['n']}. {h['clip']} — {h['what']}{why}"), ""]

    L += ["CLIPS", "-" * 5, ""]
    for c in doc["clips"]:
        s = c.get("summary") or {}
        at = f"  {_at(c)}" if _at(c) else ""
        L.append(f"{c['n']:>2}. {c['clip']}  ({clock(c['duration'])}){at}")
        if s.get("headline"):
            L.append(f"    {s['headline']}")
    L.append("")

    for c in doc["clips"]:
        s = c.get("summary") or {}
        head = f"{c['n']}. {s.get('headline') or c['clip']}"
        L += ["", "=" * WIDTH, head, "=" * WIDTH, ""]
        meta = [c["clip"], clock(c["duration"])]
        if _at(c):
            meta.append(_at(c))
        meta.append(f"{c['words']} words")
        L += ["  ".join(meta), ""]
        if s.get("summary"):
            L += [_wrap(s["summary"]), ""]
        if s.get("topics"):
            L += [_wrap("Topics: " + ", ".join(s["topics"])), ""]

        if s.get("anecdotes"):
            L += ["ANECDOTES", ""]
            for a in s["anecdotes"]:
                mark = f" [{a['strength']}]" if a.get("strength") else ""
                L.append(f"  [{clock(a['start'])}-{clock(a['end'])}] "
                         f"{a['title']}{mark}")
                if a.get("what"):
                    L += ["", _wrap(a["what"], indent="    ")]
                if a.get("why"):
                    L += ["", _wrap(f"Why: {a['why']}", indent="    ")]
                L.append("")

        if s.get("quotes"):
            L += ["QUOTES", ""]
            for q in s["quotes"]:
                L += [_wrap(f"[{clock(q['start'])}] \"{q['text']}\"", indent="  ")]
                if q.get("why"):
                    L += [_wrap(q["why"], indent="      ")]
                L.append("")

        L += ["TRANSCRIPT", ""]
        L.append(_clip_body(c))

    return "\n".join(L).rstrip() + "\n"


def _clip_body(clip: dict) -> str:
    if not clip["paragraphs"]:
        return "  (no speech in this clip)\n"
    out = []
    for p in clip["paragraphs"]:
        who = clip.get("names", {}).get(p["speaker"], p["speaker"])
        out.append(f"[{clock(p['start'])}] {who}:")
        out.append(_wrap(p["text"], indent="  "))
        out.append("")
    return "\n".join(out)


def render_clip_text(clip: dict, doc: dict) -> str:
    """One clip on its own, for pasting where only that clip matters."""
    s = clip.get("summary") or {}
    head = s.get("headline") or clip["clip"]
    L = [head, "=" * min(len(head), WIDTH)]
    meta = [clip["clip"], clock(clip["duration"])]
    if _at(clip):
        meta.append(_at(clip))
    L += ["  ".join(meta), ""]
    if s.get("summary"):
        L += [_wrap(s["summary"]), ""]
    for a in s.get("anecdotes") or []:
        L += [f"[{clock(a['start'])}-{clock(a['end'])}] {a['title']}",
              _wrap(a.get("what", ""), indent="  "), ""]
    L += ["-" * WIDTH, ""]
    L.append(_clip_body(clip))
    return "\n".join(L).rstrip() + "\n"
