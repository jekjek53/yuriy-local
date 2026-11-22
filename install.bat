@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo Установка Юрия 0.6.0-m4. Нужны Python 3.12 или 3.13 x64 и интернет.

rem An existing compatible venv does not require Python Launcher.
if exist ".venv\Scripts\python.exe" goto check_existing
if not "%~1"=="" goto use_explicit

set "PY_EXE=py"
set "PY_ARGS=-3.13"
call :check_python
if not errorlevel 1 goto create_venv
set "PY_ARGS=-3.12"
call :check_python
if not errorlevel 1 goto create_venv

rem A bundled or PATH interpreter may be absent from py -0p.
set "PY_EXE=python"
set "PY_ARGS="
call :check_python
if not errorlevel 1 goto create_venv
set "PY_EXE=python3"
call :check_python
if not errorlevel 1 goto create_venv
goto no_python

:use_explicit
set "PY_EXE=%~1"
set "PY_ARGS="
call :check_python
if errorlevel 1 goto bad_explicit
goto create_venv

:check_existing
set "PY_EXE=%~dp0.venv\Scripts\python.exe"
set "PY_ARGS="
call :check_python
if errorlevel 1 goto bad_existing
goto install_dependencies

:create_venv
echo Найден совместимый Python:
"%PY_EXE%" %PY_ARGS% -c "import sys; print(sys.executable); print(sys.version)"
"%PY_EXE%" %PY_ARGS% -m venv .venv
if errorlevel 1 goto fail

:install_dependencies
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo.
echo Установка завершена. Запустите start.bat.
pause
exit /b 0

:no_python
echo Не найден Python 3.12 или 3.13 x64 через py, python или python3.
echo Python 3.14 и другие версии этой сборкой пока не проверены.
echo Установите совместимую версию: https://www.python.org/downloads/windows/
echo Или укажите путь к имеющемуся Python в командной строке:
echo install.bat "C:\полный путь\python.exe"
pause
exit /b 1

:bad_explicit
echo Указанный файл не запускается или это не Python 3.12 / 3.13 x64.
echo Проверьте полный путь к python.exe и его версию.
pause
exit /b 1

:bad_existing
echo Существующее окружение .venv не работает или имеет неподдерживаемую версию.
echo Оно не изменено. Переименуйте .venv и повторите установку.
echo Рабочую папку с проектами не удаляйте.
pause
exit /b 1

:fail
echo Ошибка установки. Не закрывайте окно: скопируйте сообщение об ошибке.
pause
exit /b 1

:check_python
"%PY_EXE%" %PY_ARGS% -c "import sys,struct; sys.exit(0 if sys.version_info[:2] in ((3,12),(3,13)) and struct.calcsize('P')==8 else 1)" >nul 2>nul
exit /b %errorlevel%
