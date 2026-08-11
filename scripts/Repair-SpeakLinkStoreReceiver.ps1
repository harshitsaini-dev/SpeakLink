<#
.SYNOPSIS
    Repair or upgrade an installed Store Receiver, keeping its identity.

.DESCRIPTION
    Repair reads the settings this computer already has and reinstalls on top of
    them. It never asks the operator to remember a device selector or a backend
    address, because the whole point of saving them was that nobody should have
    to.

    Repair and upgrade are the same operation with a different package: point it
    at the package already installed and it repairs; point it at a newer one and
    it upgrades. Both stop the running Receiver first, replace the files, re-hash
    every one of them against the build manifest, and start it again - so a
    running executable is never overwritten underneath itself.

    The DPAPI Device credential is never touched, so an upgrade is not a
    re-enrolment.

    If the reinstall fails its own verification the previous installation has
    already been replaced; the old package folder is the rollback, which is why
    the kit tells you to keep it.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [string]$TaskName = 'SpeakLink Store Receiver',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver-app'),
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver'),
    [string]$AudioOutputDevice,
    [string]$BackendUrl,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$stateRoot = $StateRoot
$configPath = Join-Path $stateRoot 'config.json'

Write-Output '=== repairing the SpeakLink Store Receiver ==='

if (-not (Test-Path $configPath)) {
    throw ("No saved settings at $configPath, so there is nothing to repair. " +
           'Use Install-SpeakLinkStoreReceiver.ps1 for a first installation.')
}

$config = Get-Content $configPath -Raw | ConvertFrom-Json

# Explicit arguments win, so a technician can correct one setting - a speaker
# that was replaced, an HQ that moved - without retyping the rest.
if (-not $BackendUrl) { $BackendUrl = $config.backend_url }
if (-not $AudioOutputDevice) { $AudioOutputDevice = $config.audio_output_device }

if (-not $BackendUrl) { throw 'The saved settings name no backend, and none was given.' }

Write-Output "  saved backend : $($config.backend_url)"
Write-Output "  saved audio   : $($config.audio_sink) -> $($config.audio_output_device)"
if ($BackendUrl -ne $config.backend_url) { Write-Output "  OVERRIDE backend: $BackendUrl" }
if ($AudioOutputDevice -ne $config.audio_output_device) { Write-Output "  OVERRIDE audio  : $AudioOutputDevice" }
Write-Output "  installed ver : $($config.installed_version) ($($config.source_commit))"

$credentialPath = Join-Path $stateRoot 'device-credential.bin'
Write-Output "  credential    : $(if (Test-Path $credentialPath) { 'present - will be preserved' } else { 'ABSENT - this Store is not enrolled' })"

$install = Join-Path $PSScriptRoot 'Install-SpeakLinkStoreReceiver.ps1'
if (-not (Test-Path $install)) { throw "Install-SpeakLinkStoreReceiver.ps1 is missing beside this script." }

$parameters = @{
    PackagePath = $PackagePath
    BackendUrl = $BackendUrl
    AudioOutputDevice = $AudioOutputDevice
    AudioSink = $(if ($config.audio_sink) { $config.audio_sink } else { 'windows' })
    TaskName = $TaskName
    InstallRoot = $InstallRoot
    StateRoot = $StateRoot
}
if ($config.expected_hq_host) { $parameters['ExpectedHqHost'] = $config.expected_hq_host }
if ($DryRun) { $parameters['DryRun'] = $true }

Write-Output ''
& $install @parameters

Write-Output ''
Write-Output 'SPEAKLINK_STORE_RECEIVER_REPAIRED'
Write-Output 'The Device credential and the saved settings were preserved.'
