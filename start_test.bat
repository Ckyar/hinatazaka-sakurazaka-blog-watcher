@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Please run install.bat first.
  exit /b 1
)
if not exist ".env.test" (
  echo [ERROR] Copy .env.test.example to .env.test and fill in the test settings first.
  exit /b 1
)
set "POKA_ENV_FILE=.env.test"
.venv\Scripts\python.exe app.py
exit /b %errorlevel%
