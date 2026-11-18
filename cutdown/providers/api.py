"""API transport: OpenRouter and OpenCode Zen / Go over HTTPS, in three wire formats.

OpenRouter (https://openrouter.ai/api/v1) serves every model on
/chat/completions, so its provider is configured with `wire = "chat"`. Its
routing preferences (`provider: {data_collection: "deny"}` and the like) go in
the provider's `extra_body`, which is merged into every request body.

Two OpenCode base URLs exist and they are not interchangeable:

  https://opencode.ai/zen/v1        pay-as-you-go credits
  https://opencode.ai/zen/go/v1     the OpenCode Go subscription

The same API key is used for both; asking the credits endpoint for a Go model
returns 401 CreditsError, which reads like an auth failure but is not one.

Verified 2026-09-24 against the Go endpoint:

  - Every request must carry an `x-opencode-session` header or it is refused
    with 400 MissingSessionID. One stable id per provider instance, so retries
    of the same prompt land on the same route and hit the prompt cache.
  - Each model is served on one wire format only, and asking the wrong one
    returns 5xx "Endpoint is unavailable", which reads like an outage:
        /responses          gpt-*-luna, grok-*
        /messages           (Anthropic shape) qwen*, minimax*; also works for
                            deepseek, kimi
        /chat/completions   deepseek, glm, kimi, mimo, hy, minimax, qwen
    `wire = "auto"` starts from `wire_by_model` and tries the other two on that
    error, remembering what worked for the rest of the process.

Zen sits behind a WAF that returns 403 to requests with no User-Agent, so one is
always sent.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import pathlib
import time
import uuid
import urllib.error
import urllib.parse
import urllib.request

from ..util import CutdownError, log
from . import Provider

USER_AGENT = "cutdown"
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}


WIRES = ("chat", "messages", "responses")
WIRE_PATHS = {"chat": "/chat/completions", "messages": "/messages",
              "responses": "/responses"}
# What each (base_url, model) turned out to speak, for the life of the process.
_LEARNED: dict[tuple[str, str], str] = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib's default handler can forward authentication headers.
        return None


def open_api(request: urllib.request.Request, *, timeout: float):
    url = urllib.parse.urlsplit(request.full_url)
    local = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not url.hostname or url.username is not None or url.password is not None
            or (url.scheme != "https" and not (url.scheme == "http" and local))):
        raise CutdownError("API endpoints require HTTPS or a loopback HTTP address")
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _wrong_wire(status: int, payload: str) -> bool:
    """The gateway's answer to a model asked on a format it does not serve."""
    return status in (400, 404, 500, 502, 503) and (
        "Endpoint is unavailable" in payload or "not supported" in payload.lower())


def _retry_wait(status: int, retry_after: str | None, attempt: int) -> float:
    """Seconds to wait before retrying a transient failure.

    A 429 is a per-minute rate limit (OpenRouter holds new accounts to 20
    requests a minute per model), so the short backoff used for 5xx would spend
    every retry inside the same window. Honour Retry-After when sent.
    """
    try:
        if retry_after is not None:
            return min(max(float(retry_after), 1.0), 60.0)
    except ValueError:
        pass
    if status == 429:
        return min(15 * attempt, 60)
    return min(2 ** attempt, 20)


def _merge(base: dict, over: dict) -> dict:
    """Recursive dict merge; `over` wins. Used for a provider's extra_body."""
    out = dict(base)
    for k, v in over.items():
        out[k] = (_merge(out[k], v) if isinstance(v, dict)
                  and isinstance(out.get(k), dict) else v)
    return out


class ApiProvider(Provider):
    kind = "api"

    def __init__(self, name: str, cfg: dict):
        super().__init__(name, cfg)
        self.session = f"cutdown-{uuid.uuid4().hex[:16]}"

    def preflight(self) -> None:
        env = self.cfg.get("api_key_env", "OPENCODE_KEY")
        if not self._key():
            raise CutdownError(
                f"provider {self.name!r} needs an API key in ${env}. "
                f"Put it in .env next to cutdown.toml, or export it."
            )

    def _key(self) -> str:
        env = self.cfg.get("api_key_env", "OPENCODE_KEY")
        key = os.environ.get(env, "").strip()
        if key:
            return key
        # Convenience: read a sibling .env rather than making the producer export.
        for d in [pathlib.Path.cwd(), *pathlib.Path.cwd().parents]:
            f = d / ".env"
            if not f.exists():
                continue
            for line in f.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{env}="):
                    return line.split("=", 1)[1].strip().strip('"\'')
            break
        return ""

    def describe(self) -> str:
        return f"{self.name} (api {self.cfg.get('base_url', '')})"

    # ------------------------------------------------------------ wire choice

    def wires_for(self, model: str) -> list[str]:
        """Wire formats to try for this model, most likely first."""
        base = self.cfg.get("base_url", "")
        fixed = self.cfg.get("wire", "chat")
        if fixed != "auto":
            if fixed not in WIRES:
                raise CutdownError(f"providers.{self.name}.wire must be one of "
                                   f"auto, {', '.join(WIRES)}; got {fixed!r}")
            return [fixed]
        first = _LEARNED.get((base, model))
        if first is None:
            for prefix, wire in (self.cfg.get("wire_by_model") or {}).items():
                if model.startswith(prefix) and wire in WIRES:
                    first = wire
                    break
        first = first or "chat"
        return [first] + [w for w in WIRES if w != first]

    def _body(self, wire: str, system: str, user: str, model: str, extra_body=None) -> dict:
        max_tokens = self.cfg.get("max_tokens", 32000)
        if wire == "responses":
            # Reasoning models on this format reject a fixed temperature.
            return _merge({"model": model, "instructions": system,
                           "input": [{"role": "user", "content": user}],
                           "max_output_tokens": max_tokens},
                          _merge(self.cfg.get("extra_body") or {}, extra_body or {}))
        if wire == "messages":
            body = {"model": model, "system": system, "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": user}]}
        else:
            body = {"model": model, "max_tokens": max_tokens,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}]}
        if "temperature" in self.cfg:
            body["temperature"] = self.cfg["temperature"]
        return _merge(body, _merge(self.cfg.get("extra_body") or {}, extra_body or {}))

    def _headers(self, wire: str | None = None) -> dict:
        key = self._key()
        h = {"Authorization": f"Bearer {key}",
             "content-type": "application/json", "user-agent": USER_AGENT}
        if wire == "messages":
            # Anthropic shape only. OpenRouter switches its own replies (even
            # /models) to Anthropic's format when it sees these headers.
            h.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        h.update(self.cfg.get("headers") or {})
        header = self.cfg.get("session_header")
        if header:
            h[header] = self.session
        return h

    # ------------------------------------------------------------ send

    def send(self, *, system: str, user: str, model: str,
             artifacts: pathlib.Path, extra_body=None) -> tuple[str, dict]:
        base = self.cfg.get("base_url", "").rstrip("/")
        wires = self.wires_for(model)
        last = ""
        for n, wire in enumerate(wires):
            path = (self.cfg.get("path") if wire == "chat" and self.cfg.get("path")
                    else WIRE_PATHS[wire])
            status, payload, meta = self._post(base + path, wire, system, user, model, extra_body)
            if status == 200:
                _LEARNED[(self.cfg.get("base_url", ""), model)] = wire
                return self._content(wire, payload, meta)
            last = f"HTTP {status}: {payload[:400]}"
            if n + 1 < len(wires) and _wrong_wire(status, payload):
                log(f"  {self.name}/{model}: not served on /{wire}, trying "
                    f"/{wires[n + 1]}")
                continue
            break

        hint = ""
        if "CreditsError" in last:
            hint = ("\nThis account has no pay-as-you-go balance. If you are on the "
                    "OpenCode Go subscription, point base_url at "
                    "https://opencode.ai/zen/go/v1 and use a Go model.")
        elif "MissingSessionID" in last:
            hint = (f"\nThe gateway wants a session header; set "
                    f"providers.{self.name}.session_header = \"x-opencode-session\".")
        elif last.startswith("HTTP 403"):
            hint = "\nZen returns 403 to requests without a User-Agent; check any proxy in front of it."
        elif "data policy" in last or "No endpoints found" in last:
            hint = (f"\nNo host for {model} meets this provider's routing rules "
                    f"(providers.{self.name}.extra_body.provider). Pick another "
                    f"model, or relax the rule if the producer agrees.")
        elif last.startswith("HTTP 402"):
            hint = f"\nThe {self.name} account is out of credit."
        elif "Endpoint is unavailable" in last:
            hint = (f"\n{model} is not served on any wire format this provider "
                    f"tried ({', '.join(wires)}). Check the model id against "
                    f"`cutdown models`.")
        if last.startswith("HTTP 400") and "reasoning" in payload.lower():
            hint += "\nThis model or provider rejected the stage's extra_body. Remove extra_body from that stage's entry under [stages]."
        raise CutdownError(f"{self.name}/{model} request failed. {last}{hint}")

    def _post(self, url: str, wire: str, system: str, user: str,
              model: str, extra_body=None) -> tuple[int, str, dict]:
        """One wire format, with transport retries on transient statuses."""
        body = json.dumps(self._body(wire, system, user, model, extra_body)).encode()
        timeout = self.cfg.get("timeout_sec", 300)
        # A socket timeout only fires on a silent connection. A provider that
        # trickles bytes, or a reasoning model that thinks for an hour, would
        # otherwise block the whole pipeline, so cap the wall clock too.
        deadline = self.cfg.get("deadline_sec", timeout)
        tries = int(self.cfg.get("transport_retries", 3))
        status, payload = 0, ""
        for attempt in range(1, tries + 1):
            retry_after = None
            req = urllib.request.Request(url, data=body, headers=self._headers(wire),
                                         method="POST")
            t0 = time.time()
            try:
                status, payload = self._fetch(req, timeout, deadline)
            except TimeoutError:
                raise CutdownError(
                    f"{self.name}/{model} returned nothing after {deadline}s. "
                    f"Either the model is too slow for this prompt "
                    f"({len(user)} chars) or the request hung. Raise "
                    f"providers.{self.name}.deadline_sec, or route this stage at a "
                    f"faster model."
                )
            except urllib.error.HTTPError as e:
                status, payload = e.code, e.read().decode(errors="replace")
                retry_after = e.headers.get("Retry-After") if e.headers else None
            except urllib.error.URLError as e:
                status, payload = 0, str(e.reason)
            except OSError as e:
                status, payload = 0, str(e)
            meta = {"status": status, "elapsed_sec": round(time.time() - t0, 2),
                    "url": url, "wire": wire, "model": model}
            if status == 200 or _wrong_wire(status, payload):
                return status, payload, meta
            if status in RETRY_STATUS and attempt < tries:
                wait = _retry_wait(status, retry_after, attempt)
                log(f"  {self.name}: HTTP {status} {payload[:100]} - retrying in "
                    f"{wait}s ({attempt}/{tries - 1})")
                time.sleep(wait)
                continue
            break
        return status, payload, {"status": status, "url": url, "wire": wire}

    @staticmethod
    def _fetch(req, timeout: float, deadline: float) -> tuple[int, str]:
        """urlopen with a hard wall-clock deadline on top of the socket timeout."""
        def do():
            with open_api(req, timeout=timeout) as r:
                return r.status, r.read().decode()

        # Not a `with` block: its exit waits for the worker, which would hold the
        # caller for as long as the hung request lasts.
        ex = cf.ThreadPoolExecutor(1)
        fut = ex.submit(do)
        try:
            return fut.result(timeout=deadline)
        except cf.TimeoutError:
            fut.cancel()
            raise TimeoutError
        finally:
            ex.shutdown(wait=False)

    @staticmethod
    def _content(wire: str, payload: str, meta: dict) -> tuple[str, dict]:
        try:
            d = json.loads(payload)
        except json.JSONDecodeError:
            raise CutdownError(f"provider returned non-JSON:\n{payload[:400]}")
        if d.get("type") == "error" or (wire == "responses" and d.get("error")):
            raise CutdownError(f"provider error: {json.dumps(d.get('error'))[:400]}")
        try:
            if wire == "responses":
                content = d.get("output_text") or "".join(
                    c.get("text", "") for item in d.get("output", [])
                    if item.get("type") == "message"
                    for c in item.get("content", []) if c.get("type") == "output_text")
                finish = d.get("status")
                if d.get("incomplete_details"):
                    finish = json.dumps(d["incomplete_details"])
            elif wire == "messages":
                content = "".join(c.get("text", "") for c in d["content"]
                                  if c.get("type") == "text")
                finish = d.get("stop_reason")
            else:
                choice = d["choices"][0]
                content = choice["message"].get("content") or ""
                finish = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise CutdownError(f"unexpected {wire} response shape:\n{payload[:400]}")
        meta = {**meta, "usage": d.get("usage"), "cost": d.get("cost"),
                "finish_reason": finish}
        if not content.strip():
            raise CutdownError(
                f"model returned empty content (finish_reason={finish!r}). Some "
                f"reasoning models spend the whole budget thinking; raise "
                f"max_tokens for this provider or pick another model."
            )
        return content, meta
