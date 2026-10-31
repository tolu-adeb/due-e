@echo off
title DUE-E
cd /d "%~dp0"
if not exist .venv\Scripts\due-e.exe (
  echo DUE-E isn't installed yet. Run test-due-e.ps1 first.
  pause
  exit /b 1
)
echo Starting DUE-E... web app at http://127.0.0.1:8787  (close this window to stop)
start "" http://127.0.0.1:8787
.venv\Scripts\due-e.exe run
pause
