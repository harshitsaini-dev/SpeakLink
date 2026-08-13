<#
.SYNOPSIS
    Remove the Store Receiver. Keeps the Device credential unless told otherwise.

.DESCRIPTION
    Uninstalling the software is not a decision to un-enrol the Store. Those are
    different decisions with different consequences: reinstalling is free,
    re-enrolling needs an administrator at HQ to issue a fresh one-time code and
    somebody at the Store to type it. So the DPAPI credential survives by
    default, and removing it takes an explicit switch.

    Logs also survive by default. They are the only record of why a Store was
    being uninstalled, and deleting them during a fault is how the fault gets
    investigated twice.

    Processes are matched by full executable path before anything is stopped.
    Nothing else on the machine is touched.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = 'SpeakLink Store Receiver',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver-app'),
    [switch]$RemoveCredential,
    [switch]$RemoveLogs,
    [switch]$RemoveSettings
)

$ErrorActionPreference = 'Stop'

$stateRoot = Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver'
$credentialPath = Join-Path $stateRoot 'device-credential.bin'
$configPath = Join-Path $stateRoot 'config.json'
$logDirectory = Join-Path $stateRoot 'logs'

Write-Output '=== removing the SpeakLink Store Receiver ==='

# ---- the task --------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $execute = ($task.Actions | ForEach-Object { $_.Execute }) -join ' '
    if ($execute -notmatch 'SpeakLinkReceiver') {
        throw ("The task '$TaskName' runs '$execute', which is not a SpeakLink " +
               'executable. Refusing to remove a task this script did not install.')
    }
    if ($PSCmdlet.ShouldProcess($TaskName, 'Unregister the scheduled task')) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Output '  scheduled task removed'
    }
} else {
    Write-Output "  no scheduled task named '$TaskName'"
}

# ---- running processes, matched by path ------------------------------------
$running = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe' OR Name = 'SpeakLinkReceiver.exe'" |
             Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
if ($running.Count -eq 0) {
    Write-Output '  no Receiver running from the install root'
} else {
    # Background FIRST, then the console Receiver.
    #
    # The scheduled task is already unregistered above, so Windows will not
    # relaunch anything - but both executables are stopped here and the order
    # they are stopped in is not arbitrary. SpeakLinkReceiverBackground is the
    # one the task starts and the one that owns the run; stopping the console
    # Receiver while the background one is still alive risks the very restart
    # race this ordering removes. Sorting is cheap insurance against a
    # non-deterministic Get-CimInstance order.
    $ordered = @($running | Sort-Object -Property @{
        Expression = { if ($_.Name -eq 'SpeakLinkReceiverBackground.exe') { 0 } else { 1 } }
    })
    foreach ($process in $ordered) {
        Write-Output "  stopping PID $($process.ProcessId)  $($process.ExecutablePath)"
        if ($PSCmdlet.ShouldProcess("PID $($process.ProcessId)", 'Stop the Receiver')) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }

    # WAIT for the processes to actually be gone, bounded.
    #
    # This was a flat 'Start-Sleep -Seconds 2', which is a guess in both
    # directions: it wastes two seconds when the process died instantly, and it
    # is not enough when Windows takes longer - and then Remove-Item below hits
    # a still-locked SpeakLinkReceiver.exe and the whole uninstall fails on a
    # race rather than on anything real.
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline) {
        $stillRunning = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe' OR Name = 'SpeakLinkReceiver.exe'" |
                          Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
        if ($stillRunning.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    $stillRunning = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe' OR Name = 'SpeakLinkReceiver.exe'" |
                      Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
    if ($stillRunning.Count -gt 0) {
        $names = ($stillRunning | ForEach-Object { "PID $($_.ProcessId)" }) -join ', '
        throw ("The Receiver is still running after 20 seconds ($names). " +
               'Nothing has been deleted. Close it and run Uninstall again, or ' +
               'restart this computer and try once more.')
    }
    Write-Output '  all Receiver processes have exited'
}

# ---- the installed program --------------------------------------------------
if (Test-Path $InstallRoot) {
    if ($PSCmdlet.ShouldProcess($InstallRoot, 'Remove the installed program')) {
        # Retried briefly rather than attempted once. A file handle can outlive
        # the process that held it by a moment, and antivirus routinely holds
        # an executable open just after it exits - both produce a failure that
        # is gone a second later, and neither is worth failing an uninstall on.
        # The message on the last attempt names the actual obstacle instead of
        # surfacing a raw PowerShell error.
        $removed = $false
        foreach ($attempt in 1..5) {
            try {
                Remove-Item $InstallRoot -Recurse -Force -ErrorAction Stop
                $removed = $true
                break
            } catch {
                if ($attempt -eq 5) {
                    throw ("Could not remove $InstallRoot because a file there is " +
                           "still in use: $($_.Exception.Message) " +
                           'The scheduled task and the Receiver processes have ' +
                           'already been stopped, so nothing is running. Close ' +
                           'any window open in that folder, or restart this ' +
                           'computer, then run Uninstall again.')
                }
                Start-Sleep -Milliseconds 500
            }
        }
        if ($removed) { Write-Output "  removed $InstallRoot" }
    }
} else {
    Write-Output '  nothing installed at the install root'
}

# ---- everything below survives unless explicitly asked for -------------------
if ($RemoveSettings -and (Test-Path $configPath)) {
    if ($PSCmdlet.ShouldProcess($configPath, 'Remove the saved settings')) {
        Remove-Item $configPath -Force
        Write-Output '  settings removed'
    }
} elseif (Test-Path $configPath) {
    Write-Output '  settings KEPT (pass -RemoveSettings to delete)'
}

if ($RemoveLogs -and (Test-Path $logDirectory)) {
    if ($PSCmdlet.ShouldProcess($logDirectory, 'Remove the logs')) {
        Remove-Item $logDirectory -Recurse -Force
        Write-Output '  logs removed'
    }
} elseif (Test-Path $logDirectory) {
    Write-Output '  logs KEPT (pass -RemoveLogs to delete)'
}

if ($RemoveCredential -and (Test-Path $credentialPath)) {
    Write-Output ''
    Write-Output '  WARNING: removing the Device credential UN-ENROLS this Store.'
    Write-Output '           Getting it back needs an administrator at HQ to issue a'
    Write-Output '           fresh one-time code and somebody here to type it.'
    # The same sentence receiver_agent.remove_local_credential already prints.
    # It was missing here, which is how an operator ends up with a Device that
    # HQ still lists as enrolled and that will never connect again - a Store
    # that looks fine on the dashboard and is silent.
    Write-Output '           The Device is NOT revoked at HQ. Ask an administrator to'
    Write-Output '           revoke it, or it stays listed as enrolled while never'
    Write-Output '           connecting again.'
    if ($PSCmdlet.ShouldProcess($credentialPath, 'Remove the Device credential')) {
        Remove-Item $credentialPath -Force
        Write-Output '  Device credential removed - this Store is no longer enrolled'
    }
} elseif (Test-Path $credentialPath) {
    Write-Output '  Device credential KEPT - this Store stays enrolled'
}

Write-Output ''
Write-Output 'SPEAKLINK_STORE_RECEIVER_REMOVED'
