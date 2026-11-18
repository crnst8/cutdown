"""Broadcast Wave `bext` chunk: build it, and read it back to verify.

TimeReference is the whole point. Pro Tools' Spot -> Timestamp reads it as an
offset in samples from midnight (or, as used here, from the start of the master),
which is what lets a folder of clips drop back onto the timeline in the right
places instead of arriving as orphans.
"""
from __future__ import annotations

import pathlib
import shutil
import struct
import subprocess
from datetime import datetime

from .util import CutdownError

BEXT_FIXED_SIZE = 602


def _fixed(s: str, n: int) -> bytes:
    b = s.encode("ascii", "replace")[:n]
    return b + b"\x00" * (n - len(b))


def build_bext(*, time_reference: int, description: str = "", originator: str = "",
               originator_reference: str = "", origination: datetime | None = None,
               coding_history: str = "") -> bytes:
    """A BWF version-1 bext chunk payload."""
    if time_reference < 0:
        raise CutdownError(f"negative BWF time_reference ({time_reference})")
    dt = origination or datetime.now()
    out = b"".join([
        _fixed(description, 256),
        _fixed(originator, 32),
        _fixed(originator_reference, 32),
        _fixed(dt.strftime("%Y-%m-%d"), 10),
        _fixed(dt.strftime("%H:%M:%S"), 8),
        struct.pack("<II", time_reference & 0xFFFFFFFF, (time_reference >> 32) & 0xFFFFFFFF),
        struct.pack("<H", 1),          # version
        b"\x00" * 64,                  # UMID
        b"\x00" * 10,                  # loudness fields (version 2; reserved in v1)
        b"\x00" * 180,                 # reserved
    ])
    assert len(out) == BEXT_FIXED_SIZE, len(out)
    if coding_history:
        out += coding_history.encode("ascii", "replace") + b"\r\n"
    return out


def read_bext(path: pathlib.Path) -> dict | None:
    """Parse the bext chunk straight out of the file. Returns None if absent."""
    with open(path, "rb") as f:
        hdr = f.read(12)
        if len(hdr) < 12 or hdr[:4] not in (b"RIFF", b"RF64", b"BW64"):
            raise CutdownError(f"{path}: not a RIFF/WAVE file")
        while True:
            ch = f.read(8)
            if len(ch) < 8:
                return None
            cid, size = struct.unpack("<4sI", ch)
            pos = f.tell()
            if cid == b"bext":
                body = f.read(min(size, 100000))
                if len(body) < 346:
                    raise CutdownError(f"{path}: truncated bext chunk")
                lo, hi = struct.unpack("<II", body[338:346])
                return {
                    "description": body[:256].split(b"\x00")[0].decode("ascii", "replace"),
                    "originator": body[256:288].split(b"\x00")[0].decode("ascii", "replace"),
                    "originator_reference": body[288:320].split(b"\x00")[0].decode("ascii", "replace"),
                    "origination_date": body[320:330].decode("ascii", "replace").strip("\x00"),
                    "origination_time": body[330:338].decode("ascii", "replace").strip("\x00"),
                    "time_reference": lo | (hi << 32),
                    "version": struct.unpack("<H", body[346:348])[0] if len(body) >= 348 else 0,
                }
            f.seek(pos + size + (size % 2))


def read_bext_via_bwfmetaedit(path: pathlib.Path) -> dict | None:
    """Cross-check with bwfmetaedit when it happens to be installed."""
    exe = shutil.which("bwfmetaedit")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--out-xml", str(path)], capture_output=True,
                           text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e)}
    xml = r.stdout
    out: dict = {"raw": xml}
    for tag in ("TimeReference", "Description", "Originator", "OriginationDate"):
        a = xml.find(f"<{tag}>")
        b = xml.find(f"</{tag}>")
        if a != -1 and b > a:
            out[tag] = xml[a + len(tag) + 2:b]
    return out


def verify(path: pathlib.Path, expected_time_reference: int | None = None) -> dict:
    """Read the stamp back off disk. Raises if it did not survive."""
    got = read_bext(path)
    if got is None:
        raise CutdownError(f"{path}: no bext chunk survived the write")
    if expected_time_reference is not None and got["time_reference"] != expected_time_reference:
        raise CutdownError(
            f"{path}: bext TimeReference is {got['time_reference']}, "
            f"expected {expected_time_reference}"
        )
    cross = read_bext_via_bwfmetaedit(path)
    if cross and "TimeReference" in cross:
        if int(cross["TimeReference"]) != got["time_reference"]:
            raise CutdownError(
                f"{path}: bwfmetaedit reads TimeReference {cross['TimeReference']} "
                f"but the chunk contains {got['time_reference']}"
            )
        got["bwfmetaedit_agrees"] = True
    return got
