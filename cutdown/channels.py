"""Split-channel recordings: one voice on the left, the other on the right.

When each speaker has their own mic on their own channel, the channels already
say who is talking, and they say it better than diarization can. In that mode
each channel is transcribed on its own, with diarization off, and the two word
lists are merged by time:

    host-left     L = host,  R = guest
    guest-left    L = guest, R = host
    mixed         both voices on both channels: transcribe once and diarize

Mics bleed. The guest's mic hears the host faintly, and ASR sometimes writes
those words down a second time on the wrong channel. A word that appears on both
channels at overlapping times with the same text is kept only on the channel
where it is louder.

The merged transcript is WhisperX-shaped (`word_segments`, speakers SPEAKER_L
and SPEAKER_R) and records `cutdown_channels: "split"`, so a transcript from the
other mode is never recovered by mistake. Which side is the guest is not stored
in it: ingest is told, so swapping host-left for guest-left needs no new
transcription.
"""
from __future__ import annotations

import pathlib
import re
import struct
import subprocess

from .util import CutdownError, log, note, read_json, write_json
from .wav import WAVE_FORMAT_EXTENSIBLE, WAVE_FORMAT_IEEE_FLOAT, WAVE_FORMAT_PCM, WavInfo

MODES = {
    "mixed": "both voices on both channels - transcribe once and diarize",
    "host-left": "split: host on the left channel, guest on the right",
    "guest-left": "split: guest on the left channel, host on the right",
}
LEFT, RIGHT = "SPEAKER_L", "SPEAKER_R"
MARK = "split"
CHUNK_FRAMES = 1 << 20


def check(mode: str | None) -> str:
    mode = (mode or "mixed").strip().lower()
    if mode not in MODES:
        raise CutdownError(f"channels must be one of {', '.join(MODES)}; got {mode!r}")
    return mode


def is_split(mode: str | None) -> bool:
    return check(mode) != "mixed"


def guest_speaker(mode: str | None) -> str | None:
    """The speaker label that is the guest, when the channels say so."""
    mode = check(mode)
    return {"host-left": RIGHT, "guest-left": LEFT}.get(mode)


def channel_count(audio: pathlib.Path) -> int | None:
    """Channels in a WAV, or None when the file is not one cutdown can read."""
    try:
        return WavInfo(audio).channels
    except (CutdownError, OSError):
        return None


# ---------------------------------------------------------------- split

def _mono_header(info: WavInfo, n_frames: int) -> bytes:
    width = info.bits_per_sample // 8
    tag = info.audio_format
    if tag == WAVE_FORMAT_EXTENSIBLE:
        # The sub-format GUID starts with the real format tag.
        sub = struct.unpack("<H", info.fmt_raw[24:26])[0] if len(info.fmt_raw) >= 26 \
            else WAVE_FORMAT_PCM
        tag = WAVE_FORMAT_IEEE_FLOAT if sub == WAVE_FORMAT_IEEE_FLOAT else WAVE_FORMAT_PCM
    fmt = struct.pack("<HHIIHH", tag, 1, info.sample_rate, info.sample_rate * width,
                      width, info.bits_per_sample)
    data = n_frames * width
    return (b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + data) + b"WAVE"
            + b"fmt " + struct.pack("<I", len(fmt)) + fmt
            + b"data" + struct.pack("<I", data))


def _split_wav(audio: pathlib.Path, outs: list[pathlib.Path]) -> None:
    """Deinterleave the first two channels, byte for byte. No decode."""
    info = WavInfo(audio)
    if info.channels < 2:
        raise CutdownError(
            f"{audio.name} has {info.channels} channel; split-channel mode needs a "
            f"stereo file. Set channels to mixed.")
    if info.channels > 2:
        log(f"  {audio.name} has {info.channels} channels; using the first two")
    width, block = info.bits_per_sample // 8, info.block_align
    total = info.total_frames
    handles = [open(p, "wb") for p in outs]
    try:
        for h in handles:
            h.write(_mono_header(info, total))
        with open(audio, "rb") as f:
            f.seek(info.data_offset)
            done = 0
            while done < total:
                n = min(CHUNK_FRAMES, total - done)
                buf = f.read(n * block)
                n = len(buf) // block
                if not n:
                    break
                for ch, h in enumerate(handles):
                    out = bytearray(n * width)
                    for k in range(width):
                        out[k::width] = buf[ch * width + k::block][:n]
                    h.write(out)
                done += n
    finally:
        for h in handles:
            h.close()


def _split_ffmpeg(audio: pathlib.Path, outs: list[pathlib.Path]) -> None:
    from .media import find_ffmpeg
    exe = find_ffmpeg()
    cmd = [exe, "-y", "-v", "error", "-i", str(audio),
           "-filter_complex", "[0:a]pan=mono|c0=c0[l];[0:a]pan=mono|c0=c1[r]",
           "-map", "[l]", str(outs[0]), "-map", "[r]", str(outs[1])]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise CutdownError(f"ffmpeg could not split {audio.name}:\n{r.stderr[-800:]}")


def split(audio: pathlib.Path, outdir: pathlib.Path) -> list[pathlib.Path]:
    """Write <stem>.L.wav and <stem>.R.wav; reuse them if already written."""
    outdir.mkdir(parents=True, exist_ok=True)
    outs = [outdir / f"{audio.stem}.L.wav", outdir / f"{audio.stem}.R.wav"]
    src = audio.stat()
    if all(p.exists() and p.stat().st_mtime >= src.st_mtime and _has_audio(p)
           for p in outs):
        return outs
    note(f"  splitting {audio.name} into left and right channels")
    if channel_count(audio) is not None:
        _split_wav(audio, outs)
    else:
        _split_ffmpeg(audio, outs)
    for p in outs:
        if not _has_audio(p):
            p.unlink(missing_ok=True)
            raise CutdownError(
                f"splitting {audio.name} produced no audio in {p.name}; "
                f"the source's data chunk may be unreadable")
    return outs


def _has_audio(path: pathlib.Path) -> bool:
    try:
        return WavInfo(path).total_frames > 0
    except CutdownError:
        return False


# ---------------------------------------------------------------- merge

def _norm(word: str) -> str:
    return re.sub(r"[^\w']", "", word.lower())


def _level(info: WavInfo, start: float, end: float) -> float:
    """Mean absolute sample value over a span, sampled sparsely. Mono PCM/float."""
    frames = info.read_frames(info.frame_at(start),
                             max(1, info.frame_at(end) - info.frame_at(start)))
    width = info.bits_per_sample // 8
    step = width * max(1, len(frames) // width // 400)
    total, n = 0.0, 0
    for i in range(0, len(frames) - width + 1, step):
        b = frames[i:i + width]
        if info.audio_format == WAVE_FORMAT_IEEE_FLOAT and width in (4, 8):
            v = abs(struct.unpack("<f" if width == 4 else "<d", b)[0])
        elif width == 1:
            v = abs(b[0] - 128)
        else:
            v = abs(int.from_bytes(b, "little", signed=True))
        total += v
        n += 1
    return total / n if n else 0.0


def merge(left_json: pathlib.Path, right_json: pathlib.Path,
          mono: list[pathlib.Path] | None, out: pathlib.Path) -> dict:
    """Two single-speaker transcripts -> one, bleed removed. Returns counts."""
    words = []
    for path, spk in ((left_json, LEFT), (right_json, RIGHT)):
        for w in read_json(path).get("word_segments", []):
            if "start" in w and "end" in w:
                words.append({**w, "speaker": spk})
    words.sort(key=lambda w: (w["start"], w["end"]))

    infos = {}
    if mono:
        try:
            infos = {LEFT: WavInfo(mono[0]), RIGHT: WavInfo(mono[1])}
        except (CutdownError, OSError):
            infos = {}
    drop: set[int] = set()
    for i, a in enumerate(words):
        if i in drop:
            continue
        for j in range(i + 1, len(words)):
            b = words[j]
            if b["start"] >= a["end"]:
                break
            if j in drop or b["speaker"] == a["speaker"] or _norm(a["word"]) != _norm(b["word"]):
                continue
            overlap = min(a["end"], b["end"]) - max(a["start"], b["start"])
            shorter = min(a["end"] - a["start"], b["end"] - b["start"]) or 1e-6
            if overlap / shorter < 0.5:
                continue
            if infos:
                la = _level(infos[a["speaker"]], a["start"], a["end"])
                lb = _level(infos[b["speaker"]], b["start"], b["end"])
                loser = j if la >= lb else i
            else:
                loser = j if a.get("score", 0) >= b.get("score", 0) else i
            drop.add(loser)
            if loser == i:
                break
    kept = [w for n, w in enumerate(words) if n not in drop]
    write_json(out, {"word_segments": kept, "cutdown_channels": MARK,
                     "sources": [left_json.name, right_json.name]})
    stats = {"left": sum(1 for w in kept if w["speaker"] == LEFT),
             "right": sum(1 for w in kept if w["speaker"] == RIGHT),
             "bleed_removed": len(drop)}
    note(f"  merged channels: {stats['left']} words left, {stats['right']} right, "
         f"{stats['bleed_removed']} bleed duplicates removed -> {out.name}")
    return stats
