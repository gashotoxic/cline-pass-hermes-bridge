@echo off
REM ClinePass Bridge Watchdog
REM Checks if bridge is healthy; restarts it if not.
REM Called by Windows Task Scheduler every 5 minutes.

set BRIDGE_DIR=C:\Users\Gasho\cline-pass-hermes-bridge
set BRIDGE_SCRIPT=%BRIDGE_DIR%\cline_pass_bridge.py
set BRIDGE_PORT=8317

REM Quick health check — exit 0 if healthy
curl -s -m 3 http://127.0.0.1:%BRIDGE_PORT%/health >nul 2>&1
if %errorlevel% == 0 exit /b 0

REM Bridge is down — try to restart
echo [%date% %time%] Bridge down, restarting... >> %BRIDGE_DIR%\watchdog.log

REM Kill any python process listening on our port
for /f "tokens=5" %%a in ('netstat -ano ^| findstr :%BRIDGE_PORT% ^| findstr LISTENING') do (
    taskkill /F /PID %%a >nul 2>&1
)

REM Small delay to let port release
timeout /t 2 /nobreak >nul

REM Start bridge
start "" /min pythonw "%BRIDGE_SCRIPT%"

REM Wait 3s and verify
timeout /t 3 /nobreak >nul
curl -s -m 3 http://127.0.0.1:%BRIDGE_PORT%/health >nul 2>&1
if %errorlevel% == 0 (
    echo [%date% %time%] Bridge restarted OK >> %BRIDGE_DIR%\watchdog.log
) else (
    echo [%date% %time%] Bridge restart FAILED >> %BRIDGE_DIR%\watchdog.log
)
