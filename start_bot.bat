@echo off
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo Virtual environment not found! Run install.bat first.
    pause
    exit /b 1
)

if not exist ".env" (
    echo ERROR: .env file not found!
    echo Copy .env.example to .env and fill it.
    echo.
    copy .env.example .env
    echo .env created from template. Open it and set TELEGRAM_BOT_TOKEN.
    echo.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
python bot.py
pause
