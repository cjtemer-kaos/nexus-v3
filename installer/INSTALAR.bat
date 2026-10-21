@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0Nexus v3.exe" (
  echo.
  echo  Extrae TODO el contenido del ZIP en una carpeta antes de ejecutar INSTALAR.bat.
  echo  No lo ejecutes desde dentro del ZIP.
  echo.
  pause
  exit /b 1
)

echo.
echo  ==========================================
echo   Nexus v3 - Instalador automatico
echo  ==========================================
echo.
echo  Esto instala la app en %%LOCALAPPDATA%%\Programs\Nexus v3,
echo  crea el entorno de Python, instala dependencias y crea
echo  accesos directos en el Escritorio y el Menu Inicio.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
if errorlevel 1 (
  echo.
  echo  La instalacion no termino correctamente.
  echo  Revisa el log en %%TEMP%%\nexus-v3-install.log
  echo.
  pause
  exit /b 1
)

exit /b 0
