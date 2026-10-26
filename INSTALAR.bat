@echo off
setlocal
cd /d "%~dp0"

echo.
echo  ==========================================
echo   Nexus v3 - instalacion automatica
echo  ==========================================
echo.
echo  Esto prepara TODO: entorno de Python, dependencias,
echo  interfaz, grafo de conocimiento, MCP y verificacion.
echo  Puede tardar varios minutos y necesita internet.
echo.

where powershell.exe >nul 2>nul
if errorlevel 1 (
  echo  ERROR: no se encontro PowerShell.
  pause
  exit /b 1
)

rem Algunos antivirus borran bootstrap.ps1 al ejecutarlo. El propio instalador
rem deja una copia en %LOCALAPPDATA%\NexusV3: la restauramos para poder reinstalar.
if not exist "%~dp0bootstrap.ps1" (
  if exist "%LOCALAPPDATA%\NexusV3\bootstrap.ps1" (
    echo  Restaurando el instalador desde el respaldo local...
    copy /y "%LOCALAPPDATA%\NexusV3\bootstrap.ps1" "%~dp0bootstrap.ps1" >nul
  )
)
if not exist "%~dp0bootstrap.ps1" (
  echo  ERROR: falta bootstrap.ps1 en esta carpeta.
  echo  Volve a extraer el ZIP o a clonar el repositorio completo.
  pause
  exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0bootstrap.ps1" %*
set "RC=%ERRORLEVEL%"

rem Se restauro o no, dejamos el respaldo al dia para la proxima.
if exist "%~dp0bootstrap.ps1" (
  if not exist "%LOCALAPPDATA%\NexusV3" mkdir "%LOCALAPPDATA%\NexusV3" >nul 2>nul
  copy /y "%~dp0bootstrap.ps1" "%LOCALAPPDATA%\NexusV3\bootstrap.ps1" >nul 2>nul
)

if not "%RC%"=="0" (
  echo.
  echo  La instalacion no termino completa.
  echo  Revisa INSTALAR-LOG.txt en esta carpeta.
  echo.
)

exit /b %RC%
