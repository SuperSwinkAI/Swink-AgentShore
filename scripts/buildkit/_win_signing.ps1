<#
.SYNOPSIS
    Windows Authenticode signing carve-out for the build spine.

.DESCRIPTION
    The genuinely PowerShell-native half of the Windows build: certificate-store
    lookup, self-signed dev-cert creation/trust, signtool.exe resolution, and the
    actual `signtool sign` invocation. The Python spine (scripts/buildkit/windows.py)
    orchestrates everything else and shells out to this helper for signing so the
    cert-store logic stays idiomatic PowerShell (and CI-testable on a real runner).

    Actions:
      -Action SetupCert   Create/reuse the local self-signed code-signing cert
                          (optionally trust it) and exit. Requires -SelfSign.
      -Action Sign        Resolve signtool + a code-signing thumbprint and sign
                          -File. Honors -SelfSign / -CertificateThumbprint / auto.

    Exit code 0 on success, non-zero on failure (the spine treats that as fatal).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet("SetupCert", "Sign")][string]$Action,
    [string]$File = "",
    [switch]$NoSign,
    [switch]$SelfSign,
    [switch]$TrustSelfSignedCertificate,
    [string]$SelfSignedCertificateSubject = "CN=AgentShore Local Dev Code Signing",
    [string]$SignTool = "",
    [string]$CertificateThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "    $msg" }
function Die($msg) { Write-Error $msg; exit 1 }

function Resolve-SignTool {
    if ($SignTool) {
        if (-not (Test-Path $SignTool)) { Die "signtool.exe not found: $SignTool" }
        return (Resolve-Path $SignTool).Path
    }
    $cmd = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kitsRoot = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $kitsRoot) {
        $candidate = Get-ChildItem -Path $kitsRoot -Filter signtool.exe -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "\\x64\\signtool\.exe$" } |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) { return $candidate.FullName }
    }
    return ""
}

function Add-CertificateToCurrentUserRoot {
    param([Parameter(Mandatory = $true)]$Certificate)
    $rootStore = [System.Security.Cryptography.X509Certificates.X509Store]::new(
        [System.Security.Cryptography.X509Certificates.StoreName]::Root,
        [System.Security.Cryptography.X509Certificates.StoreLocation]::CurrentUser
    )
    try {
        $rootStore.Open([System.Security.Cryptography.X509Certificates.OpenFlags]::ReadWrite)
        $existing = $rootStore.Certificates.Find(
            [System.Security.Cryptography.X509Certificates.X509FindType]::FindByThumbprint,
            $Certificate.Thumbprint, $false)
        if ($existing.Count -eq 0) {
            $rootStore.Add($Certificate)
            Write-Info "Trusted self-signed certificate in CurrentUser\Root."
        } else {
            Write-Info "Self-signed certificate is already trusted in CurrentUser\Root."
        }
    } finally {
        $rootStore.Close()
    }
}

function New-SelfSignedCodeSigningCertificate {
    if ($SelfSignedCertificateSubject -notmatch "^CN=") {
        Die "SelfSignedCertificateSubject must start with CN=."
    }
    $minValidity = (Get-Date).AddDays(7)
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Subject -eq $SelfSignedCertificateSubject -and
            $_.HasPrivateKey -and $_.NotAfter -gt $minValidity
        } |
        Sort-Object NotAfter -Descending | Select-Object -First 1
    if (-not $cert) {
        Write-Step "Creating local self-signed code-signing certificate"
        $cert = New-SelfSignedCertificate `
            -Type CodeSigningCert -Subject $SelfSignedCertificateSubject `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -KeyAlgorithm RSA -KeyLength 3072 -HashAlgorithm SHA256 `
            -KeyExportPolicy NonExportable -NotAfter (Get-Date).AddYears(3)
    } else {
        Write-Step "Reusing local self-signed code-signing certificate"
    }
    Write-Info "Subject: $($cert.Subject)"
    Write-Info "Thumbprint: $($cert.Thumbprint)"
    Write-Info "Expires: $($cert.NotAfter)"
    if ($TrustSelfSignedCertificate) {
        Add-CertificateToCurrentUserRoot -Certificate $cert
    } else {
        Write-Info "Not trusted locally. Pass -TrustSelfSignedCertificate to add it to CurrentUser\Root."
    }
    return $cert.Thumbprint
}

function Resolve-CodeSigningThumbprint {
    if ($SelfSign) { return New-SelfSignedCodeSigningCertificate }
    if ($CertificateThumbprint) {
        return ($CertificateThumbprint -replace "\s", "").ToUpperInvariant()
    }
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
        Sort-Object NotAfter -Descending | Select-Object -First 1
    if ($cert) { return $cert.Thumbprint }
    return ""
}

if ($Action -eq "SetupCert") {
    [void](New-SelfSignedCodeSigningCertificate)
    Write-Step "Self-signed certificate setup complete"
    exit 0
}

# Action == Sign
if (-not $File) { Die "Sign action requires -File." }
if (-not (Test-Path $File)) { Die "File to sign not found: $File" }

$resolvedSignTool = Resolve-SignTool
$resolvedThumbprint = Resolve-CodeSigningThumbprint
if (-not $resolvedSignTool -or -not $resolvedThumbprint) {
    if (-not $resolvedSignTool) { Write-Info "signtool.exe not found." }
    if (-not $resolvedThumbprint) { Write-Info "No current-user code-signing certificate with a private key was found." }
    # Exit 2 = "could not sign" — distinct from a hard failure so the caller can
    # decide (debug builds tolerate it; release builds must not).
    exit 2
}

Write-Step "Authenticode signing $(Split-Path -Leaf $File)"
& $resolvedSignTool sign /fd SHA256 /td SHA256 /tr $TimestampUrl /sha1 $resolvedThumbprint $File
if ($LASTEXITCODE -ne 0) { Die "signtool failed with exit code $LASTEXITCODE for $File" }
exit 0
