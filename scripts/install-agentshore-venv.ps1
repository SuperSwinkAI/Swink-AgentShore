<#
.SYNOPSIS
    Provision the AgentShore Desktop managed sidecar venv on Windows.

.DESCRIPTION
    Windows equivalent of scripts/install-agentshore-venv.sh. It runs in the
    elevated installer context and creates/replaces:

        %ProgramData%\AgentShore\venv

    The Tauri supervisor launches:

        %ProgramData%\AgentShore\venv\Scripts\python.exe
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

function Stop-AgentShoreProcessId {
    param([Parameter(Mandatory = $true)][int]$ProcessId)

    if ($ProcessId -eq $PID) { return }
    try {
        Stop-Process -Id $ProcessId -Force -ErrorAction Stop
        Write-Info "Stopped process id $ProcessId"
    } catch {
        Write-Info "Could not stop process id ${ProcessId}: $($_.Exception.Message)"
    }
}

function Stop-AgentShoreRuntimeProcesses {
    Write-Step "Stopping running AgentShore runtime processes"

    foreach ($name in @("AgentShore", "agentshore-desktop")) {
        Get-Process -Name $name -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-AgentShoreProcessId -ProcessId $_.Id }
    }

    try {
        Get-CimInstance Win32_Process |
            Where-Object {
                $commandLine = [string]$_.CommandLine
                $commandLine -match "(?i)agentshore\.sidecar" -or
                    $commandLine -match "(?i)\bagentshore(\.exe)?\s+dashboard\b"
            } |
            ForEach-Object { Stop-AgentShoreProcessId -ProcessId ([int]$_.ProcessId) }
    } catch {
        Write-Info "Could not inspect process command lines: $($_.Exception.Message)"
    }
}

function Remove-DirectoryWithRetry {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path $Path)) { return }

    for ($attempt = 1; $attempt -le 5; $attempt++) {
        try {
            Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq 5) {
                throw
            }
            Write-Info "Retrying venv cleanup after lock or access error: $($_.Exception.Message)"
            Start-Sleep -Seconds 2
        }
    }
}

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
$VenvPath = Join-Path $env:ProgramData "AgentShore\venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"

Write-Step "Locating uv"
$uv = Resolve-Uv
Write-Info "Using uv: $uv"

Write-Step "Provisioning managed venv"
Write-Info "Path: $VenvPath"
New-Item -ItemType Directory -Path (Split-Path -Parent $VenvPath) -Force | Out-Null
if (Test-Path $VenvPath) {
    Stop-AgentShoreRuntimeProcesses
    Remove-DirectoryWithRetry -Path $VenvPath
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
