@echo off
setlocal
cd /d "%~dp0"
set "ROOT=%CD%"
set "PY=%ROOT%\.venv-ft314\Scripts\python.exe"
set "ELECTRON=%ROOT%\node_modules\electron\dist\electron.exe"
set "MAIN=%ROOT%\electron\dist\main.js"

if not exist "%PY%" goto noinstall
if not exist "%ELECTRON%" goto noinstall

if not exist "%MAIN%" (
  echo Compilando Electron...
  call npm run build:electron
  if errorlevel 1 goto buildfail
)
if not exist "%ROOT%\ui\dist\index.html" (
  echo Compilando la interfaz...
  call npm run build:ui
  if errorlevel 1 goto buildfail
)

set "NEXUS_PYTHON=%PY%"
set "ELECTRON_IS_DEV=0"
start "" "%ELECTRON%" "%MAIN%"
exit /b 0

:noinstall
echo.
echo  Nexus v3 todavia no esta instalado en esta carpeta.
echo  Ejecuta INSTALAR.bat (una sola vez) y volve a intentar.
echo.
timeout /t 10 >nul
exit /b 1

:buildfail
echo.
echo  No se pudo compilar la app. Revisa el mensaje de arriba
echo  o ejecuta INSTALAR.bat para reparar la instalacion.
echo.
timeout /t 15 >nul
exit /b 1
