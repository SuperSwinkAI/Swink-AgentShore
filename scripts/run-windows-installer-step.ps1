<#
.SYNOPSIS
    Run one bundled Windows installer helper and surface failures clearly.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StepName,
    [Parameter(Mandatory = $true)][string]$ScriptPath,
    [string]$Wheel = "",
    [switch]$Optional
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$LogDir = Join-Path $env:LOCALAPPDATA "AgentShore\install-logs"
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$SafeStep = ($StepName -replace "[^A-Za-z0-9_.-]", "-").Trim("-")
$LogPath = Join-Path $LogDir "$SafeStep.log"

$arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $ScriptPath)
if ($Wheel) {
    $arguments += @("-Wheel", $Wheel)
}

try {
    $PowerShellPath = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path $PowerShellPath)) {
        $PowerShellPath = "powershell.exe"
    }

    & $PowerShellPath @arguments > $LogPath 2> "$LogPath.err"
    $exitCode = $LASTEXITCODE
} catch {
    Add-Content -Path $LogPath -Value "Failed to launch ${StepName}: $($_.Exception.Message)"
    if ($Optional) { exit 0 }
    throw
}

if (Test-Path "$LogPath.err") {
    Get-Content "$LogPath.err" | Add-Content $LogPath
    Remove-Item "$LogPath.err" -Force -ErrorAction SilentlyContinue
}

if ($exitCode -ne 0) {
    $message = "$StepName failed during installation. See $LogPath."
    Add-Content -Path $LogPath -Value $message
    if ($Optional) {
        Add-Content -Path $LogPath -Value "Optional component failure ignored; it can be installed later from the app."
        exit 0
    }
    Write-Error $message
    exit $exitCode
}

exit 0
