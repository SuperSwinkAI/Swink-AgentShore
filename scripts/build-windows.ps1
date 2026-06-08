<#
.SYNOPSIS
    Build the AgentShore Windows desktop installer.

.DESCRIPTION
    Windows parity for scripts/build-macos.sh. Produces a user-level Inno Setup
    wizard installer for the Tauri desktop shell plus the managed Python
    sidecar venv, with the same deliberate component choices as the macOS pkg:

      - AgentShore Desktop: required.
      - Timelapse Capture: optional, unchecked by default.
      - AgentShore CLI: optional, checked by default.

    The installer is per-user and does not require elevation. The desktop app
    installs under %LocalAppData%\Programs\AgentShore, and the managed sidecar
    venv is provisioned under %LocalAppData%\AgentShore\venv, matching the
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
    [string]$SignTool = "",
    [string]$CertificateThumbprint = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
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

function Resolve-CodeSigningThumbprint {
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

    Invoke-Checked "uv" "--native-tls" "run" "python" $EulaBuilderPath $LicenseSourcePath $LicensePath
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
try { Invoke-Checked "uv" "--native-tls" "build" "--wheel" "--out-dir" $WheelStageDir } finally { Pop-Location }
$WheelPath = Get-NewestWheel $WheelStageDir
$WheelFileName = Split-Path -Leaf $WheelPath
Write-Info "Wheel: $WheelFileName"

Write-Step "Building Tauri executable ($BuildMode)"
Clear-StaleSetupArtifacts
Push-Location $DesktopDir
try {
    $PreviousCargoHttpCheckRevoke = [Environment]::GetEnvironmentVariable("CARGO_HTTP_CHECK_REVOKE", "Process")
    $env:AGENTSHORE_SKIP_BD_SIDECAR = "1"
    $env:CARGO_HTTP_CHECK_REVOKE = "false"
    Write-Info "Temporarily disabled Cargo Schannel revocation checks for crate downloads."
    if ($DebugBuild) {
        Invoke-Checked "npx" "tauri" "build" "--debug" "--no-bundle" "--config" $WindowsTauriConfig "--" "--locked"
    } else {
        Invoke-Checked "npx" "tauri" "build" "--no-bundle" "--config" $WindowsTauriConfig "--" "--locked"
    }
} finally {
    Remove-Item Env:\AGENTSHORE_SKIP_BD_SIDECAR -ErrorAction SilentlyContinue
    if ($null -eq $PreviousCargoHttpCheckRevoke) {
        Remove-Item Env:\CARGO_HTTP_CHECK_REVOKE -ErrorAction SilentlyContinue
    } else {
        $env:CARGO_HTTP_CHECK_REVOKE = $PreviousCargoHttpCheckRevoke
    }
    Pop-Location
}

$AppExe = Join-Path $TargetDir "agentshore-desktop.exe"
if (-not (Test-Path $AppExe)) { Die "Tauri build finished but $AppExe does not exist" }

if (-not $NoSign) {
    $ResolvedSignTool = Resolve-SignTool
    $ResolvedThumbprint = Resolve-CodeSigningThumbprint
    if ($ResolvedSignTool -and $ResolvedThumbprint) {
        Invoke-AuthenticodeSign -FilePath $AppExe -SignToolPath $ResolvedSignTool -Thumbprint $ResolvedThumbprint
    } else {
        Write-Step "Skipping Authenticode signing"
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
Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\install-agentshore-venv.ps1") -Destination $InstallerStageDir
Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\install-agentshore-cli.ps1") -Destination $InstallerStageDir
Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\install-timelapse.ps1") -Destination $InstallerStageDir
Copy-Item -LiteralPath (Join-Path $RepoRoot "scripts\run-windows-installer-step.ps1") -Destination $InstallerStageDir

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
