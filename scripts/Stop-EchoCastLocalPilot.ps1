<#
.SYNOPSIS
    Stop the EchoCast LOCAL PILOT MODE processes started by
    Start-EchoCastLocalPilot.ps1.

.DESCRIPTION
    Reads only the scoped pilot PID files, verifies that each recorded process
    really is an EchoCast pilot process before touching it, asks it to stop
    gracefully, and only force-terminates a verified pilot PID after a
    documented timeout.

    It never terminates arbitrary Python, Node or Uvicorn processes, and it
    never deletes the pilot database.
#>
[CmdletBinding()]
param(
    [string]$PilotRoot,
    [int]$GracefulTimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
if (-not $PilotRoot) {
    $PilotRoot = Join-Path $env:LOCALAPPDATA 'EchoCast-AI\local-pilot'
}
$pilotRuntime = Join-Path $PilotRoot 'runtime'

Write-Output '=== Stopping EchoCast LOCAL PILOT MODE ==='

function Stop-PilotProcess {
    param(
        [string]$PidFile,
        [string]$Label,
        [string[]]$ExpectedCommandFragments
    )

    if (-not (Test-Path $PidFile)) {
        Write-Output "  $Label : no PID file, nothing to stop."
        return
    }

    $raw = (Get-Content $PidFile -Raw).Trim()
    $processId = 0
    if (-not [int]::TryParse($raw, [ref]$processId) -or $processId -le 0) {
        Write-Output "  $Label : PID file is unreadable; removing the stale file."
        Remove-Item $PidFile -Force
        return
    }

    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $processId" -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Write-Output "  $Label : process $processId is not running; removing the stale PID file."
        Remove-Item $PidFile -Force
        return
    }

    # Only touch a process that is provably an EchoCast pilot process.
    $commandLine = [string]$process.CommandLine
    $belongsToPilot = $false
    foreach ($fragment in $ExpectedCommandFragments) {
        if ($commandLine -like "*$fragment*") { $belongsToPilot = $true }
    }
    if (-not $belongsToPilot) {
        Write-Output "  $Label : process $processId does NOT look like an EchoCast pilot process. Refusing to stop it."
        Write-Output "           Inspect it yourself, then remove $PidFile if it is stale."
        return
    }

    Write-Output "  $Label : stopping process $processId gracefully..."
    try {
        Stop-Process -Id $processId -ErrorAction Stop
    } catch {
        Write-Output "  $Label : could not signal process $processId; it may have already exited."
    }

    $deadline = (Get-Date).AddSeconds($GracefulTimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-Process -Id $processId -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 250
    }

    if ($null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)) {
        Write-Output "  $Label : still running after $GracefulTimeoutSeconds s; force-terminating the verified pilot PID."
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    if (Test-Path $PidFile) { Remove-Item $PidFile -Force }
    Write-Output "  $Label : stopped."
}

Stop-PilotProcess -PidFile (Join-Path $pilotRuntime 'frontend.pid') `
    -Label 'Frontend' `
    -ExpectedCommandFragments @('yarn', 'craco', 'react-scripts')

Stop-PilotProcess -PidFile (Join-Path $pilotRuntime 'backend.pid') `
    -Label 'Backend ' `
    -ExpectedCommandFragments @('uvicorn')

# Clear process-scoped pilot values from this session.
foreach ($name in 'ADMIN_PASSWORD', 'JWT_SECRET', 'ECHOCAST_DB_PATH', 'REACT_APP_BACKEND_URL') {
    if (Test-Path "env:$name") { Remove-Item "env:$name" -ErrorAction SilentlyContinue }
}

Write-Output ''
Write-Output 'Local pilot processes stopped and session pilot variables cleared.'
Write-Output "The pilot database was NOT deleted. It remains under $PilotRoot\data."
Write-Output 'To remove it deliberately, use the reset command documented in'
Write-Output 'LOCAL_PILOT_TEST_RUNBOOK.md (it requires an explicit --reset-pilot-db flag).'
