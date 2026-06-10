<#
.SYNOPSIS
    Build the AgentShore Windows desktop installer.

.DESCRIPTION
    Windows parity for scripts/build-macos.sh. Produces a machine-wide Inno Setup
    admin installer for the Tauri desktop shell plus the managed Python
    sidecar venv, with the same deliberate component choices as the macOS pkg:

      - AgentShore Desktop: required.
      - Timelapse Capture: optional, unchecked by default.
      - AgentShore CLI: optional, checked by default.

    The installer is machine-wide and requires elevation. The desktop app
    installs under %ProgramFiles%\AgentShore, and the managed sidecar
    venv is provisioned under %ProgramData%\AgentShore\venv, matching the
    Rust supervisor's Windows lookup path.

.PARAMETER SkipDashboard
    Reuse existing dashboard build outputs.

.PARAMETER DebugBuild
    Build a debug Tauri executable instead of release.

.PARAMETER Install
    Launch the generated installer after building it.

.PARAMETER Iscc
    Explicit path to ISCC.exe. If omitted, the script checks PATH, then the
    standard Inno Setup 6 install locations.

.PARAMETER NoSign
    Skip Authenticode signing even if signtool.exe and a code-signing
    certificate are available.

.PARAMETER SelfSign
    Create or reuse a local current-user self-signed code-signing certificate
    and use it for Authenticode signing. This is for local installer testing
    only; public releases must use a CA-backed or managed signing certificate.

.PARAMETER TrustSelfSignedCertificate
    Add the self-signed certificate to the current user's Trusted Root store so
    local verification treats the signature as trusted. Only valid with
    -SelfSign.

.PARAMETER SetupSelfSignedCertificateOnly
    Create or reuse the local self-signed certificate, optionally trust it, and
    exit before building any artifacts. Only valid with -SelfSign.

.PARAMETER SelfSignedCertificateSubject
    Subject name for the local development self-signed certificate.

.PARAMETER AllowNoRevocationCheck
    Required on machines with Avast HTTPS scanning (Schannel interception breaks
    cargo TLS cert revocation). Use -AllowNoRevocationCheck only on such machines.
    Disables CARGO_HTTP_CHECK_REVOKE for the Tauri build step.

.PARAMETER SignTool
    Explicit path to signtool.exe. If omitted, the script checks PATH, then the
    standard Windows Kits locations.

.PARAMETER CertificateThumbprint
    SHA-1 thumbprint of the Authenticode code-signing certificate to use. If
    omitted, the script auto-detects a current-user code-signing certificate
    with a private key.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
    .\scripts\build-windows.ps1 -SkipDashboard
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
$DesktopDir = Join-Path $RepoRoot "desktop"
$TauriDir = Join-Path $DesktopDir "src-tauri"
$BuildMode = if ($DebugBuild) { "debug" } else { "release" }
$TargetDir = Join-Path $TauriDir "target\$BuildMode"
$StageDir = Join-Path $TauriDir "target\windows-installer"
$AppStageDir = Join-Path $StageDir "app"
$InstallerStageDir = Join-Path $StageDir "installer"
$OutputDir = Join-Path $DesktopDir "dist"
$TemplatePath = Join-Path $RepoRoot "packaging\desktop\windows\AgentShore.iss.in"
$WindowsTauriConfig = Join-Path $RepoRoot "packaging\desktop\windows\tauri.windows-installer.conf.json"
$LicensePath = Join-Path $RepoRoot "packaging\desktop\installer-resources\EULA.rtf"
$LicenseSourcePath = Join-Path $RepoRoot "LICENSE"
$EulaBuilderPath = Join-Path $RepoRoot "packaging\desktop\installer-resources\build-eula-rtf.py"
$IconPath = Join-Path $TauriDir "icons\icon.ico"
$PinnedUvVersion = "uv 0.8.11"

function Write-Step($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "    $msg" }
function Die($msg) { Write-Error $msg; exit 1 }

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Die "$FilePath failed with exit code $LASTEXITCODE"
    }
}

function Resolve-Iscc {
    if ($Iscc) {
        if (-not (Test-Path $Iscc)) { Die "ISCC.exe not found: $Iscc" }
        return (Resolve-Path $Iscc).Path
    }

    $cmd = Get-Command iscc.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }

    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) {
            return $candidate
        }
    }

    Die "Inno Setup 6 compiler not found. Install Inno Setup 6 or pass -Iscc <path-to-ISCC.exe>."
}

function Resolve-Uv {
    $cmd = Get-Command uv.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        $cmd = Get-Command uv -ErrorAction SilentlyContinue
    }
    if (-not $cmd) {
        Die "uv not found. Install uv $PinnedUvVersion and retry."
    }
    return $cmd.Source
}

function Assert-UvVersion {
    param([Parameter(Mandatory = $true)][string]$UvPath)

    $version = (& $UvPath --version)
    if ($LASTEXITCODE -ne 0) {
        Die "uv --version failed with exit code $LASTEXITCODE"
    }
    if (-not ([string]$version).StartsWith($PinnedUvVersion)) {
        Die "Expected $PinnedUvVersion for reproducible Windows installer provisioning, got '$version'."
    }
    Write-Info "Using uv: $UvPath ($version)"
}

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

function Assert-SigningOptions {
    if ($NoSign -and $SelfSign) {
        Die "Use either -NoSign or -SelfSign, not both."
    }
    if ($TrustSelfSignedCertificate -and -not $SelfSign) {
        Die "-TrustSelfSignedCertificate requires -SelfSign."
    }
    if ($SetupSelfSignedCertificateOnly -and -not $SelfSign) {
        Die "-SetupSelfSignedCertificateOnly requires -SelfSign."
    }
    if ($SelfSign -and $CertificateThumbprint) {
        Die "Use either -SelfSign or -CertificateThumbprint, not both."
    }
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
            $Certificate.Thumbprint,
            $false
        )
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

function New-AgentShoreSelfSignedCodeSigningCertificate {
    if ($SelfSignedCertificateSubject -notmatch "^CN=") {
        Die "SelfSignedCertificateSubject must start with CN=."
    }

    $minValidity = (Get-Date).AddDays(7)
    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Subject -eq $SelfSignedCertificateSubject -and
            $_.HasPrivateKey -and
            $_.NotAfter -gt $minValidity
        } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1

    if (-not $cert) {
        Write-Step "Creating local self-signed code-signing certificate"
        $cert = New-SelfSignedCertificate `
            -Type CodeSigningCert `
            -Subject $SelfSignedCertificateSubject `
            -CertStoreLocation "Cert:\CurrentUser\My" `
            -KeyAlgorithm RSA `
            -KeyLength 3072 `
            -HashAlgorithm SHA256 `
            -KeyExportPolicy NonExportable `
            -NotAfter (Get-Date).AddYears(3)
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
    if ($SelfSign) {
        return New-AgentShoreSelfSignedCodeSigningCertificate
    }

    if ($CertificateThumbprint) {
        return ($CertificateThumbprint -replace "\s", "").ToUpperInvariant()
    }

    $cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert -ErrorAction SilentlyContinue |
        Where-Object { $_.HasPrivateKey -and $_.NotAfter -gt (Get-Date) } |
        Sort-Object NotAfter -Descending |
        Select-Object -First 1
    if ($cert) { return $cert.Thumbprint }

    return ""
}

function Invoke-AuthenticodeSign {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string]$SignToolPath,
        [Parameter(Mandatory = $true)][string]$Thumbprint
    )

    Write-Step "Authenticode signing $(Split-Path -Leaf $FilePath)"
    & $SignToolPath sign /fd SHA256 /td SHA256 /tr $TimestampUrl /sha1 $Thumbprint $FilePath
    if ($LASTEXITCODE -ne 0) {
        Die "signtool failed with exit code $LASTEXITCODE for $FilePath"
    }
}

function Invoke-EulaGenerator {
    Write-Step "Regenerating EULA.rtf from LICENSE"
    if (-not (Test-Path $EulaBuilderPath)) { Die "EULA generator missing: $EulaBuilderPath" }
    if (-not (Test-Path $LicenseSourcePath)) { Die "LICENSE missing: $LicenseSourcePath" }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        & $python.Source $EulaBuilderPath $LicenseSourcePath $LicensePath
        if ($LASTEXITCODE -ne 0) { Die "EULA generator failed with exit code $LASTEXITCODE" }
        return
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        & $py.Source -3 $EulaBuilderPath $LicenseSourcePath $LicensePath
        if ($LASTEXITCODE -ne 0) { Die "EULA generator failed with exit code $LASTEXITCODE" }
        return
    }

    Invoke-Checked $UvPath "--native-tls" "run" "python" $EulaBuilderPath $LicenseSourcePath $LicensePath
}

function Clear-StaleSetupArtifacts {
    if (-not (Test-Path $OutputDir)) { return }

    $staleSetups = Get-ChildItem -Path $OutputDir -Filter "AgentShoreSetup-*.exe" -ErrorAction SilentlyContinue
    foreach ($setup in $staleSetups) {
        try {
            Remove-Item -LiteralPath $setup.FullName -Force -ErrorAction Stop
            Write-Info "Removed stale setup artifact: $($setup.Name)"
        } catch {
            Die "Could not remove stale setup artifact $($setup.FullName). Close any open installer windows or security scanner handles, then retry. $($_.Exception.Message)"
        }
    }
}

function Read-TauriVersion {
    $configPath = Join-Path $TauriDir "tauri.conf.json"
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    return [string]$config.version
}

function Get-NewestWheel {
    param([string]$WheelDir)
    $wheel = Get-ChildItem -Path $WheelDir -Filter "agentshore-*-py3-none-any.whl" |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $wheel) {
        Die "uv build did not produce an agentshore wheel under $WheelDir"
    }
    return $wheel.FullName
}

Assert-SigningOptions
if ($SetupSelfSignedCertificateOnly) {
    [void](New-AgentShoreSelfSignedCodeSigningCertificate)
    Write-Step "Self-signed certificate setup complete"
    exit 0
}

$UvPath = Resolve-Uv
Assert-UvVersion $UvPath

Write-Step "Stopping running AgentShore desktop processes"
foreach ($name in @("AgentShore", "agentshore-desktop")) {
    Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

if (-not $SkipDashboard) {
    Write-Step "Building dashboard bridge static"
    Push-Location (Join-Path $RepoRoot "dashboard")
    try { Invoke-Checked "npm" "run" "build" } finally { Pop-Location }

    Write-Step "Building dashboard lib bundle"
    Push-Location (Join-Path $RepoRoot "dashboard")
    try { Invoke-Checked "npm" "run" "build:lib" } finally { Pop-Location }
} else {
    Write-Step "Skipping dashboard build (-SkipDashboard)"
}

Write-Step "Skipping bundled bd sidecar binary"
Write-Info "Windows installer provisions bd during install via the managed sidecar venv."

Write-Step "Building Tauri frontend"
Push-Location $DesktopDir
try { Invoke-Checked "npm" "run" "build:tauri-frontend" } finally { Pop-Location }

Write-Step "Building agentshore Python wheel"
$WheelStageDir = Join-Path $TauriDir "target\agentshore-wheel"
Remove-Item -LiteralPath $WheelStageDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $WheelStageDir | Out-Null
Push-Location $RepoRoot
try { Invoke-Checked $UvPath "--native-tls" "build" "--wheel" "--out-dir" $WheelStageDir } finally { Pop-Location }
$WheelPath = Get-NewestWheel $WheelStageDir
$WheelFileName = Split-Path -Leaf $WheelPath
Write-Info "Wheel: $WheelFileName"

Write-Step "Building Tauri executable ($BuildMode)"
Clear-StaleSetupArtifacts
Push-Location $DesktopDir
try {
    # Windows provisions bd at install time via the managed sidecar venv, so the
    # Tauri build must not try to bundle it as an externalBin. The build.rs guard
    # skips ensure_bd_sidecar() when AGENTSHORE_SKIP_BD_SIDECAR is set.
    $env:AGENTSHORE_SKIP_BD_SIDECAR = "1"
    if ($AllowNoRevocationCheck) {
        # Required on machines with Avast HTTPS scanning (Schannel interception breaks cargo TLS cert revocation).
        # Use -AllowNoRevocationCheck only on such machines.
        $PreviousCargoHttpCheckRevoke = [Environment]::GetEnvironmentVariable("CARGO_HTTP_CHECK_REVOKE", "Process")
        $env:CARGO_HTTP_CHECK_REVOKE = "false"
        Write-Info "Temporarily disabled Cargo Schannel revocation checks for crate downloads (-AllowNoRevocationCheck)."
    }
    if ($DebugBuild) {
        Invoke-Checked "npx" "tauri" "build" "--debug" "--no-bundle" "--config" $WindowsTauriConfig "--" "--locked"
    } else {
        Invoke-Checked "npx" "tauri" "build" "--no-bundle" "--config" $WindowsTauriConfig "--" "--locked"
    }
} finally {
    Remove-Item Env:\AGENTSHORE_SKIP_BD_SIDECAR -ErrorAction SilentlyContinue
    if ($AllowNoRevocationCheck) {
        if ($null -eq $PreviousCargoHttpCheckRevoke) {
            Remove-Item Env:\CARGO_HTTP_CHECK_REVOKE -ErrorAction SilentlyContinue
        } else {
            $env:CARGO_HTTP_CHECK_REVOKE = $PreviousCargoHttpCheckRevoke
        }
    }
    Pop-Location
}

$AppExe = Join-Path $TargetDir "agentshore-desktop.exe"
if (-not (Test-Path $AppExe)) { Die "Tauri build finished but $AppExe does not exist" }

Write-Step "Building Windows provisioner ($BuildMode)"
Push-Location $TauriDir
try {
    # agentshore-provisioner is a [[bin]] in the agentshore-desktop crate, so this
    # cargo build runs that crate's build.rs -> tauri_build::build(), which validates
    # bundle.externalBin. On Windows bd is provisioned at install time (never bundled),
    # so feed tauri_build the same externalBin:[] override the Tauri-exe phase passes
    # via --config; without it the build fails on the missing agentshore-bd sidecar.
    $env:AGENTSHORE_SKIP_BD_SIDECAR = "1"
    $env:TAURI_CONFIG = (Get-Content -Raw -LiteralPath $WindowsTauriConfig)
    if ($AllowNoRevocationCheck) {
        $PreviousCargoHttpCheckRevoke = [Environment]::GetEnvironmentVariable("CARGO_HTTP_CHECK_REVOKE", "Process")
        $env:CARGO_HTTP_CHECK_REVOKE = "false"
    }
    if ($DebugBuild) {
        Invoke-Checked "cargo" "build" "--bin" "agentshore-provisioner" "--locked"
    } else {
        Invoke-Checked "cargo" "build" "--release" "--bin" "agentshore-provisioner" "--locked"
    }
} finally {
    Remove-Item Env:\AGENTSHORE_SKIP_BD_SIDECAR -ErrorAction SilentlyContinue
    Remove-Item Env:\TAURI_CONFIG -ErrorAction SilentlyContinue
    if ($AllowNoRevocationCheck) {
        if ($null -eq $PreviousCargoHttpCheckRevoke) {
            Remove-Item Env:\CARGO_HTTP_CHECK_REVOKE -ErrorAction SilentlyContinue
        } else {
            $env:CARGO_HTTP_CHECK_REVOKE = $PreviousCargoHttpCheckRevoke
        }
    }
    Pop-Location
}
$ProvisionerExe = Join-Path $TargetDir "agentshore-provisioner.exe"
if (-not (Test-Path $ProvisionerExe)) { Die "Provisioner build finished but $ProvisionerExe does not exist" }

if (-not $NoSign) {
    $ResolvedSignTool = Resolve-SignTool
    $ResolvedThumbprint = Resolve-CodeSigningThumbprint
    if ($ResolvedSignTool -and $ResolvedThumbprint) {
        Invoke-AuthenticodeSign -FilePath $AppExe -SignToolPath $ResolvedSignTool -Thumbprint $ResolvedThumbprint
        Invoke-AuthenticodeSign -FilePath $ProvisionerExe -SignToolPath $ResolvedSignTool -Thumbprint $ResolvedThumbprint
    } else {
        if (-not $DebugBuild) {
            Die "Release Windows builds must be Authenticode-signed to reduce SmartScreen/AV heuristics. Install signtool.exe and a current-user code-signing certificate, pass -CertificateThumbprint, or intentionally pass -NoSign for local-only testing."
        }
        Write-Step "Skipping Authenticode signing for debug build"
        if (-not $ResolvedSignTool) { Write-Info "signtool.exe not found." }
        if (-not $ResolvedThumbprint) { Write-Info "No current-user code-signing certificate with a private key was found." }
    }
} else {
    Write-Step "Skipping Authenticode signing (-NoSign)"
}

Write-Step "Staging installer payload"
Remove-Item -LiteralPath $StageDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $AppStageDir | Out-Null
New-Item -ItemType Directory -Path $InstallerStageDir | Out-Null
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

Copy-Item -LiteralPath $AppExe -Destination (Join-Path $AppStageDir "agentshore-desktop.exe")
Copy-Item -LiteralPath $WheelPath -Destination (Join-Path $InstallerStageDir $WheelFileName)
Copy-Item -LiteralPath $ProvisionerExe -Destination (Join-Path $InstallerStageDir "agentshore-provisioner.exe")
Copy-Item -LiteralPath $UvPath -Destination (Join-Path $InstallerStageDir "uv.exe")

Invoke-EulaGenerator

Write-Step "Compiling Inno Setup installer"
$IsccPath = Resolve-Iscc
$Version = Read-TauriVersion
$IssOut = Join-Path $StageDir "AgentShore.iss"
Copy-Item -LiteralPath $TemplatePath -Destination $IssOut

$isccArgs = @(
    "/DAppVersion=$Version",
    "/DStageDir=$StageDir",
    "/DOutputDir=$OutputDir",
    "/DWheelFileName=$WheelFileName",
    "/DUvFileName=uv.exe",
    "/DProvisionerFileName=agentshore-provisioner.exe",
    "/DLicenseFile=$LicensePath",
    "/DIconFile=$IconPath",
    $IssOut
)
Invoke-Checked $IsccPath @isccArgs

$SetupOut = Join-Path $OutputDir "AgentShoreSetup-$Version-x64.exe"
if (-not (Test-Path $SetupOut)) {
    Die "Inno Setup completed but expected installer is missing: $SetupOut"
}

if (-not $NoSign -and $ResolvedSignTool -and $ResolvedThumbprint) {
    Invoke-AuthenticodeSign -FilePath $SetupOut -SignToolPath $ResolvedSignTool -Thumbprint $ResolvedThumbprint
}

Write-Step "Build complete"
Write-Info "Installer: $SetupOut"

if ($Install) {
    Write-Step "Launching installer"
    Start-Process -FilePath $SetupOut -Wait
}
