<#
.SYNOPSIS
    Run the automated one-Store live-audio software smoke test.

.DESCRIPTION
    Prepares the isolated pilot database and the deterministic synthetic
    WebM/Opus fixture, then runs the whole software audio path end to end and
    stops every child process it started.

    A pass means READY_FOR_ONE_STORE_LIVE_AUDIO_SOFTWARE_TEST and nothing more.
    It does not prove a correct Windows output device, an amplifier, a
    Bluetooth link, audible Store speakers, EchoGuard or SPEAKER_VERIFIED.

    The protected database backend\echocast_live.db is never used.

.NOTES
    Credentials are read from the current session's environment and inherited
    by the child processes. They are never placed in a command argument and
    never printed.
#>
[CmdletBinding()]
param(
    [string]$PilotRoot,
    [switch]$PrepareOnly
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repositoryRoot 'backend'
$venvPython = Join-Path $backendDir '.venv\Scripts\python.exe'
$tool = Join-Path $repositoryRoot 'tools\local_audio_pilot.py'

Write-Output '=== EchoCast one-Store live-audio SOFTWARE smoke ==='

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython. See README.md."
}
if ($null -eq (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw 'ffmpeg was not found on PATH. The audio pilot needs FFmpeg with Opus and WebM support.'
}
if ($null -eq (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    throw 'ffprobe was not found on PATH.'
}
if ([string]::IsNullOrWhiteSpace($env:ADMIN_PASSWORD)) {
    throw 'ADMIN_PASSWORD is not set for this PowerShell session. Set a temporary pilot-only value first. It is never stored by this script.'
}
if ([string]::IsNullOrWhiteSpace($env:JWT_SECRET)) {
    throw 'JWT_SECRET is not set for this PowerShell session. Set a temporary pilot-only value first. It is never stored by this script.'
}
if ([string]::IsNullOrWhiteSpace($env:ADMIN_USERNAME)) { $env:ADMIN_USERNAME = 'pilot-operator' }

$arguments = @()
if ($PilotRoot) { $arguments += @('--pilot-root', $PilotRoot) }

Push-Location $backendDir
try {
    Write-Output '--- prepare (isolated database + deterministic fixture) ---'
    & $venvPython $tool prepare @arguments
    if ($LASTEXITCODE -ne 0) { throw "Audio pilot prepare failed with exit code $LASTEXITCODE." }

    if ($PrepareOnly) {
        Write-Output 'Prepare finished. Skipping the smoke run because -PrepareOnly was supplied.'
        return
    }

    Write-Output ''
    Write-Output '--- smoke (end-to-end software audio path) ---'
    & $venvPython $tool smoke @arguments
    $smokeExit = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Output ''
if ($smokeExit -eq 0) {
    Write-Output 'Result: ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED'
    Write-Output 'This proves the software path only: one Receiver connected, became'
    Write-Output 'READY after real FFmpeg/codec checks, received WebM/Opus audio, FFmpeg'
    Write-Output 'decoded it, and stop/cleanup worked.'
    Write-Output 'It does NOT prove speakers, amplifier, Bluetooth or output-device routing.'
} else {
    Write-Output "Result: FAILED (exit code $smokeExit)"
    Write-Output 'Exit codes: 1 = safety/startup, 2 = audio assertion, 3 = cleanup.'
}

exit $smokeExit
