@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found. Install Python 3.10 or newer first.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" python -m venv .venv
if errorlevel 1 goto :failed

call .venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :failed
call .venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :failed

if not exist ".env" copy ".env.example" ".env" >nul
echo.
echo Installation complete. Edit .env, then run start.bat.
pause
exit /b 0

:failed
echo.
echo [ERROR] Installation failed. See the messages above.
pause
exit /b 1
