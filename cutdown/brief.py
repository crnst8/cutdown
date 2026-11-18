"""The producer's run sheet: guest, show, and the ordered list of segments."""
from __future__ import annotations

import pathlib

import yaml

from .util import CutdownError

VALID_KINDS = {"guest_pick", "own_work", "other"}


class Brief:
    def __init__(self, data: dict, path: pathlib.Path | None = None):
        self.data = data
        self.path = path
        self.validate()

    @classmethod
    def load(cls, path: pathlib.Path) -> "Brief":
        if not path.exists():
            raise CutdownError(f"brief not found: {path}. "
                               f"`cutdown discover <build> -o brief.yaml` drafts one.")
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError as e:
            raise CutdownError(f"{path} is not valid YAML: {e}")
        return cls(data, path)

    def validate(self) -> None:
        d = self.data
        where = f"{self.path}: " if self.path else ""
        segs = d.get("segments")
        if not segs or not isinstance(segs, list):
            raise CutdownError(f"{where}brief needs a non-empty `segments:` list")
        seen = set()
        for i, s in enumerate(segs, 1):
            if not isinstance(s, dict):
                raise CutdownError(f"{where}segment {i} is not a mapping")
            sid = s.get("id")
            if not sid:
                raise CutdownError(f"{where}segment {i} has no `id`")
            if sid in seen:
                raise CutdownError(f"{where}duplicate segment id {sid!r}")
            seen.add(sid)
            t = s.get("target_sec")
            if not (isinstance(t, list) and len(t) == 2
                    and all(isinstance(x, (int, float)) for x in t)):
                raise CutdownError(f"{where}{sid}: target_sec must be [low, high] in seconds")
            if t[0] > t[1]:
                raise CutdownError(f"{where}{sid}: target_sec low is above high ({t})")
            a = s.get("anchor") or {}
            kind = a.get("kind", "guest_pick")
            if kind not in VALID_KINDS:
                raise CutdownError(f"{where}{sid}: anchor kind {kind!r} must be one of "
                                   f"{', '.join(sorted(VALID_KINDS))}")

    @property
    def guest(self) -> str:
        return self.data.get("guest", "") or ""

    @property
    def show(self) -> str:
        return self.data.get("show", "") or ""

    @property
    def episode(self) -> str:
        return self.data.get("episode", "") or ""

    @property
    def master(self) -> str:
        return self.data.get("master", "") or ""

    @property
    def segments(self) -> list[dict]:
        return self.data["segments"]

    def track_lines(self) -> str:
        """The track list a model may use as topic ids."""
        out = []
        for s in self.segments:
            a = s.get("anchor") or {}
            title = a.get("title", "") or "(untitled)"
            artist = a.get("artist", "") or "?"
            out.append(f"  {s['id']}  {artist} - {title}  [{a.get('kind', 'guest_pick')}]")
        return "\n".join(out) if out else "  (no brief yet)"


def dump_brief(data: dict) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
