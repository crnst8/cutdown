"""Per-episode context: what the producer knows before the models see anything.

Guest name, show, the records likely to come up, names and spellings, and free
notes. This exists because the models are working from an ASR transcript: "Kenny
Beats" comes out as "Kenny Beat's", a title gets misheard, and a track the guest
mentions once in passing is invisible without a nudge.

Sources merge in this order, later winning:
  1. context.json / context.yaml in the build directory (an earlier run)
  2. the brief, if one exists
  3. CLI flags, a preset (`--preset`), and today's detail (`--detail`)

`instructions` is the producer's standing direction for the show (usually from
a preset) and `detail` is what is particular to this recording: what is in it,
what to keep, how to split it up. Every prompt is told to follow both.
`extras`, `targets` and `config` are settings rather than prompt text; they
usually arrive with a preset (see presets.py).

Persisted to build/context.json so every stage sees the same thing and the
pipeline stays resumable.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field, asdict

import yaml

from .util import CutdownError, read_json, write_json


@dataclass
class EpisodeContext:
    guest: str = ""
    host: str = ""
    show: str = ""
    episode: str = ""
    # Records that may come up. Free-form "Artist - Title" strings; the producer
    # does not have to be exhaustive or right, these are hints.
    songs: list[str] = field(default_factory=list)
    # Names, places, labels and their correct spellings, for ASR mangling.
    names: list[str] = field(default_factory=list)
    # Anything else worth telling a model about this episode.
    notes: str = ""
    # The producer's direction: the shape of the recording and what to make of
    # it. Standing instructions (from a preset) and today's detail, kept apart
    # so re-applying a preset does not wipe the day's note.
    instructions: str = ""
    detail: str = ""
    # The preset this context was started from, for the record.
    preset: str = ""
    # Keep the talk no segment used, grouped into topic files. None = let the
    # discover stage decide from the instructions.
    extras: bool | None = None
    # Default segment lengths: {"guest_pick": [90, 240], "own_work": [60, 180]}
    targets: dict = field(default_factory=dict)
    # Overlaid on cutdown.toml for this episode, e.g. {"transcribe":
    # {"max_speakers": 4}}. Changes stage hashes like any config edit.
    config: dict = field(default_factory=dict)
    # How the voices sit in the file: "mixed" (diarize), "host-left" or
    # "guest-left" (one speaker per channel). See channels.py.
    channels: str = ""

    def merge(self, **kw) -> "EpisodeContext":
        out = EpisodeContext(**asdict(self))
        for k, v in kw.items():
            if v in (None, "", [], ()):
                continue
            if not hasattr(out, k):
                continue
            cur = getattr(out, k)
            if isinstance(cur, dict):
                setattr(out, k, {**cur, **v})
            elif isinstance(cur, list):
                seen = list(cur)
                for item in v:
                    if item not in seen:
                        seen.append(item)
                setattr(out, k, seen)
            else:
                setattr(out, k, v)
        return out

    def block(self) -> str:
        """The prompt-facing rendering. Empty sections are omitted entirely."""
        parts = []
        if self.guest:
            parts.append(f"Guest: {self.guest}")
        if self.host:
            parts.append(f"Host: {self.host}")
        if self.show:
            parts.append(f"Show: {self.show}")
        if self.episode:
            parts.append(f"Episode: {self.episode}")
        if not self.is_empty():
            parts.append("Speaker labels: each side was recorded on its own channel, so HOST and GUEST are reliable. Every guest voice is labelled GUEST." if self.channels in ("host-left", "guest-left") else "Speaker labels: HOST and GUEST come from automatic diarization and can be wrong, most often on short turns.")
        if self.songs:
            parts.append("\nRecords the producer expects to come up. The transcript is "
                         "ASR output, so titles and artist names in it may be misheard "
                         "or misspelt; match on sound and sense, not on exact spelling.\n"
                         + "\n".join(f"  - {s}" for s in self.songs))
        if self.names:
            parts.append("\nNames and their correct spellings:\n"
                         + "\n".join(f"  - {n}" for n in self.names))
        if self.notes:
            parts.append(f"\nProducer's notes on this episode:\n{self.notes.strip()}")
        direction = self.direction()
        if direction:
            parts.append("\nProducer's instructions for this recording. They describe "
                         "what is in it and what to make of it; follow them, and where "
                         "they conflict with the general guidance, they win:\n"
                         + direction)
        return "\n".join(parts) if parts else "(no episode context supplied)"

    def is_empty(self) -> bool:
        return not any([self.guest, self.host, self.show, self.episode, self.songs,
                        self.names, self.notes, self.instructions, self.detail])

    def direction(self) -> str:
        return "\n\n".join(x.strip() for x in (self.instructions, self.detail)
                           if x and x.strip())

    def target_for(self, kind: str) -> list | None:
        t = self.targets.get(kind)
        if isinstance(t, list) and len(t) == 2:
            return [int(t[0]), int(t[1])]
        return None


def _from_mapping(d: dict) -> EpisodeContext:
    if not isinstance(d, dict):
        return EpisodeContext()
    def as_list(v):
        if v is None:
            return []
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return [str(x) for x in v]
    extras = d.get("extras")
    return EpisodeContext(
        guest=str(d.get("guest", "") or ""),
        host=str(d.get("host", "") or ""),
        show=str(d.get("show", "") or ""),
        episode=str(d.get("episode", "") or ""),
        songs=as_list(d.get("songs")),
        names=as_list(d.get("names")),
        notes=str(d.get("notes", "") or ""),
        instructions=str(d.get("instructions", "") or ""),
        detail=str(d.get("detail", "") or ""),
        preset=str(d.get("preset", "") or ""),
        extras=bool(extras) if extras is not None else None,
        targets=dict(d.get("targets") or {}),
        config=dict(d.get("config") or {}),
        channels=str(d.get("channels", "") or ""),
    )


def load(build: pathlib.Path, *, brief=None, overrides: dict | None = None) -> EpisodeContext:
    ctx = EpisodeContext()

    stored = build / "context.json"
    if stored.exists():
        ctx = _from_mapping(read_json(stored))

    yml = build / "context.yaml"
    if yml.exists():
        try:
            ctx = ctx.merge(**asdict(_from_mapping(yaml.safe_load(yml.read_text()) or {})))
        except yaml.YAMLError as e:
            raise CutdownError(f"{yml} is not valid YAML: {e}")

    if brief is not None:
        from_brief = _from_mapping({
            "guest": brief.guest, "show": brief.show, "episode": brief.episode,
            **{k: v for k, v in brief.data.items()
               if k in ("host", "songs", "names", "notes", "instructions", "detail")},
        })
        ctx = ctx.merge(**asdict(from_brief))

    if overrides:
        ctx = ctx.merge(**{k: v for k, v in overrides.items()
                           if v not in (None, "", [], {})})

    return ctx


def save(build: pathlib.Path, ctx: EpisodeContext) -> None:
    """Idempotent: an unchanged context must not touch the file.

    Stages declare context.json as an input, so rewriting it with identical
    content would bump its mtime and make every downstream stage look stale.
    """
    path = build / "context.json"
    new = asdict(ctx)
    if path.exists():
        try:
            old = read_json(path)
            defaults = asdict(EpisodeContext())
            if isinstance(old, dict) and {**defaults, **old} == new:
                return
        except CutdownError:
            pass
    write_json(path, new)


def read_notes_arg(value: str | None) -> str:
    """--context takes either a path to a file or the text itself."""
    if not value:
        return ""
    p = pathlib.Path(value)
    if p.exists() and p.is_file():
        return p.read_text()
    return value
