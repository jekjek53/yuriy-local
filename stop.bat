@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
 echo Python-окружение не найдено. Используйте Ctrl+C в окне службы.
 pause
 exit /b 1
)
".venv\Scripts\python.exe" -m yuriy --stop
pause
