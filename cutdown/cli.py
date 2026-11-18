"""cutdown command line.

Every stage is invocable on its own against on-disk state. `run` chains them and
skips whatever is already current.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

from . import bwf, pipeline, presets
from .brief import Brief
from .config import CONFIG_NAME, Config, write_default_config
from .context import read_notes_arg
from .util import CutdownError, note, paint, set_quiet


def _add_common(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--config", type=pathlib.Path, help=f"path to {CONFIG_NAME}")
    ap.add_argument("--prompts", type=pathlib.Path,
                    help="directory of prompt templates overriding the built-ins")
    ap.add_argument("-q", "--quiet", action="store_true")


def _add_model(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--provider", help="override the provider for this stage")
    ap.add_argument("--model", help="override the model for this stage")
    ap.add_argument("--dry-run", action="store_true",
                    help="write and print the full prompts, send nothing")


def _add_context(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group("episode context")
    g.add_argument("--host", help="host name")
    g.add_argument("--guest", help="guest name")
    g.add_argument("--show", help="show name")
    g.add_argument("--episode", help="episode id or title")
    g.add_argument("--song", action="append", default=[], metavar="ARTIST - TITLE",
                   help="a record that may come up (repeatable)")
    g.add_argument("--name", action="append", default=[], metavar="NAME",
                   help="a name and its correct spelling, for ASR mangling (repeatable)")
    g.add_argument("--context", metavar="FILE|TEXT",
                   help="free notes on this episode; a path or the text itself")
    g.add_argument("--preset", metavar="NAME",
                   help="start from a saved context preset (`cutdown preset list`)")
    g.add_argument("--detail", metavar="FILE|TEXT",
                   help="today's direction on top of the preset: what is in this "
                        "recording and what to keep; a path or the text itself")
    g.add_argument("--channels", choices=["mixed", "host-left", "guest-left"],
                   help="how the voices sit in the file: mixed (diarize, the "
                        "default), host-left (host on L, guest on R) or guest-left")


def _overrides(a) -> dict:
    over = presets.apply(getattr(a, "preset", None),
                         read_notes_arg(getattr(a, "detail", None)))
    flags = {"host": getattr(a, "host", None), "guest": getattr(a, "guest", None), "show": getattr(a, "show", None),
             "episode": getattr(a, "episode", None), "songs": getattr(a, "song", []),
             "names": getattr(a, "name", []),
             "notes": read_notes_arg(getattr(a, "context", None)),
             "channels": getattr(a, "channels", None)}
    # Explicit flags win over the preset; lists add to it.
    for k, v in flags.items():
        if isinstance(v, list):
            over[k] = list(dict.fromkeys([*over.get(k, []), *v]))
        elif v:
            over[k] = v
    return over


def _config(a, near: pathlib.Path | None = None) -> Config:
    cfg = Config.load(a.config, search_from=near)
    stage = getattr(a, "_stage", None)
    if stage and (getattr(a, "provider", None) or getattr(a, "model", None)):
        s = dict(cfg.get(f"stages.{stage}") or {})
        if a.provider:
            s["provider"] = a.provider
        if a.model:
            s["model"] = a.model
        cfg.data.setdefault("stages", {})[stage] = s
    return cfg


def _paths(build: pathlib.Path, outdir: pathlib.Path | None) -> pipeline.Paths:
    build = build.resolve()
    if outdir is None:
        outdir = build.parent if build.name == "build" else build
    return pipeline.Paths(build=build, outdir=outdir.resolve())


def _prompt_path(prompt: str, hint: str) -> pathlib.Path:
    """Ask for a path, but only when someone is there to answer."""
    if not sys.stdin.isatty():
        raise CutdownError(hint)
    try:
        value = input(prompt).strip()
    except EOFError as e:
        raise CutdownError("no path supplied") from e
    if not value:
        raise CutdownError("no path supplied")
    return clean_path(value)


def clean_path(value: str) -> pathlib.Path:
    """A path as typed, tab-completed, or dragged into the terminal.

    Tab completion escapes spaces and shell characters; dragging a file in from
    Finder does the same, or wraps it in quotes, and leaves a trailing space.
    None of that is part of the path.
    """
    import re
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    else:
        value = re.sub(r"\\(.)", r"\1", value)
    return pathlib.Path(value).expanduser()


def _audio_path(audio: pathlib.Path | None) -> pathlib.Path:
    """Return an explicit audio path, prompting when the CLI is interactive."""
    if audio is not None:
        return audio
    return _prompt_path("Audio file to transcribe: ",
                        "audio path required: pass it as `cutdown transcribe <audio>`")


# ---------------------------------------------------------------- stages

def cmd_transcribe(a) -> int:
    from .transcribe import run_transcribe
    audio = _audio_path(a.audio)
    cfg = _config(a, audio.parent)
    tcfg = {**cfg.get("transcribe", {}), "channels": a.channels or "mixed"}
    out = run_transcribe(audio, a.outdir, tcfg, dry_run=a.dry_run,
                         recover=not a.force)
    note(f"next: cutdown ingest {out} --guest '<name>' -o <build>")
    return 0


def cmd_ingest(a) -> int:
    from .ingest import run_ingest
    cfg = _config(a, a.json_in.parent)
    p = _paths(a.outdir, None)
    ctx = pipeline.load_context(p, overrides=_overrides(a), config=cfg)
    ing = cfg.get("ingest", {})
    for flag, key in (("gap_split", "gap_split"), ("max_dur", "max_dur"),
                      ("section_gap", "section_gap")):
        if getattr(a, flag) is not None:
            ing[key] = getattr(a, flag)
    if a.no_repair:
        ing["repair"] = False
    cfg.data["ingest"] = ing
    from .channels import guest_speaker
    run_ingest(a.json_in, p.build, guest=a.guest or ctx.guest,
               guest_speaker=guest_speaker(ctx.channels),
               **{k: v for k, v in ing.items() if k in pipeline.INGEST_KEYS})
    pipeline.mark_done("ingest", p, cfg, whisper_json=a.json_in.resolve())
    return 0


def cmd_discover(a) -> int:
    from .discover import run_discover
    a._stage = "discover"
    cfg = _config(a, a.build)
    p = _paths(a.build, None)
    ctx = pipeline.load_context(p, overrides=_overrides(a), config=cfg)
    run_discover(p.build, cfg, ctx, out=a.outfile, run_dir=p.run_dir("discover"),
                 dry_run=a.dry_run, prompts_dir=a.prompts)
    if not a.dry_run:
        pipeline.mark_done("discover", p, cfg, brief_path=a.outfile.resolve(),
                           prompts_dir=a.prompts)
    return 0


def cmd_label(a) -> int:
    from .label import run_label
    a._stage = "label"
    cfg = _config(a, a.build)
    p = _paths(a.build, None)
    brief = Brief.load(a.brief) if a.brief else None
    ctx = pipeline.load_context(p, brief=brief, overrides=_overrides(a), config=cfg)
    run_label(p.build, cfg, ctx, brief=brief, run_dir=p.run_dir("label"),
              dry_run=a.dry_run, prompts_dir=a.prompts)
    if not a.dry_run:
        pipeline.mark_done("label", p, cfg, prompts_dir=a.prompts)
    return 0


def cmd_assemble(a) -> int:
    from .assemble import run_assemble
    a._stage = "assemble"
    cfg = _config(a, a.build)
    p = _paths(a.build, None)
    brief = Brief.load(a.brief)
    ctx = pipeline.load_context(p, brief=brief, overrides=_overrides(a), config=cfg)
    run_assemble(p.build, cfg, ctx, brief, run_dir=p.run_dir("assemble"),
                 dry_run=a.dry_run, prompts_dir=a.prompts)
    if not a.dry_run:
        pipeline.mark_done("assemble", p, cfg, brief_path=a.brief.resolve(),
                           prompts_dir=a.prompts)
    return 0


def cmd_revise(a) -> int:
    from .revise import run_revise
    cfg = _config(a, a.build)
    p = _paths(a.build, None)
    brief = Brief.load(a.brief)
    ctx = pipeline.load_context(p, brief=brief, overrides=_overrides(a), config=cfg)
    if not pipeline.revise_status(p)["warnings"]:
        raise CutdownError("no warnings to revise")
    run_revise(p.build, cfg, ctx, brief, run_dir=p.run_dir("revise"), prompts_dir=a.prompts)
    for stage in ("assemble", "topics", "check"):
        pipeline.mark_done(stage, p, cfg, brief_path=a.brief.resolve(), prompts_dir=a.prompts)
    return 0


def cmd_check(a) -> int:
    from .check import run_check
    a._stage = "check"
    cfg = _config(a, a.build)
    p = _paths(a.build, None)
    ctx = pipeline.load_context(p, overrides=_overrides(a), config=cfg)
    run_check(p.build, cfg, ctx, run_dir=p.run_dir("check"), dry_run=a.dry_run,
              prompts_dir=a.prompts)
    if not a.dry_run:
        pipeline.mark_done("check", p, cfg, prompts_dir=a.prompts)
    return 0


def cmd_topics(a) -> int:
    from .topics import run_topics
    a._stage = "topics"
    cfg = _config(a, a.build)
    p = _paths(a.build, None)
    brief = Brief.load(a.brief) if a.brief else None
    ctx = pipeline.load_context(p, brief=brief, overrides=_overrides(a), config=cfg)
    if a.keep:
        ctx.extras = True
    run_topics(p.build, cfg, ctx, brief, run_dir=p.run_dir("topics"),
               dry_run=a.dry_run, prompts_dir=a.prompts)
    if not a.dry_run:
        pipeline.mark_done("topics", p, cfg,
                           brief_path=a.brief.resolve() if a.brief else None,
                           prompts_dir=a.prompts)
    return 0


def cmd_render(a) -> int:
    from .render import run_render
    from .report import write_report
    cfg = _config(a, a.build)
    p = _paths(a.build, a.outdir)
    ctx = pipeline.load_context(p, overrides=_overrides(a), config=cfg)
    kw = pipeline.render_kwargs(cfg, a.ffmpeg)
    if a.gap_ms is not None:
        kw["stringout_gap_ms"] = a.gap_ms
    if a.clip_template:
        kw["clip_template"] = a.clip_template
    if a.no_chunks:
        kw["chunks"] = False
    run_render(p.build, p.outdir, a.master, skip_admin_check=a.skip_admin_check, **kw)
    path = write_report(p.build, p.outdir, ctx, fillers=cfg.get("fillers", {}))
    note(f"report -> {path}")
    pipeline.mark_done("render", p, cfg, master=a.master.resolve())
    return 0


# ---------------------------------------------------------------- notes

def cmd_notes(a) -> int:
    """A separate mode: clips already exported from the DAW -> a shareable doc."""
    from .notes import run_notes
    a._stage = "notes"
    clips = list(a.clips) or [_prompt_path(
        "Folder of exported clips: ",
        "clips required: pass them as `cutdown notes <folder-or-files>`")]
    cfg = _config(a, clips[0].parent if clips[0].is_file() else clips[0])
    if a.provider or a.model:
        # One mode, one model: an override meant for the clip notes is meant for
        # the pass across them too.
        d = dict(cfg.get("stages.digest") or {})
        d.update({k: v for k, v in (("provider", a.provider), ("model", a.model))
                  if v})
        cfg.data["stages"]["digest"] = d
    p = _paths(a.outdir / "build", a.outdir)
    ctx = pipeline.load_context(p, overrides=_overrides(a))
    run_notes(clips, p.build, p.outdir, cfg, ctx, run_dir=p.run_dir("notes"),
              host=a.host or "", order=a.order, drop_fillers=a.drop_fillers,
              summarise=not a.no_summary, digest=not a.no_digest,
              force=a.force, resummarise=a.resummarise, dry_run=a.dry_run,
              prompts_dir=a.prompts)
    if a.open and not a.dry_run:
        import subprocess
        subprocess.run(["open", str(p.outdir)], check=False)
    return 0


# ---------------------------------------------------------------- run

def _outdir_for(audio: pathlib.Path | None) -> pathlib.Path:
    """Keep the episode folder in the current working directory."""
    from .render import slugify
    if audio is None:
        raise CutdownError("pass -o <outdir>, or --audio so one can be named for it")
    return pathlib.Path.cwd() / "ep" / (slugify(audio.stem, 48) or "episode")


def make_job(a, outdir: pathlib.Path) -> pipeline.Job:
    cfg = _config(a, outdir)
    p = _paths(outdir / "build", outdir)
    brief = a.brief or (outdir / "brief.yaml")
    audio = a.audio.expanduser().resolve() if getattr(a, "audio", None) else None
    return pipeline.Job(
        paths=p, config=cfg, brief_path=brief.resolve(), audio=audio,
        whisper_json=a.json_in.resolve() if getattr(a, "json_in", None) else None,
        master=a.master.expanduser().resolve() if getattr(a, "master", None) else None,
        prompts_dir=getattr(a, "prompts", None), dry_run=getattr(a, "dry_run", False),
        overrides=_overrides(a), ffmpeg=getattr(a, "ffmpeg", None),
        skip_check=getattr(a, "no_check", False))


def cmd_run(a) -> int:
    if a.audio and not a.audio.exists():
        raise CutdownError(f"audio not found: {a.audio}")
    outdir = a.outdir or _outdir_for(a.audio or a.json_in)
    job = make_job(a, outdir)
    if not (a.audio or a.json_in or job.io()["whisper_json"]):
        raise CutdownError("give either --json <whisperx.json> or --audio <wav>")

    forced = set(a.force or [])
    if "all" in forced:
        forced = set(pipeline.STAGES)
    for s in forced:
        if s not in pipeline.STAGES:
            raise CutdownError(f"--force {s!r}: unknown stage. "
                               f"Choose from {', '.join(pipeline.STAGES)} or 'all'.")
    stages = list(pipeline.STAGES)
    if a.until:
        if a.until not in stages:
            raise CutdownError(f"--until {a.until!r}: unknown stage")
        stages = stages[:stages.index(a.until) + 1]

    ctx = job.context(job.brief())
    note(paint("cutdown", "bold") + paint(f"  {outdir}", "dim")
         + (paint(f"  preset {ctx.preset}", "cyan") if ctx.preset else "")
         + (paint("  DRY RUN", "yellow") if job.dry_run else ""))
    note(paint(ctx.block(), "dim"))
    def decide(kind, question, detail):
        if kind == "revise" and a.revise: return True
        if kind == "revise" and a.no_revise: return False
        if not sys.stdin.isatty(): return False
        note(paint(detail, "dim"))
        try:
            return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False
    results = pipeline.run_all(job, stages, forced=forced, decide=decide)
    _summary(job, results)
    if job.dry_run:
        note("--dry-run: prompts written, nothing sent, nothing rendered.")
    return 0


def _summary(job: pipeline.Job, results: dict[str, str]) -> None:
    marks = {"done": paint("done", "green"), "skipped": paint("current", "dim"),
             "dry": paint("dry-run", "yellow"), "revised": paint("revised", "green"), "declined": paint("not revised", "dim")}
    note("")
    for s, r in results.items():
        t = job.timings.get(s)
        note(f"  {s:<11} {marks.get(r, r):<18} "
             + (paint(f"{t:6.1f}s", "dim") if t is not None and r == "done" else ""))
    rj = job.paths.outdir / "render.json"
    if results.get("render") == "done" and rj.exists():
        from .util import read_json
        r = read_json(rj)
        d = r.get("dirs") or {}
        note("")
        note(paint("output", "bold") + f"  {job.paths.outdir}")
        note(f"  {d.get('final', '1-final')}/   {len(r.get('segments', []))} songs"
             f", {len(r.get('topics', []))} topics")
        note(f"  {d.get('clips', '2-clips')}/   {r.get('clips', 0)} clips")
        if r.get("chunks"):
            note(f"  {d.get('chunks', '3-chunks')}/  {len(r['chunks'])} unedited chunks")
        note("  report.md")


# ---------------------------------------------------------------- utilities

def cmd_studio(a) -> int:
    from .studio import launch
    if a.audio and not a.audio.expanduser().exists():
        raise CutdownError(f"audio not found: {a.audio}")
    if a.outdir is None:
        a.outdir = _outdir_for(a.audio.expanduser()) if a.audio else pathlib.Path(".")
    return launch(a)


def cmd_start(a) -> int:
    """`cutdown` on its own: the whole job from one command.

    Asks for the audio (drag it into the terminal), opens the studio on its
    episode folder, asks for the context on a new episode, and runs every stage
    that is not already done.
    """
    audio = getattr(a, "audio", None)
    if audio is None:
        note(paint("cutdown", "bold")
             + paint("  drag the recording in, then press return", "dim"))
        audio = _prompt_path("Audio file: ", "run `cutdown <audio file>`, or "
                             "`cutdown --help` for every command")
    if not audio.exists():
        raise CutdownError(f"audio not found: {audio}")
    a.audio, a.outdir, a.autorun = audio, _outdir_for(audio), True
    return cmd_studio(a)


# ---------------------------------------------------------------- presets

def cmd_preset(a) -> int:
    import os
    import subprocess
    act = a.action
    if act == "list":
        names = presets.names()
        if not names:
            note(f"no presets yet in {presets.presets_dir()}")
            note("  make one:  cutdown preset new <name>")
            note("  or keep an episode's context:  cutdown preset save <name> --from ep/<x>")
            return 0
        for n in names:
            d = presets.load(n)
            first = (d.get("instructions") or d.get("notes") or "").strip().splitlines()
            note(paint(f"{n:<18}", "bold") + " "
                 + " / ".join(str(d[k]) for k in ("show",) if d.get(k))
                 + (paint(f"  {first[0][:70]}", "dim") if first else ""))
        note(paint(f"\n{presets.presets_dir()}", "dim"))
        return 0
    if not a.pname:
        raise CutdownError(f"`cutdown preset {act}` needs a preset name")
    if act == "show":
        note(presets.path_for(a.pname).read_text() if presets.path_for(a.pname).exists()
             else presets.load(a.pname))
        return 0
    if act == "rm":
        note(f"deleted {presets.delete(a.pname)}")
        return 0
    if act in ("new", "edit"):
        path = presets.path_for(a.pname)
        if not path.exists():
            if act == "edit":
                presets.load(a.pname)  # raises with the list of what exists
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"name: {presets.slug(a.pname)}\n" + presets.TEMPLATE)
        subprocess.run([os.environ.get("EDITOR", "vi"), str(path)])
        presets.load(a.pname)  # validate what was written
        note(f"saved {path}")
        return 0
    if act == "save":
        from . import context as ctxmod
        data = {}
        if a.source:
            build = a.source / "build" if (a.source / "build").is_dir() else a.source
            ctx = ctxmod.load(build)
            if ctx.is_empty():
                raise CutdownError(f"no context in {build} to save")
            data = presets.from_context(ctx)
            if a.with_detail and ctx.detail:
                data["instructions"] = ctx.direction()
        over = _overrides(a)
        over.pop("preset", None)
        detail = over.pop("detail", "")
        data.update({k: v for k, v in over.items() if k not in ("guest", "episode")})
        if detail:
            data["instructions"] = "\n\n".join(
                x for x in (data.get("instructions", ""), detail) if x)
        if a.extras is not None:
            data["extras"] = a.extras == "keep"
        if not any(v not in (None, "", [], {}) for v in data.values()):
            raise CutdownError("nothing to save: pass --from <episode>, or context "
                               "flags such as --show and --detail")
        note(f"saved {presets.save(a.pname, data)}")
        return 0
    raise CutdownError(f"unknown preset action {act!r}")


# ---------------------------------------------------------------- models

def cmd_models(a) -> int:
    """What the configured providers offer, and what each stage is routed to."""
    import json
    import urllib.request
    from .providers import get_named_provider
    from .providers.api import open_api
    cfg = _config(a)
    note(paint("routing", "bold"))
    for stage in [*(s for s in pipeline.STAGES if s in pipeline.MODEL_STAGES),
                  "notes", "digest"]:
        r = cfg.get(f"stages.{stage}") or {}
        fb = (f"  fallback {r['fallback_provider']}/{r['fallback_model']}"
              if r.get("fallback_provider") else "")
        note(f"  {stage:<9} {r.get('provider', '?')}/{r.get('model', '?')}"
             + paint(fb, "dim"))
    routing = cfg.get("stages.revise")
    note("  revise    " + (f"{routing['provider']}/{routing['model']}" if routing else "(same as assemble)"))
    name = a.provider or cfg.get("stages.discover.provider") or "openrouter"
    prov = get_named_provider(cfg, name)
    if prov.kind != "api":
        return 0
    prov.preflight()
    url = prov.cfg["base_url"].rstrip("/") + "/models"
    req = urllib.request.Request(url, headers=prov._headers())
    try:
        with open_api(req, timeout=30) as r:
            listed = {m["id"]: m for m in json.loads(r.read().decode())["data"]}
    except Exception as e:  # noqa: BLE001 - network, auth, shape: all reported
        raise CutdownError(f"could not list models at {url}: {e}")
    ids = sorted(i for i in listed if not a.match or a.match.lower() in i.lower())
    note(paint(f"\n{name}: {len(ids)} models", "bold") + paint(f"  {url}", "dim"))
    for i in ids:
        # OpenRouter lists USD per token; OpenCode lists ids only.
        price = listed[i].get("pricing") or {}
        try:
            cost = (f"${float(price['prompt']) * 1e6:.3f} in / "
                    f"${float(price['completion']) * 1e6:.3f} out per M")
        except (KeyError, TypeError, ValueError):
            cost = ""
        note(f"  {i}  " + paint(f"[{prov.wires_for(i)[0]}] {cost}".rstrip(), "dim"))
    return 0


def cmd_verify_bwf(a) -> int:
    for path in a.wavs:
        info = bwf.read_bext(path)
        if info is None:
            note(f"{path}: NO bext chunk")
            continue
        cross = bwf.read_bext_via_bwfmetaedit(path)
        agree = ""
        if cross and "TimeReference" in cross:
            agree = (" (bwfmetaedit agrees)"
                     if int(cross["TimeReference"]) == info["time_reference"]
                     else f" (bwfmetaedit says {cross['TimeReference']} - DISAGREES)")
        note(f"{path}: TimeReference {info['time_reference']}"
             f" = {info['time_reference'] / (a.rate or 48000):.3f}s @ "
             f"{a.rate or 48000} Hz{agree}")
        note(f"    v{info['version']}  {info['description']!r}  "
             f"{info['originator']} {info['origination_date']}")
    return 0


def cmd_init(a) -> int:
    path = a.outfile
    if path.exists() and not a.force:
        raise CutdownError(f"{path} exists. Pass --force to overwrite.")
    write_default_config(path)
    note(f"wrote {path}")
    return 0


# ---------------------------------------------------------------- parser

def _commands(ap: argparse.ArgumentParser) -> set[str]:
    for act in ap._actions:
        if isinstance(act, argparse._SubParsersAction):
            return set(act.choices)
    return set()


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="cutdown",
        description="Cut a diarized interview into track-anchored radio segments. "
                    "Run with no arguments to pick an audio file and open the studio.")
    ap.set_defaults(func=cmd_start, host=None, config=None, prompts=None, quiet=False,
                    audio=None, brief=None, json_in=None, master=None, guest=None,
                    show=None, episode=None, song=[], name=[], context=None)
    ap.add_argument("--preset", metavar="NAME",
                    help="with no command: start from this preset")
    ap.add_argument("--detail", metavar="FILE|TEXT",
                    help="with no command: today's direction for this recording")
    ap.add_argument("--channels", choices=["mixed", "host-left", "guest-left"],
                    help="with no command: host-left or guest-left when each "
                         "speaker is on their own channel")
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("transcribe", help="run WhisperX with diarization")
    s.add_argument("audio", nargs="?", type=pathlib.Path,
                   help="audio file; omit to enter its path interactively")
    s.add_argument("-o", "--outdir", type=pathlib.Path, default=pathlib.Path("out"))
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--force", action="store_true",
                   help="transcribe again even when a completed transcript is present")
    s.add_argument("--channels", choices=["mixed", "host-left", "guest-left"],
                   help="host-left / guest-left: one speaker per channel; each is "
                        "transcribed on its own and merged (no diarization)")
    _add_common(s)
    s.set_defaults(func=cmd_transcribe)

    s = sub.add_parser("ingest", help="WhisperX JSON -> units and sections")
    s.add_argument("json_in", type=pathlib.Path)
    s.add_argument("-o", "--outdir", type=pathlib.Path, default=pathlib.Path("build"))
    s.add_argument("--gap-split", type=float)
    s.add_argument("--max-dur", type=float)
    s.add_argument("--section-gap", type=float,
                   help="a silence this long (seconds) starts a new section")
    s.add_argument("--no-repair", action="store_true",
                   help="skip the late-diarization-boundary repair pass")
    _add_context(s)
    _add_common(s)
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("discover", help="propose a draft brief from the transcript")
    s.add_argument("build", type=pathlib.Path)
    s.add_argument("-o", "--outfile", type=pathlib.Path,
                   default=pathlib.Path("brief.yaml"))
    _add_context(s)
    _add_model(s)
    _add_common(s)
    s.set_defaults(func=cmd_discover)

    s = sub.add_parser("label", help="tag units with topic and role")
    s.add_argument("build", type=pathlib.Path)
    s.add_argument("--brief", type=pathlib.Path,
                   help="optional; gives the model the track list to tag against")
    _add_context(s)
    _add_model(s)
    _add_common(s)
    s.set_defaults(func=cmd_label)

    s = sub.add_parser("assemble", help="choose and order units per segment")
    s.add_argument("build", type=pathlib.Path)
    s.add_argument("--brief", type=pathlib.Path, required=True)
    _add_context(s)
    _add_model(s)
    _add_common(s)
    s.set_defaults(func=cmd_assemble)

    s = sub.add_parser("revise", help="revise the checked edit once")
    s.add_argument("build", type=pathlib.Path)
    s.add_argument("--brief", type=pathlib.Path, required=True)
    _add_context(s)
    _add_common(s)
    s.set_defaults(func=cmd_revise)

    s = sub.add_parser("check", help="craft check on the assembled text")
    s.add_argument("build", type=pathlib.Path)
    _add_context(s)
    _add_model(s)
    _add_common(s)
    s.set_defaults(func=cmd_check)

    s = sub.add_parser("topics", help="group the leftover talk into topic files")
    s.add_argument("build", type=pathlib.Path)
    s.add_argument("--brief", type=pathlib.Path,
                   help="its `extras:` decides whether this runs, and gives topic hints")
    s.add_argument("--keep", action="store_true",
                   help="run even when the brief says leftover talk is not kept")
    _add_context(s)
    _add_model(s)
    _add_common(s)
    s.set_defaults(func=cmd_topics)

    s = sub.add_parser("render", help="write the final files, clips, chunks and manifest")
    s.add_argument("build", type=pathlib.Path)
    s.add_argument("--master", type=pathlib.Path, required=True)
    s.add_argument("-o", "--outdir", type=pathlib.Path)
    s.add_argument("--gap-ms", type=int, help="silence between clips in a stringout")
    s.add_argument("--ffmpeg", help="ffmpeg binary, used only for non-PCM masters")
    s.add_argument("--clip-template", metavar="TPL",
                   help="clip filename template. Tokens: seg, clip, title, artist, "
                        "kind, ids, first_id, last_id, episode, guest. "
                        "Default: {seg}_{clip:02d}_{title}_{ids}")
    s.add_argument("--no-chunks", action="store_true",
                   help="do not write the unedited chunks folder")
    s.add_argument("--skip-admin-check", action="store_true",
                   help="render without labels.jsonl present (admin talk NOT filtered)")
    _add_context(s)
    _add_common(s)
    s.set_defaults(func=cmd_render)

    s = sub.add_parser(
        "notes", help="exported clips -> readable transcript + AI summaries",
        description="Read a folder of clips exported from a finished edit and "
                    "write notes.md / notes.txt: a verbatim transcript per clip, "
                    "with the anecdotes in it summarised and timestamped.")
    s.add_argument("clips", nargs="*", type=pathlib.Path,
                   help="clip files or the folder holding them; omit to be asked")
    s.add_argument("-o", "--outdir", type=pathlib.Path, default=pathlib.Path("notes"))
    s.add_argument("--order", choices=["time", "name"], default="time",
                   help="session order from the clips' BWF stamps, or filename "
                        "order (default: time, falling back to name)")
    s.add_argument("--drop-fillers", action="store_true",
                   help="remove standalone um/uh from the transcript")
    s.add_argument("--no-summary", action="store_true",
                   help="transcript only; no model call at all")
    s.add_argument("--no-digest", action="store_true",
                   help="per-clip notes only; skip the pass across all of them")
    s.add_argument("--resummarise", action="store_true",
                   help="ask again for the summaries, keeping the transcripts")
    s.add_argument("--force", action="store_true",
                   help="transcribe and summarise everything again")
    s.add_argument("--open", action="store_true",
                   help="reveal the output folder when it is written")
    _add_context(s)
    _add_model(s)
    _add_common(s)
    s.set_defaults(func=cmd_notes)

    s = sub.add_parser(
        "run", help="chain every stage, skipping what is current",
        description="Audio in, finished folders out. With no brief, discover "
                    "drafts one from the talk and the run carries on; check it "
                    "afterwards in <outdir>/brief.yaml.")
    s.add_argument("--audio", type=pathlib.Path, help="transcribe this first")
    s.add_argument("--json", dest="json_in", type=pathlib.Path,
                   help="an existing WhisperX JSON instead of --audio")
    s.add_argument("--brief", type=pathlib.Path,
                   help="run sheet (default <outdir>/brief.yaml, drafted if missing)")
    s.add_argument("--master", type=pathlib.Path,
                   help="WAV to cut from (default: the brief's, else --audio)")
    s.add_argument("-o", "--outdir", type=pathlib.Path,
                   help="episode folder (default ep/<audio name>)")
    s.add_argument("--force", action="append", default=[], metavar="STAGE",
                   help=f"re-run one stage: {', '.join(pipeline.STAGES)}, or all")
    s.add_argument("--until", metavar="STAGE",
                   help="stop after this stage (e.g. --until discover to review "
                        "the brief first)")
    s.add_argument("--revise", action="store_true", help='answer yes to "Revise?" after check')
    s.add_argument("--no-revise", action="store_true", help="answer no")
    s.add_argument("--no-check", action="store_true", help="skip the craft check")
    s.add_argument("--ffmpeg")
    s.add_argument("--dry-run", action="store_true")
    _add_context(s)
    _add_common(s)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("studio", help="full-screen interface for the whole pipeline")
    s.add_argument("-o", "--outdir", type=pathlib.Path,
                   help="episode folder (default ep/<audio name>, else .)")
    s.add_argument("--brief", type=pathlib.Path)
    s.add_argument("--audio", type=pathlib.Path)
    s.add_argument("--json", dest="json_in", type=pathlib.Path)
    s.add_argument("--master", type=pathlib.Path)
    _add_context(s)
    _add_common(s)
    s.set_defaults(func=cmd_studio)

    s = sub.add_parser(
        "preset", help="saved context for a show: list, new, edit, save, show, rm",
        description="Presets hold what is the same every episode - the show, names, "
                    "how the recording is made, what to keep. Load one with "
                    "--preset NAME and add today's --detail on top.")
    s.add_argument("action", choices=["list", "show", "new", "edit", "save", "rm"],
                   nargs="?", default="list")
    s.add_argument("pname", nargs="?", metavar="name")
    s.add_argument("--from", dest="source", type=pathlib.Path, metavar="EPISODE",
                   help="save: start from this episode folder's context")
    s.add_argument("--with-detail", action="store_true",
                   help="save: fold that episode's --detail into the instructions")
    s.add_argument("--extras", choices=["keep", "drop"],
                   help="save: keep leftover talk as topic files, or not")
    _add_context(s)
    _add_common(s)
    s.set_defaults(func=cmd_preset)

    s = sub.add_parser("models", help="list the provider's models and the routing")
    s.add_argument("--provider",
                   help="provider to list (default: the one discover is routed to)")
    s.add_argument("--match", help="only list model ids containing this text")
    _add_common(s)
    s.set_defaults(func=cmd_models)

    s = sub.add_parser("verify-bwf", help="read the BWF timestamp back off a clip")
    s.add_argument("wavs", nargs="+", type=pathlib.Path)
    s.add_argument("--rate", type=int, help="sample rate for the seconds figure")
    _add_common(s)
    s.set_defaults(func=cmd_verify_bwf)

    s = sub.add_parser("init", help=f"write a starter {CONFIG_NAME}")
    s.add_argument("-o", "--outfile", type=pathlib.Path,
                   default=pathlib.Path(CONFIG_NAME))
    s.add_argument("--force", action="store_true")
    _add_common(s)
    s.set_defaults(func=cmd_init)

    return ap


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    ap = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    # `cutdown today.wav` is `cutdown` with the file already given.
    audio = None
    if argv and not argv[0].startswith("-") and argv[0] not in _commands(ap):
        audio = clean_path(argv[0])
        if audio.is_file():
            argv = argv[1:]
        else:
            audio = None
    a = ap.parse_args(argv)
    if audio is not None and getattr(a, "cmd", None) is None:
        a.audio = audio
    set_quiet(getattr(a, "quiet", False))
    try:
        return a.func(a) or 0
    except CutdownError as e:
        print(f"\ncutdown: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
