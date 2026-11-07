@echo off
rem DLsite compare tool standalone launcher. Keep this file ASCII-only.
chcp 65001 >nul
cd /d "%~dp0"

rem Already running? Just open the page.
netstat -ano | findstr ":7100" | findstr LISTENING >nul
if %errorlevel%==0 (
    echo Server already running, opening page...
    start "" http://localhost:7100/
    exit
)

where py >nul 2>nul
if %errorlevel%==0 (
    start "DLsite-compare server" py server.py
    goto opened
)

where python >nul 2>nul
if %errorlevel%==0 (
    start "DLsite-compare server" python server.py
    goto opened
)

echo [ERROR] No Python interpreter found.
pause
exit

:opened
ping -n 3 127.0.0.1 >nul
start "" http://localhost:7100/
echo Server started. Close the "DLsite-compare server" window to stop it.
pause
exit
