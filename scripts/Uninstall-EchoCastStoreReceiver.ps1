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
    [string]$TaskName = 'EchoCast Store Receiver',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'EchoCast-AI\receiver-app'),
    [switch]$RemoveCredential,
    [switch]$RemoveLogs,
    [switch]$RemoveSettings
)

$ErrorActionPreference = 'Stop'

$stateRoot = Join-Path $env:LOCALAPPDATA 'EchoCast-AI\receiver'
$credentialPath = Join-Path $stateRoot 'device-credential.bin'
$configPath = Join-Path $stateRoot 'config.json'
$logDirectory = Join-Path $stateRoot 'logs'

Write-Output '=== removing the EchoCast Store Receiver ==='

# ---- the task --------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($task) {
    $execute = ($task.Actions | ForEach-Object { $_.Execute }) -join ' '
    if ($execute -notmatch 'EchoCastReceiver') {
        throw ("The task '$TaskName' runs '$execute', which is not an EchoCast " +
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
$running = @(Get-CimInstance Win32_Process -Filter "Name = 'EchoCastReceiverBackground.exe' OR Name = 'EchoCastReceiver.exe'" |
             Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
if ($running.Count -eq 0) {
    Write-Output '  no Receiver running from the install root'
} else {
    foreach ($process in $running) {
        Write-Output "  stopping PID $($process.ProcessId)  $($process.ExecutablePath)"
        if ($PSCmdlet.ShouldProcess("PID $($process.ProcessId)", 'Stop the Receiver')) {
            Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2
}

# ---- the installed program --------------------------------------------------
if (Test-Path $InstallRoot) {
    if ($PSCmdlet.ShouldProcess($InstallRoot, 'Remove the installed program')) {
        Remove-Item $InstallRoot -Recurse -Force
        Write-Output "  removed $InstallRoot"
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
    if ($PSCmdlet.ShouldProcess($credentialPath, 'Remove the Device credential')) {
        Remove-Item $credentialPath -Force
        Write-Output '  Device credential removed - this Store is no longer enrolled'
    }
} elseif (Test-Path $credentialPath) {
    Write-Output '  Device credential KEPT - this Store stays enrolled'
}

Write-Output ''
Write-Output 'ECHOCAST_STORE_RECEIVER_REMOVED'
