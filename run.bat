@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PROJECT_ROOT=%SCRIPT_DIR%"
set "VENV_PATH=%PROJECT_ROOT%\.venv"
set "REQ_FILE=%PROJECT_ROOT%\python\requirements.txt"
set "SERVER_MODULE=python.server"
set "APP_URL=http://127.0.0.1:8765"

if /I "%~1"=="--help" goto :help
if /I "%~1"=="-h" goto :help

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python is required and must be on PATH.
  exit /b 1
)

if not exist "%REQ_FILE%" (
  echo [ERROR] Missing requirements file: %REQ_FILE%
  exit /b 1
)

if not exist "%VENV_PATH%\Scripts\python.exe" (
  python -m venv "%VENV_PATH%"
  if errorlevel 1 exit /b 1
)

call "%VENV_PATH%\Scripts\activate.bat"
if errorlevel 1 exit /b 1

python -m pip install --upgrade pip
if errorlevel 1 exit /b 1
python -m pip install -r "%REQ_FILE%"
if errorlevel 1 exit /b 1

set "PYTHONPATH=%PROJECT_ROOT%;%PYTHONPATH%"
cd /d "%PROJECT_ROOT%"
start "LLM Testbench" "%APP_URL%"
python -m %SERVER_MODULE% %*
exit /b %errorlevel%

:help
echo Usage: run.bat [--host HOST] [--port PORT] [--log-level LEVEL]
echo.
echo Creates .venv if needed, installs dependencies from python\requirements.txt,
echo then starts the LLM Testbench backend server.
exit /b 0
