@echo off
REM Lanza nexus-v3 con Python FREE-THREADED (sin GIL) -> multithreading real.
REM Uso: scripts\run_ft.bat [args...]   (ej: scripts\run_ft.bat 8765)
setlocal
set "ROOT=%~dp0.."
if not exist "%ROOT%\.venv-ft314\Scripts\python.exe" (
  echo [run_ft] Falta .venv-ft314. Crear con:
  echo   uv venv --python cpython-3.14.7+freethreaded .venv-ft314
  echo   uv pip install --python .venv-ft314\Scripts\python.exe -r requirements-ft.txt
  exit /b 1
)
"%ROOT%\.venv-ft314\Scripts\python.exe" "%ROOT%\start_server.py" %*
