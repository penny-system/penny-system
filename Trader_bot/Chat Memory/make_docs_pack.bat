@echo off
setlocal

REM ============================================================
REM Penny System - Generate Docs Pack ZIP
REM Output:
REM C:\Users\kieyf\penny-system\Trader_bot\Chat Memory\docs_pack.zip
REM ============================================================

REM Set absolute paths (no guessing)
set "ROOT=C:\Users\kieyf\penny-system"
set "BOT=%ROOT%\Trader_bot"
set "VENV=%ROOT%\venv"
set "CHATMEM=%BOT%\Chat Memory"
set "ZIPPATH=%CHATMEM%\docs_pack.zip"

REM Go to Trader_bot
cd /d "%BOT%"

REM Ensure Chat Memory folder exists
if not exist "%CHATMEM%" (
  mkdir "%CHATMEM%"
)

REM Activate virtual environment (absolute path)
call "%VENV%\Scripts\activate.bat"
if errorlevel 1 (
  echo [ERROR] Could not activate venv at %VENV%\Scripts\activate.bat
  pause
  exit /b 1
)

REM Run snapshot generator if it exists
if exist "tools\make_snapshot.py" (
  python "tools\make_snapshot.py"
)

REM Validate docs folder exists
if not exist "docs" (
  echo [ERROR] docs folder not found at %BOT%\docs
  pause
  exit /b 1
)

REM Delete old ZIP if exists
if exist "%ZIPPATH%" del /q "%ZIPPATH%"

REM Create ZIP
powershell -NoProfile -Command "Compress-Archive -Path '%BOT%\docs\*' -DestinationPath '%ZIPPATH%' -Force"
if errorlevel 1 (
  echo [ERROR] Failed to create ZIP.
  pause
  exit /b 1
)

echo.
echo ✅ Docs pack created successfully:
echo %ZIPPATH%
echo.
pause
endlocal