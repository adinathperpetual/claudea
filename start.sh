#!/usr/bin/env bash
cd "$(dirname "$0")"
[ -x .venv/bin/python ] && [ -f .env ] || { echo "Run ./setup.sh first."; exit 1; }
exec .venv/bin/python -m finesse_sync serve --open
