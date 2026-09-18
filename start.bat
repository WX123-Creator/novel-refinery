@echo off
title Novel Refinery - Launcher
color 0B

echo ============================================
echo     Novel Refinery - One-Click Launcher
echo     Local Run / Privacy Safe / Smart Extract
echo ============================================
echo.

:: -------- Check API Key --------
echo [1/4] Checking model config...
cd /d "%~dp0"

python -c "from config import ZHIPU_API_KEY; exit(0 if ZHIPU_API_KEY and not ZHIPU_API_KEY.startswith('YOUR_') else 1)" 2>nul
if %errorlevel% neq 0 (
    echo [!] ZHIPU_API_KEY is not configured in config.py!
    echo [!] Please set a valid API key before launching.
    echo.
    echo   Open config.py, find:
    echo     ZHIPU_API_KEY = "YOUR_ZHIPU_API_KEY_HERE"
    echo   Replace with your real key from https://open.bigmodel.cn/
    echo.
    mshta "javascript:var s=new ActiveXObject('WScript.Shell');s.Popup('Please configure ZHIPU_API_KEY in config.py first!\n\nGet key at: https://open.bigmodel.cn/',0,'Novel Refinery - Alert',48);close()" 2>nul
    echo Press any key to exit...
    pause >nul
    exit /b 1
)
echo [OK] Model config check passed
echo.

:: -------- Create/Activate venv --------
echo [2/4] Setting up virtual environment...
if not exist "venv\" (
    echo   - Creating virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [!] Failed to create venv. Make sure Python 3.8+ is installed.
        pause
        exit /b 1
    )
    echo   - Virtual environment created
)
rem venv activation skipped (using venv python directly)
if %errorlevel% neq 0 (
    echo [!] Failed to activate virtual environment
    pause
    exit /b 1
)
echo [OK] Virtual environment ready
echo.

:: -------- Install dependencies --------
echo [3/4] Installing dependencies...
venv\Scripts\pip.exe install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo [!] Dependency install failed. Check your network connection.
    pause
    exit /b 1
)
echo [OK] Dependencies installed
echo.

:: -------- Find available port --------
echo [4/4] Checking available port...
setlocal enabledelayedexpansion

set FOUND_PORT=
for %%p in (5001 5002 5003 5004 5005 5006 5007 5008 5009 5010) do (
    netstat -ano | findstr ":%%p " >nul 2>&1
    if !errorlevel! neq 0 (
        set "FOUND_PORT=%%p"
        goto :port_found
    )
)

echo [!] All ports 5001-5010 are busy. Please free a port manually.
pause
exit /b 1

:port_found
echo [OK] Using port !FOUND_PORT!
echo.

:: -------- Launch app in background --------
echo ============================================
echo   Starting Novel Refinery on port !FOUND_PORT!...
echo   Browser will open automatically
echo ============================================
echo.

:: Set environment variable for the app
set DEPLOY_RUN_PORT=!FOUND_PORT!
echo !FOUND_PORT!> app_port.txt

:: Launch in background
REM Create VBS launcher (silent, no black window)
set VBS_FILE=%TEMP%\novel_refinery_launcher.vbs
echo Dim shell, cmd > "%VBS_FILE%"
echo Set shell = CreateObject("WScript.Shell") >> "%VBS_FILE%"
echo cmd = "venv\Scripts\python.exe app.py > app.log 2>&1" >> "%VBS_FILE%"
echo shell.Run cmd, 0, False >> "%VBS_FILE%"
echo Set shell = Nothing >> "%VBS_FILE%"
cscript //nologo "%VBS_FILE%"
if exist "%VBS_FILE%" del /q "%VBS_FILE%"

:: Wait for server to start
echo Waiting for server...
timeout /t 5 /nobreak >nul

:wait_loop
netstat -ano | findstr ":%FOUND_PORT% " >nul 2>&1
if !errorlevel! neq 0 (
    echo   - Still waiting...
    timeout /t 3 /nobreak >nul
    goto :wait_loop
)

:: Open browser
start http://127.0.0.1:!FOUND_PORT!

echo.
echo ============================================
echo   App is running at http://127.0.0.1:!FOUND_PORT!
echo   To stop: double-click stop.bat
echo ============================================
echo.

endlocal

:: Keep window alive briefly, then auto-close
timeout /t 5 /nobreak >nul
exit