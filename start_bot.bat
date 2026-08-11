@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
set "CONFIG_FILE=%~dp0config.yaml"
set "ENV_FILE=%~dp0.env"

if not exist "%PYTHON_EXE%" (
    echo [ERROR] Python environment was not found:
    echo "%PYTHON_EXE%"
    echo Follow the Python installation section in README.md first.
    echo.
    echo Press any key to close this window.
    pause >nul
    exit /b 1
)

if not exist "%CONFIG_FILE%" (
    echo [ERROR] config.yaml was not found.
    echo Run: Copy-Item config.example.yaml config.yaml
    echo Then set the real path to terminal64.exe.
    echo.
    echo Press any key to close this window.
    pause >nul
    exit /b 1
)

if not exist "%ENV_FILE%" (
    echo [ERROR] .env with Telegram settings was not found.
    echo Run: Copy-Item .env.example .env
    echo Then set TELEGRAM_API_ID, TELEGRAM_API_HASH and TELEGRAM_PHONE.
    echo.
    echo Press any key to close this window.
    pause >nul
    exit /b 1
)

echo Starting Aurum Research Club - MT5 bot...
echo Press Ctrl+C twice quickly to stop.
echo.

:restart
"%PYTHON_EXE%" -m aurum_bot.main --config "%CONFIG_FILE%"
set "BOT_EXIT_CODE=%ERRORLEVEL%"

if "%BOT_EXIT_CODE%"=="0" (
    echo.
    echo [INFO] Bot exited cleanly (code 0). Not restarting.
    goto :end
)

echo.
echo [WARN] Bot exited with code %BOT_EXIT_CODE%.
echo Restarting in 10 seconds... Press Ctrl+C to abort.
timeout /t 10 /nobreak >nul
goto :restart

:end

endlocal & exit /b %BOT_EXIT_CODE%
