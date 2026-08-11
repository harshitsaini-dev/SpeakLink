<#
.SYNOPSIS
    One command that installs, upgrades, repairs or removes a Store Receiver.

.DESCRIPTION
    THE PROBLEM THIS SOLVES

    Installing a Store used to be a sequence: unpack a kit, read a runbook,
    call Install-SpeakLinkStoreReceiver.ps1 with five parameters, know which
    audio device selector to pass, and know - before starting - whether this
    machine already had a Receiver on it. Half of those are decisions nobody
    at a till should be making, and the one that matters most (is this an
    install or an upgrade?) was left to the person to notice.

    So this asks the machine instead. It looks for an existing installation and
    picks the verb from what it finds.

    THE FOUR VERBS, AND WHAT EACH IS FOR

    Install   - a machine with no Receiver. Installs, then hands over to the
                enrolment wizard so the Store can be enrolled.
    Upgrade   - a machine that already has one. Replaces the program files and
                nothing else: the DPAPI Device credential, the settings and the
                chosen audio device all survive, because an upgrade must not be
                a re-enrolment. A Store that had to be re-enrolled on every
                update would be re-enrolled by whoever happened to be at the
                till.
    Repair    - the program files are there but the Store is not working:
                the scheduled task was deleted, the runtime was half-copied by
                a virus scanner, the task points at a path that has moved.
                Re-copies the files and rebuilds the task, and - this is the
                point - keeps the credential, so a repair is not an enrolment
                either.
    Uninstall - stops the runtime, removes the task and the program files, and
                ASKS before touching the credential. Removing the software and
                revoking the Store's identity are different decisions: an
                uninstall for a machine being reimaged wants the credential
                gone, and one for a machine being moved to a new desk does not.

    WHAT IS NEVER TOUCHED WITHOUT BEING ASKED FOR

    The Device credential is sealed with DPAPI to the Store user's account. It
    is preserved by Install, Upgrade and Repair, and removed by Uninstall only
    with -RemoveCredential. Nothing here revokes anything at HQ: a Device
    removed from a machine still exists in HQ until an operator says otherwise,
    which is deliberate - the machine cannot be trusted to decide that.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    # Auto is the point of the whole script: it looks and decides.
    [ValidateSet('Auto', 'Install', 'Upgrade', 'Repair', 'Uninstall')]
    [string]$Action = 'Auto',

    # Where the kit is. Defaults to the folder this script was run from, which
    # is what happens when somebody unzips the kit and double-clicks.
    [string]$PackagePath,

    [string]$BackendUrl,
    [string]$AudioOutputDevice,
    [ValidateSet('null', 'windows')][string]$AudioSink = 'windows',

    [string]$TaskName = 'SpeakLink Store Receiver',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver-app'),
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver'),

    # Uninstall only. Off by default: removing the software and revoking the
    # Store's identity are different decisions.
    [switch]$RemoveCredential,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }

function Write-Step   { param([string]$Text) Write-Host "==> $Text" -ForegroundColor Cyan }
function Write-Ok     { param([string]$Text) Write-Host "    $Text" -ForegroundColor Green }
function Write-Note   { param([string]$Text) Write-Host "    $Text" -ForegroundColor Gray }
function Write-Problem{ param([string]$Text) Write-Host "    $Text" -ForegroundColor Yellow }

$configPath      = Join-Path $StateRoot 'config.json'
$credentialPath  = Join-Path $StateRoot 'device-credential.bin'
$backgroundExe   = Join-Path $InstallRoot 'SpeakLinkReceiverBackground.exe'

# ---------------------------------------------------------------------------
# What is already on this machine
# ---------------------------------------------------------------------------
function Get-InstalledState {
    $task = $null
    try { $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { $task = $null }

    [pscustomobject]@{
        HasProgram    = Test-Path -LiteralPath $backgroundExe
        HasTask       = $null -ne $task
        HasConfig     = Test-Path -LiteralPath $configPath
        HasCredential = Test-Path -LiteralPath $credentialPath
        Version       = if (Test-Path -LiteralPath (Join-Path $InstallRoot 'kit-manifest.json')) {
                            try {
                                (Get-Content -LiteralPath (Join-Path $InstallRoot 'kit-manifest.json') -Raw |
                                    ConvertFrom-Json).version
                            } catch { $null }
                        } else { $null }
    }
}

function Resolve-Action {
    param([object]$State)
    if ($Action -ne 'Auto') { return $Action }
    if (-not $State.HasProgram) { return 'Install' }
    # Program present but the task is gone: the Store looks installed and does
    # nothing, which is precisely the case Repair exists for. Choosing Upgrade
    # here would also work, but it would hide a broken machine behind a
    # version bump.
    if (-not $State.HasTask) { return 'Repair' }
    return 'Upgrade'
}

function Resolve-PackagePath {
    if ($PackagePath) {
        # Checked even when it was given explicitly. Without this, pointing at
        # the wrong folder installed nothing and reported "Payload copied" -
        # the worst possible outcome, because the Store looks installed.
        $given = (Resolve-Path -LiteralPath $PackagePath).Path
        if (-not (Test-Path -LiteralPath (Join-Path $given 'SpeakLinkReceiverBackground.exe'))) {
            throw ("No Receiver payload found in $given - it does not contain " +
                   "SpeakLinkReceiverBackground.exe. Point -PackagePath at the " +
                   "unzipped Store Kit.")
        }
        return $given
    }
    # The kit as unzipped: this script sits beside the payload.
    foreach ($candidate in @($scriptRoot, (Join-Path $scriptRoot 'receiver'), (Split-Path -Parent $scriptRoot))) {
        if ($candidate -and (Test-Path -LiteralPath (Join-Path $candidate 'SpeakLinkReceiverBackground.exe'))) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw ("No Receiver payload found. Run this from the unzipped Store Kit, " +
           "or pass -PackagePath pointing at the folder that contains " +
           "SpeakLinkReceiverBackground.exe.")
}

function Stop-Runtime {
    Write-Step 'Stopping the Receiver'
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop; Write-Ok 'Scheduled task stopped.' }
    catch { Write-Note 'No running scheduled task to stop.' }

    foreach ($name in @('SpeakLinkReceiverBackground', 'SpeakLinkReceiver')) {
        Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object {
            try { $_ | Stop-Process -Force -ErrorAction Stop; Write-Ok "Stopped $name (pid $($_.Id))." }
            catch { Write-Problem "Could not stop $name (pid $($_.Id)): $($_.Exception.Message)" }
        }
    }
    # A file that a dying process still has open cannot be replaced, and the
    # failure looks like a corrupt package rather than a timing problem.
    Start-Sleep -Milliseconds 700
}

function Copy-Payload {
    param([string]$Source)
    Write-Step "Copying the Receiver into $InstallRoot"
    if ($DryRun) { Write-Note 'Dry run: nothing copied.'; return }

    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    $failures = @()
    Get-ChildItem -LiteralPath $Source -Recurse -File | ForEach-Object {
        $relative = $_.FullName.Substring($Source.Length).TrimStart('\')
        $destination = Join-Path $InstallRoot $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null

        # Retried, because an antivirus scanning a freshly written executable
        # holds it open for a moment and the copy fails with a sharing
        # violation. Three files out of forty-four failed this way once, all of
        # them PE binaries, and the installer reported a generic IO error.
        $copied = $false
        foreach ($attempt in 1..5) {
            try { Copy-Item -LiteralPath $_.FullName -Destination $destination -Force -ErrorAction Stop; $copied = $true; break }
            catch { Start-Sleep -Milliseconds (200 * $attempt) }
        }
        if (-not $copied) { $failures += $relative }
    }
    if ($failures.Count -gt 0) {
        throw ("These files could not be written, most likely because " +
               "antivirus was holding them open: " + ($failures -join ', '))
    }
    Write-Ok 'Payload copied.'
}

function Install-Task {
    Write-Step 'Registering the logon task'
    if ($DryRun) { Write-Note 'Dry run: task not registered.'; return }

    $action    = New-ScheduledTaskAction -Execute $backgroundExe
    $trigger   = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                    -DontStopIfGoingOnBatteries -StartWhenAvailable `
                    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
                    -ExecutionTimeLimit ([TimeSpan]::Zero)
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Ok "Task '$TaskName' registered for $env:USERNAME."
}

function Start-Runtime {
    if ($DryRun) { return }
    try { Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop; Write-Ok 'Receiver started.' }
    catch { Write-Problem "The task was registered but did not start: $($_.Exception.Message)" }
}

function Write-Config {
    param([object]$Existing)
    if (-not $BackendUrl -and -not $Existing) {
        throw "This machine has no settings yet, so -BackendUrl is required."
    }
    if ($DryRun) { return }
    New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null

    # An upgrade keeps every setting it is not explicitly told to change. This
    # is why an upgrade is not a re-enrolment: the HQ address and the chosen
    # audio device are the two things a Store was set up with, and losing
    # either turns a working shop into a silent one.
    $config = if ($Existing) { $Existing } else { [pscustomobject]@{} }
    $values = @{}
    foreach ($property in $config.PSObject.Properties) { $values[$property.Name] = $property.Value }
    if ($BackendUrl)        { $values['backend_url'] = $BackendUrl }
    if ($AudioOutputDevice) { $values['audio_output_device'] = $AudioOutputDevice }
    $values['audio_sink'] = $AudioSink

    ($values | ConvertTo-Json -Depth 5) | Set-Content -LiteralPath $configPath -Encoding utf8
    Write-Ok "Settings written to $configPath"
}

function Read-ExistingConfig {
    if (-not (Test-Path -LiteralPath $configPath)) { return $null }
    try { return (Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json) }
    catch { Write-Problem 'The existing settings file could not be read; it will be rewritten.'; return $null }
}

# ---------------------------------------------------------------------------
# The verbs
# ---------------------------------------------------------------------------
function Invoke-InstallOrUpgrade {
    param([string]$Verb, [object]$State)
    $source = Resolve-PackagePath
    Write-Note "Payload: $source"
    if ($State.Version) { Write-Note "Installed version: $($State.Version)" }

    Stop-Runtime
    Copy-Payload -Source $source
    Write-Config -Existing (Read-ExistingConfig)
    Install-Task
    Start-Runtime

    if ($Verb -eq 'Upgrade') {
        Write-Ok 'Upgrade complete. This Store is still enrolled - the Device credential was not touched.'
    } elseif ($State.HasCredential) {
        Write-Ok 'Install complete, and an existing Device credential was found and kept.'
    } else {
        Write-Ok 'Install complete.'
        Write-Note 'This Store is not enrolled yet. Run SpeakLinkStoreSetup.exe and enter the one-time enrolment code from HQ.'
    }
}

function Invoke-Repair {
    param([object]$State)
    Write-Note 'Repairing: the program files and the logon task are rebuilt; the Device credential and settings are kept.'
    $source = Resolve-PackagePath
    Stop-Runtime
    Copy-Payload -Source $source
    # Settings are NOT rewritten from parameters here unless they were passed:
    # a repair is for a machine whose configuration was right and whose runtime
    # was not.
    if ($BackendUrl -or $AudioOutputDevice) { Write-Config -Existing (Read-ExistingConfig) }
    Install-Task
    Start-Runtime
    Write-Ok 'Repair complete.'
    if (-not $State.HasCredential) {
        Write-Problem 'There is no Device credential on this machine, so it is not enrolled. Run SpeakLinkStoreSetup.exe.'
    }
}

function Invoke-Uninstall {
    param([object]$State)
    Stop-Runtime

    Write-Step 'Removing the logon task'
    if (-not $DryRun) {
        try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop; Write-Ok 'Task removed.' }
        catch { Write-Note 'No task to remove.' }
    }

    Write-Step "Removing the program files from $InstallRoot"
    if (-not $DryRun -and (Test-Path -LiteralPath $InstallRoot)) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok 'Program files removed.'
    }

    if ($RemoveCredential) {
        # Asked for explicitly. This is the difference between "take the
        # software off this machine" and "this machine is no longer this
        # Store" - and the second one cannot be undone from here, because the
        # credential is sealed to this Windows account and a new one has to be
        # issued by HQ.
        Write-Step 'Removing the Device credential and settings'
        if (-not $DryRun) {
            Remove-Item -LiteralPath $StateRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Ok 'Credential and settings removed. This machine would have to be enrolled again.'
        Write-Note 'The Device still exists at HQ. Revoke or delete it there if this machine is not coming back.'
    } else {
        Write-Ok 'Uninstall complete. The Device credential and settings were KEPT.'
        Write-Note 'Re-installing this kit will pick them up and the Store will not need enrolling again.'
        Write-Note 'Run with -RemoveCredential to remove them as well.'
    }
}

# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
$state = Get-InstalledState
$verb = Resolve-Action -State $state

Write-Host ''
Write-Host '  SpeakLink Store Kit' -ForegroundColor White
Write-Host '  -------------------' -ForegroundColor DarkGray
Write-Note ("This machine: program {0}, task {1}, enrolled {2}" -f `
    $(if ($state.HasProgram) { 'present' } else { 'absent' }),
    $(if ($state.HasTask) { 'present' } else { 'absent' }),
    $(if ($state.HasCredential) { 'yes' } else { 'no' }))
Write-Step "Action: $verb"
if ($DryRun) { Write-Problem 'Dry run - nothing on this machine will change.' }

switch ($verb) {
    'Install'   { Invoke-InstallOrUpgrade -Verb 'Install' -State $state }
    'Upgrade'   { Invoke-InstallOrUpgrade -Verb 'Upgrade' -State $state }
    'Repair'    { Invoke-Repair -State $state }
    'Uninstall' { Invoke-Uninstall -State $state }
}

Write-Host ''
