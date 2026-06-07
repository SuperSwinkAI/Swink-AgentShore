<#
.SYNOPSIS
    Install the AgentShore CLI from the bundled wheel on Windows.

.DESCRIPTION
    Installer component helper for the Windows wizard's AgentShore CLI choice.
    Installs the same bundled wheel as the desktop sidecar, with [all] extras,
    via uv tool install into the user's uv tool directory.
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
$WheelUri = ([System.Uri]$WheelPath).AbsoluteUri

Write-Step "Locating uv"
$uv = Resolve-Uv
Write-Info "Using uv: $uv"

Write-Step "Installing agentshore CLI from bundled wheel"
Write-Info "Wheel: $WheelPath"
& $uv tool install --native-tls --force --reinstall "agentshore[all] @ $WheelUri"
if ($LASTEXITCODE -ne 0) { Die "uv tool install failed with exit $LASTEXITCODE." }

Write-Step "Ensuring uv's tool bin directory is on PATH"
& $uv tool update-shell | Out-Null

$bin = Join-Path $env:USERPROFILE ".local\bin\agentshore.exe"
if (-not (Test-Path $bin)) {
    $cmd = Get-Command agentshore -ErrorAction SilentlyContinue
    if ($cmd) { $bin = $cmd.Source }
}
if (-not (Test-Path $bin)) {
    Die "agentshore.exe not found after install. Restart the shell or check 'uv tool list'."
}

$version = (& $bin --version 2>&1 | Select-Object -First 1)
Write-Step "Installed CLI"
Write-Info "binary:  $bin"
Write-Info "version: $version"
