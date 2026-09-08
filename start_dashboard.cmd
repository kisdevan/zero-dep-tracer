@echo off
REM zero-dep tracer dashboard. Double-click: starts the local server and opens your browser.
REM Close this window (or press Ctrl+C) to stop. Works fully offline - no internet needed.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
python dashboard.py traces.jsonl 8790 --open
pause
