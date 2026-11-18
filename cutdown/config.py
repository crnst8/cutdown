"""Configuration: baked-in defaults, overridden by cutdown.toml.

Every value the model routing depends on lives here so that `run` can hash the
slice of config a stage cares about and decide whether that stage is current.
"""
from __future__ import annotations

import copy
import hashlib
import json
import pathlib
import tomllib

from .util import CutdownError, log

CONFIG_NAME = "cutdown.toml"

DEFAULTS: dict = {
    # Refuse to send transcript text to anything not named here. Unpublished
    # talent audio is involved; provider choice is the producer's, not a default.
    "allowed_providers": ["openrouter", "zengo", "zen"],
    "providers": {
        # OpenRouter, pay-as-you-go, one OpenAI-shaped /chat/completions for
        # every model. `provider` is OpenRouter's routing object: data_collection
        # "deny" restricts each request to upstream hosts that do not store or
        # train on prompts, because the prompts are unpublished talent transcript.
        "openrouter": {
            "type": "api",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key_env": "OPENROUTER_KEY",
            "wire": "chat",
            "headers": {"X-Title": "cutdown"},
            "extra_body": {"provider": {"data_collection": "deny"}},
            "timeout_sec": 300,
            "deadline_sec": 600,
            "max_tokens": 32000,
            "temperature": 0,
        },
        # OpenCode Go subscription. Verified 2026-09-24: Bearer auth, 35 models,
        # a session header on every request, and each model served on exactly
        # one of three wire formats (see providers/api.py).
        "zengo": {
            "type": "api",
            "base_url": "https://opencode.ai/zen/go/v1",
            "api_key_env": "OPENCODE_KEY",
            "session_header": "x-opencode-session",
            # "auto" starts from wire_by_model (prefix match) and falls through
            # to the other two formats on "Endpoint is unavailable".
            "wire": "auto",
            "wire_by_model": {"gpt-": "responses", "grok-": "responses",
                              "qwen": "messages", "minimax": "messages"},
            "timeout_sec": 300,
            # Wall-clock cap. A socket timeout only fires on a silent connection;
            # some models on this plan will sit on a 10 KB prompt indefinitely.
            "deadline_sec": 600,
            "max_tokens": 32000,
            # Tagging the same transcript twice should not produce a different
            # admin count. Accepted by the Go models; delete this key for any
            # model that rejects the parameter.
            "temperature": 0,
        },
        # OpenCode Zen pay-as-you-go. Same key, different endpoint and model set.
        # Asking this one for a Go model returns 401 CreditsError.
        "zen": {
            "type": "api",
            "base_url": "https://opencode.ai/zen/v1",
            "api_key_env": "OPENCODE_KEY",
            "session_header": "x-opencode-session",
            "wire": "auto",
            "wire_by_model": {"gpt-": "responses"},
            "timeout_sec": 300,
            "max_tokens": 32000,
        },
    },
    "revise": {"neighbours": 8},
    "fillers": {"remove": True, "min_gap": 0.12, "max_pause": 0.45},
    "stages": {
        "discover": {"provider": "openrouter", "model": "openai/gpt-6-luna"},
        "label": {"provider": "openrouter", "model": "openai/gpt-6-luna", "extra_body": {"reasoning": {"effort": "low"}}},
        "assemble": {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash"},
        # A different model family reads the assembled text cold.
        "check": {"provider": "openrouter", "model": "openai/gpt-6-luna", "extra_body": {"reasoning": {"effort": "low"}}},
        # Groups the leftover talk into topics: a long read, plain labels out.
        "topics": {"provider": "openrouter", "model": "openai/gpt-6-luna", "extra_body": {"reasoning": {"effort": "low"}}},
        # Notes mode. One call per clip, one more for the digest across them.
        # Prose for a human to read, so a strong general model over a cheap one.
        "notes": {"provider": "openrouter", "model": "openai/gpt-6-luna"},
        "digest": {"provider": "openrouter", "model": "openai/gpt-6-luna"},
    },
    "transcribe": {
        # Verified against the reference run. uvx keeps WhisperX out of this
        # project's environment; the version and python are pinned so a new
        # release (or a python torch has no wheels for) cannot change a run.
        "command": ["uvx", "--python", "3.12", "whisperx==3.8.6"],
        "model": "large-v3",
        "language": "en",
        "device": "cpu",
        "compute_type": "int8",
        "diarize": True,
        "min_speakers": 2,
        "max_speakers": 2,
        "hf_token_env": "HF_TOKEN",
        "timeout_sec": 14400,
    },
    "ingest": {
        "gap_split": 1.20,
        "max_dur": 25.0,
        "min_gap": 0.15,
        "pad": 0.08,
        "repair": True,
        "repair_max_words": 2,
        # Sections: a pause this long starts a new one (a record played in the
        # room and not recorded), and one longer than section_max is split at
        # its widest pause. Sections become the unedited chunk files.
        "section_gap": 20.0,
        "section_max": 360.0,
    },
    "topics": {
        # A topic with less talk than this is dropped rather than written.
        "min_sec": 20,
    },
    "label": {
        # Units per prompt window. 0 = whole file in one prompt (the default;
        # the reference index is ~29 KB, which fits comfortably).
        "window_units": 0,
        "window_overlap": 0,
    },
    "assemble": {
        "max_iterations": 2,
        # Finish the guest's thought before enforcing the nominal upper target.
        "completion_grace_sec": 20,
    },
    # `cutdown notes`: exported clips -> a readable, shareable document. A
    # separate mode, not a stage of the cutdown pipeline.
    "notes": {
        "extensions": [".wav", ".aif", ".aiff", ".flac", ".mp3", ".m4a",
                       ".mp4", ".mov"],
        # "time" puts the clips in session order using their BWF stamps, and
        # falls back to filename order when any clip is missing one.
        "order": "time",
        # Paragraphing, for reading rather than for cutting: a paragraph breaks
        # on a speaker change, on a pause once it has enough in it to stand
        # alone, and on length at a sentence end.
        "para_gap": 1.60,
        "para_max_sec": 40.0,
        "drop_fillers": False,
        # Overlaid on [transcribe]. Clips are short and often one voice, and the
        # interview defaults would split a solo answer across two speakers.
        "transcribe": {"min_speakers": 1, "max_speakers": 2},
    },
    "render": {
        "stringout_gap_ms": 500,
        "ffmpeg": "ffmpeg",
        # Filenames are what the producer actually scans in Finder, so they
        # carry the context rather than making you look it up in the manifest.
        # Tokens: seg, clip, title, artist, kind, ids, first_id, last_id,
        #         episode, guest. Empty tokens collapse rather than leaving gaps.
        "clip_template": "{seg}_{clip:02d}_{title}_{ids}",
        "stringout_template": "{seg}_{title}",
        # Output folders, numbered so Finder lists them finished-first.
        "dirs": {"final": "1-final", "clips": "2-clips", "chunks": "3-chunks"},
        # Write the recording, unedited, cut at its sections into 3-chunks/.
        "chunks": True,
    },
}

# Which config keys each stage's freshness depends on.
STAGE_CONFIG_KEYS = {
    "transcribe": ["transcribe"],
    "ingest": ["ingest"],
    "discover": ["stages.discover", "providers"],
    "label": ["label", "stages.label", "providers"],
    "assemble": ["fillers", "assemble", "stages.assemble", "providers"],
    "check": ["fillers", "stages.check", "providers"],
    "topics": ["topics", "stages.topics", "providers"],
    "render": ["fillers", "render"],
    "notes": ["notes", "stages.notes", "stages.digest", "providers"],
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
        if k == "stages" and isinstance(v, dict):
            for stage, options in v.items():
                if isinstance(options, dict) and options.get("extra_body") == {}:
                    out[k][stage]["extra_body"] = {}
    return out


class Config:
    def __init__(self, data: dict, source: pathlib.Path | None = None):
        self.data = data
        self.source = source

    @classmethod
    def load(cls, path: pathlib.Path | None = None, search_from: pathlib.Path | None = None) -> "Config":
        if path is None:
            here = (search_from or pathlib.Path.cwd()).resolve()
            for d in [here, *here.parents]:
                cand = d / CONFIG_NAME
                if cand.exists():
                    path = cand
                    break
        if path is None:
            return cls(copy.deepcopy(DEFAULTS), None)
        if not path.exists():
            raise CutdownError(f"config not found: {path}")
        try:
            over = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise CutdownError(f"{path}: {e}")
        log(f"config: {path}")
        return cls(_deep_merge(DEFAULTS, over), path)

    def overlay(self, over: dict) -> None:
        """Merge an episode's config overlay (from its context) over this one."""
        if over:
            self.data = _deep_merge(self.data, over)

    def get(self, dotted: str, default=None):
        cur = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def stage(self, stage: str) -> dict:
        s = self.get(f"stages.{stage}")
        if not s:
            raise CutdownError(f"no [stages.{stage}] routing configured")
        if "provider" not in s or "model" not in s:
            raise CutdownError(f"[stages.{stage}] needs both provider and model")
        return s

    def provider(self, name: str) -> dict:
        p = self.get(f"providers.{name}")
        if not p:
            raise CutdownError(f"unknown provider {name!r}; configured: "
                               f"{', '.join(sorted(self.get('providers', {})))}")
        return p

    def check_allowed(self, name: str) -> None:
        allowed = self.get("allowed_providers", [])
        if name not in allowed:
            raise CutdownError(
                f"provider {name!r} is not in allowed_providers ({', '.join(allowed)}). "
                f"Nothing was sent. Add it to {CONFIG_NAME} if you intend to send "
                f"talent audio transcript there."
            )

    def stage_hash(self, stage: str, prompts_dir: pathlib.Path | None = None) -> str:
        """Hash of the config slice this stage's output depends on."""
        blob = {k: self.get(k) for k in STAGE_CONFIG_KEYS.get(stage, [])}
        # Hash resolved text, including custom overrides and built-in fallbacks.
        # Prompt iteration must invalidate cached selections/checks automatically.
        from . import prompts
        templates = {p.stem: prompts.load(p.stem, prompts_dir)
                     for p in prompts.BUILTIN.glob(f"{stage}.*.md")}
        if templates:
            blob["prompts"] = templates
        return hashlib.sha256(
            json.dumps(blob, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]


def write_default_config(path: pathlib.Path) -> None:
    path.write_text(SAMPLE_TOML)


SAMPLE_TOML = """\
# cutdown configuration.
# Providers not listed here are refused before anything is sent. Unpublished
# talent audio is involved, so this list is the producer's call.
allowed_providers = ["openrouter", "zengo", "zen"]

# OpenRouter, pay-as-you-go: one /chat/completions for every model. The
# `provider` table is OpenRouter's routing object; data_collection = "deny"
# keeps the transcript off hosts that store or train on prompts.
[providers.openrouter]
type = "api"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_KEY"
wire = "chat"
headers = { "X-Title" = "cutdown" }
extra_body = { provider = { data_collection = "deny" } }

# OpenCode Go subscription. Each model is served on one wire format;
# "auto" finds it (see providers/api.py). Every request carries a session header.
[providers.zengo]
type = "api"
base_url = "https://opencode.ai/zen/go/v1"
api_key_env = "OPENCODE_KEY"
session_header = "x-opencode-session"
wire = "auto"
wire_by_model = { "gpt-" = "responses", "grok-" = "responses", "qwen" = "messages", "minimax" = "messages" }

# OpenCode Zen pay-as-you-go. Same key, different endpoint and model set.
[providers.zen]
type = "api"
base_url = "https://opencode.ai/zen/v1"
api_key_env = "OPENCODE_KEY"
session_header = "x-opencode-session"
wire = "auto"

# `cutdown models` lists what the provider offers and what each stage uses.
[stages]
# extra_body is merged into the request for that stage only. Remove it if a provider rejects it.
discover = { provider = "openrouter", model = "openai/gpt-6-luna" }
label    = { provider = "openrouter", model = "openai/gpt-6-luna", extra_body = { reasoning = { effort = "low" } } }
assemble = { provider = "openrouter", model = "deepseek/deepseek-v4.1-flash" }
# Choose a different model family for check.
check    = { provider = "openrouter", model = "openai/gpt-6-luna", extra_body = { reasoning = { effort = "low" } } }
topics   = { provider = "openrouter", model = "openai/gpt-6-luna", extra_body = { reasoning = { effort = "low" } } }
# `cutdown notes`: one call per clip, one more for the digest across them.
notes    = { provider = "openrouter", model = "openai/gpt-6-luna" }
digest   = { provider = "openrouter", model = "openai/gpt-6-luna" }

[ingest]
section_gap = 20.0      # a silence this long starts a new section (a record played in the room)
section_max = 360.0     # a longer section is split at its widest pause

[topics]
min_sec = 20            # a topic with less talk than this is dropped

[notes]
order = "time"          # "time" = session order from the BWF stamps; "name"
para_gap = 1.60         # a pause this long can start a new paragraph
para_max_sec = 40.0     # break a long answer at a sentence end past this
drop_fillers = false    # true removes standalone um/uh from the transcript

# Overlaid on [transcribe]. Clips are short and often one voice.
[notes.transcribe]
min_speakers = 1
max_speakers = 2

[label]
window_units = 0     # 0 = whole index in one prompt

[revise]
neighbours = 8   # how far from the current edit the reviser may reach, in units

[fillers]
remove = true        # code removes isolated um/uh where a measured pause allows
min_gap = 0.12       # seconds of pause needed on both sides of an internal um
max_pause = 0.45     # the pause left where an um was removed is capped at this

[assemble]
max_iterations = 2
completion_grace_sec = 20  # finish the current guest thought; 0 = strict upper target

[transcribe]
model = "large-v3"
language = "en"
device = "cpu"
compute_type = "int8"
diarize = true
min_speakers = 2
max_speakers = 2
hf_token_env = "HF_TOKEN"

[render]
stringout_gap_ms = 500
ffmpeg = "ffmpeg"
# Tokens: seg, clip, title, artist, kind, ids, first_id, last_id, episode, guest.
# e.g. "{seg}_{clip:02d}_{artist}_{title}_{ids}" to include the artist too.
clip_template = "{seg}_{clip:02d}_{title}_{ids}"
stringout_template = "{seg}_{title}"
chunks = true           # write the unedited recording, cut at its sections
dirs = { final = "1-final", clips = "2-clips", chunks = "3-chunks" }
"""
