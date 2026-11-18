"""Prompt template loading.

Templates live in cutdown/prompts/ as plain text, not as strings in the code:
the producer will iterate on the wording far more often than on the pipeline.
Point `--prompts <dir>` or `prompts_dir` in cutdown.toml at a copy to try
alternatives without touching the package.

Substitution is {{name}}, not str.format, because these templates are full of
JSON braces.
"""
from __future__ import annotations

import pathlib
import re

from .util import CutdownError

BUILTIN = pathlib.Path(__file__).parent / "prompts"
_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def load(name: str, prompts_dir: pathlib.Path | None = None) -> str:
    for base in ([prompts_dir] if prompts_dir else []) + [BUILTIN]:
        p = base / f"{name}.md"
        if p.exists():
            return p.read_text()
    raise CutdownError(f"prompt template {name}.md not found in "
                       f"{prompts_dir or ''} or {BUILTIN}")


def render(name: str, prompts_dir: pathlib.Path | None = None, **vars_) -> str:
    text = load(name, prompts_dir)
    missing: list[str] = []

    def sub(m):
        k = m.group(1)
        if k not in vars_:
            missing.append(k)
            return m.group(0)
        return str(vars_[k])

    out = _PLACEHOLDER.sub(sub, text)
    if missing:
        raise CutdownError(f"{name}.md references unknown placeholders: "
                           f"{', '.join(sorted(set(missing)))}")
    return out
