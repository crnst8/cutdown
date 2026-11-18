"""Stage orchestration, shared by the CLI and the studio.

Every stage is individually invocable and writes its state to disk, so a failed
model call at stage 4 never costs the stage 2 work. `run` walks the stages in
order and skips any whose declared inputs, outputs and config slice are unchanged.

`Job` holds everything one episode needs (paths, config, brief, audio, master)
and `run_stage` is the only place a stage is dispatched, so `cutdown run` and the
studio cannot drift apart.
"""
from __future__ import annotations

import pathlib
import time
from dataclasses import dataclass, field

from . import context as ctxmod
from . import state
from .util import CutdownError, note, now_stamp, stage_line, read_json, write_json

STAGES = ["transcribe", "ingest", "discover", "label", "assemble", "topics",
          "check", "render"]
MODEL_STAGES = {"discover", "label", "assemble", "topics", "check"}

STAGE_HELP = {
    "transcribe": "WhisperX + diarization",
    "ingest": "words -> units, sections",
    "discover": "draft the brief from the talk",
    "label": "topic + role per unit",
    "assemble": "choose and order per song",
    "topics": "group leftover talk by topic",
    "check": "craft check, cold read",
    "render": "final, clips, chunks",
}

INGEST_KEYS = ("gap_split", "max_dur", "min_gap", "pad", "repair",
               "repair_max_words", "section_gap", "section_max")


@dataclass
class Paths:
    build: pathlib.Path
    outdir: pathlib.Path

    @property
    def runs(self) -> pathlib.Path:
        return self.outdir / "runs"

    def run_dir(self, stage: str) -> pathlib.Path:
        return self.runs / f"{now_stamp()}-{stage}"


def stage_io(stage: str, p: Paths, *, whisper_json: pathlib.Path | None = None,
             brief_path: pathlib.Path | None = None,
             master: pathlib.Path | None = None) -> tuple[list, list]:
    """Declared (inputs, outputs) used for the up-to-date comparison."""
    b = p.build
    if stage == "transcribe":
        return ([], [whisper_json] if whisper_json else [])
    if stage == "ingest":
        return ([whisper_json] if whisper_json else [],
                [b / "units.jsonl", b / "units.txt", b / "timeline.json",
                 b / "unit_words.json", b / "sections.json"])
    if stage == "discover":
        return ([b / "units.jsonl", b / "context.json"],
                [brief_path] if brief_path else [])
    if stage == "label":
        return ([b / "units.jsonl", b / "context.json"], [b / "labels.jsonl"])
    if stage == "assemble":
        return ([b / "labels.jsonl", b / "context.json", b / "units.jsonl",
                 b / "timeline.json", b / "unit_words.json", b / "sections.json"]
                + ([brief_path] if brief_path else []),
                [b / "segments.json"])
    if stage == "topics":
        return ([b / "segments.json", b / "labels.jsonl", b / "context.json",
                 b / "units.jsonl", b / "timeline.json", b / "sections.json"]
                + ([brief_path] if brief_path else []),
                [b / "topics.json"])
    if stage == "check":
        return ([b / "segments.json", b / "topics.json", b / "context.json",
                 b / "units.jsonl", b / "timeline.json", b / "unit_words.json"],
                [b / "check.json"])
    if stage == "render":
        return ([b / "segments.json", b / "timeline.json", b / "topics.json",
                 b / "sections.json", b / "check.json", b / "units.jsonl",
                 b / "unit_words.json", b / "labels.jsonl"]
                + ([master] if master else []),
                [p.outdir / "manifest.csv", p.outdir / "render.json",
                 p.outdir / "report.md"])
    raise CutdownError(f"unknown stage {stage!r}")


def is_current(stage: str, p: Paths, config, **io) -> bool:
    prompts_dir = io.pop("prompts_dir", None)
    inputs, outputs = stage_io(stage, p, **io)
    if not outputs:
        return False
    return state.is_current(p.build, stage, inputs=inputs, outputs=outputs,
                            config_hash=config.stage_hash(stage, prompts_dir))


def mark_done(stage: str, p: Paths, config, **io) -> None:
    prompts_dir = io.pop("prompts_dir", None)
    inputs, outputs = stage_io(stage, p, **io)
    state.record(p.build, stage, inputs=inputs, outputs=outputs,
                 config_hash=config.stage_hash(stage, prompts_dir))


def load_context(p: Paths, brief=None, overrides: dict | None = None, config=None):
    """Merge, persist and return the episode context.

    With `config`, the context's own config overlay (usually from a preset) is
    applied to it, so stage hashes and routing see it.
    """
    from . import channels
    ctx = ctxmod.load(p.build, brief=brief, overrides=overrides)
    channels.check(ctx.channels)
    p.build.mkdir(parents=True, exist_ok=True)
    ctxmod.save(p.build, ctx)
    if config is not None:
        if ctx.config:
            config.overlay(ctx.config)
        # The channel layout decides how transcribe runs and who ingest calls the
        # guest, so it belongs in both stages' config hashes.
        mode = channels.check(ctx.channels)
        config.data.setdefault("transcribe", {})["channels"] = mode
        config.data.setdefault("ingest", {})["channels"] = mode
    return ctx


def summarise_skip(stage: str) -> None:
    note(f"  {stage}: up to date, skipped")


# ---------------------------------------------------------------- job

@dataclass
class Job:
    """One episode's worth of inputs, shared by `cutdown run` and the studio."""
    paths: Paths
    config: object
    brief_path: pathlib.Path
    audio: pathlib.Path | None = None
    whisper_json: pathlib.Path | None = None
    master: pathlib.Path | None = None
    prompts_dir: pathlib.Path | None = None
    dry_run: bool = False
    overrides: dict | None = None
    ffmpeg: str | None = None
    skip_check: bool = False
    on_line: object = None
    on_proc: object = None
    timings: dict = field(default_factory=dict)

    def io(self) -> dict:
        wj = self.whisper_json
        if wj is None and self.audio:
            wj = self.paths.build / "transcript" / f"{self.audio.stem}.json"
        return {"whisper_json": wj.resolve() if wj else None,
                "brief_path": self.brief_path,
                "master": self.resolve_master(), "prompts_dir": self.prompts_dir}

    def brief(self):
        from .brief import Brief
        return Brief.load(self.brief_path) if self.brief_path.exists() else None

    def resolve_master(self) -> pathlib.Path | None:
        """--master, else the brief's `master:` (relative to the brief), else audio."""
        if self.master:
            return self.master.resolve()
        if self.brief_path.exists():
            from .brief import Brief
            try:
                m = Brief.load(self.brief_path).master
            except CutdownError:
                m = ""
            if m:
                mp = pathlib.Path(m).expanduser()
                mp = mp if mp.is_absolute() else self.brief_path.parent / mp
                if mp.exists():
                    return mp.resolve()
        if self.audio and self.audio.suffix.lower() in (".wav", ".wave"):
            return self.audio.resolve()
        return None

    def context(self, brief=None):
        return load_context(self.paths, brief=brief, overrides=self.overrides,
                            config=self.config)


def run_stage(job: Job, stage: str, *, force: bool = False) -> str:
    """Run one stage if it is not current. Returns "done", "skipped" or "dry"."""
    from .assemble import run_assemble
    from .check import run_check
    from .discover import run_discover
    from .ingest import run_ingest
    from .label import run_label
    from .render import run_render
    from .report import write_report
    from .topics import run_topics
    from .transcribe import run_transcribe

    p, cfg = job.paths, job.config
    p.build.mkdir(parents=True, exist_ok=True)
    brief = job.brief()
    ctx = job.context(brief)
    io = job.io()
    if force:
        state.clear(p.build, stage)

    # A brief is the producer's to edit. Discover drafts one when there is none;
    # after that it only runs when asked, so a corrected brief is never lost.
    if stage == "discover" and not force and job.brief_path.exists():
        summarise_skip("discover")
        return "skipped"
    if stage == "check" and job.skip_check:
        note("check: skipped (--no-check)")
        return "skipped"
    if not force and is_current(stage, p, cfg, **io):
        summarise_skip(stage)
        return "skipped"

    t0 = time.monotonic()
    if stage == "transcribe":
        if not job.audio:
            if io["whisper_json"] and io["whisper_json"].exists():
                summarise_skip("transcribe")
                return "skipped"
            raise CutdownError("no audio to transcribe: pass --audio, or --json "
                               "with an existing WhisperX transcript")
        out = run_transcribe(job.audio, p.build / "transcript",
                             cfg.get("transcribe", {}), dry_run=job.dry_run,
                             on_line=job.on_line, on_proc=job.on_proc,
                             recover=not force)
        io["whisper_json"] = out.resolve()
    elif stage == "ingest":
        wj = io["whisper_json"]
        if not wj or not wj.exists():
            raise CutdownError("no WhisperX JSON - transcribe first, or pass --json")
        from .channels import guest_speaker
        run_ingest(wj, p.build, guest=ctx.guest,
                   guest_speaker=guest_speaker(ctx.channels),
                   **{k: v for k, v in cfg.get("ingest", {}).items()
                      if k in INGEST_KEYS})
    elif stage == "discover":
        run_discover(p.build, cfg, ctx, out=job.brief_path,
                     run_dir=p.run_dir("discover"), dry_run=job.dry_run,
                     prompts_dir=job.prompts_dir,
                     audio=job.master or job.audio)
    elif stage == "label":
        run_label(p.build, cfg, ctx, brief=brief, run_dir=p.run_dir("label"),
                  dry_run=job.dry_run, prompts_dir=job.prompts_dir)
    elif stage == "assemble":
        if brief is None:
            raise CutdownError(f"no brief at {job.brief_path} - run discover first")
        run_assemble(p.build, cfg, ctx, brief, run_dir=p.run_dir("assemble"),
                     dry_run=job.dry_run, prompts_dir=job.prompts_dir)
    elif stage == "topics":
        run_topics(p.build, cfg, ctx, brief, run_dir=p.run_dir("topics"),
                   dry_run=job.dry_run, prompts_dir=job.prompts_dir)
    elif stage == "check":
        run_check(p.build, cfg, ctx, run_dir=p.run_dir("check"),
                  dry_run=job.dry_run, prompts_dir=job.prompts_dir)
    elif stage == "render":
        if job.dry_run:
            note("render: skipped on --dry-run")
            return "dry"
        master = io["master"]
        if not master:
            raise CutdownError("no master WAV - pass --master or set `master:` "
                               "in the brief")
        run_render(p.build, p.outdir, master, **render_kwargs(cfg, job.ffmpeg))
        note(f"report -> {write_report(p.build, p.outdir, ctx, fillers=cfg.get('fillers', {}))}")
    else:
        raise CutdownError(f"unknown stage {stage!r}")

    job.timings[stage] = time.monotonic() - t0
    if job.dry_run:
        return "dry"
    mark_done(stage, p, cfg, **io)
    return "done"


def render_kwargs(cfg, ffmpeg: str | None = None) -> dict:
    r = cfg.get("render", {})
    return {
        "fillers": cfg.get("fillers", {}),
        "stringout_gap_ms": r.get("stringout_gap_ms", 500),
        "ffmpeg": ffmpeg or r.get("ffmpeg", "ffmpeg"),
        "clip_template": r.get("clip_template", "{seg}_{clip:02d}_{title}_{ids}"),
        "stringout_template": r.get("stringout_template", "{seg}_{title}"),
        "dirs": r.get("dirs") or {},
        "chunks": r.get("chunks", True),
    }


def run_all(job: Job, stages: list[str] | None = None, *,
            forced: set[str] | None = None, decide=None) -> dict[str, str]:
    """Run stages in order; returns stage -> outcome."""
    forced = forced or set()
    results: dict[str, str] = {}
    for s in stages or STAGES:
        stage_line(s, STAGE_HELP.get(s, ""))
        results[s] = run_stage(job, s, force=s in forced)
        if s == "ingest": maybe_swap(job, decide)
        if s == "check":
            result = maybe_revise(job, decide)
            if result != "none": results["revise"] = result
    return results


PROMPT_STATE = "prompts.json"


def revise_status(p: Paths) -> dict:
    cp = p.build / "check.json"
    chk = read_json(cp) if cp.exists() else {}
    warnings = chk.get("warnings", [])
    groups = []
    for name, key in (("segments.json", "segments"), ("topics.json", "topics")):
        path = p.build / name
        if path.exists(): groups += read_json(path).get(key, [])
    path = p.build / ".state" / PROMPT_STATE
    saved = read_json(path) if path.exists() else {}
    return {"warnings": len(warnings), "groups": len({w.get("segment") for w in warnings}),
            "pieces": sum(bool(g.get("order") or g.get("ids")) for g in groups),
            "revised": bool(chk.get("revision")),
            "declined": cp.exists() and saved.get("revise_declined") == state._stamp(cp)}


def maybe_revise(job: Job, decide) -> str:
    p, cfg = job.paths, job.config
    status = revise_status(p)
    if job.dry_run or job.skip_check or decide is None or not status["warnings"] or status["revised"] or status["declined"]:
        return "none"
    if not decide("revise", "Revise?", f"{status['warnings']} warnings in {status['groups']} of {status['pieces']} pieces"):
        path = p.build / ".state" / PROMPT_STATE
        saved = read_json(path) if path.exists() else {}
        saved["revise_declined"] = state._stamp(p.build / "check.json")
        write_json(path, saved)
        return "declined"
    from . import revise
    stage_line("revise", "fix check warnings, one pass")
    brief = job.brief()
    ctx = job.context(brief)
    revise.run_revise(p.build, cfg, ctx, brief, run_dir=p.run_dir("revise"), prompts_dir=job.prompts_dir)
    for s in ("assemble", "topics", "check"):
        mark_done(s, p, cfg, **job.io())
    return "revised"


def maybe_swap(job: Job, decide) -> bool:
    p = job.paths
    path = p.build / "roles.json"
    if decide is None or job.dry_run or not path.exists(): return False
    roles = read_json(path)
    ctx = ctxmod.load(p.build)
    saved_path = p.build / ".state" / PROMPT_STATE
    saved = read_json(saved_path) if saved_path.exists() else {}
    if roles.get("mode") != "split" or roles.get("verdict") != "swapped" or saved.get("swap_declined") == ctx.channels:
        return False
    detail = f"the side set as guest asks {roles['guest']['rate']:.2f} questions per 100 words; the side set as host asks {roles['host']['rate']:.2f}"
    if not decide("swap", "Host and guest look swapped. Swap them?", detail):
        saved["swap_declined"] = ctx.channels
        write_json(saved_path, saved)
        return False
    ctx.channels = "guest-left" if ctx.channels == "host-left" else "host-left"
    ctxmod.save(p.build, ctx)
    if job.overrides and "channels" in job.overrides: job.overrides["channels"] = ctx.channels
    run_stage(job, "ingest", force=True)
    return True
