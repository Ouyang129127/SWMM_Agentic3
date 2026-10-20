@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m uvicorn web_app:app --host 127.0.0.1 --port 8001
) else (
  python -m uvicorn web_app:app --host 127.0.0.1 --port 8001
)
