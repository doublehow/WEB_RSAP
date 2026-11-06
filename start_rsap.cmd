@echo off
setlocal enabledelayedexpansion
rem ============================================================
rem  RSAP startup script - for Task Scheduler (boot trigger)
rem  run.py -> uvicorn app.main:app ; port and trusted proxies from settings
rem    (env RSAP_WEB_PORT > config.json web_port > 5001)
rem  Working dir must be the project root (config.json / data\ are relative)
rem  log: %APPDIR%\logs\RSAP_yyyyMMdd.log (kept 30 days)
rem  Behind a reverse proxy / SLB: trusted proxy IPs come from config.json
rem    "trusted_proxies" (list, default 127.0.0.1), never "*"
rem  NOTE: keep this file ASCII-only with CRLF line endings; cmd.exe under
rem    a DBCS code page (CP950) misparses UTF-8 comments and LF-only lines.
rem ============================================================
set "APPDIR=%~dp0"
if "%APPDIR:~-1%"=="\" set "APPDIR=%APPDIR:~0,-1%"
set "TAG=RSAP"
set "LOGDIR=%APPDIR%\logs"
set "PYTHONUTF8=1"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" 2>nul
for /f "delims=" %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd"') do set "TODAY=%%i"
if not defined TODAY set "TODAY=nodate"
set "LOG=%LOGDIR%\%TAG%_%TODAY%.log"
forfiles /p "%LOGDIR%" /m %TAG%_*.log /d -30 /c "cmd /c del @path" >nul 2>&1
call :log "==================== %TAG% starting ===================="
call :log "host=%COMPUTERNAME%  user=%USERNAME%  appdir=%APPDIR%"
cd /d "%APPDIR%"
if errorlevel 1 (
    call :log "[FATAL] cannot chdir: %APPDIR%"
    exit /b 1
)
set "PY=%APPDIR%\.venv\Scripts\python.exe"
if not exist "%PY%" (
    call :log "[FATAL] interpreter not found: %PY%"
    exit /b 9009
)
call :log "exec: %PY% -u run.py  (port / trusted_proxies from config.json)"
"%PY%" -u run.py >> "%LOG%" 2>&1
set "RC=%errorlevel%"
call :log "==================== %TAG% exited, errorlevel=%RC% ===================="
exit /b %RC%
:log
echo [%date% %time%] %~1>> "%LOG%"
goto :eof
