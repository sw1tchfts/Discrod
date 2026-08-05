<#
.SYNOPSIS
    Remove the Discrod capture APO and restore endpoint effects.

.DESCRIPTION
    Reverses register-apo.ps1:
      1. Restores each capture endpoint's FxProperties from the backups written
         at registration time (or, with -PurgeClsidOnly, just deletes our CLSID
         values while leaving everything else untouched).
      2. Unregisters the COM server (regsvr32 /u).
      3. Optionally re-enables protected audio (removes DisableProtectedAudioDG).
      4. Restarts the audio service.

    Run from an ELEVATED PowerShell.

.PARAMETER PurgeClsidOnly
    Skip restoring from backup; instead scan all capture endpoints and delete
    any FxProperties value that points at the Discrod APO CLSID. Use this if the
    backups are gone or you attached to several endpoints.

.PARAMETER ReenableProtectedAudio
    Also remove the DisableProtectedAudioDG override set at registration.
#>
[CmdletBinding()]
param(
    [switch]$PurgeClsidOnly,
    [switch]$ReenableProtectedAudio
)

$ErrorActionPreference = "Stop"

$ApoClsid = "{7C9D5A24-6B3E-4C1F-9A2D-3E8F1B0C6A50}"
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
    $root = Split-Path -Parent $PSScriptRoot
    foreach ($c in @(
        (Join-Path $root "build\Release\DiscrodApo.dll"),
        (Join-Path $root "build\DiscrodApo.dll"),
        (Join-Path $root "x64\Release\DiscrodApo.dll"))) {
        if (Test-Path $c) { return (Resolve-Path $c).Path }
    }
    return $null
}

Assert-Admin

if ($PurgeClsidOnly) {
    Write-Host "== Purging Discrod APO CLSID from all capture endpoints" -ForegroundColor Cyan
    Get-ChildItem $CaptureRoot | ForEach-Object {
        $fx = Join-Path $_.PSPath "FxProperties"
        if (-not (Test-Path $fx)) { return }
        $props = Get-ItemProperty $fx
        foreach ($p in $props.PSObject.Properties) {
            if ($p.Value -is [string] -and $p.Value -eq $ApoClsid) {
                Remove-ItemProperty -Path $fx -Name $p.Name -ErrorAction SilentlyContinue
                Write-Host "  removed $($p.Name) from $(Split-Path $_.PSPath -Leaf)"
            }
        }
    }
} else {
    Write-Host "== Restoring FxProperties from backups in $BackupDir" -ForegroundColor Cyan
    if (Test-Path $BackupDir) {
        Get-ChildItem $BackupDir -Filter *.reg | ForEach-Object {
            & reg.exe import $_.FullName | Out-Null
            Write-Host "  restored $($_.BaseName)"
        }
    } else {
        Write-Warning "No backups found. Re-run with -PurgeClsidOnly to strip the CLSID directly."
    }
}

Write-Host "== Unregistering COM server" -ForegroundColor Cyan
$dll = Resolve-Dll
if ($dll) {
    & regsvr32.exe /u /s $dll
    Write-Host "  unregistered $dll"
} else {
    Write-Warning "DiscrodApo.dll not found; skipping regsvr32 /u (COM keys may remain)."
}

if ($ReenableProtectedAudio) {
    Write-Host "== Re-enabling protected audio" -ForegroundColor Cyan
    Remove-ItemProperty -Path $AudioKey -Name "DisableProtectedAudioDG" -ErrorAction SilentlyContinue
}

Write-Host "== Restarting audio service" -ForegroundColor Cyan
Restart-Service -Name Audiosrv -Force
Write-Host "Done." -ForegroundColor Green
