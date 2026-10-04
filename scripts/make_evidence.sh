#!/usr/bin/env sh
set -eu
cd "$(dirname "$0")/.."
if [ -x .venv/Scripts/python.exe ]; then
  PY=.venv/Scripts/python.exe
elif [ -x .venv/bin/python ]; then
  PY=.venv/bin/python
else
  PY=python3
fi
exec "$PY" scripts/make_evidence.py "$@"
