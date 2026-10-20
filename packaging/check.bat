@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
"%~dp0python\python.exe" -X utf8 "%~dp0selfcheck.py" > "%~dp0selfcheck-result.txt" 2>&1
set "CHECK_EXIT=%ERRORLEVEL%"
type "%~dp0selfcheck-result.txt"
echo.
if not "%CHECK_EXIT%"=="0" echo Check failed. See selfcheck-result.txt.
echo Result saved to selfcheck-result.txt
pause
exit /b %CHECK_EXIT%
