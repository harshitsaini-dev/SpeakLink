<#
.SYNOPSIS
    Register a disposable At-Logon scheduled task that starts the packaged
    SpeakLink Receiver on this computer.

.DESCRIPTION
    WHAT THIS DOES AND, MORE IMPORTANTLY, WHAT IT DOES NOT

    The task starts the Receiver when THIS Windows user logs on. It is not a
    Windows service. It does not start before logon, it does not run as SYSTEM,
    and it does not keep a Store playing announcements while the desktop sits at
    the lock screen with nobody signed in. Those are real requirements for 44
    Store computers and none of them are met here - they need a service, and a
    service needs a different design, because the Receiver plays audio into a
    user's session and session 0 has no audio device.

    An At-Logon task is the right shape for a pilot on two desktops. It is not
    the shape of the production deployment, and this script is named for the
    pilot so nobody mistakes one for the other.

    WHY THE TASK STORES NO PASSWORD

    The principal is the current interactive user with no stored credential.
    A task that stores a password puts a reusable Windows credential into the
    Task Scheduler credential store on every Store computer, and the Receiver
    does not need it: it runs in the operator's own session, which is exactly
    where the sound card is.

    WHY RESTART IS BOUNDED

    Three restarts a minute apart, then it stops. An unbounded restart policy
    turns a Receiver that cannot authenticate into a machine that reconnects
    forever, which looks like a network problem, fills the backend log, and
    hides the actual refusal. When the task gives up, the Receiver's own log
    says why.

    NOTHING SECRET REACHES THE TASK

    The credential lives in DPAPI under the operator's account. The task
    definition carries a backend URL, a log directory and a credential file
    path - no code, no credential, no token. The XML is exported to a temporary
    file for inspection and that file is deleted; the task definition itself is
    readable by anyone who can read Task Scheduler, which is why it must stay
    boring.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [string]$BackendUrl = 'http://192.168.4.134:8000',
    [string]$ExpectedHqHost = '192.168.4.134',
    [string]$TaskName = 'SpeakLink Receiver LAN Pilot (disposable)',
    [string]$LogDirectory,
    [string]$CredentialPath,
    [int]$RestartCount = 3,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

Write-Output '=== installing the SpeakLink Receiver logon task (pilot, disposable) ==='

# ---------------------------------------------------------------------------
# Inputs. Everything is checked before anything is registered.
# ---------------------------------------------------------------------------
$exe = Join-Path $PackagePath 'SpeakLinkReceiver.exe'
if (-not (Test-Path $exe)) {
    throw "No SpeakLinkReceiver.exe in $PackagePath. Build one with .\scripts\Build-SpeakLinkReceiver.ps1."
}
$exe = (Resolve-Path $exe).Path

$packagedFfmpeg = Join-Path $PackagePath 'ffmpeg.exe'
if (-not (Test-Path $packagedFfmpeg)) {
    throw "The package at $PackagePath has no ffmpeg.exe beside the executable. The Agent resolves FFmpeg relative to itself and would find nothing."
}

if ($BackendUrl -match '^http://' -and $BackendUrl -notmatch '^http://(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') {
    throw "Refusing to install a task that sends a Device credential over plain HTTP to a non-private address: $BackendUrl"
}
if ($BackendUrl -match '(?i)[?&](token|credential|password|secret)=') {
    throw 'The backend URL carries a credential in its query string. It would be stored in the task definition and readable by anyone who can open Task Scheduler.'
}

if (-not $LogDirectory) {
    $LogDirectory = Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver\logs'
}

Write-Output "  task        : $TaskName"
Write-Output "  executable  : $exe"
Write-Output "  backend     : $BackendUrl"
Write-Output "  logs        : $LogDirectory"
Write-Output "  user        : $env:USERDOMAIN\$env:USERNAME (interactive, no stored password)"
Write-Output "  restarts    : $RestartCount, one minute apart, then it stops"
Write-Output '  NOT a service: no boot-before-logon, no SYSTEM, no locked-desktop operation'

# ---------------------------------------------------------------------------
# The command line. Nothing secret may appear on it.
# ---------------------------------------------------------------------------
$argumentList = @(
    'run'
    '--backend-url'; $BackendUrl
    '--log-directory'; $LogDirectory
)
if ($BackendUrl -match '^http://') {
    $argumentList += @('--allow-insecure-private-lan', '--expected-hq-host', $ExpectedHqHost)
}
if ($CredentialPath) { $argumentList += @('--credential-path', $CredentialPath) }

$arguments = ($argumentList | ForEach-Object {
    if ($_ -match '\s') { '"' + $_ + '"' } else { $_ }
}) -join ' '

foreach ($forbidden in @('--code', '--credential', '--token', '--password')) {
    if ($arguments -match [regex]::Escape($forbidden)) {
        throw "The task command line contains $forbidden. A credential must never be stored in a task definition."
    }
}

Write-Output "  arguments   : $arguments"

if ($DryRun -or -not $PSCmdlet.ShouldProcess($TaskName, 'Register the At-Logon Receiver task')) {
    Write-Output ''
    Write-Output 'Dry run: every input was checked. No task was registered.'
    exit 0
}

# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output '  an earlier task of this name exists and will be replaced'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $exe -Argument $arguments -WorkingDirectory $PackagePath
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount $RestartCount -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
                       -Principal $principal -Settings $settings `
                       -Description ('SpeakLink Receiver, private LAN pilot. Disposable. ' +
                                     'Remove with .\scripts\Uninstall-SpeakLinkReceiverLanPilot.ps1.') | Out-Null

# ---------------------------------------------------------------------------
# Prove nothing secret was written into the definition
# ---------------------------------------------------------------------------
$xml = Export-ScheduledTask -TaskName $TaskName
foreach ($pattern in @('speaklink_rcv_v1\.', 'ECHO-[A-Z0-9]{4}-', '(?i)password', '(?i)bearer', '(?i)secret')) {
    if ($xml -match $pattern) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        throw "The registered task definition matched /$pattern/. The task has been removed rather than left in place."
    }
}
Write-Output '  task definition carries no credential, code, password or secret   PASS'

Write-Output ''
Write-Output 'SPEAKLINK_RECEIVER_TASK_INSTALLED'
Write-Output "Verify with .\scripts\Test-SpeakLinkReceiverLanPilot.ps1 -TaskName `"$TaskName`""
Write-Output "Remove with  .\scripts\Uninstall-SpeakLinkReceiverLanPilot.ps1 -TaskName `"$TaskName`""
Write-Output ''
Write-Output 'This task starts the Receiver at logon of this user only. It is not'
Write-Output 'evidence of boot-time, service, SYSTEM or locked-desktop operation.'
