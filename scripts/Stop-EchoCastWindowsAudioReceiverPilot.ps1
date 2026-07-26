<#
.SYNOPSIS
    Stop the hardware-mode audio Receiver pilot started by
    Start-EchoCastWindowsAudioReceiverPilot.ps1.

.DESCRIPTION
    Reads only the scoped pilot PID file, verifies the recorded process really
    is the EchoCast audio Receiver pilot before touching it, then stops the
    COMPLETE process tree that PID owns - the venv python.exe launcher, the base
    interpreter it spawns, and any FFmpeg child - and reports success only after
    every owned PID is confirmed gone.

    It never terminates unrelated Python or FFmpeg processes, never stops a
    process by name, never changes the Windows default device and never changes
    system volume.

.NOTES
    The Receiver writes its secret-free JSON report when its session loop ends,
    which happens on a normal broadcast stop - that is where the chunk, byte and
    frame counters come from. Windows has no SIGTERM, so stopping an IDLE
    Receiver from here is a hard terminate and no report is written. That is
    fine: an idle Receiver has no session counters to lose.
#>
[CmdletBinding()]
param(
    [string]$PilotRoot,
    [int]$GracefulTimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'

if (-not $PilotRoot) {
    $PilotRoot = Join-Path $env:LOCALAPPDATA 'EchoCast-AI\local-pilot'
}
$pidFile = Join-Path $PilotRoot 'runtime\windows-audio-receiver.pid'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'

. (Join-Path $PSScriptRoot 'EchoCastProcessTree.ps1')

Write-Output '=== Stopping EchoCast hardware-mode audio Receiver pilot ==='

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython. It is required to work out which processes the Receiver owns; refusing to stop anything by guesswork."
}

$stopped = Stop-EchoCastProcessTree `
    -PidFile $pidFile `
    -Label 'Receiver' `
    -ExpectedCommandFragments @('audio_receiver_pilot.py') `
    -VenvPython $venvPython `
    -RepositoryRoot $repositoryRoot `
    -GracefulTimeoutSeconds $GracefulTimeoutSeconds

foreach ($name in 'ECHOCAST_RECEIVER_TOKEN', 'ECHOCAST_AUDIO_SINK_MODE', 'ECHOCAST_AUDIO_OUTPUT_DEVICE') {
    if (Test-Path "env:$name") { Remove-Item "env:$name" -ErrorAction SilentlyContinue }
}

Write-Output ''
Write-Output 'The Windows default device and system volume were never changed.'
Write-Output ''

if ($stopped) {
    Write-Output 'Receiver stopped: every owned process is gone (including any FFmpeg'
    Write-Output 'child), the output stream is released and session pilot variables are'
    Write-Output 'cleared.'
    exit 0
}

Write-Output 'RECEIVER NOT FULLY STOPPED. Session pilot variables were cleared, but'
Write-Output 'inspect the survivors listed above before starting another Receiver.'
Write-Output 'Their PID file was deliberately kept so they stay traceable.'
exit 1
