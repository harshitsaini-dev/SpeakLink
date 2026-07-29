<#
.SYNOPSIS
    Read-only recovery diagnostics for a Store computer.

.DESCRIPTION
    One command a technician can ask a Store to run over the phone. It reads,
    reports and changes nothing: it never deletes a task, never removes a
    credential and never re-enrols anything.

    It reports whether the credential FILE exists. It never opens it, and there
    is no code path here that could print its contents.

    A check that cannot be read reports UNKNOWN, never PASS and never FAIL - an
    earlier checker in this repository read "Access is denied" as "not
    installed" and reported a missing firewall rule that was there all along.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'SpeakLink Store Receiver',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver-app'),
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver'),
    [string]$HqUrl
)

$ErrorActionPreference = 'Continue'

$configPath = Join-Path $StateRoot 'config.json'
$credentialPath = Join-Path $StateRoot 'device-credential.bin'
$logDirectory = Join-Path $StateRoot 'logs'

function Line { param([string]$Label, $Value) Write-Output ('  {0,-30}{1}' -f $Label, $Value) }

Write-Output '=== SpeakLink Store recovery diagnostics (read-only) ==='
Write-Output ''

Line 'windows user' "$env:USERDOMAIN\$env:USERNAME"
Line 'computer' $env:COMPUTERNAME
Line 'time (UTC)' ((Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm:ss'))
Write-Output ''

# ---- the scheduled task ----------------------------------------------------
Write-Output '--- scheduled task ---'
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Line 'task' "NOT REGISTERED ('$TaskName')"
    Line 'meaning' 'the Receiver will not start at logon'
} else {
    $action = $task.Actions | Select-Object -First 1
    Line 'task' $TaskName
    Line 'state' $task.State
    Line 'runs' $action.Execute
    Line 'arguments' $action.Arguments
    Line 'working dir' $action.WorkingDirectory
    Line 'principal' "$($task.Principal.UserId) / $($task.Principal.LogonType) / $($task.Principal.RunLevel)"
    Line 'multiple instances' $task.Settings.MultipleInstances
    Line 'start when available' $task.Settings.StartWhenAvailable
    Line 'restart count' $task.Settings.RestartCount
    $repetition = $task.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval }
    Line 'repetition' $(if ($repetition) { "$($repetition[0].Repetition.Interval) for $($repetition[0].Repetition.Duration)" } else { 'NONE' })
    Line 'triggers' (($task.Triggers | ForEach-Object { $_.CimClass.CimClassName -replace 'MSFT_Task', '' }) -join ', ')

    # The two things that put a black window on the counter.
    $windowed = $action.Execute -match 'SpeakLinkReceiverBackground\.exe'
    Line 'windowed executable' $(if ($windowed) { 'yes (correct)' } else { 'NO - this is the console build' })
    $wrapped = "$($action.Execute) $($action.Arguments)" -match '(?i)(powershell|pwsh|cmd)\.exe|\.(ps1|bat|cmd)\b'
    Line 'shell wrapper' $(if ($wrapped) { 'YES - this will show a window' } else { 'none (correct)' })

    try {
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction Stop
        Line 'last run' $info.LastRunTime
        Line 'last result' "$($info.LastTaskResult) $(switch ($info.LastTaskResult) {
            0 { '(stopped normally)' } 1 { '(refused - configuration)' }
            2 { '(authentication refused - re-enrol needed)' }
            3 { '(network problem)' } 4 { '(already running - not a failure)' }
            267009 { '(currently running)' } 267011 { '(has not run yet)' }
            default { '' } })"
        Line 'next run' $info.NextRunTime
    } catch {
        Line 'task history' 'UNKNOWN (could not be read)'
    }
}
Write-Output ''

# ---- installation ----------------------------------------------------------
Write-Output '--- installation ---'
Line 'install root' $InstallRoot
Line 'background exe' $(if (Test-Path (Join-Path $InstallRoot 'SpeakLinkReceiverBackground.exe')) { 'present' } else { 'MISSING' })
Line 'operator exe' $(if (Test-Path (Join-Path $InstallRoot 'SpeakLinkReceiver.exe')) { 'present' } else { 'MISSING' })
Line 'ffmpeg' $(if (Test-Path (Join-Path $InstallRoot 'ffmpeg.exe')) { 'present' } else { 'MISSING' })
Write-Output ''

# ---- identity and settings, never their contents ---------------------------
Write-Output '--- identity and settings ---'
# Presence only. This script has no code path that reads the credential.
Line 'credential file' $(if (Test-Path $credentialPath) { 'present (contents never read)' } else { 'ABSENT - this computer is not enrolled' })
Line 'config file' $(if (Test-Path $configPath) { 'present' } else { 'ABSENT' })
if (Test-Path $configPath) {
    try {
        $config = Get-Content $configPath -Raw | ConvertFrom-Json
        Line 'backend url' $config.backend_url
        Line 'audio sink' $(if ($config.audio_sink) { $config.audio_sink } else { 'null (audio is DISCARDED)' })
        Line 'audio device' $(if ($config.audio_output_device) { $config.audio_output_device } else { '<not set>' })
        Line 'installed version' $config.installed_version
        Line 'source commit' $config.source_commit
        if (-not $HqUrl) { $HqUrl = $config.backend_url }
    } catch {
        Line 'config' 'UNREADABLE'
    }
}
Write-Output ''

# ---- running processes -----------------------------------------------------
Write-Output '--- processes ---'
$background = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe'" -ErrorAction SilentlyContinue)
$console = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiver.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -match '\brun\b' })
$ffmpeg = @(Get-CimInstance Win32_Process -Filter "Name = 'ffmpeg.exe'" -ErrorAction SilentlyContinue)
Line 'background receivers' $background.Count
Line 'console receivers' "$($console.Count) $(if ($console.Count -gt 0) { '- these show a window' } else { '' })"
Line 'ffmpeg processes' "$($ffmpeg.Count) $(if ($ffmpeg.Count -gt 1) { '- more than one is unexpected' } else { '' })"
if ($background.Count -gt 1) { Line 'WARNING' 'more than one Receiver is running' }
Write-Output ''

# ---- HQ reachability -------------------------------------------------------
Write-Output '--- HQ ---'
if (-not $HqUrl) {
    Line 'hq url' 'UNKNOWN (no config and none supplied)'
} else {
    Line 'hq url' $HqUrl
    if ($HqUrl -match '^https?://([^:/]+)(?::(\d+))?') {
        $hqHost = $Matches[1]
        $hqPort = if ($Matches[2]) { [int]$Matches[2] } else { 80 }
        try {
            $reach = Test-NetConnection -ComputerName $hqHost -Port $hqPort -WarningAction SilentlyContinue
            Line 'tcp reachable' $reach.TcpTestSucceeded
        } catch { Line 'tcp reachable' 'UNKNOWN' }
    }
}
Write-Output ''

# ---- logs, redacted --------------------------------------------------------
Write-Output '--- recent log ---'
if (-not (Test-Path $logDirectory)) {
    Line 'logs' 'no log directory yet'
} else {
    $files = @(Get-ChildItem $logDirectory -Filter 'receiver*.log*' -ErrorAction SilentlyContinue)
    Line 'log files' "$($files.Count) ($(($files | Measure-Object Length -Sum).Sum) bytes)"
    $newest = $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($newest) {
        Line 'newest' $newest.Name
        Write-Output ''
        Write-Output '  last 12 lines (redacted again on the way out):'
        Get-Content $newest.FullName -Tail 12 | ForEach-Object {
            $safe = $_ -replace 'speaklink_rcv_v1\.[A-Za-z0-9._\-]+', '<REDACTED credential>' `
                        -replace '(?i)(bearer|basic)\s+\S+', '$1 <REDACTED>' `
                        -replace 'ECHO(-[A-Z0-9]{4}){2,}', '<REDACTED code>'
            Write-Output "    $safe"
        }
    }
}

Write-Output ''
Write-Output 'SPEAKLINK_STORE_RECOVERY_DIAGNOSTICS_COMPLETE'
Write-Output ''
Write-Output 'Read-only. Nothing was deleted, re-enrolled or changed.'
Write-Output 'This report contains no password, credential or token.'
