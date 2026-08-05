<#
.SYNOPSIS
    Remove the Discrod Virtual Cable device and driver package.

.DESCRIPTION
    Deletes the root-enumerated device node, removes the staged driver package
    from the driver store, and optionally turns test-signing back off.
    Run from an ELEVATED PowerShell.

.PARAMETER DisableTestSigning
    Also run `bcdedit /set testsigning off` (takes effect after reboot).
#>
[CmdletBinding()]
param(
    [switch]$DisableTestSigning
)

$ErrorActionPreference = "Stop"

$principal = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell."
}

# --- remove the device node --------------------------------------------------
Write-Host "== Removing device node" -ForegroundColor Cyan
$instanceIds = pnputil /enum-devices /class MEDIA 2>$null | Out-String |
    Select-String -Pattern "Instance ID:\s+(ROOT\\DISCRODAUDIO\\\S+)" -AllMatches |
    ForEach-Object { $_.Matches } | ForEach-Object { $_.Groups[1].Value }
if ($instanceIds) {
    foreach ($id in $instanceIds) {
        Write-Host "Removing $id"
        pnputil /remove-device $id
    }
} else {
    Write-Host "No Discrod device node found."
}

# --- remove the driver package ----------------------------------------------
Write-Host "== Removing driver package from the driver store" -ForegroundColor Cyan
$published = pnputil /enum-drivers | Out-String
$blocks = $published -split "(?=Published Name:)" |
    Where-Object { $_ -match "discrodaudio\.inf" }
if ($blocks) {
    foreach ($block in $blocks) {
        if ($block -match "Published Name:\s+(oem\d+\.inf)") {
            $oem = $Matches[1]
            Write-Host "Deleting $oem"
            pnputil /delete-driver $oem /uninstall /force
        }
    }
} else {
    Write-Host "No staged DiscrodAudio package found."
}

# --- test signing ------------------------------------------------------------
if ($DisableTestSigning) {
    Write-Host "== Disabling test-signing (effective after reboot)" -ForegroundColor Cyan
    bcdedit /set testsigning off
}

Write-Host "Done." -ForegroundColor Green
