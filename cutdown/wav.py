"""Minimal RIFF/WAVE reader and writer.

Clips are produced by slicing the master's data chunk on sample boundaries and
writing a new header around those bytes. No decode, no encode, no resample: the
output is a byte-for-byte copy of the master's samples, so bit depth and sample
rate are preserved by construction and a clip's length is exact to the sample.

Reads plain RIFF and RF64/BW64. Writes plain RIFF (clips and stringouts are far
below the 4 GB limit).
"""
from __future__ import annotations

import pathlib
import struct

from .util import CutdownError

WAVE_FORMAT_PCM = 0x0001
WAVE_FORMAT_IEEE_FLOAT = 0x0003
WAVE_FORMAT_EXTENSIBLE = 0xFFFE


def _chunk_follows(f) -> bool:
    """True if the next 8 bytes look like a chunk header (printable ASCII id).

    Used to tell a genuinely empty data chunk from one whose size field was
    left unset. The file position is restored.
    """
    pos = f.tell()
    head = f.read(8)
    f.seek(pos)
    if len(head) < 8:
        return True  # nothing after the header: really empty
    return all(0x20 <= b <= 0x7E for b in head[:4])


class WavInfo:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.fmt_raw = b""
        self.data_offset = 0
        self.data_size = 0
        self.channels = 0
        self.sample_rate = 0
        self.bits_per_sample = 0
        self.block_align = 0
        self.audio_format = 0
        self.bext_time_reference = 0
        self.has_bext = False
        self._parse()

    def _parse(self) -> None:
        with open(self.path, "rb") as f:
            hdr = f.read(12)
            if len(hdr) < 12:
                raise CutdownError(f"{self.path}: too short to be a WAV")
            riff, _riff_size, wave = struct.unpack("<4sI4s", hdr)
            if riff not in (b"RIFF", b"RF64", b"BW64") or wave != b"WAVE":
                raise CutdownError(
                    f"{self.path}: not a RIFF/WAVE file (found {riff!r}/{wave!r}). "
                    f"Convert it to WAV first, or pass a WAV master."
                )
            ds64_data = None
            while True:
                ch = f.read(8)
                if len(ch) < 8:
                    break
                cid, size = struct.unpack("<4sI", ch)
                pos = f.tell()
                if cid == b"ds64":
                    body = f.read(min(size, 28))
                    if len(body) >= 16:
                        _riff64, data64 = struct.unpack("<QQ", body[:16])
                        ds64_data = data64
                elif cid == b"fmt ":
                    self.fmt_raw = f.read(size)
                    if len(self.fmt_raw) < 16:
                        raise CutdownError(f"{self.path}: truncated fmt chunk")
                    (self.audio_format, self.channels, self.sample_rate, _byte_rate,
                     self.block_align, self.bits_per_sample) = struct.unpack(
                        "<HHIIHH", self.fmt_raw[:16])
                elif cid == b"bext":
                    body = f.read(min(size, 610))
                    self.has_bext = True
                    if len(body) >= 346:
                        lo, hi = struct.unpack("<II", body[338:346])
                        self.bext_time_reference = lo | (hi << 32)
                elif cid == b"data":
                    self.data_offset = pos
                    self.data_size = size
                    if size == 0xFFFFFFFF and ds64_data is not None:
                        self.data_size = ds64_data
                    elif size in (0, 0xFFFFFFFF) and not _chunk_follows(f):
                        # Header never finalised (streaming or crashed
                        # recorders, e.g. Netia writes 0): the samples run
                        # to the end of the file.
                        self.data_size = 0xFFFFFFFFFFFFFFFF
                    break
                f.seek(pos + size + (size % 2))

        if not self.fmt_raw:
            raise CutdownError(f"{self.path}: no fmt chunk")
        if not self.data_offset:
            raise CutdownError(f"{self.path}: no data chunk")
        if self.audio_format not in (WAVE_FORMAT_PCM, WAVE_FORMAT_IEEE_FLOAT,
                                     WAVE_FORMAT_EXTENSIBLE):
            raise CutdownError(
                f"{self.path}: audio format 0x{self.audio_format:04X} is compressed. "
                f"The byte-level cutter needs uncompressed PCM; render decodes "
                f"it once with ffmpeg."
            )
        if self.block_align == 0:
            raise CutdownError(f"{self.path}: fmt chunk has block_align 0")
        actual = self.path.stat().st_size - self.data_offset
        if self.data_size > actual:
            self.data_size = actual - (actual % self.block_align)

    @property
    def total_frames(self) -> int:
        return self.data_size // self.block_align

    @property
    def duration(self) -> float:
        return self.total_frames / self.sample_rate

    def describe(self) -> str:
        return (f"{self.sample_rate} Hz  {self.bits_per_sample}-bit  "
                f"{self.channels} ch  {self.duration:.3f} s")

    def frame_at(self, seconds: float) -> int:
        """Sample index for a time, clamped into the file."""
        n = int(round(seconds * self.sample_rate))
        return max(0, min(n, self.total_frames))

    def read_frames(self, start_frame: int, n_frames: int) -> bytes:
        start_frame = max(0, min(start_frame, self.total_frames))
        n_frames = max(0, min(n_frames, self.total_frames - start_frame))
        with open(self.path, "rb") as f:
            f.seek(self.data_offset + start_frame * self.block_align)
            return f.read(n_frames * self.block_align)

    def silence(self, n_frames: int) -> bytes:
        """Digital black in this file's format."""
        if self.audio_format == WAVE_FORMAT_IEEE_FLOAT or self.bits_per_sample != 8:
            return b"\x00" * (n_frames * self.block_align)
        return b"\x80" * (n_frames * self.block_align)  # 8-bit PCM is unsigned


def write_wav(path: pathlib.Path, info: WavInfo, frames: bytes,
              extra_chunks: list[tuple[bytes, bytes]] | None = None) -> None:
    """Write a WAV with the source's fmt chunk copied verbatim.

    extra_chunks are written between fmt and data, in order.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    chunks: list[tuple[bytes, bytes]] = [(b"fmt ", info.fmt_raw)]
    chunks += extra_chunks or []
    chunks.append((b"data", frames))

    body = b""
    for cid, payload in chunks:
        body += struct.pack("<4sI", cid, len(payload)) + payload
        if len(payload) % 2:
            body += b"\x00"
    riff_size = 4 + len(body)
    if riff_size > 0xFFFFFFFF:
        raise CutdownError(f"{path}: output exceeds the 4 GB RIFF limit")
    with open(path, "wb") as f:
        f.write(struct.pack("<4sI4s", b"RIFF", riff_size, b"WAVE"))
        f.write(body)
