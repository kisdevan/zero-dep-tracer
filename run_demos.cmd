@echo off
REM Runs the three offline demos in sequence (Mock LLM, no API key, no internet).
REM Keep start_dashboard.cmd open in another window to watch traces appear live.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo === 1/3 Pure Python loop - normal (resets traces.jsonl) ===
python demo_loop.py --reset
echo.
echo === 2/3 Pure Python loop - false green ===
python demo_loop.py --false-green
echo.
echo === 3/3 LangGraph - normal ===
python demo_langgraph.py
echo.
pause
