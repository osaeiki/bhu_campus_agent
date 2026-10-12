@echo off
cd /d %~dp0..
if not exist .venv (
  echo Dependencies not installed. Please run scripts\install.bat first.
  pause
  exit /b 1
)
echo Starting BHU Campus Agent ...
echo Web:  http://127.0.0.1:8000
echo Keep this window open. Closing it stops the server.
.venv\Scripts\python run.py
pause
