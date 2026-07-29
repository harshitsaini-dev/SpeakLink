<#
.SYNOPSIS
    Put a damaged EchoCast HQ installation back, without touching HQ's data.

.DESCRIPTION
    WHAT REPAIR MEANS HERE

    Application files and the scheduled task. Nothing else. The database, the
    keys, the configuration, the backups and the logs are HQ - 44 Stores, every
    user account, every Device credential - and this script never writes to any
    of them.

    That restraint is the entire design. The obvious "helpful" repair is to
    re-initialize when something looks wrong, and re-initializing is precisely
    how a persistent server becomes an empty one that starts cleanly and has
    lost every Store. This script would rather fail and say so.

    WHAT IT WILL DO

    Replace application files that are missing or whose hash does not match the
    package manifest. Re-register the scheduled task, but only after confirming
    the existing task is one of ours.

    WHAT IT WILL NOT DO

    Create a database. Reset a user. Rotate a key. Delete a backup. Re-enrol a
    Store. Remove anything from the persistent root.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [string]$TaskName = 'EchoCast HQ Runtime',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'EchoCast-AI\hq-app'),
    [string]$PersistentRoot = (Join-Path $env:LOCALAPPDATA 'EchoCast-AI\persistent-lan-server'),
    [string]$HqUser = "$env:USERDOMAIN\$env:USERNAME",
    [ValidateRange(5, 60)][int]$RepetitionMinutes = 10,
    [ValidateRange(1, 31)][int]$RepetitionDurationDays = 1,
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

Write-Output '=== repairing the EchoCast HQ installation ==='
Write-Output '  the persistent database, keys, config, backups and logs are NOT touched'
Write-Output ''

if (-not (Test-Path $PackagePath)) { throw "No package at $PackagePath." }
$PackagePath = (Resolve-Path $PackagePath).Path
$sumsFile = Join-Path $PackagePath 'SHA256SUMS.txt'
if (-not (Test-Path $sumsFile)) { throw 'That package has no SHA256SUMS.txt to repair against.' }
if ((Get-PeSubsystem (Join-Path $PackagePath 'EchoCastHQRuntime.exe')) -ne 2) {
    throw 'The package runtime is a console application. Refusing to repair with it.'
}

# ---------------------------------------------------------------------------
# The persistent root is inspected and reported. Never written.
# ---------------------------------------------------------------------------
$persistentReport = [ordered]@{
    'database' = (Join-Path $PersistentRoot 'data\echocast.db')
    'keys'     = (Join-Path $PersistentRoot 'keys')
    'config'   = (Join-Path $PersistentRoot 'config')
    'backups'  = (Join-Path $PersistentRoot 'backups')
    'logs'     = (Join-Path $PersistentRoot 'logs')
}
$missingData = @()
foreach ($entry in $persistentReport.GetEnumerator()) {
    $present = Test-Path $entry.Value
    Write-Output ('  {0,-10} {1}' -f $entry.Key, $(if ($present) { 'present - preserved' } else { 'MISSING' }))
    if (-not $present) { $missingData += $entry.Key }
}
if ($missingData -contains 'database' -or $missingData -contains 'keys') {
    throw ("HQ's own data is missing ($($missingData -join ', ')). This is not an " +
           'application-file problem and repairing application files would hide it. ' +
           "Restore from $PersistentRoot\backups, or run " +
           'Test-EchoCastPersistentLanServer.ps1 to see what survived. Nothing was changed.')
}

# ---------------------------------------------------------------------------
# Which application files are actually wrong
# ---------------------------------------------------------------------------
$broken = @()
foreach ($line in (Get-Content $sumsFile)) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split '  ', 2
    $relative = $parts[1] -replace '/', '\'
    $installed = Join-Path $InstallRoot $relative
    if (-not (Test-Path $installed)) { $broken += $relative; continue }
    if ((Get-FileHash $installed -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) {
        $broken += $relative
    }
}

if ($broken.Count -eq 0) {
    Write-Output ''
    Write-Output '  every application file already matches the package            PASS'
} else {
    Write-Output ''
    Write-Output "  $($broken.Count) application file(s) missing or corrupt:"
    $broken | Select-Object -First 10 | ForEach-Object { Write-Output "    - $_" }
    if ($broken.Count -gt 10) { Write-Output "    ... and $($broken.Count - 10) more" }
}

# ---------------------------------------------------------------------------
# Is the task ours, and is it right
# ---------------------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$taskVerdict = 'correct'
if (-not $task) {
    $taskVerdict = 'missing'
} else {
    $ran = ($task.Actions | ForEach-Object { "$($_.Execute)" }) -join ' '
    if ($ran -notmatch 'EchoCastHQRuntime\.exe') {
        throw ("A task named '$TaskName' exists and runs '$ran', which is not an " +
               'EchoCast HQ runtime. Refusing to modify a task that is not ours.')
    }
    $wrong = @()
    if ($task.Settings.MultipleInstances -ne 'IgnoreNew') { $wrong += 'MultipleInstances' }
    if ($task.Settings.StartWhenAvailable -ne $true) { $wrong += 'StartWhenAvailable' }
    if ($task.Settings.ExecutionTimeLimit -notin @('PT0S', $null, '')) { $wrong += 'ExecutionTimeLimit' }
    if (-not ($task.Triggers | Where-Object {
            $_.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' -and $_.Repetition.Interval })) {
        $wrong += 'recovery trigger'
    }
    if ($task.Principal.LogonType -ne 'Interactive') { $wrong += 'principal' }
    if ($wrong.Count -gt 0) { $taskVerdict = "wrong: $($wrong -join ', ')" }
}
Write-Output "  scheduled task: $taskVerdict"

if ($broken.Count -eq 0 -and $taskVerdict -eq 'correct') {
    Write-Output ''
    Write-Output 'Nothing needed repairing.'
    Write-Output 'ECHOCAST_HQ_REPAIR_NOT_NEEDED'
    exit 0
}

if ($DryRun -or -not $PSCmdlet.ShouldProcess($TaskName, 'Repair the EchoCast HQ installation')) {
    Write-Output ''
    Write-Output 'Dry run: nothing was replaced and no task was changed.'
    exit 0
}

# ---------------------------------------------------------------------------
# Replace only what is broken
# ---------------------------------------------------------------------------
foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name = 'EchoCastHQRuntime.exe'" |
                       Where-Object { $_.ExecutablePath -and
                                      $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })) {
    Write-Output "  stopping the running HQ runtime, PID $($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($broken.Count -gt 0) { Start-Sleep -Seconds 3 }

foreach ($relative in $broken) {
    $source = Join-Path $PackagePath $relative
    $target = Join-Path $InstallRoot $relative
    if (-not (Test-Path $source)) { throw "The package is missing $relative too." }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item $source $target -Force
}
if ($broken.Count -gt 0) {
    Write-Output "  replaced $($broken.Count) application file(s)                          PASS"
}

if ($taskVerdict -ne 'correct') {
    if ($task) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    $installedExe = Join-Path $InstallRoot 'EchoCastHQRuntime.exe'
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
    Write-Output '  scheduled task re-registered                                 PASS'
}

Write-Output ''
Write-Output 'ECHOCAST_HQ_REPAIRED'
Write-Output "Verify : .\Test-EchoCastHQAutoStart.ps1 -TaskName `"$TaskName`" -InstallRoot `"$InstallRoot`""
Write-Output 'The persistent database, keys, configuration, backups and logs were'
Write-Output 'not read for content, not copied and not modified.'
