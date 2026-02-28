@echo off
REM ─────────────────────────────────────────────────────────────────────────────
REM  run_weekly_refresh.bat
REM  Weekly dynamic universe refresh for Penny System.
REM
REM  To add to Windows Task Scheduler:
REM    - Action: Start a program
REM    - Program: C:\Users\kieyf\penny-system\Trader_bot\run_weekly_refresh.bat
REM    - Trigger: Weekly, Sunday at 8:00 PM
REM    - "Start in": C:\Users\kieyf\penny-system\Trader_bot
REM ─────────────────────────────────────────────────────────────────────────────

cd /d "%~dp0"

echo [%date% %time%] Activating virtual environment...
call ..\venv\Scripts\activate.bat

echo [%date% %time%] Starting weekly universe refresh...
python src_universe_refresh.py

echo [%date% %time%] Refresh complete.
pause
