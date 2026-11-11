#!/bin/bash
# macOS / Linux: double-click (or run) to start the jobagent web app. Pulls the latest version first.
cd "$(dirname "$0")"
if [ -d .git ] && command -v git >/dev/null; then
  echo "Checking for updates..."
  git pull --ff-only -q && echo "Up to date." || echo "Update failed (offline?). Starting the current version."
fi
if [ ! -d .venv ]; then
  echo "First run: creating virtual environment and installing..."
  python3 -m venv .venv && . .venv/bin/activate && pip install -q -e . || { echo "Install failed. Is Python 3.10+ installed?"; read -p "Press enter"; exit 1; }
else
  . .venv/bin/activate
  pip install -q -e . 2>/dev/null   # picks up any new dependencies after an update
fi
jobagent serve
