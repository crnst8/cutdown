"""Provider abstraction.

Two transport types, one interface. Nothing past this boundary knows whether a
stage went out over HTTPS to an API or into a subscription-backed CLI on this
machine: every stage asks for `request_json(...)` and gets validated JSON back.

Every raw request and response pair is written under runs/<timestamp>/ before it
is parsed, so a malformed reply can be read afterwards rather than guessed at.
"""
from __future__ import annotations

import json
import pathlib
import re
from dataclasses import dataclass, field

from ..schema import ValidationError, validate
from ..util import CutdownError, log, note


@dataclass
class Result:
    data: dict
    raw: str
    attempts: int
    provider: str
    model: str
    meta: dict = field(default_factory=dict)


class Provider:
    """Base transport. Subclasses implement send()."""

    kind = "base"

    def __init__(self, name: str, cfg: dict):
        self.name = name
        self.cfg = cfg

    def preflight(self) -> None:
        """Fail early and clearly if this provider cannot possibly work."""

    def send(self, *, system: str, user: str, model: str,
             artifacts: pathlib.Path, extra_body=None) -> tuple[str, dict]:
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.name} ({self.kind})"


def get_named_provider(config, name: str) -> Provider:
    """Build one allow-listed provider by name."""
    config.check_allowed(name)
    cfg = config.provider(name)
    kind = cfg.get("type")
    if kind == "api":
        from .api import ApiProvider
        p: Provider = ApiProvider(name, cfg)
    elif kind == "cli":
        from .headless import CliProvider
        p = CliProvider(name, cfg)
    else:
        raise CutdownError(f"provider {name!r} has unknown type {kind!r}; "
                           f"expected 'api' or 'cli'")
    return p


def get_provider(config, stage: str) -> tuple[Provider, str]:
    """Resolve a stage to a live provider, refusing anything not allow-listed."""
    routing = config.stage(stage)
    name, model = routing["provider"], routing["model"]
    return get_named_provider(config, name), model


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict:
    """Pull the JSON object out of a reply that may carry prose or fences."""
    if not text or not text.strip():
        raise ValidationError("the reply was empty")
    candidates = [m.group(1) for m in _FENCE.finditer(text)]
    candidates.append(text)
    for chunk in candidates:
        chunk = chunk.strip()
        for opener, closer in (("{", "}"), ("[", "]")):
            a = chunk.find(opener)
            b = chunk.rfind(closer)
            if a == -1 or b <= a:
                continue
            try:
                obj = json.loads(chunk[a:b + 1])
            except json.JSONDecodeError:
                continue
            return obj if isinstance(obj, dict) else {"items": obj}
    raise ValidationError("no parseable JSON object was found in the reply")


RETRY_PREFACE = (
    "Your previous reply could not be used. It failed validation with:\n\n"
    "  {error}\n\n"
    "Your previous reply was:\n\n{previous}\n\n"
    "Send the corrected JSON object only. No prose, no code fence, no commentary. "
    "Do not change any decision that was already valid; fix only what the error names."
)


def request_json(provider: Provider, *, model: str, system: str, user: str,
                 schema: dict, run_dir: pathlib.Path, tag: str,
                 dry_run: bool = False, max_attempts: int = 2, extra_body=None) -> Result:
    """Send, validate, and retry once with the validation error fed back.

    On --dry-run the complete prompts are written to the run directory and
    printed, and nothing leaves the machine.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / f"{tag}.system.txt").write_text(system)
    (run_dir / f"{tag}.user.txt").write_text(user)
    (run_dir / f"{tag}.schema.json").write_text(json.dumps(schema, indent=1))

    if dry_run:
        note(f"--- dry-run {tag} -> {provider.describe()} model={model}")
        note(f"    system {len(system)} chars, user {len(user)} chars, "
             f"nothing sent. Prompts written to {run_dir}")
        return Result(data={}, raw="", attempts=0, provider=provider.name,
                      model=model, meta={"dry_run": True})

    provider.preflight()
    prompt, last_error, last_raw = user, None, ""
    for attempt in range(1, max_attempts + 1):
        artifacts = run_dir / f"{tag}.attempt{attempt}"
        (run_dir / f"{tag}.attempt{attempt}.request.txt").write_text(prompt)
        raw, meta = provider.send(system=system, user=prompt, model=model,
                                  artifacts=artifacts, extra_body=extra_body)
        (run_dir / f"{tag}.attempt{attempt}.response.txt").write_text(raw or "")
        (run_dir / f"{tag}.attempt{attempt}.meta.json").write_text(
            json.dumps(meta, indent=1, default=str))
        last_raw = raw
        try:
            data = extract_json(raw)
            validate(data, schema)
        except ValidationError as e:
            last_error = str(e)
            log(f"  {tag} attempt {attempt} rejected: {last_error}")
            if attempt == max_attempts:
                break
            prompt = RETRY_PREFACE.format(error=last_error, previous=(raw or "")[:6000]) \
                + "\n\n--- original request follows ---\n\n" + user
            continue
        if isinstance(meta.get("usage"), dict):
            u = meta["usage"]
            cost = u.get("cost")
            log(f"  {tag}: {u.get('prompt_tokens', '?')} in / {u.get('completion_tokens', '?')} out tokens"
                + (f", ${cost:.4f}" if isinstance(cost, (int, float)) else ""))
        return Result(data=data, raw=raw, attempts=attempt, provider=provider.name,
                      model=model, meta=meta)

    raise CutdownError(
        f"{tag}: {provider.describe()} did not return usable JSON after "
        f"{max_attempts} attempts. Last error: {last_error}. "
        f"Raw replies are in {run_dir}."
    )


def stage_options(config, stage) -> dict:
    return {"extra_body": (config.get(f"stages.{stage}") or {}).get("extra_body")}
