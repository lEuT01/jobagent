#!/bin/bash
# macOS / Linux: double-click (or run) to start the jobagent web app. Pulls the latest version first.
cd "$(dirname "$0")"
if [ -d .git ] && command -v git >/dev/null; then
  echo "Checking for updates..."
  git pull --ff-only -q && echo "Up to date." || echo "Update failed (offline?). Starting the current version."
fi
# pick the newest python3 available (3.10+ needed)
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10 or newer is needed. Install it with:  brew install python   (or from python.org), then run this again."
  read -p "Press enter"; exit 1
fi
if [ ! -d .venv ]; then
  echo "First run: creating virtual environment with $PY and installing..."
  "$PY" -m venv .venv && . .venv/bin/activate && python -m pip install -q --upgrade pip && pip install -q -e . \
    || { echo "Install failed."; read -p "Press enter"; exit 1; }
else
  . .venv/bin/activate
  pip install -q -e . 2>/dev/null   # picks up any new dependencies after an update
fi
jobagent serve
