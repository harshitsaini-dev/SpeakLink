<#
.SYNOPSIS
    Remove the SpeakLink HQ auto-start and its application files. Keep the data.

.DESCRIPTION
    Uninstalling an application and destroying HQ's data are different
    decisions, and this script only makes the first one. The persistent root -
    database, keys, configuration, backups, logs - is left exactly where it is,
    so re-installing later brings back 44 Stores rather than an empty server.

    -RemovePersistentData exists as a declared parameter and refuses to run. It
    is declared so that keeping the data reads as a decision rather than an
    oversight, and refused because deleting every Store, user and Device
    credential is not something this sprint should be able to do by flag. When
    that flow is built it will need its own typed confirmation, a verified
    backup first, and a separate review.

    WHAT IS VERIFIED BEFORE ANYTHING IS REMOVED

    That the task runs a SpeakLink HQ runtime, and that a process to be stopped
    was started from the install root being uninstalled. A task name and a
    process name are both things somebody else can use.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'SpeakLink HQ Runtime',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\hq-app'),
    [string]$PersistentRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\persistent-lan-server'),
    [switch]$KeepApplicationFiles,
    [switch]$RemovePersistentData,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

if ($RemovePersistentData) {
    throw ('-RemovePersistentData is not implemented. Removing the persistent ' +
           'root deletes every Store, user account and Device credential HQ has, ' +
           'and that needs a verified backup and a typed confirmation this script ' +
           'does not have. Nothing was changed.')
}

Write-Output '=== removing SpeakLink HQ auto-start ==='
Write-Output "  persistent data at $PersistentRoot is PRESERVED"
Write-Output ''

# ---------------------------------------------------------------------------
# The task - only if it is ours
# ---------------------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$removeTask = $false
if (-not $task) {
    Write-Output "  no scheduled task named '$TaskName'"
} else {
    $ran = ($task.Actions | ForEach-Object { "$($_.Execute)" }) -join ' '
    if ($ran -notmatch 'SpeakLinkHQRuntime\.exe') {
        throw ("The task '$TaskName' runs '$ran', which is not a SpeakLink HQ " +
               'runtime. Refusing to remove a task that is not ours. Nothing was changed.')
    }
    Write-Output "  task '$TaskName' runs $ran - ours, will be removed"
    $removeTask = $true
}

# ---------------------------------------------------------------------------
# The processes - only ones started from this install root
# ---------------------------------------------------------------------------
$live = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkHQRuntime.exe'" |
          Where-Object { $_.ExecutablePath -and
                         $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
foreach ($process in $live) {
    Write-Output "  will stop HQ runtime PID $($process.ProcessId) ($($process.ExecutablePath))"
}
$others = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkHQRuntime.exe'" |
            Where-Object { -not ($_.ExecutablePath -and
                                 $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase')) })
foreach ($process in $others) {
    Write-Output "  NOT stopping PID $($process.ProcessId): it runs from $($process.ExecutablePath)"
}

if ($DryRun -or -not $PSCmdlet.ShouldProcess($TaskName, 'Remove SpeakLink HQ auto-start')) {
    Write-Output ''
    Write-Output 'Dry run: nothing was stopped, unregistered or deleted.'
    exit 0
}

foreach ($process in $live) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($live.Count -gt 0) {
    Start-Sleep -Seconds 3
    Write-Output "  stopped $($live.Count) HQ runtime process(es)                        PASS"
}

if ($removeTask) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output '  scheduled task removed                                       PASS'
}

# The children the runtime started are uvicorn and http.server running under
# the machine's Python, so they are matched by their command line rather than
# by their image name - stopping every python.exe on an HQ desk would be a
# rude way to end an uninstall.
$children = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
              Where-Object { $_.CommandLine -and
                             ($_.CommandLine -match 'uvicorn\s+server:app' -or
                              $_.CommandLine -match 'http\.server') -and
                             $_.CommandLine -match [regex]::Escape($PersistentRoot.Split('\')[-1]) })
foreach ($process in $children) {
    Write-Output "  stopping child PID $($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}

if (-not $KeepApplicationFiles) {
    $resolvedPersistent = if (Test-Path $PersistentRoot) { (Resolve-Path $PersistentRoot).Path } else { $PersistentRoot }
    if ($InstallRoot.StartsWith($resolvedPersistent, 'OrdinalIgnoreCase')) {
        throw 'The install root is inside the persistent data root. Refusing to delete it.'
    }
    if (Test-Path $InstallRoot) {
        Remove-Item $InstallRoot -Recurse -Force
        Write-Output "  application files removed from $InstallRoot            PASS"
    }
}

$statusFile = Join-Path $env:LOCALAPPDATA 'SpeakLink\hq-runtime-status.json'
if (Test-Path $statusFile) { Remove-Item $statusFile -Force }

Write-Output ''
Write-Output 'SPEAKLINK_HQ_AUTO_START_REMOVED'
Write-Output ''
Write-Output "PRESERVED: $PersistentRoot"
Write-Output '  the database, keys, configuration, backups and logs are untouched.'
Write-Output '  Re-installing brings HQ back with every Store, user and Device.'
