<#
.SYNOPSIS
    List Windows audio OUTPUT devices for the SpeakLink one-Store pilot.

.DESCRIPTION
    Read-only. It opens no audio stream, plays no sound, changes no Windows
    default device and changes no system volume.

    Copy the SELECTOR of the device you want (for example 'index:3') and use it
    as SPEAKLINK_AUDIO_OUTPUT_DEVICE. The pilot never picks a device for you.

    Prefer a wired USB audio adapter or a 3.5 mm output into the amplifier AUX.
    A Bluetooth endpoint may be listed but is never selected automatically.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
$tool = Join-Path $repositoryRoot 'tools\windows_audio_devices.py'

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython. See README.md."
}

Write-Output '=== SpeakLink Windows audio OUTPUT devices (read-only) ==='
Write-Output ''

Push-Location $repositoryRoot
try {
    & $venvPython $tool list
    $code = $LASTEXITCODE
}
finally {
    Pop-Location
}

Write-Output ''
Write-Output 'Nothing was opened and nothing was changed.'
Write-Output 'A device appearing here does NOT mean an amplifier or speaker is connected.'
exit $code
