<#
.SYNOPSIS
    Provision the optional Timelapse Capture toolchain on Windows.

.DESCRIPTION
    Runs the canonical Python installer from the managed AgentShore sidecar
    venv. This keeps the Windows installer aligned with the macOS pkg:
    Timelapse is opt-in, heavier, and can be retried later from the desktop app
    if the optional install fails.
#>
[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" }
function Write-Info($msg) { Write-Host "    $msg" }
function Die($msg) { Write-Error $msg; exit 1 }

$VenvPython = Join-Path $env:LOCALAPPDATA "AgentShore\venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Die "managed venv python missing at $VenvPython (Desktop component must install first)."
}

Write-Step "Provisioning Timelapse Capture via managed venv"
Write-Info "Python: $VenvPython"

$code = @"
import asyncio
import sys

from agentshore.timelapse.setup import install_timelapse

result = asyncio.run(install_timelapse())
print(result.message)
sys.exit(0 if result.success else 1)
"@

$tmp = [System.IO.Path]::GetTempFileName() + ".py"
try {
    Set-Content -Path $tmp -Value $code -Encoding UTF8
    & $VenvPython $tmp
    if ($LASTEXITCODE -ne 0) { Die "timelapse installer failed with exit $LASTEXITCODE." }
} finally {
    Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
}
