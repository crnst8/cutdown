"""WhisperX convenience wrapper.

Reproduces the invocation this project was verified against:

  uvx --python 3.12 whisperx==3.8.6 <wav> --model large-v3 --language en --device cpu \\
      --compute_type int8 --diarize --min_speakers 2 --max_speakers 2 \\
      --output_format json --output_dir <out>

WhisperX decodes audio by running `ffmpeg` by name, so the child gets a PATH
with a known-working ffmpeg first (see media.py). Its diarization is handed the
decoded waveform, so torchcodec failing to load FFmpeg's shared libraries only
prints a warning. Anything else is surfaced rather than swallowed.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import time
import json

from .util import CutdownError, log, note

RPATH_HINT = (
    "\nA library failed to load. ffmpeg itself was checked before the run, so this\n"
    "is inside WhisperX's own environment. Clear it and let uvx rebuild it:\n"
    "  uv cache clean whisperx torch torchcodec torchaudio\n"
)


def _completed_transcript(path: pathlib.Path) -> bool:
    """Whether *path* looks like the JSON WhisperX leaves on a successful run.

    WhisperX writes its JSON before this process gets a chance to record stage
    state.  Checking the completed artifact here makes that small interruption
    window resumable instead of needlessly transcribing the audio again.
    """
    try:
        return path.is_file() and path.stat().st_size > 0 and isinstance(
            json.loads(path.read_text()), dict)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return False


def hf_token(cfg: dict) -> str:
    env = cfg.get("hf_token_env", "HF_TOKEN")
    tok = os.environ.get(env, "").strip()
    if tok:
        return tok
    for d in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]:
        f = d / ".env"
        if f.exists():
            for line in f.read_text().splitlines():
                if line.strip().startswith(f"{env}="):
                    return line.split("=", 1)[1].strip().strip('"\'')
            break
    return ""


def build_command(audio: pathlib.Path, outdir: pathlib.Path, cfg: dict) -> list[str]:
    cmd = list(cfg.get("command", ["uvx", "whisperx"]))
    cmd += [str(audio),
            "--model", str(cfg.get("model", "large-v3")),
            "--language", str(cfg.get("language", "en")),
            "--device", str(cfg.get("device", "cpu")),
            "--compute_type", str(cfg.get("compute_type", "int8"))]
    if cfg.get("diarize", True):
        cmd += ["--diarize",
                "--min_speakers", str(cfg.get("min_speakers", 2)),
                "--max_speakers", str(cfg.get("max_speakers", 2))]
    cmd += ["--output_format", "json", "--output_dir", str(outdir)]
    return cmd


def whisperx_env(ffmpeg: str | None = None) -> dict[str, str]:
    """The environment WhisperX runs in: a working `ffmpeg` first on PATH."""
    from .media import tool_env
    return tool_env(ffmpeg)


def _mode_of(path: pathlib.Path) -> str:
    """"split" for a merged two-channel transcript, "mixed" for plain WhisperX."""
    try:
        return "split" if json.loads(path.read_text()).get("cutdown_channels") \
            else "mixed"
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return "mixed"


def run_transcribe(audio: pathlib.Path, outdir: pathlib.Path, cfg: dict, *,
                   dry_run: bool = False, on_line=None, on_proc=None,
                   recover: bool = True) -> pathlib.Path:
    """Transcribe `audio` to <outdir>/<stem>.json.

    `cfg["channels"]` picks the path: "mixed" (the default) runs WhisperX once
    with diarization; "host-left" / "guest-left" transcribe each channel on its
    own and merge them (see channels.py).
    """
    from . import channels
    if not audio.exists():
        raise CutdownError(f"audio not found: {audio}")
    outdir.mkdir(parents=True, exist_ok=True)
    expected = outdir / f"{audio.stem}.json"
    split = channels.is_split(cfg.get("channels"))
    want = "split" if split else "mixed"

    if recover and _completed_transcript(expected):
        if _mode_of(expected) == want:
            note(f"  recovered existing transcript -> {expected}")
            return expected
        note(f"  existing transcript is {_mode_of(expected)}-channel; "
             f"transcribing again as {want}")

    if not split:
        return _whisperx(audio, outdir, cfg, dry_run=dry_run, on_line=on_line,
                         on_proc=on_proc)

    # One voice per channel: no diarization, and each side is cached on its own
    # so an interruption between them costs one channel, not both.
    chdir = outdir / "channels"
    if dry_run:
        note(f"transcribe: split {audio.name} into L/R, WhisperX each without "
             f"diarization, merge -> {expected.name}")
        note("  --dry-run: not started")
        return expected
    mono = channels.split(audio, chdir)
    one = {**cfg, "diarize": False}
    sides = []
    for m in mono:
        done = chdir / f"{m.stem}.json"
        if recover and _completed_transcript(done):
            note(f"  recovered {done.name}")
            sides.append(done)
            continue
        note(f"  transcribing the {'left' if m.stem.endswith('.L') else 'right'} "
             f"channel")
        sides.append(_whisperx(m, chdir, one, dry_run=False, on_line=on_line,
                               on_proc=on_proc))
    channels.merge(sides[0], sides[1], mono, expected)
    return expected


def _whisperx(audio: pathlib.Path, outdir: pathlib.Path, cfg: dict, *,
              dry_run: bool, on_line, on_proc) -> pathlib.Path:
    expected = outdir / f"{audio.stem}.json"
    token = hf_token(cfg) if cfg.get("diarize", True) and not dry_run else ""
    if cfg.get("diarize", True) and not dry_run and not token:
        raise CutdownError(
            f"diarization needs a Hugging Face token in "
            f"${cfg.get('hf_token_env', 'HF_TOKEN')}. Put it in .env, or set "
            f"transcribe.diarize = false (which makes the guest/host split "
            f"unusable, so don't)."
        )

    cmd = build_command(audio, outdir, cfg)
    note("transcribe: " + " ".join(cmd))
    if dry_run:
        note("  --dry-run: not started")
        return expected
    if not shutil.which(cmd[0]):
        raise CutdownError(f"{cmd[0]!r} is not on PATH. WhisperX is run via uvx so it "
                           f"stays out of this project's environment; install uv.")

    env = whisperx_env()
    if token:
        env["HF_TOKEN"] = token

    t0 = time.time()
    tail: list[str] = []
    # Its own process group: WhisperX shells out to ffmpeg, and a caller that
    # wants this stopped (the studio quitting) must be able to take the lot.
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=env, start_new_session=True)
    if on_proc:
        on_proc(proc)
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if token:
            line = line.replace(token, "<hf_token>")
        tail.append(line)
        del tail[:-60]
        if on_line:
            on_line(line)
        else:
            log("  " + line)
    rc = proc.wait(timeout=cfg.get("timeout_sec", 14400))
    elapsed = time.time() - t0

    if rc != 0:
        detail = "\n".join(tail[-25:])
        hint = RPATH_HINT if ("torchcodec" in detail or "Library not loaded" in detail
                              or "dyld" in detail) else ""
        raise CutdownError(f"whisperx exited {rc} after {elapsed/60:.1f} min.\n"
                           f"{detail}{hint}")
    if not expected.exists():
        found = sorted(p for p in outdir.glob("*.json") if _completed_transcript(p))
        if len(found) == 1:
            expected = found[0]
        else:
            raise CutdownError(f"whisperx finished but produced no JSON in {outdir}")
    note(f"  transcribed in {elapsed/60:.1f} min -> {expected}")
    return expected
