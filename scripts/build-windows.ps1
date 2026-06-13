<#
.SYNOPSIS
    Thin shim over the cross-platform build spine (scripts/buildkit windows).

.DESCRIPTION
    All build logic lives in the Python spine (scripts/buildkit/windows.py); the
    genuinely PowerShell-native Authenticode/cert-store operations live in
    scripts/buildkit/_win_signing.ps1, which the spine invokes. This shim only
    bootstraps `uv`, maps the historical PowerShell parameters to the spine's CLI
    flags, and hands off to `uv run python -m scripts.buildkit windows`.

    The parameter surface is unchanged so existing callers (CI:
    `scripts\build-windows.ps1 -NoSign`) keep working. See
    docs/design/build-pipeline-unification.md.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1 -NoSign
#>
[CmdletBinding()]
param(
    [switch]$SkipDashboard,
    [switch]$DebugBuild,
    [switch]$Install,
    [string]$Iscc = "",
    [switch]$NoSign,
    [switch]$SelfSign,
    [switch]$TrustSelfSignedCertificate,
    [switch]$SetupSelfSignedCertificateOnly,
    [string]$SelfSignedCertificateSubject = "CN=AgentShore Local Dev Code Signing",
    [string]$SignTool = "",
    [string]$CertificateThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com",
    [switch]$AllowNoRevocationCheck
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Error "uv not found — install uv (https://docs.astral.sh/uv/) and retry"
    exit 1
}

$cliArgs = @("run", "python", "-m", "scripts.buildkit", "windows")
if ($SkipDashboard) { $cliArgs += "--skip-dashboard" }
if ($DebugBuild) { $cliArgs += "--debug" }
if ($Install) { $cliArgs += "--install" }
if ($Iscc) { $cliArgs += @("--iscc", $Iscc) }
if ($NoSign) { $cliArgs += "--no-sign" }
if ($SelfSign) { $cliArgs += "--self-sign" }
if ($TrustSelfSignedCertificate) { $cliArgs += "--trust-self-signed-certificate" }
if ($SetupSelfSignedCertificateOnly) { $cliArgs += "--setup-self-signed-certificate-only" }
if ($SelfSignedCertificateSubject) { $cliArgs += @("--self-signed-subject", $SelfSignedCertificateSubject) }
if ($SignTool) { $cliArgs += @("--sign-tool", $SignTool) }
if ($CertificateThumbprint) { $cliArgs += @("--certificate-thumbprint", $CertificateThumbprint) }
if ($TimestampUrl) { $cliArgs += @("--timestamp-url", $TimestampUrl) }
if ($AllowNoRevocationCheck) { $cliArgs += "--allow-no-revocation-check" }

Push-Location $RepoRoot
try {
    & uv @cliArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} finally {
    Pop-Location
}
