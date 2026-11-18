#!/usr/bin/env bash
set -euo pipefail

root="$(cd -P "$(dirname "$0")" && pwd)"
if ! command -v uv >/dev/null 2>&1; then
    printf 'Install uv first (macOS with Homebrew: brew install uv).\n' >&2
    exit 1
fi

uv sync --project "$root" --python 3.12 --locked --no-dev --no-editable
"$root/.venv/bin/cutdown" --help >/dev/null
printf '\nInstalled. From the checkout, run ./bin/cutdown --help.\n'
