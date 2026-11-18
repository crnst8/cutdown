#!/bin/bash
# Prompt for a recording and open Studio.
ROOT="$(cd -P "$(dirname "$0")" && pwd)"

# Without -r, `read` removes the backslashes inserted by Bash's tab completion
# for spaces and shell-special characters in file paths.
read -e -p "Audio file: " audio_path
if [[ -z "$audio_path" ]]; then
  echo "No audio file selected." >&2
  exit 1
fi
if [[ ! -f "$audio_path" ]]; then
  echo "Audio file not found: $audio_path" >&2
  exit 1
fi

# Optional: a saved preset, e.g. ./fp.sh radio
preset=()
[[ -n "$1" ]] && preset=(--preset "$1")

# exec so the studio owns this terminal outright: no wrapper shell left behind
# holding on to children when it exits.
exec "$ROOT/bin/cutdown" studio --audio "$audio_path" "${preset[@]}"
