<#
.SYNOPSIS
    Read-only preflight: report machine states that conflict with kernel
    anti-cheat, and how to revert each one.

.DESCRIPTION
    Checks this machine WITHOUT changing anything:

      1. Secure Boot state (Confirm-SecureBootUEFI).
      2. Test-signing boot mode (bcdedit /enum {current}; needs an elevated
         PowerShell - reported as unknown otherwise).
      3. The DisableProtectedAudioDG value under
         HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Audio.
      4. Whether the Discrod capture APO CLSID is attached to any capture
         endpoint (MMDevices FxProperties scan).
      5. Which kernel anti-cheat products are present (Riot Vanguard,
         Easy Anti-Cheat, BattlEye, FACEIT).

    Run it BEFORE installing the dev (unsigned) driver or APO transport, and
    AFTER uninstalling one to confirm the machine is back in an
    anti-cheat-safe state. Every check degrades gracefully: a state that
    cannot be read (not elevated, legacy BIOS) is reported as unknown, never
    as a risk.

    Why these states matter (as of Aug 2026 - anti-cheat vendor behavior
    changes over time; check the vendor's current support pages):
      * Riot Vanguard refuses to launch protected games while Windows
        test-signing mode is enabled and, on Windows 11, requires Secure Boot
        + TPM 2.0 (errors VAN9001/VAN9003). That is a launch block /
        integrity refusal, not a cheating ban, and it clears once the state
        is reverted. Easy Anti-Cheat, BattlEye and FACEIT are similarly
        hostile to test-signing mode.
      * DisableProtectedAudioDG=1 lowers protection of the audiodg.exe
        protected process system-wide. Its interaction with anti-cheat is not
        publicly documented; avoid it on a machine that runs kernel
        anti-cheat.
      * A properly signed driver or APO is ordinary audio software (same
        category as VB-CABLE or NVIDIA Broadcast) and is not an anti-cheat
        concern. The signed transports in ALTERNATIVES.md need none of the
        states above.

    Exit codes: 0 = no risky state found, 2 = one or more risky states found.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

# Must match apo/src/apo_guids.h and the apo scripts.
$ApoClsid    = "{7C9D5A24-6B3E-4C1F-9A2D-3E8F1B0C6A50}"
$CaptureRoot = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
$AudioKey    = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Audio"

function Test-AntiCheatPresent {
    # Returns the names of kernel anti-cheat products found on this machine
    # (empty when none). Best-effort: known Windows services plus the Riot
    # Vanguard install folder.
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

function Get-EndpointName([string]$endpointKey) {
    $props = Join-Path $endpointKey "Properties"
    # PKEY_Device_FriendlyName
    $nameVal = "{a45c254e-df1c-4efd-8020-67d146a850e0},2"
    try {
        $p = Get-ItemProperty -Path $props -ErrorAction Stop
        if ($p.$nameVal) { return [string]$p.$nameVal }
    } catch {}
    return (Split-Path $endpointKey -Leaf)
}

function Write-State([string]$label, [string]$value, [string]$color) {
    Write-Host ("  {0,-26} {1}" -f ($label + ":"), $value) -ForegroundColor $color
}

$risks   = @()   # each entry: @{ Risk = <one-line description>; Revert = <exact command> }
$unknown = @()

Write-Host "== Discrod anti-cheat preflight (read-only) ==" -ForegroundColor Cyan
Write-Host ""

# --- 1. Secure Boot ----------------------------------------------------------
try {
    if (Confirm-SecureBootUEFI) {
        Write-State "Secure Boot" "ON" Green
    } else {
        Write-State "Secure Boot" "OFF" Red
        $risks += @{
            Risk   = "Secure Boot is OFF. Riot Vanguard on Windows 11 requires Secure Boot + TPM 2.0 and refuses to launch protected games without it (VAN9001/VAN9003)."
            Revert = "Re-enable Secure Boot in the UEFI firmware settings (no script can do this for you)."
        }
    }
} catch {
    if ($_.Exception -is [System.PlatformNotSupportedException]) {
        Write-State "Secure Boot" "not supported (legacy BIOS, no UEFI)" Yellow
    } else {
        Write-State "Secure Boot" "unknown (Confirm-SecureBootUEFI failed; usually needs an elevated PowerShell)" Yellow
        $unknown += "Secure Boot"
    }
}

# --- 2. Test-signing boot mode -----------------------------------------------
$testSigning = "unknown"
$testSigningNote = "bcdedit needs an elevated PowerShell"
try {
    $bcd = bcdedit /enum "{current}" 2>$null | Out-String
    if ($LASTEXITCODE -eq 0 -and $bcd) {
        # Match the value token rather than a literal "Yes": bcdedit localizes
        # Yes/No on non-English Windows, and mis-reading a localized "Yes" as
        # "off" would be a false all-clear. The element name is never localized;
        # an absent testsigning line means the element is unset (off).
        if ($bcd -match "(?m)^testsigning\s+(\S+)") {
            if ($Matches[1] -eq "Yes") { $testSigning = "on" }
            elseif ($Matches[1] -eq "No") { $testSigning = "off" }
            else { $testSigningNote = "bcdedit reported '$($Matches[1])' (localized output) - verify manually: bcdedit /enum {current}" }
        } else {
            $testSigning = "off"
        }
    }
} catch {}
if ($testSigning -eq "on") {
    Write-State "Test-signing boot mode" "ON" Red
    $risks += @{
        Risk   = "Test-signing boot mode is ON. Vanguard-protected games refuse to launch (VAN9001/VAN9003) while it is enabled; Easy Anti-Cheat, BattlEye and FACEIT are similarly hostile to it."
        Revert = "driver\scripts\uninstall-driver.ps1 -DisableTestSigning   (elevated; takes effect after a reboot)"
    }
} elseif ($testSigning -eq "off") {
    Write-State "Test-signing boot mode" "off" Green
} else {
    Write-State "Test-signing boot mode" "unknown ($testSigningNote)" Yellow
    $unknown += "test-signing boot mode"
}

# --- 3. DisableProtectedAudioDG ----------------------------------------------
$dpadg = $null
try {
    $dpadg = (Get-ItemProperty -Path $AudioKey -Name "DisableProtectedAudioDG" -ErrorAction Stop).DisableProtectedAudioDG
} catch { $dpadg = $null }
if ($null -eq $dpadg) {
    Write-State "DisableProtectedAudioDG" "not set (protected audio intact)" Green
} elseif ($dpadg -eq 1) {
    Write-State "DisableProtectedAudioDG" "1 (audiodg protection lowered)" Red
    $risks += @{
        Risk   = "DisableProtectedAudioDG=1 lowers protection of the audiodg.exe protected process system-wide. Its interaction with anti-cheat is not publicly documented - avoid it on a machine running kernel anti-cheat."
        Revert = "apo\scripts\unregister-apo.ps1 -ReenableProtectedAudio   (elevated)"
    }
} else {
    Write-State "DisableProtectedAudioDG" "$dpadg (unexpected value; only 1 lowers audiodg protection)" Yellow
}

# --- 4. Discrod APO attachment -----------------------------------------------
$apoAttached = @()
try {
    if (Test-Path $CaptureRoot) {
        foreach ($ep in (Get-ChildItem $CaptureRoot -ErrorAction SilentlyContinue)) {
            $fx = Join-Path $ep.PSPath "FxProperties"
            if (-not (Test-Path $fx)) { continue }
            $props = Get-ItemProperty -Path $fx -ErrorAction SilentlyContinue
            if (-not $props) { continue }
            foreach ($p in $props.PSObject.Properties) {
                if ($p.Value -is [string] -and $p.Value -eq $ApoClsid) {
                    $apoAttached += (Get-EndpointName $ep.PSPath)
                    break
                }
            }
        }
    }
} catch {}
if ($apoAttached.Count -gt 0) {
    Write-State "Discrod APO attached" (($apoAttached -join ", ") + "  (info only - fine with a signed DLL; detach with apo\scripts\unregister-apo.ps1)") Yellow
} else {
    Write-State "Discrod APO attached" "no" Green
}

# --- 5. Kernel anti-cheat presence -------------------------------------------
$antiCheats = @(Test-AntiCheatPresent)
if ($antiCheats.Count -gt 0) {
    Write-State "Kernel anti-cheat" ($antiCheats -join ", ") Cyan
} else {
    Write-State "Kernel anti-cheat" "none detected" Green
}

# --- verdict -----------------------------------------------------------------
Write-Host ""
Write-Host "== Verdict ==" -ForegroundColor Cyan
if ($risks.Count -eq 0) {
    Write-Host "No risky machine state found." -ForegroundColor Green
    if ($unknown.Count -gt 0) {
        Write-Host ("Not verified: " + ($unknown -join ", ") + ". Rerun from an elevated PowerShell for a complete report.") -ForegroundColor Yellow
    }
    exit 0
}

if ($antiCheats.Count -gt 0) {
    Write-Host ("Anti-cheat present (" + ($antiCheats -join ", ") + ") and risky machine states found:") -ForegroundColor Red
} else {
    Write-Host "No kernel anti-cheat detected, but risky states found - they will block anti-cheat-protected games if one is installed later:" -ForegroundColor Yellow
}
foreach ($r in $risks) {
    Write-Host ""
    Write-Host ("  RISK:   " + $r.Risk) -ForegroundColor Red
    Write-Host ("  REVERT: " + $r.Revert) -ForegroundColor Yellow
}
Write-Host ""
Write-Host "The signed transports in ALTERNATIVES.md need none of these states."
Write-Host "(Anti-cheat behavior as of Aug 2026 - see the vendor's support pages for current requirements.)"
if ($unknown.Count -gt 0) {
    Write-Host ("Not verified: " + ($unknown -join ", ") + ". Rerun from an elevated PowerShell for a complete report.") -ForegroundColor Yellow
}
exit 2
