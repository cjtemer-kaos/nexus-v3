param(
  [string]$InstallDir = (Join-Path $env:LOCALAPPDATA "Programs\Nexus v3"),
  [switch]$NoLaunch,
  [switch]$NoShortcuts,
  [switch]$SkipOptional,
  [switch]$SkipPythonInstall
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $env:TEMP "nexus-v3-install.log"

function Write-Log {
  param([string]$Message)
  $line = "[{0}] {1}" -f (Get-Date -Format "HH:mm:ss"), $Message
  Write-Host $line
  try { Add-Content -Path $Log -Value $line -Encoding UTF8 } catch {}
}

function Fail {
  param([string]$Message)
  Write-Log "ERROR: $Message"
  Write-Host ""
  Write-Host "ERROR: $Message" -ForegroundColor Red
  Write-Host "Log: $Log" -ForegroundColor Yellow
  exit 1
}

function Test-Python {
  param([string]$Exe)
  if (-not $Exe) { return $false }
  if (-not (Test-Path -LiteralPath $Exe)) { return $false }
  if ($Exe -like "*\WindowsApps\python*.exe") { return $false }
  try {
    $out = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -ne 0) { return $false }
    $first = ($out | Select-Object -First 1)
    if (-not $first) { return $false }
    $parts = $first.Trim().Split('.')
    if ($parts.Count -lt 2) { return $false }
    $major = [int]$parts[0]
    $minor = [int]$parts[1]
    return ($major -eq 3 -and $minor -ge 11)
  } catch {
    return $false
  }
}

function Resolve-Python {
  if ($env:NEXUS_PYTHON) {
    if (Test-Python $env:NEXUS_PYTHON) {
      return (Resolve-Path -LiteralPath $env:NEXUS_PYTHON).Path
    }
  }

  $pyCmd = Get-Command py -ErrorAction SilentlyContinue
  if ($pyCmd) {
    foreach ($ver in @("3.14", "3.13", "3.12", "3.11", "3")) {
      try {
        $exe = & py "-$ver" -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $exe) {
          $exe = ($exe | Select-Object -First 1).Trim()
          if (Test-Python $exe) { return $exe }
        }
      } catch {}
    }
  }

  $py = Get-Command python -ErrorAction SilentlyContinue
  if ($py -and $py.Source) {
    if (Test-Python $py.Source) { return $py.Source }
  }

  $candidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python314\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"),
    (Join-Path $env:LOCALAPPDATA "Programs\Python\Python311\python.exe"),
    "C:\Program Files\Python314\python.exe",
    "C:\Program Files\Python313\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Program Files\Python311\python.exe"
  )
  foreach ($c in $candidates) {
    if (Test-Python $c) { return $c }
  }
  return $null
}

function Refresh-Path {
  $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
  $user = [Environment]::GetEnvironmentVariable("Path", "User")
  $env:Path = (@($machine, $user) | Where-Object { $_ }) -join ";"
}

function Install-Python {
  if ($SkipPythonInstall) { return $null }
  $winget = Get-Command winget -ErrorAction SilentlyContinue
  if (-not $winget) { return $null }

  foreach ($id in @("Python.Python.3.14", "Python.Python.3.13", "Python.Python.3.12")) {
    Write-Log "Python no encontrado. Intentando instalar $id con winget..."
    try {
      & winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements 2>&1 |
        ForEach-Object { Write-Log "$_" }
      Refresh-Path
      $found = Resolve-Python
      if ($found) { return $found }
    } catch {
      Write-Log "winget fallo para ${id}: $($_.Exception.Message)"
    }
  }
  return $null
}

function Test-ProcessPath {
  param($Proc, [string]$Path)
  try {
    if (-not $Proc.Path) { return $false }
    return ([IO.Path]::GetFullPath($Proc.Path) -ieq [IO.Path]::GetFullPath($Path))
  } catch {
    return $false
  }
}

Write-Log "Nexus v3 - instalador automatico"
Write-Log "Origen: $ScriptDir"
Write-Log "Destino: $InstallDir"

$srcFull = [IO.Path]::GetFullPath($ScriptDir).TrimEnd('\')
$dstFull = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')

if ($srcFull -ne $dstFull) {
  if ($srcFull.StartsWith($dstFull + "\", [StringComparison]::OrdinalIgnoreCase)) {
    Fail "No extraigas el ZIP dentro de la carpeta de instalacion. Extrae el ZIP en otra carpeta y ejecuta INSTALAR.bat."
  }

  Write-Log "Copiando aplicacion..."
  $srcExe = Join-Path $srcFull "Nexus v3.exe"
  $dstExe = Join-Path $dstFull "Nexus v3.exe"
  $procs = Get-Process -ErrorAction SilentlyContinue | Where-Object { (Test-ProcessPath $_ $srcExe) -or (Test-ProcessPath $_ $dstExe) }
  foreach ($p in $procs) {
    try { Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue } catch {}
  }
  if ($procs) { Start-Sleep -Milliseconds 700 }

  if (-not (Test-Path -LiteralPath $dstFull)) {
    New-Item -ItemType Directory -Force -Path $dstFull | Out-Null
  }

  try {
    Copy-Item -Path (Join-Path $srcFull "*") -Destination $dstFull -Recurse -Force -ErrorAction Stop
  } catch {
    Fail "No se pudo copiar la aplicacion a $dstFull. Cerra Nexus v3 si esta abierto y volve a intentar. Detalle: $($_.Exception.Message)"
  }
}

$AppDir = $dstFull
$Exe = Join-Path $AppDir "Nexus v3.exe"
if (-not (Test-Path -LiteralPath $Exe)) {
  Fail "No se encontro 'Nexus v3.exe' en $AppDir. Extrae TODO el contenido del ZIP antes de instalar."
}

$PythonExe = Resolve-Python
if (-not $PythonExe) {
  $PythonExe = Install-Python
}
if (-not $PythonExe) {
  Fail "Python 3.11+ no encontrado. Instalalo desde https://www.python.org/downloads/windows/ (marca 'Add python.exe to PATH') y volve a ejecutar INSTALAR.bat."
}
Write-Log "Python: $PythonExe"

$VenvDir = Join-Path $AppDir "resources\app.asar.unpacked\.venv-ft314"
$VenvPy = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPy)) {
  Write-Log "Creando entorno virtual en $VenvDir ..."
  & $PythonExe -m venv $VenvDir
  if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) {
    Fail "No se pudo crear el entorno virtual de Python."
  }
}

& $VenvPy -m pip --version 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
  Write-Log "Instalando pip en el entorno virtual..."
  & $VenvPy -m ensurepip --upgrade
  if ($LASTEXITCODE -ne 0) {
    Fail "No se pudo instalar pip en el entorno virtual."
  }
}

Write-Log "Actualizando pip/setuptools/wheel..."
& $VenvPy -m pip install --upgrade pip setuptools wheel --disable-pip-version-check --no-input
if ($LASTEXITCODE -ne 0) {
  Write-Log "Aviso: no se pudieron actualizar pip/setuptools/wheel; se continua con lo instalado."
}

$Req = Join-Path $AppDir "requirements-runtime.txt"
if (-not (Test-Path -LiteralPath $Req)) {
  $Req = Join-Path $AppDir "requirements.txt"
}
if (-not (Test-Path -LiteralPath $Req)) {
  Fail "No se encontro requirements-runtime.txt ni requirements.txt en $AppDir."
}

Write-Log "Instalando dependencias base (puede tardar varios minutos)..."
& $VenvPy -m pip install -r $Req --disable-pip-version-check --no-input
if ($LASTEXITCODE -ne 0) {
  Fail "Fallo la instalacion de dependencias base. Revisa el log: $Log"
}

if (-not $SkipOptional) {
  $Opt = Join-Path $AppDir "requirements-optional.txt"
  if (Test-Path -LiteralPath $Opt) {
    Write-Log "Instalando extras opcionales (si fallan, la app sigue funcionando)..."
    & $VenvPy -m pip install -r $Opt --disable-pip-version-check --no-input
    if ($LASTEXITCODE -ne 0) {
      Write-Log "Aviso: algunos extras opcionales no se instalaron. La app degrada esas funciones."
    }
  }
}

Write-Log "Verificando imports base..."
& $VenvPy -c "import fastapi, uvicorn, httpx, yaml, jsonschema, dotenv, numpy, psutil, websockets, mcp, aiohttp, aiosqlite, croniter, ruamel.yaml"
if ($LASTEXITCODE -ne 0) {
  Fail "Las dependencias base no se importan correctamente. Revisa el log: $Log"
}

# uv propio: los MCP que usan uvx no dependen del uv/uvx del sistema (en algunas
# PCs falla con 'os error 448'). Si no se puede instalar, la app sigue andando.
Write-Log "Instalando uv propio (para los MCP que usan uvx)..."
& $VenvPy -m pip install uv --disable-pip-version-check --no-input --quiet
if ($LASTEXITCODE -ne 0) {
  Write-Log "Aviso: no se pudo instalar uv; los MCP que lo usan quedaran sin binario."
}

# Raiz con src/ y scripts/ (empaquetado: app.asar.unpacked)
$AppRoot = Join-Path $AppDir "resources\app.asar.unpacked"
if (-not (Test-Path -LiteralPath (Join-Path $AppRoot "scripts\bootstrap_brain.py"))) {
  $AppRoot = $AppDir
}

$BrainScript = Join-Path $AppRoot "scripts\bootstrap_brain.py"
if (Test-Path -LiteralPath $BrainScript) {
  Write-Log "Creando el grafo e importando la base de conocimiento compartida (puede tardar unos minutos)..."
  Push-Location $AppRoot
  try {
    & $VenvPy $BrainScript
    if ($LASTEXITCODE -ne 0) {
      Write-Log "Aviso: el grafo quedo sin la base; se puede regenerar con scripts\bootstrap_brain.py"
    }
  } finally { Pop-Location }
} else {
  Write-Log "Aviso: no se encontro scripts\bootstrap_brain.py (grafo sin base)."
}

$McpScript = Join-Path $AppRoot "scripts\setup_mcp.py"
if (Test-Path -LiteralPath $McpScript) {
  Write-Log "Verificando los servidores MCP del bundle..."
  Push-Location $AppRoot
  try {
    & $VenvPy $McpScript --fix
    if ($LASTEXITCODE -ne 0) {
      Write-Log "Aviso: el bundle MCP quedo incompleto (faltan binarios o claves propias)."
    }
  } finally { Pop-Location }
} else {
  Write-Log "Aviso: no se encontro scripts\setup_mcp.py."
}

$VerifyScript = Join-Path $AppRoot "scripts\verify_install.py"
if (Test-Path -LiteralPath $VerifyScript) {
  Push-Location $AppRoot
  try {
    & $VenvPy $VerifyScript --skip-mcp
    if ($LASTEXITCODE -ne 0) {
      Write-Log "Aviso: la verificacion detecto pendientes (ver salida de arriba)."
    }
  } finally { Pop-Location }
}

if (-not $NoShortcuts) {
  Write-Log "Creando accesos directos..."
  $Wsh = New-Object -ComObject WScript.Shell
  $Desktop = [Environment]::GetFolderPath("Desktop")
  $StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
  foreach ($dir in @($Desktop, $StartMenu)) {
    if (-not (Test-Path -LiteralPath $dir)) {
      New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    $lnk = Join-Path $dir "Nexus v3.lnk"
    $sc = $Wsh.CreateShortcut($lnk)
    $sc.TargetPath = $Exe
    $sc.WorkingDirectory = $AppDir
    $sc.IconLocation = "$Exe,0"
    $sc.Description = "Nexus v3"
    $sc.Save()
    Write-Log "Acceso directo: $lnk"
  }
}

if (-not $NoLaunch) {
  Write-Log "Iniciando Nexus v3..."
  Start-Process -FilePath $Exe -WorkingDirectory $AppDir
  Start-Sleep -Seconds 5
  $running = Get-Process -ErrorAction SilentlyContinue | Where-Object { Test-ProcessPath $_ $Exe }
  if (-not $running) {
    Write-Log "Aviso: el proceso no aparece a los 5s. Revisa $AppDir\resources\app.asar.unpacked\data\backend.log"
  }
}

Write-Log "Instalacion completa."
Write-Host ""
Write-Host "Nexus v3 instalado en: $AppDir" -ForegroundColor Green
if (-not $NoShortcuts) {
  Write-Host "Accesos directos creados en Escritorio y Menu Inicio." -ForegroundColor Green
}
Write-Host "Log: $Log" -ForegroundColor DarkGray
