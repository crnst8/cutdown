"""Finding an ffmpeg that runs, and decoding what the byte-level path cannot read.

A Homebrew upgrade can leave `ffmpeg` (or an older keg such as ffmpeg@7) linked
against a dylib that no longer exists; the binary then aborts in dyld before it
prints anything. So no path is trusted by name: every candidate is run with
`-version` and the first one that exits 0 is used. The last candidate is the
static build shipped in the `imageio-ffmpeg` wheel, which links only against
system libraries and is never touched by Homebrew, so there is always one that
works.

Order: an explicit path (`--ffmpeg`, `render.ffmpeg`), `$CUTDOWN_FFMPEG`,
`ffmpeg` on PATH, the usual Homebrew/MacPorts locations, then the bundled one.
"""
from __future__ import annotations

import functools
import os
import pathlib
import re
import shutil
import subprocess

from .util import CutdownError, note

KNOWN = [
    "/opt/homebrew/opt/ffmpeg/bin/ffmpeg",
    "/opt/homebrew/opt/ffmpeg@8/bin/ffmpeg",
    "/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg",
    "/opt/homebrew/opt/ffmpeg@6/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/usr/local/opt/ffmpeg/bin/ffmpeg",
    "/opt/local/bin/ffmpeg",
    "/usr/bin/ffmpeg",
]

SHIM_DIR = pathlib.Path(os.environ.get("XDG_CACHE_HOME") or pathlib.Path.home() / ".cache") \
    / "cutdown" / "bin"


def _bundled() -> str | None:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001 - not installed, or no binary for this platform
        return None


def _candidates(preferred: str | None) -> list[str]:
    out: list[str] = []
    for c in (preferred, os.environ.get("CUTDOWN_FFMPEG"), shutil.which("ffmpeg"),
              *KNOWN):
        if not c or c == "ffmpeg":
            continue
        c = shutil.which(c) or c
        if c not in out and pathlib.Path(c).is_file():
            out.append(c)
    return out


def _probe(exe: str) -> str:
    """"" when `exe -version` succeeds, otherwise the reason it did not."""
    try:
        r = subprocess.run([exe, "-version"], capture_output=True, text=True,
                           timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return str(e)
    if r.returncode == 0:
        return ""
    lines = (r.stderr or r.stdout).strip().splitlines()
    return lines[0] if lines else f"exit {r.returncode}"


@functools.cache
def find_ffmpeg(preferred: str | None = None) -> str:
    """Absolute path of the first ffmpeg that runs. Cached for the process."""
    tried = []
    for exe in _candidates(preferred):
        why = _probe(exe)
        if not why:
            return exe
        tried.append(f"  {exe}: {why}")
    bundled = _bundled()
    if bundled:
        why = _probe(bundled)
        if not why:
            if tried:
                note("  no working system ffmpeg; using the bundled one")
            return bundled
        tried.append(f"  {bundled} (bundled): {why}")
    raise CutdownError(
        "no working ffmpeg found.\n" + ("\n".join(tried) or "  none installed") +
        "\nInstall a working FFmpeg or pass --ffmpeg /path/to/ffmpeg.")


def tool_env(preferred: str | None = None) -> dict[str, str]:
    """os.environ with a working `ffmpeg` first on PATH, for child processes
    (WhisperX) that call it by name.

    The bundled binary is not called `ffmpeg`, so PATH gets a directory holding
    a link named `ffmpeg` to whichever binary was chosen.
    """
    exe = find_ffmpeg(preferred)
    env = dict(os.environ)
    if pathlib.Path(exe).name == "ffmpeg":
        first = str(pathlib.Path(exe).parent)
    else:
        SHIM_DIR.mkdir(parents=True, exist_ok=True)
        link = SHIM_DIR / "ffmpeg"
        if not (link.is_symlink() and os.readlink(link) == exe):
            tmp = SHIM_DIR / f".ffmpeg.{os.getpid()}"
            tmp.unlink(missing_ok=True)
            tmp.symlink_to(exe)
            tmp.replace(link)
        first = str(SHIM_DIR)
    env["PATH"] = first + os.pathsep + env.get("PATH", "")
    return env


# Decoded sample format (as `ffmpeg -i` reports it) -> the PCM codec that holds
# it without loss. Lossy codecs decode to float, so they are kept as float.
_PCM = [
    (re.compile(r"\bs16p?\b"), "pcm_s16le"),
    (re.compile(r"\bs32p?\b.*\(24 bit\)"), "pcm_s24le"),
    (re.compile(r"\bs32p?\b"), "pcm_s32le"),
    (re.compile(r"\bs64p?\b"), "pcm_s32le"),
    (re.compile(r"\b(fltp?|dblp?)\b"), "pcm_f32le"),
    (re.compile(r"\bu8p?\b"), "pcm_u8"),
]


def _pcm_codec(exe: str, src: pathlib.Path) -> str:
    r = subprocess.run([exe, "-hide_banner", "-nostdin", "-protocol_whitelist",
                        "file,pipe", "-i", str(src.resolve())], capture_output=True,
                       text=True, check=False)
    m = re.search(r"Stream #\S+.*?Audio: (.*)", r.stderr)
    if not m:
        raise CutdownError(f"ffmpeg found no audio stream in {src.name}:\n"
                           f"{r.stderr.strip()[-600:]}")
    for pat, codec in _PCM:
        if pat.search(m.group(1)):
            return codec
    return "pcm_s24le"


def to_pcm_wav(src: pathlib.Path, dst: pathlib.Path,
               ffmpeg: str | None = None) -> pathlib.Path:
    """Decode `src` once to an uncompressed WAV at `dst`, keeping its sample rate,
    channel count and bit depth. Reused while newer than `src`."""
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime \
            and dst.stat().st_size > 44:
        return dst
    exe = find_ffmpeg(ffmpeg)
    codec = _pcm_codec(exe, src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.part")
    note(f"  decoding {src.name} to uncompressed {codec} -> {dst}")
    cmd = [exe, "-y", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
           "-i", str(src.resolve()), "-map", "0:a:0",
           "-c:a", codec, "-rf64", "auto", "-f", "wav", str(tmp)]
    r = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise CutdownError(f"ffmpeg could not decode {src.name}:\n{r.stderr[-800:]}")
    tmp.replace(dst)
    return dst
