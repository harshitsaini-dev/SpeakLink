<#
.SYNOPSIS
    Start one audio Receiver pilot in HARDWARE mode, sending decoded audio to
    exactly one explicitly selected Windows output device.

.DESCRIPTION
    Requires both SPEAKLINK_AUDIO_SINK_MODE=windows and an exact
    SPEAKLINK_AUDIO_OUTPUT_DEVICE selector. It prints the selected device before
    starting so you can confirm it is the wired USB/3.5 mm adapter feeding the
    amplifier AUX input, not a monitor or a Bluetooth headset.

    It never changes the Windows default device, never changes system volume,
    never configures Bluetooth and never installs a service.

    PLAYBACK_CONFIRMED from this Receiver means the selected device accepted
    PCM frames. It does NOT mean sound was audible. SPEAKER_VERIFIED remains
    unavailable until LinkGuard exists.

.NOTES
    The Store credential must be in SPEAKLINK_RECEIVER_TOKEN in this session. It
    is inherited by the child process and never appears in a command argument,
    a URL or a log.
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [string]$PilotRoot
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
$tool = Join-Path $repositoryRoot 'tools\audio_receiver_pilot.py'

if (-not $PilotRoot) {
    $PilotRoot = Join-Path $env:LOCALAPPDATA 'SpeakLink\local-pilot'
}
$pilotRuntime = Join-Path $PilotRoot 'runtime'
$pilotLogs = Join-Path $PilotRoot 'logs'
$pidFile = Join-Path $pilotRuntime 'windows-audio-receiver.pid'

Write-Output '=== SpeakLink audio Receiver pilot (WINDOWS OUTPUT DEVICE) ==='

if (-not (Test-Path $venvPython)) { throw "Python virtual environment not found at $venvPython." }
if ($null -eq (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw 'ffmpeg was not found on PATH. The Receiver cannot report READY without it.'
}
if ($env:SPEAKLINK_AUDIO_SINK_MODE -ne 'windows') {
    throw "SPEAKLINK_AUDIO_SINK_MODE must be 'windows' for hardware mode. It is currently '$($env:SPEAKLINK_AUDIO_SINK_MODE)'. Use Start-SpeakLinkAudioReceiverPilot.ps1 for the safe null sink."
}
if ([string]::IsNullOrWhiteSpace($env:SPEAKLINK_AUDIO_OUTPUT_DEVICE)) {
    throw 'SPEAKLINK_AUDIO_OUTPUT_DEVICE is not set. Run .\scripts\List-SpeakLinkAudioDevices.ps1 and copy an exact selector such as index:3.'
}
if ([string]::IsNullOrWhiteSpace($env:SPEAKLINK_RECEIVER_TOKEN)) {
    throw 'SPEAKLINK_RECEIVER_TOKEN is not set for this PowerShell session. Copy the Store credential from Store Management. It is never stored by this script.'
}

foreach ($directory in @($pilotRuntime, $pilotLogs)) {
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
}

Write-Output ''
Write-Output "Selected output device: $($env:SPEAKLINK_AUDIO_OUTPUT_DEVICE)"
Write-Output 'Confirm this is the wired adapter feeding the amplifier AUX input.'
Write-Output 'Run .\scripts\List-SpeakLinkAudioDevices.ps1 if you are unsure.'
Write-Output ''

$receiverUrl = "ws://127.0.0.1:$BackendPort/api/ws/receiver"
$reportPath = Join-Path $pilotLogs 'windows-audio-receiver-report.json'
$outLog = Join-Path $pilotLogs 'windows-audio-receiver.out.log'
$errLog = Join-Path $pilotLogs 'windows-audio-receiver.err.log'

$receiver = Start-Process -FilePath $venvPython `
    -ArgumentList @("`"$tool`"", 'run', '--url', "`"$receiverUrl`"", '--report', "`"$reportPath`"") `
    -WorkingDirectory $repositoryRoot `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru
$receiver.Id | Out-File -FilePath $pidFile -Encoding utf8 -NoNewline

Write-Output 'Hardware-mode audio Receiver pilot started.'
Write-Output "  Receiver URL : $receiverUrl"
Write-Output "  Sink mode    : windows (one explicitly selected device)"
Write-Output "  PID file     : $pidFile"
Write-Output "  Report       : $reportPath"
Write-Output ''
Write-Output 'READY will only be reported if that exact device actually opens.'
Write-Output 'PLAYBACK_CONFIRMED means frames were accepted - NOT that sound was heard.'
Write-Output 'SPEAKER_VERIFIED is not available in this milestone.'
Write-Output ''
Write-Output 'Stop it with: .\scripts\Stop-SpeakLinkWindowsAudioReceiverPilot.ps1'
