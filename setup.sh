#!/usr/bin/env bash
# One-time setup for macOS / Linux (Debian, Fedora): create a venv + deps.
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 not found. Install it first:" >&2
  echo "  Debian/Ubuntu : sudo apt install python3 python3-venv python3-pip" >&2
  echo "  Fedora        : sudo dnf install python3 python3-pip" >&2
  echo "  macOS         : brew install python" >&2
  exit 1
fi

"$PY" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

echo
echo "Setup complete. Next:"
echo "  ./run.sh init      # create config.json + folders"
echo "  ./run.sh serve     # open the dashboard (http://127.0.0.1:8042)"
