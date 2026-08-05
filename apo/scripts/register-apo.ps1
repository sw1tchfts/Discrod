<#
.SYNOPSIS
    Register the Discrod capture APO and attach it to a microphone endpoint.

.DESCRIPTION
    Driver-free transport for Discrod: the capture APO runs inside Windows'
    audio engine (audiodg.exe) on your REAL microphone, applies the mic DSP
    chain, and mixes soundboard audio the app streams over shared memory. Discord
    then just selects that same microphone.

    Steps performed (run from an ELEVATED PowerShell):
      1. regsvr32 the APO DLL (writes the COM InprocServer32 + APO registration).
      2. Enable DisableProtectedAudioDG so audiodg can load a test-signed /
         self-built APO. audiodg is a protected process and will NOT load an
         unsigned DLL otherwise. (Production: properly sign the DLL and skip
         this — see the SECURITY NOTE below.)
      3. Let you pick a capture (recording) endpoint.
      4. Back up that endpoint's FxProperties, then write the APO's CLSID into
         the mode-/endpoint-effect properties so the audio engine loads it.
      5. Restart the audio service so the change takes effect.

    !! IMPORTANT — UNSUPPORTED / VERIFY ON TARGET !!
    Injecting a custom APO into an endpoint whose INF you do not own is not an
    officially supported Windows mechanism. The COM registration (step 1),
    DisableProtectedAudioDG (step 2), and backup/restore (step 4) are
    well-understood; the exact FxProperties property keys the in-box APO proxy
    honors have varied across Windows builds. This script writes the documented
    PKEY_FX_*EffectClsid keys and ALWAYS backs up the prior values so
    unregister-apo.ps1 can restore them. If the APO does not load, restore and
    fall back to the signed virtual-cable driver (see DEPLOY.md / ALTERNATIVES.md).

    SECURITY NOTE: DisableProtectedAudioDG lowers audiodg's protection for the
    whole system. It is appropriate for a developer/test machine. On a machine
    you care about, sign the APO with a trusted certificate and leave protected
    audio enabled instead.

.PARAMETER DllPath
    Path to DiscrodApo.dll. Defaults to a Release build next to this repo.

.PARAMETER Effect
    Which effect slot to attach: Mode (default) or Endpoint. Mode effects run
    per processing-mode on the captured stream; Endpoint effects run last.

.PARAMETER NoProtectedAudioOverride
    Skip setting DisableProtectedAudioDG (use when the DLL is properly signed).
#>
[CmdletBinding()]
param(
    [string]$DllPath,
    [ValidateSet("Mode", "Endpoint")]
    [string]$Effect = "Mode",
    [switch]$NoProtectedAudioOverride
)

$ErrorActionPreference = "Stop"

# --- APO identity (must match apo/src/apo_guids.h) ---------------------------
$ApoClsid = "{7C9D5A24-6B3E-4C1F-9A2D-3E8F1B0C6A50}"

# FxProperties property keys the audio-engine APO proxy reads. Format is
# "{fmtid},pid". These are the documented composite-effect CLSID slots.
$PKEY_FX_ModeEffectClsid     = "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},7"
$PKEY_FX_EndpointEffectClsid = "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},8"

$CaptureRoot = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
$AudioKey    = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Audio"
$BackupDir   = Join-Path $env:ProgramData "Discrod\apo-backup"

function Assert-Admin {
    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated (Administrator) PowerShell."
    }
}

function Resolve-Dll {
    if ($DllPath) {
        if (-not (Test-Path $DllPath)) { throw "DLL not found: $DllPath" }
        return (Resolve-Path $DllPath).Path
    }
    $root = Split-Path -Parent $PSScriptRoot   # apo/
    $candidates = @(
        (Join-Path $root "build\Release\DiscrodApo.dll"),
        (Join-Path $root "build\DiscrodApo.dll"),
        (Join-Path $root "x64\Release\DiscrodApo.dll")
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return (Resolve-Path $c).Path } }
    throw ("DiscrodApo.dll not found. Build the APO (see apo/APO.md) or pass -DllPath. " +
           "Looked in: " + ($candidates -join "; "))
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

Assert-Admin
$dll = Resolve-Dll
Write-Host "== Registering COM server: $dll" -ForegroundColor Cyan
& regsvr32.exe /s $dll
if ($LASTEXITCODE -ne 0) { throw "regsvr32 failed ($LASTEXITCODE)" }
Write-Host "APO COM object registered ($ApoClsid)."

if (-not $NoProtectedAudioOverride) {
    Write-Host "== Enabling DisableProtectedAudioDG (test/dev)" -ForegroundColor Cyan
    New-ItemProperty -Path $AudioKey -Name "DisableProtectedAudioDG" `
        -Value 1 -PropertyType DWord -Force | Out-Null
    Write-Warning "Protected audio DG disabled system-wide. Sign the DLL and re-run with -NoProtectedAudioOverride on a non-test machine."
}

# --- pick an endpoint --------------------------------------------------------
if (-not (Test-Path $CaptureRoot)) { throw "No capture endpoints found in registry." }
$endpoints = Get-ChildItem $CaptureRoot | ForEach-Object {
    $state = (Get-ItemProperty $_.PSPath -Name "DeviceState" -ErrorAction SilentlyContinue).DeviceState
    [pscustomobject]@{ Key = $_.PSPath; Name = (Get-EndpointName $_.PSPath); State = $state }
}
# DeviceState 1 = active.
$active = $endpoints | Where-Object { $_.State -eq 1 }
if (-not $active) { $active = $endpoints }

Write-Host ""
Write-Host "Available capture endpoints:" -ForegroundColor Cyan
for ($i = 0; $i -lt $active.Count; $i++) {
    Write-Host ("  [{0}] {1}" -f $i, $active[$i].Name)
}
$sel = Read-Host "Select the microphone to attach the APO to (number)"
if ($sel -notmatch '^\d+$' -or [int]$sel -ge $active.Count) { throw "Invalid selection." }
$target = $active[[int]$sel]
Write-Host "Attaching to: $($target.Name)" -ForegroundColor Green

# --- back up + write FxProperties -------------------------------------------
$fx = Join-Path $target.Key "FxProperties"
if (-not (Test-Path $fx)) { New-Item -Path $fx -Force | Out-Null }

New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
$endpointId = Split-Path $target.Key -Leaf
$backupFile = Join-Path $BackupDir "$endpointId.reg"
$regKeyPath = $fx -replace '^HKLM:', 'HKEY_LOCAL_MACHINE'
& reg.exe export $regKeyPath $backupFile /y | Out-Null
Write-Host "Backed up existing FxProperties -> $backupFile"

$pkey = if ($Effect -eq "Endpoint") { $PKEY_FX_EndpointEffectClsid } else { $PKEY_FX_ModeEffectClsid }
New-ItemProperty -Path $fx -Name $pkey -Value $ApoClsid -PropertyType String -Force | Out-Null
Write-Host "Wrote $Effect-effect CLSID to $pkey"

# --- restart audio -----------------------------------------------------------
Write-Host "== Restarting audio service" -ForegroundColor Cyan
Restart-Service -Name Audiosrv -Force
Write-Host ""
Write-Host "Done." -ForegroundColor Green
Write-Host "  1. Start the Discrod app and tick 'APO (no driver)', then Start."
Write-Host "  2. In Discord, select the microphone you attached: $($target.Name)"
Write-Host "  3. If the mic goes silent or the APO does not load, run"
Write-Host "     unregister-apo.ps1 to restore, and see ALTERNATIVES.md."
