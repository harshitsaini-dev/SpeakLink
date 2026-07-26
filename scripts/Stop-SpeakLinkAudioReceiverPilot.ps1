<#
.SYNOPSIS
    Stop the local audio Receiver pilot started by
    Start-SpeakLinkAudioReceiverPilot.ps1.

.DESCRIPTION
    Reads only the scoped pilot PID file, verifies that the recorded process
    really is the SpeakLink audio Receiver pilot before touching it, stops it
    gracefully, and force-terminates only that verified PID after a documented
    timeout.

    It never terminates unrelated Python or FFmpeg processes. Any FFmpeg child
    is owned by the Receiver and exits with it.
#>
[CmdletBinding()]
param(
    [string]$PilotRoot,
    [int]$GracefulTimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'

if (-not $PilotRoot) {
    $PilotRoot = Join-Path $env:LOCALAPPDATA 'SpeakLink\local-pilot'
}
$pidFile = Join-Path $PilotRoot 'runtime\audio-receiver.pid'

Write-Output '=== Stopping SpeakLink audio Receiver pilot ==='

if (-not (Test-Path $pidFile)) {
    Write-Output '  No audio Receiver PID file found; nothing to stop.'
    return
}

$raw = (Get-Content $pidFile -Raw).Trim()
$processId = 0
if (-not [int]::TryParse($raw, [ref]$processId) -or $processId -le 0) {
    Write-Output '  PID file is unreadable; removing the stale file.'
    Remove-Item $pidFile -Force
    return
}

$process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
if ($null -eq $process) {
    Write-Output "  Process $processId is not running; removing the stale PID file."
    Remove-Item $pidFile -Force
    return
}

# Only touch a process that is provably the audio Receiver pilot.
if ([string]$process.CommandLine -notlike '*audio_receiver_pilot.py*') {
    Write-Output "  Process $processId does NOT look like the SpeakLink audio Receiver pilot."
    Write-Output '  Refusing to stop it. Inspect it yourself, then remove the PID file if stale.'
    return
}

# Note any FFmpeg child so the operator can confirm it exits with its parent.
$children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $processId" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -eq 'ffmpeg.exe' }

Write-Output "  Stopping audio Receiver pilot process $processId gracefully..."
try {
    Stop-Process -Id $processId -ErrorAction Stop
} catch {
    Write-Output '  Could not signal the process; it may have already exited.'
}

$deadline = (Get-Date).AddSeconds($GracefulTimeoutSeconds)
while ((Get-Date) -lt $deadline) {
    if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 250
}
if ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
    Write-Output "  Still running after $GracefulTimeoutSeconds s; force-terminating the verified pilot PID."
    Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
}

Remove-Item $pidFile -Force

foreach ($child in $children) {
    if ($null -ne (Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue)) {
        Write-Output "  NOTE: FFmpeg child $($child.ProcessId) is still running. Inspect it before stopping it."
    }
}

if (Test-Path 'env:SPEAKLINK_RECEIVER_TOKEN') {
    Remove-Item 'env:SPEAKLINK_RECEIVER_TOKEN' -ErrorAction SilentlyContinue
}

Write-Output ''
Write-Output 'Audio Receiver pilot stopped and the session credential variable cleared.'
