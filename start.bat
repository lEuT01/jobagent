@echo off
cd /d "%~dp0"
if exist .git ( echo Checking for updates... & git pull --ff-only -q || echo Update failed, starting current version. )
if not exist .venv (
  echo First run: creating virtual environment and installing...
  python -m venv .venv && .venv\Scripts\pip install -q -e . || (echo Install failed. Is Python 3.10+ installed? & pause & exit /b 1)
) else ( .venv\Scripts\pip install -q -e . >nul 2>&1 )
.venv\Scripts\jobagent serve
