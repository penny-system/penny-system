@echo off
REM === Run main daily scan (safe to run while bot is running) ===

cd /d C:\Users\kieyf\penny-system

REM Activate venv
call venv\Scripts\activate

REM Go to project folder
cd Trader_bot

python main_daily_run.py

pause