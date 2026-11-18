"""Shared helpers: logging, errors, small file utilities."""
from __future__ import annotations

import json
import os
import pathlib
import sys
from datetime import datetime, timezone


class CutdownError(Exception):
    """Anything the producer can act on. Printed without a traceback."""


_QUIET = False
_SINK = None


def set_quiet(quiet: bool) -> None:
    global _QUIET
    _QUIET = quiet


def set_sink(fn) -> None:
    """Route all output to a callback instead of the terminal (used by studio)."""
    global _SINK
    _SINK = fn


def log(msg: str) -> None:
    if _SINK is not None:
        _SINK("log", msg)
        return
    if not _QUIET:
        print(msg, file=sys.stderr)


def _tty() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, style: str) -> str:
    """ANSI styling for the terminal; plain text when piped or NO_COLOR is set."""
    codes = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33",
             "cyan": "36", "bold-cyan": "1;36"}
    if not _tty() or style not in codes:
        return text
    return f"\033[{codes[style]}m{text}\033[0m"


def note(msg: str) -> None:
    """Always shown - stage headers and counts the producer needs."""
    if _SINK is not None:
        _SINK("note", msg)
        return
    if "FLAGGED" in msg or msg.lstrip().startswith(("WARNING", "NOTE")):
        msg = paint(msg, "yellow")
    print(msg)


def stage_line(stage: str, detail: str = "") -> None:
    """The header a stage prints as it starts."""
    if _SINK is not None:
        _SINK("stage", f"{stage} {detail}".strip())
        return
    print("\n" + paint(f"▸ {stage}", "bold-cyan")
          + (paint(f"  {detail}", "dim") if detail else ""))


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def read_json(path: pathlib.Path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise CutdownError(f"missing {path} - run the stage that produces it first")
    except json.JSONDecodeError as e:
        raise CutdownError(f"{path} is not valid JSON: {e}")


def write_json(path: pathlib.Path, obj, indent: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=indent) + "\n")


def read_jsonl(path: pathlib.Path) -> list:
    if not path.exists():
        raise CutdownError(f"missing {path} - run the stage that produces it first")
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise CutdownError(f"{path}:{n} is not valid JSON: {e}")
    return out


def write_jsonl(path: pathlib.Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def hhmmss(seconds: float) -> str:
    neg = seconds < 0
    s = abs(float(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{'-' if neg else ''}{int(h):02d}:{int(m):02d}:{sec:06.3f}"


def mmss(seconds: float) -> str:
    m, s = divmod(round(float(seconds)), 60)
    return f"{int(m)}:{int(s):02d}"


def clock(seconds: float) -> str:
    """M:SS, growing to H:MM:SS only once there is an hour to show.

    For positions a reader scans rather than a machine parses: a clip at 1:04
    and its place in a two-hour session at 1:22:31 want the same function.
    """
    s = max(0, int(round(float(seconds or 0))))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
