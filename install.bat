@echo off
echo ========================================
echo   GIBDD Telegram Bot - Install
echo ========================================
echo.

cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo [1/3] Creating virtual environment...
    python -m venv venv
    echo OK
    echo.
)

echo [2/3] Activating virtual environment...
call venv\Scripts\activate.bat
echo OK
echo.

echo [3/3] Installing dependencies...
pip install python-telegram-bot==21.7 httpx==0.27.0 openpyxl==3.1.5 python-dotenv==1.0.1
echo OK
echo.

echo ========================================
echo   Installation complete!
echo ========================================
echo.
echo Next steps:
echo   1. Copy .env.example to .env
echo   2. Fill .env with your TELEGRAM_BOT_TOKEN
echo   3. Run start_bot.bat
echo.
pause
