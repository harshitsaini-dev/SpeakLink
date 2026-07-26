<#
.SYNOPSIS
    Start one local audio Receiver pilot for manual browser testing.

.DESCRIPTION
    Connects one Store Receiver to a running local pilot backend, performs the
    real FFmpeg/codec readiness checks, decodes incoming WebM/Opus audio to a
    NULL sink and reports honest acknowledgements.

    A null sink is used deliberately so nothing can be played through the wrong
    Windows audio device. This Receiver therefore proves software decoding
    only, never speaker output.

.NOTES
    The Store credential must be supplied through SPEAKLINK_RECEIVER_TOKEN in
    the current session. It is inherited by the child process and is never
    placed in a command argument, a URL, or any log.
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [string]$PilotRoot
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repositoryRoot 'backend'
$venvPython = Join-Path $backendDir '.venv\Scripts\python.exe'
$tool = Join-Path $repositoryRoot 'tools\audio_receiver_pilot.py'

if (-not $PilotRoot) {
    $PilotRoot = Join-Path $env:LOCALAPPDATA 'SpeakLink\local-pilot'
}
$pilotRuntime = Join-Path $PilotRoot 'runtime'
$pilotLogs = Join-Path $PilotRoot 'logs'
$pidFile = Join-Path $pilotRuntime 'audio-receiver.pid'

Write-Output '=== SpeakLink local audio Receiver pilot (NULL sink) ==='

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython."
}
if ($null -eq (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw 'ffmpeg was not found on PATH. The Receiver cannot report READY without it.'
}
if ([string]::IsNullOrWhiteSpace($env:SPEAKLINK_RECEIVER_TOKEN)) {
    throw 'SPEAKLINK_RECEIVER_TOKEN is not set for this PowerShell session. Copy the Store credential from Store Management and set it before starting. It is never stored by this script.'
}

foreach ($directory in @($pilotRuntime, $pilotLogs)) {
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
}

$receiverUrl = "ws://127.0.0.1:$BackendPort/api/ws/receiver"
$reportPath = Join-Path $pilotLogs 'audio-receiver-report.json'
$outLog = Join-Path $pilotLogs 'audio-receiver.out.log'
$errLog = Join-Path $pilotLogs 'audio-receiver.err.log'

$receiver = Start-Process -FilePath $venvPython `
    -ArgumentList @($tool, '--url', $receiverUrl, '--report', $reportPath) `
    -WorkingDirectory $repositoryRoot `
    -RedirectStandardOutput $outLog `
    -RedirectStandardError $errLog `
    -PassThru
$receiver.Id | Out-File -FilePath $pidFile -Encoding utf8 -NoNewline

Write-Output ''
Write-Output 'Audio Receiver pilot started.'
Write-Output "  Receiver URL : $receiverUrl"
Write-Output "  Sink mode    : null (no Windows audio device is opened)"
Write-Output "  PID file     : $pidFile"
Write-Output "  Report       : $reportPath"
Write-Output "  Logs         : $outLog"
Write-Output ''
Write-Output 'It will report READY only after real FFmpeg and Opus/WebM checks pass,'
Write-Output 'and it can never report SPEAKER_VERIFIED.'
Write-Output ''
Write-Output 'Stop it with: .\scripts\Stop-SpeakLinkAudioReceiverPilot.ps1'
