@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe -m uvicorn app:app --host 100.69.173.35 --port 8000
pause
