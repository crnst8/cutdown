"""Context presets: the standing description of a show, saved once, reused.

A preset is the part of the episode context that is the same every week - the
show, the host's name, how the recording is made, what to keep - plus settings
that follow from it (default segment lengths, whether leftover talk is kept as
topic files, config such as the number of speakers). On a given day you load
the preset and add detail about this recording:

    cutdown run --audio today.wav --preset radio \\
        --detail "four or five songs; keep the extra chat as topic files"

Stored as YAML, one file per preset, in $CUTDOWN_PRESETS or
~/.config/cutdown/presets/. Plain files so they can be edited, copied between
machines and kept in a dotfiles repo.

    name: radio
    show: Record Room
    names: [Morgan]
    instructions: |
      The guest talks about each record, then we listen to it in the room;
      the record is not in the recording, so there is a silence where it played.
    extras: true
    targets: {guest_pick: [90, 240], own_work: [60, 180]}
    config: {transcribe: {max_speakers: 3}}
"""
from __future__ import annotations

import os
import pathlib
import re
from dataclasses import asdict

import yaml

from .context import EpisodeContext, _from_mapping
from .util import CutdownError

FIELDS = ["show", "guest", "host", "episode", "songs", "names", "notes", "instructions",
          "extras", "targets", "config", "channels"]

TEMPLATE = """\
# cutdown preset. Every key is optional.
show: ""
host: ""
# Names and spellings the transcript is likely to mangle.
names: []
# Records that come up every time, if any.
songs: []
# The standing description: how the recording is made and what to make of it.
instructions: |

# How the voices sit in the file: mixed (diarize), host-left, or guest-left
# (one speaker per channel: host on L and guest on R, or the other way round).
channels: mixed
# Keep the talk no segment used, grouped into topic files alongside the songs.
extras: true
targets:
  guest_pick: [90, 240]
  own_work: [60, 180]
# Overlaid on cutdown.toml for episodes started from this preset.
config: {}
"""


def presets_dir() -> pathlib.Path:
    env = os.environ.get("CUTDOWN_PRESETS")
    if env:
        return pathlib.Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return pathlib.Path(base).expanduser() / "cutdown" / "presets"


def slug(name: str) -> str:
    s = re.sub(r"[^\w-]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise CutdownError(f"{name!r} is not usable as a preset name")
    return s


def path_for(name: str) -> pathlib.Path:
    return presets_dir() / f"{slug(name)}.yaml"


def names() -> list[str]:
    d = presets_dir()
    return sorted(p.stem for p in d.glob("*.yaml")) if d.is_dir() else []


def load(name: str) -> dict:
    p = path_for(name)
    if not p.exists():
        have = names()
        raise CutdownError(
            f"no preset {name!r} in {presets_dir()}. "
            + (f"Have: {', '.join(have)}." if have else
               "Save one with `cutdown preset save <name> ...`."))
    try:
        data = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise CutdownError(f"{p} is not valid YAML: {e}")
    if not isinstance(data, dict):
        raise CutdownError(f"{p} must be a mapping of keys to values")
    unknown = sorted(set(data) - set(FIELDS) - {"name"})
    if unknown:
        raise CutdownError(f"{p}: unknown key(s) {', '.join(unknown)}. "
                           f"Allowed: {', '.join(FIELDS)}")
    return {k: data[k] for k in FIELDS if k in data}


def save(name: str, data: dict) -> pathlib.Path:
    p = path_for(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    clean = {"name": slug(name)}
    for k in FIELDS:
        v = data.get(k)
        if v in (None, "", [], {}):
            continue
        clean[k] = v
    text = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=88)
    p.write_text(text)
    return p


def delete(name: str) -> pathlib.Path:
    p = path_for(name)
    if not p.exists():
        raise CutdownError(f"no preset {name!r} in {presets_dir()}")
    p.unlink()
    return p


def from_context(ctx: EpisodeContext) -> dict:
    """What of an episode's context is worth keeping for next time."""
    d = asdict(ctx)
    # Guest, episode and the day's detail change every week; the rest is the
    # show. The detail is folded in only when asked (`--with-detail`).
    return {k: d[k] for k in FIELDS if k not in ("guest", "episode")}


def apply(name: str | None, detail: str = "") -> dict:
    """Context overrides for a preset plus today's detail.

    The two stay separate fields: the preset's instructions still apply, and
    today's detail adds to them rather than replacing them.
    """
    data = load(name) if name else {}
    over = {k: v for k, v in asdict(_from_mapping(data)).items()
            if v not in (None, "", [], {})}
    if detail and detail.strip():
        over["detail"] = detail.strip()
    if name:
        over["preset"] = slug(name)
    return over


LAST = ".last"


def remember(name: str) -> None:
    """Note the preset used, so the next new episode starts from it."""
    try:
        d = presets_dir()
        d.mkdir(parents=True, exist_ok=True)
        (d / LAST).write_text(slug(name) + "\n")
    except OSError:
        pass


def last() -> str:
    """The preset used most recently, if it still exists."""
    try:
        name = (presets_dir() / LAST).read_text().strip()
    except OSError:
        return ""
    return name if name in names() else ""
