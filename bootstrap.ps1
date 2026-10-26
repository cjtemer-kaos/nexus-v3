# Bootstrap de nexus-v3 â€” deja una instalacion completa y verificada.
#
# Uso normal (doble clic): INSTALAR.bat
# Manual:  powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1
#
# Hace, en orden: verifica requisitos -> crea el venv -> instala Python ->
# instala Node/UI -> compila Electron y la UI -> crea el grafo e importa la
# base de conocimiento compartida -> instala y verifica los MCP -> verificacion
# final -> accesos directos.
# Es idempotente: se puede volver a correr sin romper nada.

param(
    [string]$Python = "",
    [switch]$SkipMcp,
    [switch]$SkipBuild,
    [switch]$NoShortcut,
    [switch]$NoPause,
    [switch]$Force
)

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Log = Join-Path $Root "INSTALAR-LOG.txt"
$Venv = Join-Path $Root ".venv-ft314"
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$script:Failures = @()

# Respaldo del propio instalador: algunos antivirus borran el .ps1 al ejecutarlo.
# INSTALAR.bat lo restaura desde aca, asi se puede reinstalar siempre.
try {
    $BackupDir = Join-Path $env:LOCALAPPDATA "NexusV3"
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
    $SelfPath = $PSCommandPath
    if (-not $SelfPath) { $SelfPath = $MyInvocation.MyCommand.Path }
    if ($SelfPath -and (Test-Path $SelfPath)) {
        Copy-Item $SelfPath (Join-Path $BackupDir "bootstrap.ps1") -Force
    }
} catch { }

function Wait-Enter { if (-not $NoPause) { Read-Host "  Enter para continuar" } }
# Add-Content puede fallar con la salida de npm (ANSI/control chars) y llenar la
# pantalla de errores: escribimos con .NET y sin caracteres invalidos.
function Add-LogLine([string]$Text) {
    try {
        $clean = ($Text -replace "[\x00-\x08\x0B\x0C\x0E-\x1F]", "")
        [System.IO.File]::AppendAllText($Log, $clean + "`r`n",
            (New-Object System.Text.UTF8Encoding($false)))
    } catch { }
}

function Write-Log([string]$Text) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Text
    Add-LogLine $line
}

function Write-Step([string]$Text) {
    Write-Host ""
    Write-Host "== $Text ==" -ForegroundColor Cyan
    Write-Log "STEP $Text"
}

function Write-Ok([string]$Text) {
    Write-Host "   OK   $Text" -ForegroundColor Green
    Write-Log "OK   $Text"
}

function Write-Warn2([string]$Text) {
    Write-Host "   AVISO $Text" -ForegroundColor Yellow
    Write-Log "WARN $Text"
}

function Write-Fail([string]$Text) {
    Write-Host "   FALLA $Text" -ForegroundColor Red
    Write-Log "FAIL $Text"
    $script:Failures += $Text
}

# OJO: el parametro NO puede llamarse $Args (variable automatica de PowerShell,
# el splat quedaba vacio y los comandos se ejecutaban sin argumentos).
function Invoke-Cmd([string]$Exe, [string[]]$CmdArgs, [string]$Label) {
    Write-Log ("RUN " + $Exe + " " + ($CmdArgs -join " "))
    $out = & $Exe @CmdArgs 2>&1
    $code = $LASTEXITCODE
    if ($out) {
        $out | ForEach-Object { Add-LogLine ("    " + $_) }
    }
    if ($code -ne 0) {
        Write-Fail "$Label (exit $code)"
        if ($out) { $out | Select-Object -Last 12 | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray } }
        return $false
    }
    Write-Ok $Label
    return $true
}

function Find-Python {
    if ($Python -and (Test-Path $Python)) { return $Python }
    $candidates = @()
    foreach ($v in @("3.13", "3.12", "3.11")) {
        try {
            $p = & py "-$v" -c "import sys;print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() }
        } catch { }
    }
    try {
        $p = & python -c "import sys;print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { $candidates += $p.Trim() }
    } catch { }
    foreach ($c in $candidates) {
        $ver = & $c -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -ne 0) { continue }
        $parts = $ver.Trim().Split(".")
        $maj = [int]$parts[0]; $min = [int]$parts[1]
        if ($maj -eq 3 -and $min -ge 11 -and $min -le 13) { return $c }
    }
    foreach ($c in $candidates) {
        $ver = & $c -c "import sys;print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $parts = $ver.Trim().Split(".")
            if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) { return $c }
        }
    }
    return ""
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

Write-Host ""
Write-Host "  ============================================" -ForegroundColor White
Write-Host "   Nexus v3 - instalacion automatica" -ForegroundColor White
Write-Host "  ============================================" -ForegroundColor White
Write-Host "  Carpeta: $Root"
Write-Host "  Log:     $Log"
Write-Log "=== bootstrap inicio (root=$Root) ==="

# ---------------------------------------------------------------- 0. repo sano
Write-Step "0/8 Comprobando el repositorio"
if (-not (Test-Path (Join-Path $Root "package.json")) -or
    -not (Test-Path (Join-Path $Root "src\api\server.py"))) {
    Write-Fail "Esta carpeta no es el repo de nexus-v3 (falta package.json o src\api\server.py)."
    Write-Host ""
    Write-Host "  Extrae el ZIP completo o clona el repo y volve a ejecutar INSTALAR.bat." -ForegroundColor Yellow
    Wait-Enter
    exit 1
}
Write-Ok "repositorio reconocido"

Refresh-Path

# ---------------------------------------------------------------- 1. requisitos
Write-Step "1/8 Requisitos (git, node, python)"
$git = (Get-Command git -ErrorAction SilentlyContinue)
if ($git) { Write-Ok ("git " + (& git --version)) } else { Write-Warn2 "git no esta en PATH (solo hace falta para actualizar con git pull)" }

$node = (Get-Command node -ErrorAction SilentlyContinue)
$npm = (Get-Command npm -ErrorAction SilentlyContinue)
if (-not $node -or -not $npm) {
    Write-Fail "Node.js/npm no estan instalados. Instala Node 20 o superior desde https://nodejs.org y volve a ejecutar."
    Wait-Enter
    exit 1
}
$nodeVer = (& node -v).Trim()
$nodeMajor = [int](($nodeVer.TrimStart("v").Split("."))[0])
if ($nodeMajor -lt 18) { Write-Warn2 "Node $nodeVer es viejo; se recomienda 20 o superior" } else { Write-Ok "node $nodeVer / npm $(& npm -v)" }

$pyPath = Find-Python
if (-not $pyPath) {
    Write-Fail "No se encontro Python 3.11+ . Instala Python desde https://python.org (marcando 'Add python.exe to PATH') y volve a ejecutar."
    Wait-Enter
    exit 1
}
$pyVer = (& $pyPath -c "import sys;print(sys.version.split()[0])").Trim()
Write-Ok "python $pyVer ($pyPath)"

# ---------------------------------------------------------------- 2. venv
Write-Step "2/8 Entorno de Python (.venv-ft314)"
if ($Force -and (Test-Path $Venv)) {
    Write-Warn2 "recreando el venv por -Force"
    Remove-Item $Venv -Recurse -Force -ErrorAction SilentlyContinue
}
if (-not (Test-Path $VenvPy)) {
    if (Test-Path $Venv) {
        Write-Warn2 "el venv existe pero esta incompleto: se recrea"
        Remove-Item $Venv -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not (Invoke-Cmd $pyPath @("-m", "venv", $Venv) "crear venv")) {
        Write-Fail "No se pudo crear el entorno de Python."
        Wait-Enter
        exit 1
    }
} else {
    Write-Ok "venv existente reutilizado"
}

$VenvVer = (& $VenvPy -c "import sys;print(sys.version.split()[0])" 2>$null)
Write-Ok "python del proyecto: $VenvVer"

# ---------------------------------------------------------------- 3. deps python
Write-Step "3/8 Dependencias de Python"
Invoke-Cmd $VenvPy @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel", "--quiet") "pip actualizado" | Out-Null
if (-not (Invoke-Cmd $VenvPy @("-m", "pip", "install", "-r", (Join-Path $Root "requirements.txt"), "--quiet") "requirements.txt")) {
    Write-Warn2 "reintentando sin cache de pip"
    Invoke-Cmd $VenvPy @("-m", "pip", "install", "-r", (Join-Path $Root "requirements.txt"), "--no-cache-dir", "--quiet") "requirements.txt (sin cache)" | Out-Null
}
# uv propio: evita depender del uv/uvx del sistema (roto en algunas PCs).
Invoke-Cmd $VenvPy @("-m", "pip", "install", "uv", "--quiet") "uv propio (para los MCP que lo usan)" | Out-Null

# ---------------------------------------------------------------- 4. node
Write-Step "4/8 Dependencias de Node (raiz + ui)"
Push-Location $Root
try {
    if (Test-Path (Join-Path $Root "package-lock.json")) {
        if (-not (Invoke-Cmd "npm" @("ci", "--no-audit", "--no-fund") "npm ci (raiz)")) {
            Invoke-Cmd "npm" @("install", "--no-audit", "--no-fund") "npm install (raiz)" | Out-Null
        }
    } else {
        Invoke-Cmd "npm" @("install", "--no-audit", "--no-fund") "npm install (raiz)" | Out-Null
    }
    Push-Location (Join-Path $Root "ui")
    try {
        if (Test-Path (Join-Path $Root "ui\package-lock.json")) {
            if (-not (Invoke-Cmd "npm" @("ci", "--no-audit", "--no-fund") "npm ci (ui)")) {
                Invoke-Cmd "npm" @("install", "--no-audit", "--no-fund") "npm install (ui)" | Out-Null
            }
        } else {
            Invoke-Cmd "npm" @("install", "--no-audit", "--no-fund") "npm install (ui)" | Out-Null
        }
    } finally { Pop-Location }
} finally { Pop-Location }

# ---------------------------------------------------------------- 5. build
Write-Step "5/8 Compilar Electron y la UI"
if ($SkipBuild) {
    Write-Warn2 "compilacion omitida (-SkipBuild)"
} else {
    Push-Location $Root
    try {
        Invoke-Cmd "npm" @("run", "build:electron") "compilar Electron" | Out-Null
        Invoke-Cmd "npm" @("run", "build:ui") "compilar la UI" | Out-Null
    } finally { Pop-Location }
}
$mainJs = Join-Path $Root "electron\dist\main.js"
$uiIndex = Join-Path $Root "ui\dist\index.html"
if (Test-Path $mainJs) { Write-Ok "electron\dist\main.js" } else { Write-Fail "falta electron\dist\main.js (revisar el log de compilacion)" }
if (Test-Path $uiIndex) { Write-Ok "ui\dist\index.html" } else { Write-Fail "falta ui\dist\index.html (revisar el log de compilacion)" }

# ---------------------------------------------------------------- 6. grafo
Write-Step "6/8 Base de conocimiento compartida"
Push-Location $Root
try {
    $seedDir = Join-Path $Root "brain\seed"
    $seedFiles = @()
    if (Test-Path $seedDir) {
        $seedFiles = Get-ChildItem $seedDir -File |
            Where-Object { $_.Name -like "*.nexuskg.json" -or $_.Name -like "*.nexuskg.json.gz" }
    }
    if ($seedFiles.Count -gt 0) {
        $mb = [math]::Round((($seedFiles | Measure-Object Length -Sum).Sum / 1MB), 1)
        Write-Host "   Importando la base de conocimiento ($($seedFiles.Count) archivo(s), $mb MB)." -ForegroundColor DarkGray
        Write-Host "   Puede tardar unos minutos la primera vez; en las siguientes es instantaneo." -ForegroundColor DarkGray
    }
    Invoke-Cmd $VenvPy @("scripts\bootstrap_brain.py") "grafo listo con la base compartida" | Out-Null
} finally { Pop-Location }

# ---------------------------------------------------------------- 7. mcp
Write-Step "7/8 Servidores MCP del bundle"
Push-Location $Root
try {
    if ($SkipMcp) {
        Write-Warn2 "MCP omitidos (-SkipMcp)"
    } else {
        & $VenvPy "scripts\setup_mcp.py" "--fix"
        $mcpCode = $LASTEXITCODE
        if ($mcpCode -eq 0) {
            Write-Ok "bundle MCP listo"
        } else {
            Write-Warn2 "el bundle MCP quedo incompleto (faltan binarios o claves propias); revisa la salida de arriba"
        }
    }
} finally { Pop-Location }

# ---------------------------------------------------------------- 8. verificar
Write-Step "8/8 Verificacion final"
Push-Location $Root
try {
    & $VenvPy "scripts\verify_install.py"
    $verifyCode = $LASTEXITCODE
} finally { Pop-Location }

# ---------------------------------------------------------------- accesos
if (-not $NoShortcut) {
    Write-Step "Accesos directos"
    $target = Join-Path $Root "nexus-v3.cmd"
    $icon = Join-Path $Root "build\icon.ico"
    $shell = New-Object -ComObject WScript.Shell
    $created = 0
    $desktop = [Environment]::GetFolderPath("Desktop")
    $startMenu = Join-Path ([Environment]::GetFolderPath("Programs")) "Nexus v3"
    foreach ($dir in @($desktop, $startMenu)) {
        try {
            if ($dir -eq $startMenu -and -not (Test-Path $dir)) {
                New-Item -ItemType Directory -Path $dir -Force | Out-Null
            }
            $lnk = $shell.CreateShortcut((Join-Path $dir "Nexus v3.lnk"))
            $lnk.TargetPath = $target
            $lnk.WorkingDirectory = $Root
            $lnk.WindowStyle = 7
            if (Test-Path $icon) { $lnk.IconLocation = $icon }
            $lnk.Description = "Nexus v3 - harness de agentes"
            $lnk.Save()
            $created++
        } catch {
            Write-Warn2 ("no se pudo crear el acceso en " + $dir)
        }
    }
    if ($created -gt 0) { Write-Ok "$created acceso(s) directo(s) creados" }
}

# ---------------------------------------------------------------- resumen
Write-Host ""
Write-Host "  ============================================" -ForegroundColor White
if ($script:Failures.Count -eq 0 -and $verifyCode -eq 0) {
    Write-Host "   LISTO - la instalacion quedo completa" -ForegroundColor Green
    Write-Host "  ============================================" -ForegroundColor White
    Write-Host "  Abri la app con el acceso directo 'Nexus v3'"
    Write-Host "  o con doble clic en nexus-v3.cmd"
} else {
    Write-Host "   INSTALACION INCOMPLETA" -ForegroundColor Yellow
    Write-Host "  ============================================" -ForegroundColor White
    if ($script:Failures.Count -gt 0) {
        Write-Host "  Pendientes:"
        $script:Failures | ForEach-Object { Write-Host "   - $_" -ForegroundColor Yellow }
    }
    Write-Host "  Detalle completo en: $Log"
    Write-Host "  Podes volver a ejecutar INSTALAR.bat; retoma donde fallo."
}
Write-Host ""
Write-Log ("=== bootstrap fin (fallas=" + $script:Failures.Count + ", verify=" + $verifyCode + ") ===")

Wait-Enter
if ($script:Failures.Count -eq 0 -and $verifyCode -eq 0) { exit 0 } else { exit 1 }

