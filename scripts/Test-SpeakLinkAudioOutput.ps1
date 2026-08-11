<#
.SYNOPSIS
    Play a short, quiet test chime on ONE explicitly selected Windows output
    device.

.DESCRIPTION
    Manual only. It prints the exact device first and asks you to type 'yes'
    before anything is played.

    It does not change the Windows default device, does not change system
    volume, does not touch Bluetooth, and does not loop.

    Hearing the chime is useful operator evidence. It is NOT SPEAKER_VERIFIED:
    that requires LinkGuard acoustic detection, which does not exist yet.

.PARAMETER Seconds
    Chime duration. The default is the documented 1.5 s. A Bluetooth endpoint
    can take a second or more to wake its DAC once a stream starts, so a longer
    tone tells a sleeping link apart from no audio path at all. Loudness is
    never selectable.

.EXAMPLE
    $env:SPEAKLINK_AUDIO_SINK_MODE     = 'windows'
    $env:SPEAKLINK_AUDIO_OUTPUT_DEVICE = 'index:3'
    .\scripts\Test-SpeakLinkAudioOutput.ps1

.EXAMPLE
    .\scripts\Test-SpeakLinkAudioOutput.ps1 -Seconds 5
#>
[CmdletBinding()]
param(
    [double] $Seconds = 1.5
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
$tool = Join-Path $repositoryRoot 'tools\audio_receiver_pilot.py'

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython."
}
if ($env:SPEAKLINK_AUDIO_SINK_MODE -ne 'windows') {
    throw "SPEAKLINK_AUDIO_SINK_MODE must be set to 'windows' for a hardware chime. It is currently '$($env:SPEAKLINK_AUDIO_SINK_MODE)'."
}
if ([string]::IsNullOrWhiteSpace($env:SPEAKLINK_AUDIO_OUTPUT_DEVICE)) {
    throw 'SPEAKLINK_AUDIO_OUTPUT_DEVICE is not set. Run .\scripts\List-SpeakLinkAudioDevices.ps1 and copy an exact selector such as index:3.'
}

Write-Output '=== SpeakLink output-device test chime ==='
Write-Output 'Turn the amplifier volume DOWN before continuing.'
Write-Output 'This script never changes Windows volume or the default device.'
Write-Output ''

Push-Location $repositoryRoot
try {
    & $venvPython $tool test-output --seconds $Seconds
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Output ''
Write-Output 'Reminder: hearing this chime is operator observation only.'
Write-Output 'It is NOT SPEAKER_VERIFIED and NOT AMPLIFIER_VERIFIED.'
exit $code
