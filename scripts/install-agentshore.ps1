<#
.SYNOPSIS
    Install the `agentshore` CLI on Windows 11 via `uv tool install`.

.DESCRIPTION
    Windows equivalent of scripts/install-agentshore-cli.sh. It:
      1. Locates `uv`, bootstrapping it via the official installer if absent.
      2. Installs the `agentshore` CLI with `uv tool install` from, in order
         of preference: an explicit -Wheel, the newest wheel in dist\, or the
         GitHub source (git+https) as a fallback. uv auto-provisions a
         compatible Python (3.12/3.13), so a clean box needs no preinstalled
         Python.
      3. Ensures uv's tool-bin directory is on PATH (`uv tool update-shell`).
      4. Smoke-tests the resulting `agentshore` command.

    `--native-tls` is always passed so corporate/AV HTTPS-inspection proxies
    (whose root CA lives in the Windows cert store, not in certifi) do not break
    the package downloads.

    The CLI is self-contained: a plain `pip install` of the same wheel also
    works (no extras needed) -- this script just wraps the uv-managed path.
    The desktop app ships separately as a packaged installer (issue #66); this
    script does not provision it.

.PARAMETER Wheel
    Path to an agentshore wheel to install. If omitted, the newest
    dist\agentshore-*-py3-none-any.whl is used; if none exists, the GitHub
    source is installed instead.

.PARAMETER Ref
    Git ref (branch/tag) to install when falling back to the GitHub source.
    Defaults to the repository default branch.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\install-agentshore.ps1
    .\scripts\install-agentshore.ps1 -Wheel dist\agentshore-0.2.1-py3-none-any.whl

.NOTES
    Compatible with Windows PowerShell 5.1 and PowerShell 7+. Pure-ASCII so it
    parses correctly regardless of file encoding/BOM.
#>
[CmdletBinding()]
param(
    [string]$Wheel = "",
    [string]$Ref = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/SuperSwinkAI/Swink-AgentShore"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "    $msg" }
function Die($msg) { Write-Error $msg; exit 1 }

# Repo root = parent of this script's directory.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

# --- 1. Locate or bootstrap uv ----------------------------------------------
Write-Step "Locating uv"
$uv = $null
$cmd = Get-Command uv -ErrorAction SilentlyContinue
if ($cmd) {
    $uv = $cmd.Source
} else {
    $candidate = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $candidate) { $uv = $candidate }
}

if (-not $uv) {
    Write-Step "uv not found -- bootstrapping via the official installer"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $candidate = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (Test-Path $candidate) {
        $uv = $candidate
    } else {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
        if ($cmd) { $uv = $cmd.Source }
    }
    if (-not $uv) { Die "uv install script ran but the uv binary was not found." }
}
Write-Info "Using uv: $uv"

# --- 2. Resolve the install source ------------------------------------------
Write-Step "Resolving install source"
$source = ""
if ($Wheel) {
    if (-not (Test-Path $Wheel)) { Die "Wheel not found: $Wheel" }
    $source = (Resolve-Path $Wheel).Path
    Write-Info "Source: wheel (explicit): $source"
} else {
    $dist = Join-Path $RepoRoot "dist"
    $newest = $null
    if (Test-Path $dist) {
        $newest = Get-ChildItem -Path $dist -Filter "agentshore-*-py3-none-any.whl" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
    }
    if ($newest) {
        $source = $newest.FullName
        Write-Info "Source: wheel (newest in dist): $source"
    } else {
        # Fall back to the GitHub source. Private repo: needs the user's git auth.
        $source = "git+$RepoUrl"
        if ($Ref) { $source = "git+$RepoUrl@$Ref" }
        Write-Info "Source: GitHub: $source"
        Write-Info "(no local wheel found; installing from source, which requires git access to the repo)"
    }
}

# --- 3. Install / refresh the agentshore CLI --------------------------------
Write-Step "Installing agentshore CLI"
& $uv tool install --native-tls --force --reinstall $source
if ($LASTEXITCODE -ne 0) { Die "uv tool install failed (exit $LASTEXITCODE)." }

# --- 4. Ensure the tool-bin dir is on PATH ----------------------------------
Write-Step "Ensuring uv's tool bin directory is on PATH"
& $uv tool update-shell | Out-Null
Write-Info "If 'agentshore' is not found below, restart your shell so the PATH update takes effect."

# --- 5. Smoke test ----------------------------------------------------------
Write-Step "Verifying agentshore command"
$bin = $null
$toolBin = Join-Path $env:USERPROFILE ".local\bin\agentshore.exe"
if (Test-Path $toolBin) {
    $bin = $toolBin
} else {
    $cmd = Get-Command agentshore -ErrorAction SilentlyContinue
    if ($cmd) { $bin = $cmd.Source }
}
if (-not $bin) {
    Die "agentshore command not found after install -- check '$uv tool list'. You may need to restart your shell."
}
$version = (& $bin --version 2>&1 | Select-Object -First 1)
Write-Step "Installed CLI:"
Write-Info "binary:  $bin"
Write-Info "version: $version"
