<#
.SYNOPSIS
    Build (optionally), test-sign, and install the Discrod Virtual Cable driver.

.DESCRIPTION
    Automates the development deployment loop on a Windows test machine:
      1. (-Build) msbuild the driver package.
      2. Create a self-signed test certificate on first run and trust it
         (LocalMachine Root + TrustedPublisher).
      3. Sign DiscrodAudio.sys, regenerate + sign the catalog.
      4. Verify test-signing boot mode (enables it and asks for a reboot if off).
      5. Stage the package with pnputil and create the root-enumerated device
         node (devgen or devcon, whichever the WDK put on this machine).

    Run from an ELEVATED PowerShell on the test machine. Production/distribution
    signing (EV cert + Microsoft attestation) is out of scope — see DEPLOY.md.

    !! ANTI-CHEAT WARNING !!
    This dev install turns on test-signing boot mode and requires Secure Boot
    to be off. Vanguard-protected games REFUSE TO LAUNCH in that state
    (VAN9001/VAN9003) until it is reverted; Easy Anti-Cheat, BattlEye and
    FACEIT are similarly hostile to test-signing (as of Aug 2026 - check the
    vendor's support pages). It is a launch block, not a cheating ban. Revert
    with uninstall-driver.ps1 -DisableTestSigning (then reboot) and re-enable
    Secure Boot in firmware. When a kernel anti-cheat is detected this script
    stops unless -AcknowledgeAntiCheatRisk is passed. On a gaming machine
    prefer a signed transport - see ALTERNATIVES.md, and run
    scripts\check-anticheat.ps1 for a status report.

.PARAMETER Build
    Run msbuild first (requires a Developer PowerShell / vcvars environment).

.PARAMETER Configuration
    Build configuration folder to install from. Default: Debug.

.PARAMETER AcknowledgeAntiCheatRisk
    Proceed even though a kernel anti-cheat (Riot Vanguard, Easy Anti-Cheat,
    BattlEye, FACEIT) was detected. Vanguard-protected games will refuse to
    launch until the test-signing / Secure Boot state is reverted.
#>
[CmdletBinding()]
param(
    [switch]$Build,
    [ValidateSet("Debug", "Release")]
    [string]$Configuration = "Debug",
    [switch]$AcknowledgeAntiCheatRisk
)

$ErrorActionPreference = "Stop"
$driverRoot = Split-Path -Parent $PSScriptRoot           # driver/
$packageDir = Join-Path $driverRoot "x64\$Configuration\DiscrodAudio"
$certSubject = "CN=DiscrodTestCert"
$timestampUrl = "http://timestamp.digicert.com"

function Assert-Admin {
    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated (Administrator) PowerShell."
    }
}

function Find-KitTool([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $kits = @("${env:ProgramFiles(x86)}\Windows Kits\10", "$env:ProgramFiles\Windows Kits\10")
    foreach ($kit in $kits) {
        if (-not (Test-Path $kit)) { continue }
        $hit = Get-ChildItem -Path (Join-Path $kit "bin"), (Join-Path $kit "Tools") `
            -Recurse -Filter "$name.exe" -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match "x64" } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($hit) { return $hit.FullName }
    }
    return $null
}

function Test-AntiCheatPresent {
    # Returns the names of kernel anti-cheat products found on this machine
    # (empty when none). Best-effort: known Windows services plus the Riot
    # Vanguard install folder. Keep in sync with scripts\check-anticheat.ps1.
    $services = @{
        "vgc"               = "Riot Vanguard"
        "vgk"               = "Riot Vanguard"
        "EasyAntiCheat"     = "Easy Anti-Cheat"
        "EasyAntiCheat_EOS" = "Easy Anti-Cheat"
        "BEService"         = "BattlEye"
        "FACEIT"            = "FACEIT Anti-Cheat"
    }
    $found = @()
    foreach ($name in $services.Keys) {
        if (Get-Service -Name $name -ErrorAction SilentlyContinue) {
            $found += $services[$name]
        }
    }
    if (Test-Path "$env:SystemDrive\Program Files\Riot Vanguard") {
        $found += "Riot Vanguard"
    }
    return ($found | Sort-Object -Unique)
}

Assert-Admin

# --- anti-cheat guard --------------------------------------------------------
# Runs before anything is built, trusted, signed, or installed. If test-signing
# is already active the damage is done, so warn instead of blocking.
$antiCheats = @(Test-AntiCheatPresent)
if ($antiCheats.Count -gt 0) {
    $bcdNow = bcdedit /enum "{current}" | Out-String
    if ($bcdNow -match "testsigning\s+Yes") {
        Write-Warning ("Kernel anti-cheat detected (" + ($antiCheats -join ", ") + ") and " +
            "test-signing is ALREADY on: Vanguard-protected games refuse to launch " +
            "(VAN9001/VAN9003) until you revert - uninstall-driver.ps1 -DisableTestSigning, " +
            "reboot, and re-enable Secure Boot in firmware. Continuing.")
    } elseif (-not $AcknowledgeAntiCheatRisk) {
        throw (
            "Kernel anti-cheat detected: " + ($antiCheats -join ", ") + ". This dev " +
            "install enables test-signing boot mode (and needs Secure Boot off), and " +
            "Vanguard-protected games will REFUSE TO LAUNCH (VAN9001/VAN9003) while the " +
            "machine is in that state; Easy Anti-Cheat, BattlEye and FACEIT are similarly " +
            "hostile to test-signing (as of Aug 2026 - check the vendor's support pages). " +
            "This is a launch block, not a cheating ban. Revert path: " +
            "uninstall-driver.ps1 -DisableTestSigning (then reboot) and re-enable Secure " +
            "Boot in firmware. On a gaming machine use a signed transport instead - see " +
            "ALTERNATIVES.md (zero system changes) and run scripts\check-anticheat.ps1 " +
            "for a status report. Pass -AcknowledgeAntiCheatRisk to proceed anyway."
        )
    } else {
        Write-Warning ("Kernel anti-cheat detected (" + ($antiCheats -join ", ") + ") - " +
            "proceeding per -AcknowledgeAntiCheatRisk. Revert later with " +
            "uninstall-driver.ps1 -DisableTestSigning (then reboot) and re-enable " +
            "Secure Boot in firmware.")
    }
}

if ($Build) {
    Write-Host "== Building driver ($Configuration|x64)" -ForegroundColor Cyan
    $msbuild = Find-KitTool "msbuild"
    if (-not $msbuild) { $msbuild = "msbuild" }   # present in Developer PowerShell
    & $msbuild (Join-Path $driverRoot "DiscrodAudio.vcxproj") `
        /p:Configuration=$Configuration /p:Platform=x64 /nologo
    if ($LASTEXITCODE -ne 0) { throw "msbuild failed ($LASTEXITCODE)" }
}

$sysFile = Join-Path $packageDir "DiscrodAudio.sys"
$infFile = Join-Path $packageDir "DiscrodAudio.inf"
if (-not (Test-Path $sysFile)) {
    throw "Driver package not found at $packageDir — build first (rerun with -Build from a Developer PowerShell)."
}
if (-not (Test-Path $infFile)) {
    Copy-Item (Join-Path $driverRoot "DiscrodAudio.inf") $infFile
}

# --- test certificate --------------------------------------------------------
Write-Host "== Test certificate" -ForegroundColor Cyan
$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -eq $certSubject } | Select-Object -First 1
if (-not $cert) {
    Write-Host "Creating self-signed test certificate $certSubject"
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $certSubject `
        -CertStoreLocation Cert:\CurrentUser\My -NotAfter (Get-Date).AddYears(5)
}
$cerPath = Join-Path $env:TEMP "DiscrodTestCert.cer"
Export-Certificate -Cert $cert -FilePath $cerPath | Out-Null
foreach ($store in "Cert:\LocalMachine\Root", "Cert:\LocalMachine\TrustedPublisher") {
    Import-Certificate -FilePath $cerPath -CertStoreLocation $store | Out-Null
}
Write-Host "Certificate trusted (thumbprint $($cert.Thumbprint))"

# --- sign --------------------------------------------------------------------
Write-Host "== Signing driver + catalog" -ForegroundColor Cyan
$signtool = Find-KitTool "signtool"
$inf2cat = Find-KitTool "inf2cat"
if (-not $signtool) { throw "signtool.exe not found — install the Windows SDK." }
if (-not $inf2cat) { throw "inf2cat.exe not found — install the WDK." }

& $signtool sign /fd sha256 /sha1 $cert.Thumbprint /tr $timestampUrl /td sha256 $sysFile
if ($LASTEXITCODE -ne 0) { throw "signtool failed on DiscrodAudio.sys" }
& $inf2cat /driver:$packageDir /os:10_X64
if ($LASTEXITCODE -ne 0) { throw "inf2cat failed" }
$catFile = Join-Path $packageDir "DiscrodAudio.cat"
& $signtool sign /fd sha256 /sha1 $cert.Thumbprint /tr $timestampUrl /td sha256 $catFile
if ($LASTEXITCODE -ne 0) { throw "signtool failed on DiscrodAudio.cat" }

# --- test-signing boot mode --------------------------------------------------
Write-Host "== Checking test-signing boot mode" -ForegroundColor Cyan
$bcd = bcdedit /enum "{current}" | Out-String
if ($bcd -notmatch "testsigning\s+Yes") {
    Write-Host "Enabling test-signing (required for self-signed kernel drivers)..."
    bcdedit /set testsigning on
    if ($LASTEXITCODE -ne 0) {
        throw ("bcdedit failed — Secure Boot is probably enabled. " +
               "Disable Secure Boot in firmware settings, then rerun this script.")
    }
    Write-Warning "Test-signing enabled. REBOOT, then rerun this script to install."
    exit 0
}
Write-Host "Test-signing is active."

# --- install -----------------------------------------------------------------
Write-Host "== Installing driver package" -ForegroundColor Cyan
pnputil /add-driver $infFile /install
if ($LASTEXITCODE -ne 0) { throw "pnputil /add-driver failed ($LASTEXITCODE)" }

# Root-enumerated software device: the package alone creates no devnode.
$existing = pnputil /enum-devices /class MEDIA 2>$null | Out-String
if ($existing -match "root\\discrodaudio") {
    Write-Host "Device node already present."
} else {
    $devgen = Find-KitTool "devgen"
    $devcon = Find-KitTool "devcon"
    if ($devgen) {
        & $devgen /add /instanceid 0 /hardwareid "root\DiscrodAudio"
        if ($LASTEXITCODE -ne 0) { throw "devgen failed ($LASTEXITCODE)" }
    } elseif ($devcon) {
        & $devcon install $infFile "root\DiscrodAudio"
        if ($LASTEXITCODE -ne 0) { throw "devcon failed ($LASTEXITCODE)" }
    } else {
        throw ("Neither devgen nor devcon found. Install the WDK tools, or create " +
               "the device once via Device Manager: Action > Add legacy hardware > " +
               "manual > Sound... > Have Disk > $infFile")
    }
}

Write-Host ""
Write-Host "Done. Check Sound settings for:" -ForegroundColor Green
Write-Host "  * Discrod Virtual Cable (Speakers)    <- select as output in the Discrod app"
Write-Host "  * Discrod Virtual Cable (Microphone)  <- select as input in Discord"
Write-Host "If endpoints are missing, see DEPLOY.md troubleshooting."
