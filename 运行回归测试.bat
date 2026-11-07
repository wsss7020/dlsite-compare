@echo off
rem Regression test launcher. Keep this file ASCII-only.
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 goto py
where python >nul 2>nul
if %errorlevel%==0 goto python
echo [ERROR] No Python interpreter found.
goto failed

:py
set "PY=py"
goto run
:python
set "PY=python"

:run
echo [1/2] Python backend regression tests...
"%PY%" -m unittest discover -s tests -p "test_*.py" -v
if errorlevel 1 goto failed

echo.
echo [2/2] JavaScript state sync regression tests...
where node >nul 2>nul
if errorlevel 1 goto nonode
node tests\state_sync_test.js
if errorlevel 1 goto failed
goto passed

:nonode
echo [SKIP] Node.js not found; JavaScript tests were not run.
echo Install Node.js to enable the second test group.

:passed
echo.
echo Regression tests finished. Review PASS/SKIP lines above.
pause
exit /b 0

:failed
echo.
echo [FAILED] Regression tests failed. Review errors above.
pause
exit /b 1
