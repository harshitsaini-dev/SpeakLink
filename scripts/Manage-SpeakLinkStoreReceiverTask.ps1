<#
.SYNOPSIS
    Query, start or stop the SpeakLink Store Receiver task. Nothing else.

.DESCRIPTION
    WHY THIS EXISTS AS ITS OWN SCRIPT

    SpeakLinkStoreSetup.exe's Rerun screen needs to start, stop and check the
    Store Receiver task from Python. Building a PowerShell -Command STRING in
    Python and shelling it out would mean gluing a task name into text a shell
    parses - exactly the injection shape this project refuses everywhere else.
    A dedicated script takes the task name as a real parameter, never as text
    assembled into a command line.

    WHY IT VERIFIES OWNERSHIP BEFORE ACTING

    A task name is not proof of identity. Another task could exist under the
    same name, or -TaskName could be mistyped and match something unrelated on
    a shared machine. Start and Stop both refuse unless the task's own action
    names SpeakLinkReceiverBackground.exe - the same check the auto-start and
    HQ scripts already use before touching a task.

    Status never refuses: an absent task is reported, not thrown.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'SpeakLink Store Receiver',
    [Parameter(Mandatory)][ValidateSet('Status', 'Start', 'Stop')][string]$Action
)

$ErrorActionPreference = 'Stop'

function Test-IsOurTask {
    param($Task)
    if (-not $Task) { return $false }
    $executed = ($Task.Actions | ForEach-Object { $_.Execute }) -join ' '
    return $executed -match 'SpeakLinkReceiverBackground\.exe'
}

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if ($Action -eq 'Status') {
    if (-not $task) {
        Write-Output 'NOT_REGISTERED'
        exit 0
    }
    if (-not (Test-IsOurTask $task)) {
        Write-Output 'NOT_OURS'
        exit 0
    }
    Write-Output "STATE=$($task.State)"
    $live = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe'")
    Write-Output "PROCESS_COUNT=$($live.Count)"
    exit 0
}

if (-not $task) {
    throw "No scheduled task named '$TaskName' exists. Nothing to $($Action.ToLower())."
}
if (-not (Test-IsOurTask $task)) {
    $ran = ($task.Actions | ForEach-Object { $_.Execute }) -join ' '
    throw ("The task '$TaskName' runs '$ran', which is not " +
           "SpeakLinkReceiverBackground.exe - refusing: this task is not ours.")
}

if ($Action -eq 'Start') {
    Start-ScheduledTask -TaskName $TaskName
    Write-Output 'STARTED'
    exit 0
}

if ($Action -eq 'Stop') {
    Stop-ScheduledTask -TaskName $TaskName
    Write-Output 'STOPPED'
    exit 0
}
