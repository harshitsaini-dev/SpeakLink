<#
.SYNOPSIS
    Verify an installed Store Receiver. Read-only.

.DESCRIPTION
    A check that cannot be read reports UNKNOWN, never PASS and never FAIL, and
    no marker is emitted while anything is unreadable. An earlier firewall
    checker in this repository asked Get-NetFirewallRule without elevation, was
    told "Access is denied", and scored that as "the rule is not installed" -
    reporting a missing rule that was present the whole time.

    What this cannot verify, and does not pretend to: behaviour after a reboot,
    after sign-out and sign-in, while the screen is locked, or that any sound
    was heard. Those need a person and are in the manual checklist.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'SpeakLink Store Receiver',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver-app'),
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver')
)

$ErrorActionPreference = 'Stop'

$stateRoot = $StateRoot
$configPath = Join-Path $stateRoot 'config.json'
$credentialPath = Join-Path $stateRoot 'device-credential.bin'

$script:results = @()
function Check {
    param([string]$CheckLabel, [scriptblock]$CheckBody)
    try { $verdict = & $CheckBody } catch { $verdict = 'UNKNOWN' }
    $outcome = switch ($verdict) { $true { 'PASS' } $false { 'FAIL' } default { [string]$verdict } }
    $script:results += [PSCustomObject]@{ Name = $CheckLabel; Verdict = $outcome }
    Write-Output ('  {0,-56}{1}' -f $CheckLabel, $outcome)
}

function Get-PeSubsystem {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $reader = New-Object System.IO.BinaryReader($stream)
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset + 4 + 20 + 68
        return $reader.ReadUInt16()
    } finally { $stream.Dispose() }
}

Write-Output '=== SpeakLink Store Receiver verification ==='
Write-Output "  install root: $InstallRoot"
Write-Output ''

# ---- what is installed ----------------------------------------------------
Check 'the install root exists' { Test-Path $InstallRoot }
Check 'the background executable is installed' {
    Test-Path (Join-Path $InstallRoot 'SpeakLinkReceiverBackground.exe')
}
Check 'the operator executable is installed' {
    Test-Path (Join-Path $InstallRoot 'SpeakLinkReceiver.exe')
}
Check 'FFmpeg sits beside them' { Test-Path (Join-Path $InstallRoot 'ffmpeg.exe') }
Check 'every installed file matches its build hash' {
    $bad = 0
    foreach ($line in (Get-Content (Join-Path $InstallRoot 'SHA256SUMS.txt'))) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '  ', 2
        $file = Join-Path $InstallRoot ($parts[1] -replace '/', '\')
        if (-not (Test-Path $file)) { $bad++; continue }
        if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) { $bad++ }
    }
    $bad -eq 0
}

# ---- the thing that keeps a window off the counter -------------------------
Check 'the background executable creates NO console window' {
    (Get-PeSubsystem (Join-Path $InstallRoot 'SpeakLinkReceiverBackground.exe')) -eq 2
}
Check 'the operator executable still has a console' {
    (Get-PeSubsystem (Join-Path $InstallRoot 'SpeakLinkReceiver.exe')) -eq 3
}

# ---- settings and credential ----------------------------------------------
Check 'settings are saved' { Test-Path $configPath }
Check 'the settings name a backend' {
    [bool](Get-Content $configPath -Raw | ConvertFrom-Json).backend_url
}
Check 'the settings name an audio device' {
    $config = Get-Content $configPath -Raw | ConvertFrom-Json
    $config.audio_sink -ne 'windows' -or [bool]$config.audio_output_device
}
Check 'the settings contain no secret' {
    $text = Get-Content $configPath -Raw
    $text -notmatch 'speaklink_rcv_v1' -and $text -notmatch 'ECHO(-[A-Z0-9]{4}){2,}' -and
    $text -notmatch '(?i)"(password|credential|token|secret)"'
}
Check 'the Device credential is present' { Test-Path $credentialPath }

# ---- the task --------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Check 'the scheduled task is registered' { [bool]$task }
if ($task) {
    Check 'it triggers at logon' {
        ($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -contains 'MSFT_TaskLogonTrigger'
    }
    Check 'it runs the WINDOWED executable' {
        ($task.Actions | ForEach-Object { $_.Execute }) -match 'SpeakLinkReceiverBackground\.exe'
    }
    Check 'it runs as the interactive user, not SYSTEM' {
        $task.Principal.UserId -notmatch '(?i)(SYSTEM|LOCALSERVICE|NETWORKSERVICE)'
    }
    Check 'it stores no Windows password' { $task.Principal.LogonType -eq 'Interactive' }
    Check 'it does not demand administrator rights' { $task.Principal.RunLevel -ne 'Highest' }
    Check 'a second launch is ignored rather than started' {
        $task.Settings.MultipleInstances -eq 'IgnoreNew'
    }
    Check 'the Receiver is retried on a repetition schedule' {
        [bool]($task.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval })
    }
    Check 'the repetition is bounded in time' {
        [bool]($task.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Duration })
    }
    Check 'the task carries no credential, code or password' {
        $xml = Export-ScheduledTask -TaskName $TaskName
        $xml -notmatch 'speaklink_rcv_v1' -and $xml -notmatch 'ECHO(-[A-Z0-9]{4}){2,}' -and
        $xml -notmatch '(?i)(password|bearer)'
    }
    Check 'the task arguments name no audio device' {
        # It lives in the config file instead, so the task cannot go stale
        # against a device that was renumbered.
        ($task.Actions | ForEach-Object { $_.Arguments }) -notmatch 'audio-output-device'
    }
}

# ---- is it actually running ------------------------------------------------
$live = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe'" |
          Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
Check 'exactly one Receiver is running' { $live.Count -eq 1 }
Check 'no console-mode Receiver is running in the background' {
    @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiver.exe'" |
      Where-Object { $_.CommandLine -match '\brun\b' }).Count -eq 0
}

# ---- logs ------------------------------------------------------------------
$logDirectory = Join-Path $stateRoot 'logs'
Check 'logs are being written' {
    (Test-Path $logDirectory) -and @(Get-ChildItem $logDirectory -Filter 'receiver*.log*').Count -gt 0
}
Check 'logs stay bounded' {
    if (-not (Test-Path $logDirectory)) { throw 'no log directory yet' }
    $files = @(Get-ChildItem $logDirectory -Filter 'receiver*.log*')
    ($files | Measure-Object Length -Sum).Sum -le 12MB -and $files.Count -le 12
}
Check 'no secret in any log' {
    if (-not (Test-Path $logDirectory)) { throw 'no log directory yet' }
    @(Get-ChildItem $logDirectory -File |
      Select-String -Pattern 'speaklink_rcv_v1', 'ECHO(-[A-Z0-9]{4}){2,}', 'Bearer ' -ErrorAction SilentlyContinue).Count -eq 0
}

# ---- verdict ---------------------------------------------------------------
$failed = @($script:results | Where-Object { $_.Verdict -like 'FAIL*' })
$unknown = @($script:results | Where-Object { $_.Verdict -eq 'UNKNOWN' })

Write-Output ''
if ($unknown.Count -gt 0) {
    Write-Output "  $($unknown.Count) check(s) could not be read - not a pass, not a failure:"
    $unknown | ForEach-Object { Write-Output "    - $($_.Name)" }
}
if ($failed.Count -gt 0) {
    Write-Output ''
    Write-Output "Result: FAILED ($($failed.Count) check(s))"
    $failed | ForEach-Object { Write-Output "  - $($_.Name)" }
    exit 1
}
if ($unknown.Count -gt 0) {
    Write-Output ''
    Write-Output 'Result: INCOMPLETE. No marker while a check is unreadable.'
    exit 1
}

Write-Output ''
Write-Output "  $($script:results.Count) checks passed"
Write-Output ''
Write-Output 'SPEAKLINK_STORE_RECEIVER_VERIFIED'
Write-Output ''
Write-Output 'Scope: the Receiver is installed, runs with no window, and is running now.'
Write-Output 'It says NOTHING about behaviour after a reboot, after sign-out and'
Write-Output 'sign-in, on a locked screen, or about any sound being heard. Those need'
Write-Output 'a person - see the manual checklist.'
exit 0
