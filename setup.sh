#!/usr/bin/env bash
# Mac / Linux first-time setup
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] && "$PY" -c 'import sys; sys.exit(sys.version_info < (3,10))' || { echo "Install Python 3.10+ from https://www.python.org/downloads/"; exit 1; }
[ -x .venv/bin/python ] || "$PY" -m venv .venv
.venv/bin/python -m pip install -q --upgrade pip
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m playwright install chromium
.venv/bin/python -m finesse_sync setup
echo "Setup finished. Run ./start.sh to open the tool."
