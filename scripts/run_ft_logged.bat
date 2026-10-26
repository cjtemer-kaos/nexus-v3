@echo off
REM KAOS :: envuelve run_ft.bat agregando log rotativo.
REM
REM Por que existe: el servicio de :9000 se lanza desde start_hidden_9000.vbs con
REM "cmd /c scripts\run_ft.bat --port 9000". Ese cmd corre con la ventana oculta y
REM sin redireccion, asi que stdout/stderr se perdian: data\backend.log quedo
REM congelado en la corrida de las 01:35 y no habia forma de diagnosticar nada.
REM
REM Uso: scripts\run_ft_logged.bat --port 9000
REM Log: data\nexus-server.log  (>2 MB rota a .1, se pisa la anterior)
setlocal
set "ROOT=%~dp0.."
set "LOG=%ROOT%\data\nexus-server.log"

if not exist "%ROOT%\data" mkdir "%ROOT%\data"
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 2097152 move /y "%LOG%" "%LOG%.1" >nul 2>&1

echo [%DATE% %TIME%] arranque: run_ft %* >> "%LOG%"
call "%~dp0run_ft.bat" %* >> "%LOG%" 2>&1
