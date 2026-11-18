"""CLI transport: subscription-backed coding agents invoked headlessly.

The caller supplies a trusted command and chooses its permissions.
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import time

from ..util import CutdownError, log
from . import Provider


class CliProvider(Provider):
    kind = "cli"

    def preflight(self) -> None:
        cmd = self.cfg.get("command")
        if not cmd:
            raise CutdownError(f"provider {self.name!r} has no command configured")
        if not shutil.which(cmd) and not pathlib.Path(cmd).exists():
            raise CutdownError(
                f"provider {self.name!r} needs {cmd!r} on PATH. Install it, or point "
                f"providers.{self.name}.command at the binary."
            )

    def describe(self) -> str:
        return f"{self.name} (cli {self.cfg.get('command')})"

    def send(self, *, system: str, user: str, model: str,
             artifacts: pathlib.Path, extra_body=None) -> tuple[str, dict]:
        artifacts.mkdir(parents=True, exist_ok=True)
        outfile = artifacts / "last_message.txt"
        cmd = [shutil.which(self.cfg["command"]) or self.cfg["command"]]
        for a in self.cfg.get("args", []):
            cmd.append(a.replace("{model}", model).replace("{outfile}", str(outfile)))

        stdin = user
        sys_arg = self.cfg.get("system_arg")
        if sys_arg and system:
            cmd += [sys_arg, system]
        elif system:
            # No flag for it: fold the system prompt into the message.
            stdin = f"{system}\n\n---\n\n{user}"

        t0 = time.time()
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                               timeout=self.cfg.get("timeout_sec", 900),
                               cwd=str(artifacts))
        except subprocess.TimeoutExpired:
            raise CutdownError(
                f"{self.name} timed out after {self.cfg.get('timeout_sec', 900)}s. "
                f"Raise providers.{self.name}.timeout_sec or use a smaller window."
            )
        except OSError as e:
            raise CutdownError(f"cannot run {self.name}: {e}")
        elapsed = round(time.time() - t0, 2)
        (artifacts / "stdout.txt").write_text(r.stdout or "")
        (artifacts / "stderr.txt").write_text(r.stderr or "")

        if r.returncode != 0:
            raise CutdownError(
                f"{self.name} exited {r.returncode}.\n"
                f"stderr: {(r.stderr or '').strip()[:600]}\n"
                f"Full output in {artifacts}"
            )

        meta = {"cmd": cmd, "returncode": r.returncode, "elapsed_sec": elapsed,
                "model": model, "cwd": str(artifacts)}

        if self.cfg.get("output_file"):
            if not outfile.exists():
                raise CutdownError(
                    f"{self.name} wrote no output file at {outfile}. "
                    f"stdout: {(r.stdout or '').strip()[:400]}"
                )
            return outfile.read_text(), meta

        text = r.stdout or ""
        path = self.cfg.get("response_path")
        if path:
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                raise CutdownError(
                    f"{self.name} did not emit the JSON envelope its response_path "
                    f"({path}) expects. Got: {text.strip()[:400]}"
                )
            cur = obj
            for part in path.split("."):
                if not isinstance(cur, dict) or part not in cur:
                    raise CutdownError(
                        f"{self.name}: response_path {path!r} missing from the "
                        f"envelope (keys: {list(obj) if isinstance(obj, dict) else type(obj)})"
                    )
                cur = cur[part]
            for k in ("total_cost_usd", "duration_ms", "num_turns", "session_id", "is_error"):
                if isinstance(obj, dict) and k in obj:
                    meta[k] = obj[k]
            if isinstance(obj, dict) and obj.get("is_error"):
                raise CutdownError(f"{self.name} reported an error: {str(cur)[:400]}")
            return cur if isinstance(cur, str) else json.dumps(cur), meta

        return text, meta
