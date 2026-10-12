@echo off
cd /d %~dp0..
echo [1/3] Creating virtual environment (.venv) ...
python -m venv .venv
if errorlevel 1 (
  echo Failed to create venv. Make sure Python 3.11+ is installed.
  pause
  exit /b 1
)
echo [2/3] Installing dependencies with Tsinghua mirror ...
.venv\Scripts\python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
.venv\Scripts\python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo Tsinghua mirror failed. Retrying with official PyPI ...
  .venv\Scripts\python -m pip install -r requirements.txt -i https://pypi.org/simple
)
echo [3/3] Installing Playwright Chromium ...
set PLAYWRIGHT_DOWNLOAD_HOST=https://npmmirror.com/mirrors/playwright
.venv\Scripts\python -m playwright install chromium
echo Done. Now run scripts\start.bat
pause
