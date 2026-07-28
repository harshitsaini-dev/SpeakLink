<#
.SYNOPSIS
    Register a disposable At-Logon scheduled task that starts the packaged
    EchoCast Receiver on this computer.

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
    [string]$TaskName = 'EchoCast Receiver LAN Pilot (disposable)',
    [string]$LogDirectory,
    [string]$CredentialPath,
    [int]$RestartCount = 3,
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'EchoCast-AI\receiver-app'),
    [switch]$RunInPlace,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

Write-Output '=== installing the EchoCast Receiver logon task (pilot, disposable) ==='

# ---------------------------------------------------------------------------
# Inputs. Everything is checked before anything is registered.
# ---------------------------------------------------------------------------
$PackagePath = (Resolve-Path $PackagePath).Path
$exe = Join-Path $PackagePath 'EchoCastReceiver.exe'
if (-not (Test-Path $exe)) {
    throw "No EchoCastReceiver.exe in $PackagePath. Build one with .\scripts\Build-EchoCastReceiver.ps1."
}

$packagedFfmpeg = Join-Path $PackagePath 'ffmpeg.exe'
if (-not (Test-Path $packagedFfmpeg)) {
    throw "The package at $PackagePath has no ffmpeg.exe beside the executable. The Agent resolves FFmpeg relative to itself and would find nothing."
}
if (Test-Path (Join-Path $PackagePath 'STALE-DO-NOT-DEPLOY.txt')) {
    throw "The package at $PackagePath is marked STALE-DO-NOT-DEPLOY. It is kept as evidence, not for deployment."
}

# ---------------------------------------------------------------------------
# Where the task will point. Not, by default, wherever the operator happened to
# unzip the kit.
#
# A scheduled task stores an absolute path and runs it at every logon, for
# months. If that path is a USB stick, the Store stops working the moment
# somebody takes the stick home. If it is Downloads or Temp, it stops working
# the first time Windows Storage Sense tidies up, and the failure arrives weeks
# after the cause with nothing connecting the two. So the package is copied to
# a stable per-user location and the task points at the copy.
# ---------------------------------------------------------------------------
function Test-UnstableLocation {
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetPathRoot($full)
    $drive = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID = '$($root.TrimEnd('\'))'" -ErrorAction SilentlyContinue
    # DriveType 2 = removable, 4 = network.
    if ($drive -and $drive.DriveType -in 2, 4) { return "a removable or network drive ($root)" }
    foreach ($unstable in @($env:TEMP, (Join-Path $env:USERPROFILE 'Downloads'))) {
        if ($unstable -and $full.StartsWith([System.IO.Path]::GetFullPath($unstable), 'OrdinalIgnoreCase')) {
            return "a temporary or Downloads folder ($unstable)"
        }
    }
    return $null
}

$unstable = Test-UnstableLocation $PackagePath
if ($RunInPlace) {
    if ($unstable) {
        throw ("-RunInPlace was given, but $PackagePath is on $unstable. A logon task " +
               'stores an absolute path and runs it for months; that one will disappear. ' +
               'Drop -RunInPlace so the package is copied to a stable location.')
    }
    Write-Output "  install     : running in place from $PackagePath"
    $installedRoot = $PackagePath
} else {
    $installedRoot = $InstallRoot
    Write-Output "  install     : copying the package to $installedRoot"
}

if ($BackendUrl -match '^http://' -and $BackendUrl -notmatch '^http://(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') {
    throw "Refusing to install a task that sends a Device credential over plain HTTP to a non-private address: $BackendUrl"
}
if ($BackendUrl -match '(?i)[?&](token|credential|password|secret)=') {
    throw 'The backend URL carries a credential in its query string. It would be stored in the task definition and readable by anyone who can open Task Scheduler.'
}

if (-not $LogDirectory) {
    $LogDirectory = Join-Path $env:LOCALAPPDATA 'EchoCast-AI\receiver\logs'
}

$sourcePackage = $PackagePath
$exe = Join-Path $installedRoot 'EchoCastReceiver.exe'

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

# Whole options, not substrings. The first version matched '--credential'
# anywhere, so the documented and entirely safe '--credential-path' - which
# names a FILE, and is how you point a Store at its own sealed credential -
# always tripped the guard. A guard that blocks a legitimate option is not
# being careful; it is being wrong in the safe-looking direction, which is the
# hardest kind to notice.
foreach ($forbidden in @('--code', '--credential', '--token', '--password', '--secret')) {
    if ($arguments -match ($([regex]::Escape($forbidden)) + '(?![-\w])')) {
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
# Copy, then re-hash the copy.
#
# Copying is where files get truncated, half-written or silently skipped, and a
# Receiver that is 99% copied does not announce that fact - it fails weeks later
# with a DLL error nobody connects to the install. So the installed copy is
# verified against the manifest the build produced, not assumed from the fact
# that Copy-Item did not throw.
# ---------------------------------------------------------------------------
if ($installedRoot -ne $sourcePackage) {
    if (Test-Path $installedRoot) {
        $stillRunning = @(Get-CimInstance Win32_Process -Filter "Name = 'EchoCastReceiver.exe'" |
                          Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($installedRoot, 'OrdinalIgnoreCase') })
        if ($stillRunning.Count -gt 0) {
            throw ("A Receiver is still running from $installedRoot (PID " +
                   (($stillRunning | ForEach-Object { $_.ProcessId }) -join ', ') +
                   '). Stop it before replacing the installed copy.')
        }
        Remove-Item $installedRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $installedRoot | Out-Null
    Copy-Item (Join-Path $sourcePackage '*') $installedRoot -Recurse -Force
    Write-Output "  copied      : $sourcePackage -> $installedRoot"

    $sums = Join-Path $installedRoot 'SHA256SUMS.txt'
    if (-not (Test-Path $sums)) { throw "The installed copy has no SHA256SUMS.txt; it cannot be verified." }
    $mismatched = @()
    foreach ($line in (Get-Content $sums)) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '  ', 2
        $file = Join-Path $installedRoot ($parts[1] -replace '/', '\')
        if (-not (Test-Path $file)) { $mismatched += "$($parts[1]) (missing)"; continue }
        if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) {
            $mismatched += $parts[1]
        }
    }
    if ($mismatched.Count -gt 0) {
        throw ("The installed copy does not match the package manifest: " +
               ($mismatched -join ', ') + '. Nothing was registered.')
    }
    Write-Output "  every file in the installed copy matches its build hash    PASS"
}

if (-not (Test-Path $exe)) { throw "No executable at $exe after installation." }

# ---------------------------------------------------------------------------
# Register
# ---------------------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output '  an earlier task of this name exists and will be replaced'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

$action = New-ScheduledTaskAction -Execute $exe -Argument $arguments -WorkingDirectory $installedRoot
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
                       -Description ('EchoCast Live Receiver, private LAN pilot. Disposable. ' +
                                     'Remove with .\scripts\Uninstall-EchoCastReceiverLanPilot.ps1.') | Out-Null

# ---------------------------------------------------------------------------
# Prove nothing secret was written into the definition
# ---------------------------------------------------------------------------
$xml = Export-ScheduledTask -TaskName $TaskName
foreach ($pattern in @('echocast_rcv_v1\.', 'ECHO-[A-Z0-9]{4}-', '(?i)password', '(?i)bearer', '(?i)secret')) {
    if ($xml -match $pattern) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        throw "The registered task definition matched /$pattern/. The task has been removed rather than left in place."
    }
}
Write-Output '  task definition carries no credential, code, password or secret   PASS'

Write-Output ''
Write-Output 'ECHOCAST_RECEIVER_TASK_INSTALLED'
Write-Output "Verify with .\scripts\Test-EchoCastReceiverLanPilot.ps1 -TaskName `"$TaskName`""
Write-Output "Remove with  .\scripts\Uninstall-EchoCastReceiverLanPilot.ps1 -TaskName `"$TaskName`""
Write-Output ''
Write-Output 'This task starts the Receiver at logon of this user only. It is not'
Write-Output 'evidence of boot-time, service, SYSTEM or locked-desktop operation.'
