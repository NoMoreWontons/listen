@echo off
cd /d "%~dp0"
rem Bind interface comes from .env (gitignored) so no personal address is committed.
rem Absent or unset falls back to loopback -- never binds wide by accident.
for /f "usebackq tokens=1,* delims==" %%a in (".env") do if "%%a"=="LISTEN_HOST" set "LISTEN_HOST=%%b"
if not defined LISTEN_HOST set "LISTEN_HOST=127.0.0.1"
echo [listen] binding %LISTEN_HOST%:8000
.venv\Scripts\python.exe -m uvicorn app:app --host %LISTEN_HOST% --port 8000
pause
