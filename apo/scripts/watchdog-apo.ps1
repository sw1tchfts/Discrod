<#
.SYNOPSIS
    Keep the Discrod capture APO attached after Windows evicts it.

.DESCRIPTION
    The audio engine drops a mode/endpoint effect whenever it decides the APO
    misbehaved (a fault in APOProcess, a format change, a driver update, or a
    "reset all effects" from the Sound control panel), and clears the CLSID from
    the endpoint's FxProperties. This watchdog re-writes it so the effect comes
    back on the next stream open without the user re-running registration.

    It also re-registers the COM server if its InprocServer32 key goes missing,
    and (optionally) restarts the audio service when it had to re-apply the CLSID
    so the effect reloads immediately rather than on the next device cycle.

    Two ways to run it:
      * Foreground:  watchdog-apo.ps1 -EndpointId "{0.0.1...}" -IntervalSeconds 15
      * As a logon scheduled task:  watchdog-apo.ps1 -Install
        (creates a task that runs this script at logon for the current user;
         remove with -Uninstall)

    The endpoint id is the registry key leaf under
    ...\MMDevices\Audio\Capture printed by register-apo.ps1. If omitted in
    foreground mode, the watchdog re-applies to every capture endpoint that
    already had our CLSID at least once (tracked via the backup dir).
#>
[CmdletBinding()]
param(
    [string]$EndpointId,
    [ValidateSet("Mode", "Endpoint")]
    [string]$Effect = "Mode",
    [int]$IntervalSeconds = 15,
    [switch]$RestartAudioOnReapply,
    [switch]$Install,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$ApoClsid = "{7C9D5A24-6B3E-4C1F-9A2D-3E8F1B0C6A50}"
$ClsidKey = "HKLM:\SOFTWARE\Classes\CLSID\$ApoClsid\InprocServer32"
$CaptureRoot = "HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio\Capture"
$BackupDir = Join-Path $env:ProgramData "Discrod\apo-backup"
$TaskName = "DiscrodApoWatchdog"

$PKEY = @{
    Mode     = "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},7"
    Endpoint = "{D04E05A6-594B-4FB6-A80D-01AF5EED7D1D},8"
}[$Effect]

function Assert-Admin {
    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated (Administrator) PowerShell."
    }
}

# --- scheduled-task install/uninstall ---------------------------------------
if ($Install) {
    Assert-Admin
    $ps = (Get-Command powershell.exe).Source
    $args = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    if ($EndpointId) { $args += " -EndpointId `"$EndpointId`"" }
    $args += " -Effect $Effect -IntervalSeconds $IntervalSeconds"
    $action = New-ScheduledTaskAction -Execute $ps -Argument $args
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
        -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null
    Write-Host "Installed scheduled task '$TaskName' (runs at logon)." -ForegroundColor Green
    return
}
if ($Uninstall) {
    Assert-Admin
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    return
}

# --- target endpoints --------------------------------------------------------
function Get-Targets {
    if ($EndpointId) {
        return @(Join-Path $CaptureRoot $EndpointId)
    }
    # Fall back to every endpoint we have a backup for (i.e. was registered).
    if (Test-Path $BackupDir) {
        return Get-ChildItem $BackupDir -Filter *.reg |
            ForEach-Object { Join-Path $CaptureRoot $_.BaseName } |
            Where-Object { Test-Path $_ }
    }
    return @()
}

function Ensure-Com {
    if (-not (Test-Path $ClsidKey)) {
        $root = Split-Path -Parent $PSScriptRoot
        foreach ($c in @("build\Release\DiscrodApo.dll", "build\DiscrodApo.dll",
                         "x64\Release\DiscrodApo.dll")) {
            $dll = Join-Path $root $c
            if (Test-Path $dll) {
                & regsvr32.exe /s $dll
                Write-Host "[watchdog] re-registered COM server ($dll)"
                return
            }
        }
        Write-Warning "[watchdog] COM key missing and DiscrodApo.dll not found."
    }
}

Assert-Admin
Write-Host "[watchdog] guarding Discrod APO ($Effect effect) every ${IntervalSeconds}s. Ctrl+C to stop." -ForegroundColor Cyan

while ($true) {
    try {
        Ensure-Com
        foreach ($key in Get-Targets) {
            $fx = Join-Path $key "FxProperties"
            if (-not (Test-Path $fx)) { New-Item -Path $fx -Force | Out-Null }
            $cur = (Get-ItemProperty -Path $fx -Name $PKEY -ErrorAction SilentlyContinue).$PKEY
            if ($cur -ne $ApoClsid) {
                New-ItemProperty -Path $fx -Name $PKEY -Value $ApoClsid `
                    -PropertyType String -Force | Out-Null
                Write-Host "[watchdog] re-applied CLSID to $(Split-Path $key -Leaf)"
                if ($RestartAudioOnReapply) { Restart-Service -Name Audiosrv -Force }
            }
        }
    } catch {
        Write-Warning "[watchdog] $($_.Exception.Message)"
    }
    Start-Sleep -Seconds $IntervalSeconds
}
