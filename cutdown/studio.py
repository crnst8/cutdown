"""`cutdown studio` - the interface a producer actually sits in front of.

One screen for the whole job: transcribe, ingest, discover, label, assemble,
topics, check, render. Stage status down the left with the episode context under
it, the songs and topics that came out on the right, the assembled text under
them, and the log at the bottom. Space auditions the selected file so you can
hear the edit without leaving.

`c` opens the context screen: load a preset, add today's detail, save it back as
a preset. A fresh episode opens on it.

Stages run in a worker thread through `pipeline.run_stage`, the same function
`cutdown run` uses, so nothing here is a second implementation of the pipeline.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import signal
import subprocess
import threading
import time

from . import context as ctxmod
from . import pipeline, presets
from .brief import Brief
from .config import Config
from .util import (CutdownError, mmss, now_stamp, read_json, read_jsonl,
                   set_sink, write_json)

try:
    from rich.markup import escape
except ModuleNotFoundError:  # pragma: no cover
    def escape(x):
        return x

try:
    from textual import on, work
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Horizontal, Vertical, VerticalScroll
    from textual.screen import ModalScreen
    from textual.css.query import NoMatches
    from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                                 RichLog, Select, Static, TextArea)
except ModuleNotFoundError:  # pragma: no cover
    App = None  # type: ignore

STAGE_HELP = pipeline.STAGE_HELP

MARK = {"todo": "○", "done": "●", "stale": "◐", "run": "◑", "fail": "✕",
        "skip": "–"}

# Everything a run leaves behind in the outdir. Moved aside wholesale when the
# session is pointed at a different audio file, because every one of these is
# derived from the audio and would otherwise be read as this episode's work.
# The old flat layout (clips/, stringouts/) is listed so it moves too.
ARTIFACTS = ["build", "runs", "1-final", "2-clips", "3-chunks", "clips",
             "stringouts", "brief.yaml", "render.json", "manifest.csv",
             "rejected.csv", "report.md"]
SOURCE_FILE = ".source.json"


SCREEN_KEYS = ("show", "guest", "host", "episode", "names", "songs",
               "channels", "detail", "extras")


def merged_context(current: ctxmod.EpisodeContext, over: dict) -> ctxmod.EpisodeContext:
    """Screen fields replace stored values; all remaining fields merge."""
    rest = {k: v for k, v in over.items() if k not in SCREEN_KEYS}
    ctx = current.merge(**rest)
    for k in SCREEN_KEYS:
        if k in over: setattr(ctx, k, over[k])
    return ctx


def split_lines(text):
    return list(dict.fromkeys(x.strip() for x in text.splitlines() if x.strip()))


def settings_line(ctx):
    extras = "keep" if ctx.extras is True else "drop" if ctx.extras is False else "decided from the instructions"
    voices = {"host-left":"host L / guest R", "guest-left":"guest L / host R"}.get(ctx.channels,"mixed")
    lines = [f"Leftover chat: {extras}", f"Voices: {voices}"]
    targets = [f"{k} {v[0]}-{v[1]}s" for k,v in ctx.targets.items() if isinstance(v,list) and len(v)==2]
    if targets: lines.append("Targets: " + ", ".join(targets))
    if ctx.preset: lines.append(f"Preset: {ctx.preset}")
    return "\n".join(lines)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "yes", "yes"), Binding("n,escape,enter", "no", "no")]

    def __init__(self, question: str, detail: str = ""):
        super().__init__()
        self.question, self.detail = question, detail

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Label(f"[b]{escape(self.question)}[/b] [y/N]")
            if self.detail:
                yield Label(f"[dim]{escape(self.detail)}[/dim]")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class StageRow(Static):
    """One stage in the left rail. Focusable, so enter/f can act on it."""

    can_focus = True

    def __init__(self, name: str):
        super().__init__(id=f"stage-{name}", classes="stage -todo")
        self.stage = name
        self.status = "todo"
        self.summary = ""
        self.elapsed: float | None = None

    def refresh_row(self, status: str | None = None, summary: str | None = None,
                    elapsed: float | None = None) -> None:
        if status:
            self.remove_class(f"-{self.status}")
            self.status = status
            self.add_class(f"-{status}")
        if summary is not None:
            self.summary = summary
        if elapsed is not None:
            self.elapsed = elapsed
        detail = self.summary or STAGE_HELP.get(self.stage, "")
        t = f" [dim]{self.elapsed:.0f}s[/dim]" if self.elapsed else ""
        self.update(f"{MARK.get(self.status, '?')} [b]{self.stage}[/b]{t}\n"
                    f"  [dim]{escape(detail)}[/dim]")


if App is not None:

    class ContextScreen(ModalScreen[dict | None]):
        """Preset + today's detail + the basics. Returns overrides, or None."""

        BINDINGS = [Binding("escape", "cancel", "cancel"),
                    Binding("ctrl+s", "apply", "apply")]

        def __init__(self, ctx: ctxmod.EpisodeContext, apply_label: str = "Apply",
                     audio_channels: int | None = None):
            super().__init__()
            self.ctx = ctx
            self.apply_label = apply_label
            self.audio_channels = audio_channels

        def on_mount(self) -> None:
            # Straight to the box you type in; a preset is usually preselected.
            self.query_one("#ctx-detail", TextArea).focus()
            value = self.query_one("#ctx-preset", Select).value
            if value not in (Select.BLANK, Select.NULL, None):
                self._load_preset(str(value))
            self.query_one("#ctx-preview-pane").border_title = "sent to every model"
            self._refresh_preview()
            self.on_resize()

        def compose(self) -> ComposeResult:
            names = presets.names()
            start = self.ctx.preset if self.ctx.preset in names else presets.last()
            with Horizontal(id="ctx-box"):
                with VerticalScroll(id="ctx-form"):
                    yield Label("Episode context", id="ctx-title")
                    yield Label("Preset", classes="ctx-label")
                    yield Select([(n,n) for n in names], id="ctx-preset", value=start or Select.NULL)
                    for keys in (("show", "guest"), ("host", "episode")):
                        with Horizontal(classes="ctx-pair"):
                            for key in keys:
                                with Vertical(classes="ctx-cell"):
                                    yield Label(key.title(), classes="ctx-label")
                                    yield Input(getattr(self.ctx, key), id=f"ctx-{key}")
                    n = self.audio_channels
                    yield Label("Voices in the file" + (f" ({n} channels)" if n else ""), classes="ctx-label")
                    yield Select([("Mixed - both voices on both channels (diarize)", "mixed"),
                                  ("Split - host on LEFT, guest on RIGHT", "host-left"),
                                  ("Split - guest on LEFT, host on RIGHT", "guest-left")],
                                 id="ctx-channels", allow_blank=False, value=self.ctx.channels or "mixed")
                    yield Label("Leftover chat", classes="ctx-label")
                    yield Select([("Decide from the instructions","auto"),("Keep as topic files","keep"),("Drop","drop")],
                                 id="ctx-extras", allow_blank=False, value="keep" if self.ctx.extras is True else "drop" if self.ctx.extras is False else "auto")
                    yield Label("Names and spellings, one per line", classes="ctx-label")
                    yield TextArea("\n".join(self.ctx.names), id="ctx-names")
                    yield Label("Records expected, one per line: Artist - Title", classes="ctx-label")
                    yield TextArea("\n".join(self.ctx.songs), id="ctx-songs")
                    yield Label("Preset instructions", classes="ctx-label")
                    yield Static(escape(self.ctx.instructions), id="ctx-instructions")
                    yield Label("Today's detail", classes="ctx-label")
                    yield TextArea(self.ctx.detail, id="ctx-detail", soft_wrap=True,
                                   placeholder="e.g. four or five songs; we listened to each one in the room, so there are silences; keep the other chat as topic files")
                    with Horizontal(classes="ctx-row"):
                        yield Input(id="ctx-save")
                        yield Button("Save preset", id="ctx-save-btn")
                    with Horizontal(id="ctx-buttons"):
                        yield Button(self.apply_label, variant="primary", id="ctx-apply")
                        yield Button("Cancel", id="ctx-cancel")
                with VerticalScroll(id="ctx-preview-pane"):
                    yield Static(id="ctx-preview")
                    yield Static(id="ctx-settings")

        def _refresh_preview(self) -> None:
            try:
                ctx = merged_context(self.ctx, self._collect())
                self.query_one("#ctx-preview", Static).update(escape(ctx.block()))
                self.query_one("#ctx-settings", Static).update(escape(settings_line(ctx)))
            except NoMatches:
                pass

        @on(Input.Changed)
        @on(TextArea.Changed)
        @on(Select.Changed)
        def _changed(self) -> None:
            self._refresh_preview()

        def on_resize(self) -> None:
            try:
                self.query_one("#ctx-preview-pane").display = self.size.width >= 110
            except NoMatches:
                pass

        @on(Select.Changed, "#ctx-preset")
        def _preset(self, event: Select.Changed) -> None:
            if event.value in (Select.BLANK, Select.NULL, None):
                return
            self._load_preset(str(event.value))

        def _load_preset(self, name: str) -> None:
            try:
                data = presets.load(name)
            except CutdownError as e:
                self.notify(str(e), severity="error")
                return
            for key in ("show", "guest", "host", "episode"):
                if data.get(key):
                    self.query_one(f"#ctx-{key}", Input).value = str(data[key])
            if data.get("channels") in ("mixed", "host-left", "guest-left") \
                    and not self.ctx.channels:
                self.query_one("#ctx-channels", Select).value = data["channels"]
            if data.get("names"):
                self.query_one("#ctx-names", TextArea).text = "\n".join(data["names"])
            if data.get("songs"):
                self.query_one("#ctx-songs", TextArea).text = "\n".join(data["songs"])
            if "extras" in data:
                self.query_one("#ctx-extras", Select).value = "keep" if data["extras"] is True else "drop" if data["extras"] is False else "auto"
            self.query_one("#ctx-instructions", Static).update(
                escape(str(data.get("instructions") or "")) or "[dim](none)[/dim]")
            self._refresh_preview()

        def _collect(self) -> dict:
            preset = self.query_one("#ctx-preset", Select).value
            preset = None if preset in (Select.BLANK, Select.NULL) else str(preset)
            over = presets.apply(preset, "")
            for key in ("show", "guest", "host", "episode"):
                over[key] = self.query_one(f"#ctx-{key}", Input).value.strip()
            over["names"] = split_lines(self.query_one("#ctx-names", TextArea).text)
            over["songs"] = split_lines(self.query_one("#ctx-songs", TextArea).text)
            over["channels"] = str(self.query_one("#ctx-channels", Select).value)
            over["extras"] = {"keep":True, "drop":False}.get(str(self.query_one("#ctx-extras", Select).value))
            over["detail"] = self.query_one("#ctx-detail", TextArea).text.strip()
            return over

        @on(Button.Pressed, "#ctx-save-btn")
        def _save(self) -> None:
            name = self.query_one("#ctx-save", Input).value.strip()
            if not name:
                self.notify("type a name for the preset first", severity="warning")
                return
            over = self._collect()
            data = {k: v for k, v in over.items()
                    if k not in ("guest", "episode", "preset", "detail")}
            # Today's detail becomes part of the preset's standing instructions.
            data["instructions"] = "\n\n".join(
                x for x in (over.get("instructions", ""), over.get("detail", "")) if x)
            try:
                path = presets.save(name, data)
            except CutdownError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"saved preset {presets.slug(name)} -> {path}")

        @on(Button.Pressed, "#ctx-apply")
        def action_apply(self) -> None:
            self.dismiss(self._collect())

        @on(Button.Pressed, "#ctx-cancel")
        def action_cancel(self) -> None:
            self.dismiss(None)

    class Studio(App):
        CSS_PATH = "studio.css"
        TITLE = "cutdown"

        BINDINGS = [
            Binding("r", "run_all", "run all"),
            Binding("enter", "run_stage", "run stage"),
            Binding("f", "force_stage", "force"),
            Binding("c", "context", "context"),
            Binding("v", "revise", "revise"),
            Binding("space", "audition", "play"),
            Binding("s", "stop_audio", "stop"),
            Binding("e", "edit_brief", "brief"),
            Binding("o", "reveal", "open folder"),
            Binding("a", "reveal_audio", "audio", show=False),
            Binding("d", "toggle_dry", "dry-run"),
            Binding("j,down", "cursor_down", "down", show=False),
            Binding("k,up", "cursor_up", "up", show=False),
            Binding("q", "quit", "quit"),
        ]

        def __init__(self, args):
            super().__init__()
            self.args = args
            self.outdir = pathlib.Path(args.outdir).resolve()
            self.paths = pipeline.Paths(build=self.outdir / "build", outdir=self.outdir)
            self.brief_path = (pathlib.Path(args.brief).resolve() if args.brief
                               else self.outdir / "brief.yaml")
            self.config = Config.load(args.config, search_from=self.outdir)
            self.dry_run = False
            self.busy = False
            # Run every stage that is not current as soon as there is context:
            # the whole job from one command.
            self.autorun = bool(getattr(args, "autorun", False))
            self.audio = (pathlib.Path(args.audio).expanduser().resolve()
                          if args.audio else None)
            self._player: subprocess.Popen | None = None
            self._procs: list[subprocess.Popen] = []
            self._shutting_down = False
            self._lock = threading.Lock()
            self._app_thread = threading.get_ident()
            self.segments: list[dict] = []
            # Context given on the command line (--preset, --detail, --guest...)
            # is applied once, on the first load.
            from .cli import _overrides
            self._pending = {k: v for k, v in _overrides(args).items()
                             if v not in (None, "", [], {})} or None

        # ------------------------------------------------------------ layout

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Horizontal(id="body"):
                with Vertical(id="left"):
                    with Vertical(id="stages"):
                        for s in pipeline.STAGES:
                            yield StageRow(s)
                    yield Static("", id="episode")
                with Vertical(id="right"):
                    yield DataTable(id="segments", cursor_type="row", zebra_stripes=True)
                    yield VerticalScroll(Static("", id="detail-body"), id="detail")
                    yield RichLog(id="logpane", highlight=True, markup=True, wrap=True)
            yield Static("", id="status")
            yield Footer()

        def on_mount(self) -> None:
            self._app_thread = threading.get_ident()
            self.query_one("#stages").border_title = "pipeline"
            self.query_one("#episode").border_title = "episode"
            self.query_one("#segments").border_title = "songs and topics"
            self.query_one("#detail").border_title = "assembled"
            self.query_one("#logpane").border_title = "log"
            t = self.query_one("#segments", DataTable)
            t.add_columns("", "id", "anchor", "units", "length", "target", "flags",
                          "warn")
            set_sink(self._sink)
            for s in pipeline.STAGES:
                self.query_one(f"#stage-{s}", StageRow).refresh_row()
            self.reset_if_new_audio()
            if self._pending:
                self._apply_context(self._pending)
                self._pending = None
            self.refresh_state()
            self._stage_rows()[0].focus()
            self.set_status("ready")
            if not (self.paths.build / "context.json").exists():
                self.action_context(then_run=self.autorun)
            elif self.autorun:
                self.action_run_all()

        # ------------------------------------------------------------ plumbing

        def _sink(self, kind: str, msg: str) -> None:
            # Stages call this from a worker thread, but refresh_state() also
            # drives code that logs, and that runs on the app thread.
            if kind == "stage":
                line = f"\n[b cyan]▸ {escape(msg)}[/b cyan]"
            elif kind == "note":
                line = escape(msg)
                if "FLAGGED" in msg or msg.lstrip().startswith(("WARNING", "NOTE")):
                    line = f"[yellow]{line}[/yellow]"
            else:
                line = f"[dim]{escape(msg)}[/dim]"
            if threading.get_ident() == self._app_thread:
                self.log_line(line)
            else:
                self._from_thread(self.log_line, line)

        def _from_thread(self, fn, *args) -> None:
            """Post to the app thread, unless the app is already on its way out.

            A stage killed by quitting still unwinds through its error paths,
            and those must not raise into a screen that no longer exists.
            """
            if self._shutting_down:
                return
            try:
                self.call_from_thread(fn, *args)
            except RuntimeError:
                pass

        def log_line(self, msg: str) -> None:
            self.query_one("#logpane", RichLog).write(msg)

        def set_status(self, msg: str) -> None:
            self.query_one("#status", Static).update(msg)

        # ------------------------------------------------------- child processes

        def _spawn(self, cmd: list[str]) -> subprocess.Popen:
            """Start a child in its own process group, and remember it.

            Own group so that killing it takes its children with it (whisperx
            shells out to ffmpeg, afplay is short but not instant), remembered
            so that quitting the studio is the end of every process it started.
            """
            return self.track(subprocess.Popen(cmd, start_new_session=True))

        def track(self, proc: subprocess.Popen) -> subprocess.Popen:
            with self._lock:
                self._procs = [p for p in self._procs if p.poll() is None]
                self._procs.append(proc)
            return proc

        def kill_children(self) -> None:
            """Terminate every process this session started. Safe to call twice."""
            self._shutting_down = True
            with self._lock:
                procs, self._procs = self._procs, []
            alive = []
            for p in procs:
                if p.poll() is not None:
                    continue
                if self._signal(p, signal.SIGTERM):
                    alive.append(p)
            deadline = time.monotonic() + 2.0
            while alive and time.monotonic() < deadline:
                alive = [p for p in alive if p.poll() is None]
                if alive:
                    time.sleep(0.05)
            for p in alive:
                self._signal(p, signal.SIGKILL)

        @staticmethod
        def _signal(proc: subprocess.Popen, sig: int) -> bool:
            try:
                os.killpg(os.getpgid(proc.pid), sig)
                return True
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    proc.kill()
                except OSError:
                    return False
                return True

        def on_unmount(self) -> None:
            self.kill_children()
            # Output goes back to the terminal once the screen is gone.
            set_sink(None)

        # --------------------------------------------------------- new episode

        def reset_if_new_audio(self) -> None:
            """Start clean when this outdir last held a different episode.

            Stage state, briefs, segments and renders are all derived from one
            audio file; showing the previous file's results next to a new one is
            worse than showing nothing. The old work is moved aside rather than
            deleted, so a mistaken path costs a rename, not a re-run.
            """
            if self.audio is None:
                return
            stamp = self._stamp_audio()
            marker = self.outdir / SOURCE_FILE
            previous = None
            if marker.exists():
                try:
                    previous = read_json(marker)
                except CutdownError:
                    previous = None
            if previous is not None:
                if previous.get("audio") == stamp["audio"]:
                    return
            else:
                # No marker: an outdir from before this check existed. WhisperX
                # names its JSON after the audio, so the transcript already says
                # which episode this work belongs to - believe it rather than
                # archiving work that is in fact the current one.
                stems = {p.stem for p in
                         (self.paths.build / "transcript").glob("*.json")}
                if not stems or self.audio.stem in stems:
                    write_json(marker, stamp)
                    return

            existing = [self.outdir / n for n in ARTIFACTS
                        if (self.outdir / n).exists()]
            if (self.brief_path.exists() and self.brief_path.parent == self.outdir
                    and self.brief_path not in existing):
                existing.append(self.brief_path)
            if existing:
                old = pathlib.Path(previous.get("audio", "")).stem if previous else ""
                archive = self.outdir / "_previous" / f"{now_stamp()}{'-' + old if old else ''}"
                archive.mkdir(parents=True, exist_ok=True)
                for p in existing:
                    shutil.move(str(p), str(archive / p.name))
                self.log_line(f"[yellow]new audio[/yellow] {escape(self.audio.name)} - "
                              f"reset; previous work moved to "
                              f"[dim]{escape(str(archive.relative_to(self.outdir)))}[/dim]")
            write_json(marker, stamp)

        def _stamp_audio(self) -> dict:
            st = self.audio.stat() if self.audio and self.audio.exists() else None
            return {"audio": str(self.audio),
                    "size": st.st_size if st else None,
                    "mtime": round(st.st_mtime, 6) if st else None}

        # ------------------------------------------------------------ job

        def job(self) -> pipeline.Job:
            a = self.args
            return pipeline.Job(
                paths=self.paths, config=self.config, brief_path=self.brief_path,
                audio=self.audio,
                whisper_json=pathlib.Path(a.json_in).resolve() if a.json_in else None,
                master=pathlib.Path(a.master).expanduser().resolve() if a.master else None,
                prompts_dir=getattr(a, "prompts", None), dry_run=self.dry_run,
                on_line=lambda l: self._from_thread(self.log_line,
                                                    f"[dim]{escape(l)}[/dim]"),
                on_proc=self.track)

        def _episode_context(self) -> ctxmod.EpisodeContext:
            b = self.paths.build
            return ctxmod.load(b) if b.exists() else ctxmod.EpisodeContext()

        def _apply_context(self, over: dict, *, replace=False) -> None:
            ctx = self._episode_context()
            # Detail is replaced, not merged: the box is the whole of it.
            if replace:
                ctx = merged_context(ctx, over)
            else:
                detail = over.pop("detail", None)
                ctx = ctx.merge(**over)
                if detail is not None:
                    ctx.detail = detail
            self.paths.build.mkdir(parents=True, exist_ok=True)
            ctxmod.save(self.paths.build, ctx)
            if ctx.config:
                self.config.overlay(ctx.config)

        # ------------------------------------------------------------ state

        def refresh_state(self) -> None:
            job = self.job()
            io = job.io()
            b = self.paths.build
            for s in pipeline.STAGES:
                row = self.query_one(f"#stage-{s}", StageRow)
                if row.status in ("run", "fail"):
                    continue
                try:
                    current = pipeline.is_current(s, self.paths, self.config, **io)
                except CutdownError:
                    current = False
                done = (b / ".state" / f"{s}.json").exists()
                if s == "discover" and self.brief_path.exists():
                    current = True
                row.refresh_row("done" if current else ("stale" if done else "todo"),
                                self._summary(s, io))

            ctx = self._episode_context()
            if ctx.config:
                self.config.overlay(ctx.config)
            head = " / ".join(x for x in (ctx.show, ctx.guest, ctx.episode) if x)
            self.sub_title = head or str(self.outdir)
            lines = [f"[b]{escape(head) or 'no context yet - press c'}[/b]"]
            if ctx.host:
                lines.append(f"[dim]host[/dim] {escape(ctx.host)}")
            if ctx.preset:
                lines.append(f"[cyan]preset[/cyan] {escape(ctx.preset)}")
            if ctx.detail:
                first = ctx.detail.strip().splitlines()[0]
                lines.append(f"[dim]detail:[/dim] {escape(first[:60])}"
                             + ("…" if len(first) > 60 else ""))
            if ctx.channels and ctx.channels != "mixed":
                lines.append(f"[dim]voices[/dim] {'host L / guest R' if ctx.channels == 'host-left' else 'guest L / host R'}")
            if ctx.songs or ctx.names:
                lines.append(f"[dim]{len(ctx.songs)} song hint(s), "
                             f"{len(ctx.names)} name(s)[/dim]")
            lines.append(f"[dim]brief[/dim] {self.brief_path.name}"
                         + ("" if self.brief_path.exists() else " [dim](drafted by "
                                                                "discover)[/dim]"))
            io_master = io.get("master")
            lines.append("[dim]master[/dim] "
                         + (escape(io_master.name) if io_master else "[red]not set[/red]"))
            lines.append("[dim]out[/dim] " + escape(self._short(self.outdir)))
            if self.dry_run:
                lines.append("[yellow]DRY RUN - nothing is sent[/yellow]")
            self.query_one("#episode", Static).update("\n".join(lines))
            self.refresh_segments()

        @staticmethod
        def _short(p: pathlib.Path) -> str:
            try:
                return str(p.relative_to(pathlib.Path.cwd()))
            except ValueError:
                return str(p).replace(str(pathlib.Path.home()), "~")

        def _summary(self, stage: str, io: dict) -> str:
            b = self.paths.build
            try:
                if stage == "ingest" and (b / "units.jsonl").exists():
                    n = len(read_jsonl(b / "units.jsonl"))
                    secs = (read_json(b / "sections.json")
                            if (b / "sections.json").exists() else [])
                    quiet = sum(1 for s in secs if s.get("break") == "silence")
                    return (f"{n} units · {len(secs)} sections"
                            + (f" · {quiet} silences" if quiet else "")
                            + (" · roles look swapped" if (b / "roles.json").exists() and read_json(b / "roles.json").get("verdict") == "swapped" else ""))
                if stage == "label" and (b / "labels.jsonl").exists():
                    rows = read_jsonl(b / "labels.jsonl")
                    admin = sum(1 for r in rows if r.get("role") == "admin")
                    return f"{len(rows)} tagged, {admin} admin"
                if stage == "discover" and self.brief_path.exists():
                    br = Brief.load(self.brief_path)
                    keep = (br.data.get("extras") or {}).get("keep")
                    return f"{len(br.segments)} songs" + (", keep extras" if keep else "")
                if stage == "assemble" and (b / "segments.json").exists():
                    segs = read_json(b / "segments.json")["segments"]
                    flagged = sum(1 for s in segs if s.get("flags"))
                    return f"{len(segs)} built" + (f", {flagged} flagged" if flagged else "")
                if stage == "topics" and (b / "topics.json").exists():
                    t = read_json(b / "topics.json")
                    return (f"{len(t.get('topics', []))} topics" if t.get("enabled")
                            else "off for this brief")
                if stage == "check" and (b / "check.json").exists():
                    chk = read_json(b / "check.json")
                    return ("revised · " if chk.get("revision", {}).get("groups") else "") + f"{len(chk.get('warnings', []))} warnings"
                if stage == "render" and (self.outdir / "render.json").exists():
                    r = read_json(self.outdir / "render.json")
                    return (f"{r['clips']} clips, {len(r.get('chunks') or [])} chunks")
                if stage == "transcribe":
                    wj = io["whisper_json"]
                    if wj and wj.exists():
                        return f"{wj.name}"
            except (CutdownError, KeyError, ValueError, TypeError):
                pass
            return ""

        def refresh_segments(self) -> None:
            t = self.query_one("#segments", DataTable)
            t.clear()
            self.segments = []
            sp = self.paths.build / "segments.json"
            if not sp.exists():
                self.query_one("#detail-body", Static).update(
                    "[dim]Nothing assembled yet. Press [b]r[/b] to run everything, "
                    "or [b]c[/b] to set the context first.[/dim]")
                return
            doc = read_json(sp)
            check = (read_json(self.paths.build / "check.json")
                     if (self.paths.build / "check.json").exists() else {})
            tp = self.paths.build / "topics.json"
            topics = read_json(tp).get("topics", []) if tp.exists() else []
            warn: dict[str, list] = {}
            for w in check.get("warnings", []):
                warn.setdefault(w.get("segment", ""), []).append(w)
            for kind, rows in (("song", doc["segments"]), ("topic", topics)):
                for s in rows:
                    a = s.get("anchor") or {}
                    lo, hi = (s.get("target_sec") or [None, None])[:2]
                    ws = warn.get(s["id"], [])
                    flags = ", ".join(s.get("flags") or []) or "-"
                    anchor = (" - ".join(x for x in (a.get("artist"), a.get("title"))
                                         if x) or "?"
                              if kind == "song" else s.get("title", ""))
                    t.add_row("♪" if kind == "song" else "¶", s["id"], anchor,
                              str(len(s.get("ids") or [])),
                              mmss(s.get("measured_sec") or 0),
                              f"{lo}-{hi}s" if lo is not None else "",
                              f"[red]{flags}[/red]" if s.get("flags") else flags,
                              f"[yellow]{len(ws)}[/yellow]" if ws else "0",
                              key=s["id"])
                    self.segments.append({**s, "warnings": ws, "kind": kind})
            if self.segments:
                self.show_segment(0)

        def show_segment(self, idx: int) -> None:
            if not (0 <= idx < len(self.segments)):
                return
            seg = self.segments[idx]
            units = {u["id"]: u for u in read_jsonl(self.paths.build / "units.jsonl")}
            from . import spans
            tp, wp = self.paths.build / "timeline.json", self.paths.build / "unit_words.json"
            timeline = read_json(tp) if tp.exists() else {}
            words = read_json(wp) if wp.exists() else {}
            kept = spans.group_spans(seg, timeline, words, self.config.get("fillers", {}))
            wbyid: dict[str, list] = {}
            for w in seg["warnings"]:
                wbyid.setdefault(w.get("id") or "", []).append(w)
            L = []
            rev = seg.get("revision", {})
            if rev.get("changed"):
                L.append(f"[green]revised[/green] +{len(rev.get('added', []))} -{len(rev.get('removed', []))} units")
            if seg.get("reason") or seg.get("summary"):
                L.append(f"[i dim]{escape(seg.get('reason') or seg.get('summary'))}"
                         f"[/i dim]\n")
            for w in wbyid.get("", []):
                L.append(f"[yellow]! {w.get('kind', '')}[/yellow] "
                         f"{escape(w.get('note', ''))}\n")
            for uid in (seg.get("order") or seg.get("ids") or []):
                for w in wbyid.get(uid, []):
                    sev = {"high": "red", "medium": "yellow"}.get(w.get("severity", ""), "dim")
                    L.append(f"  [{sev}]! {w.get('kind', '')}: "
                             f"{escape(w.get('note', ''))}[/{sev}]")
                u = units.get(uid, {})
                L.append(f"[b cyan]{uid}[/b cyan] [dim]{u.get('role', '')}[/dim] "
                         f"{escape(spans.kept_text(uid, units, words, kept.get(uid, [])))}\n")
            rej = seg.get("rejected") or []
            if rej:
                L.append(f"\n[dim]--- considered and rejected ({len(rej)}) ---[/dim]")
                for r in rej:
                    L.append(f"[dim]{r.get('id', '')}: {escape(r.get('why', ''))}[/dim]")
            self.query_one("#detail-body", Static).update("\n".join(L))
            self.query_one("#detail").border_title = (
                f"{seg['id']} - {seg.get('kind', '')} - "
                f"{mmss(seg.get('measured_sec') or 0)}")

        @on(DataTable.RowHighlighted, "#segments")
        def _row(self, event: DataTable.RowHighlighted) -> None:
            self.show_segment(event.cursor_row)

        # ------------------------------------------------------------ actions

        def _focused_stage(self) -> str | None:
            f = self.focused
            if isinstance(f, StageRow):
                return f.stage
            return None

        def _stage_rows(self) -> list[StageRow]:
            return list(self.query(StageRow))

        def _move_stage_focus(self, delta: int) -> bool:
            rows = self._stage_rows()
            cur = self.focused
            if not isinstance(cur, StageRow):
                return False
            i = rows.index(cur)
            rows[max(0, min(len(rows) - 1, i + delta))].focus()
            return True

        def action_context(self, then_run: bool = False) -> None:
            if self.busy:
                self.set_status("wait for the run to finish before changing context")
                return

            def done(over: dict | None) -> None:
                if over is None:
                    if then_run:
                        self.set_status("no context set - [b]c[/b] to set it, "
                                        "[b]r[/b] to run without")
                    return
                if over.get("preset"):
                    presets.remember(over["preset"])
                self._apply_context(over, replace=True)
                self.refresh_state()
                if then_run:
                    self.action_run_all()
                else:
                    self.set_status("context saved - [b]r[/b] to run")
            from .channels import channel_count
            n = channel_count(self.audio) if self.audio else None
            self.push_screen(ContextScreen(self._episode_context(),
                                           "Start" if then_run else "Apply", n), done)

        def action_toggle_dry(self) -> None:
            self.dry_run = not self.dry_run
            self.refresh_state()

        def action_cursor_down(self) -> None:
            if not self._move_stage_focus(1):
                self.screen.focus_next()

        def action_cursor_up(self) -> None:
            if not self._move_stage_focus(-1):
                self.screen.focus_previous()

        def action_run_stage(self) -> None:
            s = self._focused_stage()
            if s is None:
                self.set_status("focus a stage in the left rail first "
                                "(tab, or j/k), or press r to run everything")
                return
            self.start([s], force=False)

        def action_force_stage(self) -> None:
            s = self._focused_stage()
            if s is None:
                self.set_status("focus a stage to force it; r runs everything current")
                return
            self.start([s], force=True)

        def action_run_all(self) -> None:
            self.start(list(pipeline.STAGES), force=False)

        def action_edit_brief(self) -> None:
            if not self.brief_path.exists():
                self.set_status("no brief yet - run discover (or r) to draft one")
                return
            editor = os.environ.get("EDITOR", "vi")
            with self.suspend():
                subprocess.run([editor, str(self.brief_path)])
            self.refresh_state()

        def action_reveal(self) -> None:
            final = self.outdir / "1-final"
            self._spawn(["open", str(final if final.exists() else self.outdir)])

        def action_reveal_audio(self) -> None:
            """Open the folder the audio came from, with the file selected."""
            target = self.audio or self.job().io().get("master")
            if target is None:
                self.set_status("no audio file - pass --audio or --master")
                return
            if target.exists():
                self._spawn(["open", "-R", str(target)])
            elif target.parent.is_dir():
                self._spawn(["open", str(target.parent)])
            else:
                self.set_status(f"{target.parent} does not exist")
                return
            self.set_status(f"revealed {target.name}")

        def action_stop_audio(self) -> None:
            if self._player and self._player.poll() is None:
                self._signal(self._player, signal.SIGTERM)
                self.set_status("stopped")

        async def action_quit(self) -> None:
            self.kill_children()
            await super().action_quit()

        def action_audition(self) -> None:
            t = self.query_one("#segments", DataTable)
            if not self.segments:
                self.set_status("nothing to play yet")
                return
            seg = self.segments[t.cursor_row]
            # Final filenames follow a configurable template, so ask the render
            # summary rather than reconstructing the name here.
            path = None
            rj = self.outdir / "render.json"
            if rj.exists():
                r = read_json(rj)
                for s in (r.get("segments") or []) + (r.get("topics") or []):
                    if s.get("id") == seg["id"] and (s.get("final") or s.get("stringout")):
                        path = self.outdir / (s.get("final") or s["stringout"])
            if path is None or not path.exists():
                self.set_status(f"no file for {seg['id']} yet - run render")
                return
            self.action_stop_audio()
            self._player = self._spawn(["afplay", str(path)])
            self.set_status(f"▶ {path.name}  ({mmss(seg.get('measured_sec') or 0)})"
                            f"   [b]s[/b] stop")

        # ------------------------------------------------------------ worker

        def _decide(self, kind, question, detail) -> bool:
            done, box = threading.Event(), {}
            def show():
                def cb(answer):
                    box["a"] = bool(answer)
                    done.set()
                self.push_screen(ConfirmScreen(question, detail), cb)
            self._from_thread(show)
            while not done.wait(0.2):
                if self._shutting_down: return False
            return box.get("a", False)

        def action_revise(self) -> None:
            if self.busy:
                self.set_status("already running")
                return
            status = pipeline.revise_status(self.paths)
            if not status["warnings"] or status["revised"]:
                self.set_status("nothing to revise")
                return
            path = self.paths.build / ".state" / pipeline.PROMPT_STATE
            if path.exists():
                saved = read_json(path)
                saved.pop("revise_declined", None)
                write_json(path, saved)
            self.start(["render"], force=False, revise_first=True)

        def start(self, stages: list[str], *, force: bool, revise_first=False) -> None:
            if self.busy:
                self.set_status("already running - wait, or q to quit")
                return
            self.busy = True
            self.run_stages(stages, force, revise_first)

        @work(thread=True, exclusive=True)
        def run_stages(self, stages: list[str], force: bool, revise_first=False) -> None:
            current = stages[0] if stages else ""
            job = self.job()
            try:
                if revise_first:
                    pipeline.maybe_revise(job, lambda *a: True)
                for s in stages:
                    current = s
                    self._mark(s, "run")
                    self._from_thread(self.set_status, f"running {s}…")
                    from .util import stage_line
                    stage_line(s, STAGE_HELP.get(s, ""))
                    result = pipeline.run_stage(job, s, force=force)
                    self._mark(s, "done" if result != "dry" else "todo",
                               job.timings.get(s))
                    if s == "ingest":
                        pipeline.maybe_swap(job, self._decide)
                    if s == "check":
                        self._from_thread(self.set_status, "check done")
                        pipeline.maybe_revise(job, self._decide)
                    self._from_thread(self.refresh_state)
                self._from_thread(self.set_status,
                                  "[green]done[/green] - [b]o[/b] opens the output, "
                                  "[b]space[/b] plays the selected file")
            except CutdownError as e:
                # An expected, actionable failure: the message names the fix.
                self._mark(current, "fail")
                self._from_thread(self.log_line, f"[red]{escape(str(e))}[/red]")
                self._from_thread(self.set_status,
                                  f"[red]{current} failed[/red] - see the log")
            except Exception as e:  # surface, never swallow
                self._mark(current, "fail")
                import traceback
                for line in traceback.format_exc().splitlines()[-12:]:
                    self._from_thread(self.log_line, f"[red]{escape(line)}[/red]")
                self._from_thread(self.set_status,
                                  f"[red]{current}: {type(e).__name__}[/red] - see the log")
            finally:
                self.busy = False
                self._from_thread(self.refresh_state)

        def _mark(self, stage: str, status: str, elapsed: float | None = None) -> None:
            def go():
                row = self.query_one(f"#stage-{stage}", StageRow)
                row.remove_class(f"-{row.status}")
                row.status = status
                row.add_class(f"-{status}")
                row.refresh_row(elapsed=elapsed)
            self._from_thread(go)


def launch(args) -> int:
    if App is None:
        raise CutdownError(
            "the studio needs Textual. Install it with:\n"
            "  uv pip install textual"
        )
    app = Studio(args)
    try:
        app.run()
    finally:
        # Ctrl-C, a crash and a clean quit all end here, and none of them may
        # leave a transcribe or an audition running behind the closed screen.
        app.kill_children()
        set_sink(None)
    return 0
