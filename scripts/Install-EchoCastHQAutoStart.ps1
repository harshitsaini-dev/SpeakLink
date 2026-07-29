<#
.SYNOPSIS
    Register the EchoCast HQ Runtime to start when the HQ user signs in.

.DESCRIPTION
    WHY A SCHEDULED TASK AND NOT A WINDOWS SERVICE

    The Store Receiver cannot be a service because session 0 has no audio
    endpoint. HQ has a different reason for the same answer: a service would
    have to run as SYSTEM or as an account whose Windows password is stored,
    and it would then own the persistent database. An interactive task owned by
    the HQ user needs no stored password and no privileged account, and the
    runtime already refuses to start against anything but an initialized
    persistent profile.

    What that costs, stated plainly: **HQ does not run before somebody signs
    in.** A locked screen with the HQ user still signed in is fine. A machine
    sitting at the Windows sign-in screen is not, and nothing in this script
    changes that. If HQ must survive an unattended reboot, the answer is
    automatic sign-in - which stores a password in the registry and is NOT
    configured here.

    WHY TWO TRIGGERS

    One at logon, which is the normal path. One separate time-based trigger
    with a bounded repetition, which is the recovery path. They are separate on
    purpose: a repetition attached only to a logon trigger begins only at a
    logon, so a runtime that dies at 11am would wait until the next morning.
    That was measured on the Store Receiver task and is fixed the same way here.

    WHY THE RUNTIME MAY EXIT

    EchoCastHQRuntime.exe gives up after a bounded number of failed starts and
    exits non-zero, rather than respawning a broken backend for ever. The
    periodic trigger is what brings it back, and MultipleInstances IgnoreNew is
    what stops the two paths from starting a second copy.

    WHAT IS PRESERVED

    Everything in the persistent root. This script reads it to decide whether to
    refuse and never writes a byte into it.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [string]$TaskName = 'EchoCast HQ Runtime',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'EchoCast-AI\hq-app'),
    [string]$PersistentRoot = (Join-Path $env:LOCALAPPDATA 'EchoCast-AI\persistent-lan-server'),
    [string]$HqUser = "$env:USERDOMAIN\$env:USERNAME",
    # Bounded at both ends. Below five minutes a machine spends its morning
    # starting servers that are already starting; above an hour the recovery
    # path is slower than an operator noticing.
    [ValidateRange(5, 60)][int]$RepetitionMinutes = 10,
    [ValidateRange(1, 31)][int]$RepetitionDurationDays = 1,
    [switch]$StartNow,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

function Get-PeSubsystem {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $reader = New-Object System.IO.BinaryReader($stream)
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset + 4 + 20 + 68
        return $reader.ReadUInt16()
    } finally { $stream.Dispose() }
}

Write-Output '=== installing EchoCast HQ auto-start ==='

# ---------------------------------------------------------------------------
# The package, checked before anything is written anywhere
# ---------------------------------------------------------------------------
if (-not (Test-Path $PackagePath)) { throw "No package at $PackagePath." }
$PackagePath = (Resolve-Path $PackagePath).Path

$runtimeExe = Join-Path $PackagePath 'EchoCastHQRuntime.exe'
$frontendIndex = Join-Path $PackagePath 'frontend\index.html'
foreach ($required in @($runtimeExe, $frontendIndex,
                        (Join-Path $PackagePath 'manifest.json'),
                        (Join-Path $PackagePath 'SHA256SUMS.txt'))) {
    if (-not (Test-Path $required)) {
        throw "The package is incomplete: $required is missing."
    }
}
if (Test-Path (Join-Path $PackagePath 'STALE-DO-NOT-DEPLOY.txt')) {
    throw 'That package is marked STALE-DO-NOT-DEPLOY. Build a fresh one.'
}

# Read from the header of the file about to be installed, not from the build
# log and not from the PyInstaller flag. Subsystem 3 is a console application,
# and a console application started by a scheduled task puts a black window on
# the HQ desk - which is the defect this whole line of work already fixed once.
$subsystem = Get-PeSubsystem $runtimeExe
if ($subsystem -ne 2) {
    throw ("EchoCastHQRuntime.exe in that package is a console application " +
           "(PE subsystem $subsystem, expected 2 / WINDOWS_GUI). Installing it " +
           "would put a console window on the HQ desktop. Rebuild it with " +
           'hq_runtime.spec.')
}

# ---------------------------------------------------------------------------
# The persistent profile. Read only. Refuse rather than initialize.
# ---------------------------------------------------------------------------
# The database and the folder layout, and deliberately NOT the key container or
# the signing secret. Nothing creates those until the first start - the backend
# mints the HMAC container and the runtime mints the signing secret - so
# demanding them here refused a correctly initialized HQ with an instruction no
# procedure in this repository can carry out. That was the defect in the runtime
# and it was the same defect here.
#
# Whether a MISSING container is normal or an emergency depends on how many
# Devices are enrolled, and only the runtime can count those. It refuses at
# start, with the number, which is where that check belongs.
$persistentDatabase = Join-Path $PersistentRoot 'data\echocast.db'
foreach ($required in @($persistentDatabase,
                        (Join-Path $PersistentRoot 'keys'),
                        (Join-Path $PersistentRoot 'data'))) {
    if (-not (Test-Path $required)) {
        throw ("The persistent HQ profile is not initialized: $required is " +
               "missing. Run Initialize-EchoCastPersistentLanServer.ps1 first. " +
               'This installer will not create one - an empty HQ that starts ' +
               'cleanly is how every Store disappears at once.')
    }
}

if ($HqUser -match '(?i)\b(SYSTEM|LOCAL SERVICE|NETWORK SERVICE|LOCALSERVICE|NETWORKSERVICE)\b') {
    throw ("Refusing to register HQ as $HqUser. A service account has no " +
           'interactive session, and giving one rights over the persistent ' +
           'database is a larger change than this script should make quietly.')
}

$installedExe = Join-Path $InstallRoot 'EchoCastHQRuntime.exe'
$manifest = Get-Content (Join-Path $PackagePath 'manifest.json') -Raw | ConvertFrom-Json

Write-Output "  package      : $(Split-Path -Leaf $PackagePath) ($($manifest.source_commit_short))"
Write-Output "  runtime      : WINDOWS_GUI (PE subsystem 2 - no console)"
Write-Output "  install to   : $InstallRoot"
Write-Output "  persistent   : $PersistentRoot (read only - never written by this script)"
Write-Output "  task         : $TaskName"
Write-Output "  runs as      : $HqUser (interactive, no stored password)"
Write-Output '  planned action:'
Write-Output "    execute          : $installedExe"
Write-Output '    arguments        : (none)'
Write-Output "    working directory: $InstallRoot"
Write-Output "  triggers     : at logon of $HqUser, plus recovery every $RepetitionMinutes minute(s) for $RepetitionDurationDays day(s)"
Write-Output '  HQ does NOT run before somebody signs in. That is a property of the'
Write-Output '  design, not a setting that was left off.'

if ($DryRun -or -not $PSCmdlet.ShouldProcess($TaskName, 'Install EchoCast HQ auto-start')) {
    Write-Output ''
    Write-Output 'Dry run: every input was checked. Nothing was installed or registered.'
    exit 0
}

# ---------------------------------------------------------------------------
# Stop whatever is running from the install root, then replace it
# ---------------------------------------------------------------------------
$running = @(Get-CimInstance Win32_Process -Filter "Name = 'EchoCastHQRuntime.exe'" |
             Where-Object { $_.ExecutablePath -and
                            $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
foreach ($process in $running) {
    Write-Output "  stopping the running HQ runtime, PID $($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($running.Count -gt 0) { Start-Sleep -Seconds 3 }

# A guard, not paranoia: an operator who passes the persistent root as the
# install root would otherwise delete the database on the next line.
$resolvedPersistent = (Resolve-Path $PersistentRoot).Path
if ($InstallRoot.StartsWith($resolvedPersistent, 'OrdinalIgnoreCase')) {
    throw 'Refusing to install application files inside the persistent data root.'
}

if (Test-Path $InstallRoot) { Remove-Item $InstallRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
Copy-Item (Join-Path $PackagePath '*') $InstallRoot -Recurse -Force

$mismatched = @()
foreach ($line in (Get-Content (Join-Path $InstallRoot 'SHA256SUMS.txt'))) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split '  ', 2
    $file = Join-Path $InstallRoot ($parts[1] -replace '/', '\')
    if (-not (Test-Path $file)) { $mismatched += "$($parts[1]) (missing)"; continue }
    if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) { $mismatched += $parts[1] }
}
if ($mismatched.Count -gt 0) {
    throw "The installed copy does not match the package manifest: $($mismatched -join ', ')."
}
Write-Output '  every installed file matches its build hash                 PASS'

if ((Get-PeSubsystem $installedExe) -ne 2) {
    throw 'The INSTALLED runtime is not windowed. The copy is wrong; nothing was registered.'
}
Write-Output '  the installed runtime is windowed                           PASS'

# ---------------------------------------------------------------------------
# The task
# ---------------------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    $ran = ($existing.Actions | ForEach-Object { $_.Execute }) -join ' '
    if ($ran -notmatch 'EchoCast') {
        throw ("A task named '$TaskName' already exists and runs '$ran', which is " +
               'not ours. Choose another -TaskName rather than replacing it.')
    }
    Write-Output '  replacing the earlier EchoCast task of the same name'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# No arguments at all. Everything configurable lives in the persistent
# profile's config/hq-runtime.json, so the task definition cannot go stale and
# has nowhere to carry anything secret.
$action = New-ScheduledTaskAction -Execute $installedExe -WorkingDirectory $InstallRoot

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $HqUser
$recoveryTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date) `
    -RepetitionInterval (New-TimeSpan -Minutes $RepetitionMinutes) `
    -RepetitionDuration (New-TimeSpan -Days $RepetitionDurationDays)

$principal = New-ScheduledTaskPrincipal -UserId $HqUser -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 2) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action `
    -Trigger @($logonTrigger, $recoveryTrigger) -Principal $principal -Settings $settings `
    -Description 'EchoCast HQ Runtime. Starts at sign-in, runs with no window.' | Out-Null

# Checking the variables is not checking the task. Read back what Windows
# actually stored.
$xml = Export-ScheduledTask -TaskName $TaskName
foreach ($pattern in @('echocast_rcv_v1', 'ECHO(-[A-Z0-9]{4}){2,}',
                       '(?i)password', '(?i)bearer', '(?i)eyJ[A-Za-z0-9_-]{10,}')) {
    if ($xml -match $pattern) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        throw "The registered task matched /$pattern/ and was removed rather than left in place."
    }
}
Write-Output '  the registered task carries nothing secret                   PASS'

# ---------------------------------------------------------------------------
# Starting is a separate decision from installing
# ---------------------------------------------------------------------------
if ($StartNow) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 10
    $live = @(Get-CimInstance Win32_Process -Filter "Name = 'EchoCastHQRuntime.exe'" |
              Where-Object { $_.ExecutablePath -and
                             $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
    if ($live.Count -gt 0) {
        Write-Output "  HQ runtime started, PID $($live[0].ProcessId), no window        PASS"
    } else {
        Write-Output '  the runtime did not stay running. Read its status and log:'
        Write-Output "    $env:LOCALAPPDATA\EchoCast-AI\hq-runtime-status.json"
        Write-Output "    $PersistentRoot\logs"
    }
} else {
    Write-Output '  registered but NOT started. Installing during business hours must'
    Write-Output '  not take the running pilot ports. Start it deliberately:'
    Write-Output "    Start-ScheduledTask -TaskName `"$TaskName`""
}

Write-Output ''
Write-Output 'ECHOCAST_HQ_AUTO_START_INSTALLED'
Write-Output "Verify : .\Test-EchoCastHQAutoStart.ps1 -TaskName `"$TaskName`" -InstallRoot `"$InstallRoot`""
Write-Output "Status : $env:LOCALAPPDATA\EchoCast-AI\hq-runtime-status.json"
Write-Output "Logs   : $PersistentRoot\logs"
