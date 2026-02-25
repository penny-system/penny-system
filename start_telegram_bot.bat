@echo off
REM === Start Telegram bot in its own window ===

cd /d C:\Users\kieyf\penny-system

REM Activate venv
call venv\Scripts\activate

REM Go to project folder
cd Trader_bot

REM Start bot (this window stays open)
python telegram_bot.py

pause