<#
.SYNOPSIS
    Stop the local audio Receiver pilot started by
    Start-EchoCastAudioReceiverPilot.ps1.

.DESCRIPTION
    Reads only the scoped pilot PID file, verifies that the recorded process
    really is the EchoCast audio Receiver pilot before touching it, then stops
    the COMPLETE process tree that PID owns - the venv python.exe launcher, the
    base interpreter it spawns, and any FFmpeg child - and reports success only
    after every owned PID is confirmed gone.

    It never terminates unrelated Python or FFmpeg processes, and never stops a
    process by name.
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
$pidFile = Join-Path $PilotRoot 'runtime\audio-receiver.pid'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'

. (Join-Path $PSScriptRoot 'EchoCastProcessTree.ps1')

Write-Output '=== Stopping EchoCast audio Receiver pilot ==='

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

if (Test-Path 'env:ECHOCAST_RECEIVER_TOKEN') {
    Remove-Item 'env:ECHOCAST_RECEIVER_TOKEN' -ErrorAction SilentlyContinue
}

Write-Output ''
if ($stopped) {
    Write-Output 'Audio Receiver pilot stopped: every owned process is gone (including any'
    Write-Output 'FFmpeg child) and the session credential variable is cleared.'
    exit 0
}

Write-Output 'AUDIO RECEIVER PILOT NOT FULLY STOPPED. The session credential variable was'
Write-Output 'cleared, but inspect the survivors listed above before starting another one.'
exit 1
