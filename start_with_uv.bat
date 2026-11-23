@echo off
setlocal
cd /d "%~dp0"
if exist "%~dp0uv.exe" (
  set "UV_EXE=%~dp0uv.exe"
) else (
  where uv >nul 2>nul
  if errorlevel 1 (
    echo ERROR: uv was not found. Install uv or copy uv.exe beside this file.
    echo With internet: winget install --id astral-sh.uv -e
    pause
    exit /b 1
  )
  set "UV_EXE=uv"
)
echo Starting Kanoon Dashboard (uv managed Python 3.12)...
"%UV_EXE%" run --no-project --python 3.12 server.py
if errorlevel 1 (
  echo ERROR: Dashboard did not start. Check the log above.
  pause
  exit /b 1
)
endlocal
