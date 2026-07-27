<#
.SYNOPSIS
    Remove the disposable SpeakLink Receiver logon task, and only that task.

.DESCRIPTION
    Two rules, both learned the hard way earlier in this work.

    It refuses to remove a task it did not recognise. The name is a parameter,
    so a typo could name somebody else's scheduled task; this checks the task's
    action actually points at an SpeakLinkReceiver executable before removing it.

    It stops only Receiver processes started from the package the task names,
    verified by executable path. Nothing else on the machine is touched - not
    unrelated Python, not a language server, not a Node process that happens to
    look busy. "Stop anything that looks like it might be ours" is how a cleanup
    script kills an editor mid-save.

    The DPAPI credential is left alone. Removing an autorun task is not a
    decision to un-enrol a Device, and the two must stay separate: use
    `SpeakLinkReceiver.exe remove-local-credential` for that, which asks first.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'SpeakLink Receiver LAN Pilot (disposable)',
    [switch]$StopRunning
)

$ErrorActionPreference = 'Stop'

Write-Output '=== removing the SpeakLink Receiver logon task ==='

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Output "  no scheduled task named '$TaskName' is registered"
    Write-Output ''
    Write-Output 'SPEAKLINK_RECEIVER_TASK_ABSENT'
    exit 0
}

$execute = ($task.Actions | ForEach-Object { $_.Execute }) -join ' '
if ($execute -notmatch 'SpeakLinkReceiver') {
    throw ("The task '$TaskName' runs '$execute', which is not an SpeakLinkReceiver " +
           'executable. Refusing to remove a task this script did not install.')
}
Write-Output "  task    : $TaskName"
Write-Output "  runs    : $execute"

if ($StopRunning) {
    # Matched on the full executable path, not on a process name. Ownership is
    # checked before anything is stopped.
    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiver.exe'" |
                 Where-Object { $_.ExecutablePath -eq $execute })
    if ($running.Count -eq 0) {
        Write-Output '  running : none started from that executable'
    } else {
        foreach ($process in $running) {
            Write-Output "  stopping: PID $($process.ProcessId)  $($process.ExecutablePath)"
            if ($PSCmdlet.ShouldProcess("PID $($process.ProcessId)", 'Stop the Receiver')) {
                Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister the scheduled task')) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output '  task unregistered'
}

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    throw "The task '$TaskName' is still registered after the removal attempt."
}

Write-Output ''
Write-Output 'SPEAKLINK_RECEIVER_TASK_REMOVED'
Write-Output 'The DPAPI Device credential was NOT removed. That is a separate,'
Write-Output 'confirmed decision: SpeakLinkReceiver.exe remove-local-credential'
