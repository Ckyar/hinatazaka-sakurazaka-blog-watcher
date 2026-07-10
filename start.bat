@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Please run install.bat first.
  exit /b 1
)
.venv\Scripts\python.exe app.py
exit /b %errorlevel%
