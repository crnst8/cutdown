"""Per-stage state on disk, so `run` can skip stages whose outputs are current.

A stage is current when every declared output exists, every declared input has
the same (mtime, size) it had when the stage last succeeded, and the hash of the
config slice the stage depends on is unchanged.
"""
from __future__ import annotations

import json
import pathlib
import time

from .util import log

STATE_DIR = ".state"


def _stamp(p: pathlib.Path) -> dict | None:
    try:
        st = p.stat()
    except FileNotFoundError:
        return None
    return {"mtime": round(st.st_mtime, 6), "size": st.st_size}


def state_path(build: pathlib.Path, stage: str) -> pathlib.Path:
    return build / STATE_DIR / f"{stage}.json"


def record(build: pathlib.Path, stage: str, *, inputs: list[pathlib.Path],
           outputs: list[pathlib.Path], config_hash: str, extra: dict | None = None) -> None:
    p = state_path(build, stage)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "stage": stage,
        "completed_at": time.time(),
        "config_hash": config_hash,
        "inputs": {str(i): _stamp(i) for i in inputs},
        "outputs": {str(o): _stamp(o) for o in outputs},
        **(extra or {}),
    }, indent=1) + "\n")


def is_current(build: pathlib.Path, stage: str, *, inputs: list[pathlib.Path],
               outputs: list[pathlib.Path], config_hash: str) -> bool:
    p = state_path(build, stage)
    if not p.exists():
        return False
    try:
        st = json.loads(p.read_text())
    except json.JSONDecodeError:
        return False
    if st.get("config_hash") != config_hash:
        log(f"  {stage}: config changed")
        return False
    for o in outputs:
        if not o.exists():
            log(f"  {stage}: output missing ({o.name})")
            return False
    recorded_out = st.get("outputs", {})
    for o in outputs:
        was = recorded_out.get(str(o))
        if was is not None and _stamp(o) != was:
            log(f"  {stage}: output edited since last run ({o.name})")
            return False
    recorded_in = st.get("inputs", {})
    for i in inputs:
        was = recorded_in.get(str(i))
        if was != _stamp(i):
            log(f"  {stage}: input changed ({i.name})")
            return False
    return True


def clear(build: pathlib.Path, stage: str) -> None:
    p = state_path(build, stage)
    if p.exists():
        p.unlink()
