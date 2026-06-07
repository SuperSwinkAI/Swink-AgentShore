<#
.SYNOPSIS
    Provision the AgentShore Desktop managed sidecar venv on Windows.

.DESCRIPTION
    Windows equivalent of scripts/install-agentshore-venv.sh. It runs in the
    per-user installer context and creates/replaces:

        %LocalAppData%\AgentShore\venv

    The Tauri supervisor launches:

        %USERPROFILE%\AppData\Local\AgentShore\venv\Scripts\python.exe
        -m agentshore.sidecar

    uv is preferred because it can bootstrap a managed Python 3.12 on a clean
    Windows machine. If uv is absent, the official uv installer is used.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Wheel
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" }
function Write-Info($msg) { Write-Host "    $msg" }
function Die($msg) { Write-Error $msg; exit 1 }

function Resolve-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidate = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $candidate) { return $candidate }

    Write-Step "uv not found -- bootstrapping via the official installer"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression

    if (Test-Path $candidate) { return $candidate }
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    Die "uv install script ran but uv.exe was not found."
}

$WheelPath = (Resolve-Path $Wheel).Path
$VenvPath = Join-Path $env:LOCALAPPDATA "AgentShore\venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

Write-Step "Locating uv"
$uv = Resolve-Uv
Write-Info "Using uv: $uv"

Write-Step "Provisioning managed venv"
Write-Info "Path: $VenvPath"
New-Item -ItemType Directory -Path (Split-Path -Parent $VenvPath) -Force | Out-Null
if (Test-Path $VenvPath) {
    Remove-Item -LiteralPath $VenvPath -Recurse -Force
}

& $uv --native-tls venv --python 3.12 $VenvPath
if ($LASTEXITCODE -ne 0) { Die "uv venv failed with exit $LASTEXITCODE." }
if (-not (Test-Path $VenvPython)) { Die "venv python missing at $VenvPython" }

Write-Step "Installing agentshore wheel"
& $uv --native-tls pip install --python $VenvPython $WheelPath
if ($LASTEXITCODE -ne 0) { Die "uv pip install failed with exit $LASTEXITCODE." }

Write-Step "Verifying agentshore.sidecar import"
& $VenvPython -c "import agentshore.sidecar; print('agentshore.sidecar OK')"
if ($LASTEXITCODE -ne 0) { Die "agentshore.sidecar import failed in managed venv." }

Write-Step "Provisioning bd dependency"
$bdCode = @"
from agentshore.beads.setup import provision_bd

path = provision_bd(assume_yes=True)
if path is None:
    raise SystemExit(1)
print(path)
"@
$bdProbe = [System.IO.Path]::GetTempFileName() + ".py"
try {
    Set-Content -Path $bdProbe -Value $bdCode -Encoding UTF8
    & $VenvPython $bdProbe
    if ($LASTEXITCODE -ne 0) { Die "bd provisioning failed." }
} finally {
    Remove-Item -LiteralPath $bdProbe -Force -ErrorAction SilentlyContinue
}

$version = (& $VenvPython -c "from importlib.metadata import version; print(version('agentshore'))")
Write-Step "Installed managed venv"
Write-Info "python: $VenvPython"
Write-Info "agentshore: $version"
