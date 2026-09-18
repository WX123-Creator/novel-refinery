@echo off
title Novel Refinery - Stopper
color 0C

echo ============================================
echo     Stopping Novel Refinery...
echo ============================================
echo.

cd /d "%~dp0"

:: Default port
set PORT=5001 & if exist app_port.txt for /f "delims=" %%a in (app_port.txt) do set PORT=%%a

:: Try to find the actual port from app.log
if exist app.log (
    for /f "tokens=*" %%a in ('findstr /i "port" app.log 2^>nul') do (
        echo %%a
    )
)

:: Find python process listening on our port
echo Looking for process on port %PORT%...
set PID=
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":%PORT% " ^| findstr "LISTENING" 2^>nul') do (
    set PID=%%a
)

if defined PID (
    echo Found process PID: %PID%
    taskkill /f /pid %PID% >nul 2>&1
    if %errorlevel% equ 0 (
        echo [OK] Novel Refinery has been stopped.
    ) else (
        echo [!] Failed to stop the process. Try closing it manually.
    )
) else (
    echo [!] No running Novel Refinery process found on port %PORT%.
)

echo.
echo ============================================
echo   Press any key to close this window
echo ============================================
timeout /t 3 /nobreak >nul & exit /b 0
