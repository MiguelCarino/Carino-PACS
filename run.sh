#!/usr/bin/env bash
# Wrapper so you don't have to remember the venv path. Passes all args through.
#   ./run.sh serve            ./run.sh receive
#   ./run.sh send             ./run.sh echo --name "Example PACS"
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -x "./.venv/bin/python" ]; then
  echo "No virtualenv found — run ./setup.sh first." >&2
  exit 1
fi
exec ./.venv/bin/python -m pacs "$@"
