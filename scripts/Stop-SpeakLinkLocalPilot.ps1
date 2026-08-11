<#
.SYNOPSIS
    Stop the SpeakLink LOCAL PILOT MODE processes started by
    Start-SpeakLinkLocalPilot.ps1.

.DESCRIPTION
    Reads only the scoped pilot PID files, verifies that each recorded process
    really is an SpeakLink pilot process before touching it, then stops the
    COMPLETE process tree that PID owns - deepest descendant first - and reports
    success only after every owned PID is confirmed gone and its port released.

    Stopping only the recorded PID is not enough on Windows. 'yarn start' becomes
    a chain of cmd.exe and node.exe hops, and the process that actually holds
    port 3000 sits several levels below the PID recorded at launch:

        cmd.exe /c yarn start          <- the PID in frontend.pid
        +- node.exe corepack yarn.js
           +- cmd.exe /d /s /c "craco start"
              +- node.exe craco
                 +- cmd.exe /d /s /c "node ..."
                    +- node.exe        <- actually listening on 3000

    The ownership decision lives in tools\process_tree.py so it can be tested
    against a fixed process table (backend\tests\test_process_tree.py).

    It never terminates arbitrary Python, Node or Uvicorn processes, never stops
    a process by name, and never deletes the pilot database.
#>
[CmdletBinding()]
param(
    [string]$PilotRoot,
    [int]$GracefulTimeoutSeconds = 15
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
if (-not $PilotRoot) {
    $PilotRoot = Join-Path $env:LOCALAPPDATA 'SpeakLink\local-pilot'
}
$pilotRuntime = Join-Path $PilotRoot 'runtime'
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'

. (Join-Path $PSScriptRoot 'SpeakLinkProcessTree.ps1')

Write-Output '=== Stopping SpeakLink LOCAL PILOT MODE ==='

if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython. It is required to work out which processes the pilot owns; refusing to stop anything by guesswork."
}

$frontendStopped = Stop-SpeakLinkProcessTree `
    -PidFile (Join-Path $pilotRuntime 'frontend.pid') `
    -Label 'Frontend' `
    -ExpectedCommandFragments @('yarn', 'craco', 'react-scripts') `
    -VenvPython $venvPython `
    -RepositoryRoot $repositoryRoot `
    -GracefulTimeoutSeconds $GracefulTimeoutSeconds

$backendStopped = Stop-SpeakLinkProcessTree `
    -PidFile (Join-Path $pilotRuntime 'backend.pid') `
    -Label 'Backend ' `
    -ExpectedCommandFragments @('uvicorn') `
    -VenvPython $venvPython `
    -RepositoryRoot $repositoryRoot `
    -GracefulTimeoutSeconds $GracefulTimeoutSeconds

# A released port is the independent check: if either is still bound, something
# survived whatever the process list said.
Write-Output ''
Write-Output 'Verifying the pilot ports were released:'
$backendPortFree = Test-SpeakLinkPortReleased -Port 8000
$frontendPortFree = Test-SpeakLinkPortReleased -Port 3000

# Clear process-scoped pilot values from this session.
foreach ($name in 'ADMIN_PASSWORD', 'JWT_SECRET', 'SPEAKLINK_DB_PATH', 'REACT_APP_BACKEND_URL') {
    if (Test-Path "env:$name") { Remove-Item "env:$name" -ErrorAction SilentlyContinue }
}

Write-Output ''
Write-Output "The pilot database was NOT deleted. It remains under $PilotRoot\data."
Write-Output 'To remove it deliberately, use the reset command documented in'
Write-Output 'docs/LOCAL_PILOT_TEST_RUNBOOK.md (it requires an explicit --reset-pilot-db flag).'
Write-Output ''

# Report success only when it is true. The previous version printed "stopped"
# while the real dev server was still holding port 3000.
$allClean = $frontendStopped -and $backendStopped -and $backendPortFree -and $frontendPortFree
if ($allClean) {
    Write-Output 'Local pilot fully stopped: every owned process is gone, both ports are'
    Write-Output 'released, PID files are removed and session pilot variables are cleared.'
    exit 0
}

Write-Output 'LOCAL PILOT NOT FULLY STOPPED.'
if (-not $frontendStopped) { Write-Output '  - frontend process tree still has survivors' }
if (-not $backendStopped) { Write-Output '  - backend process tree still has survivors' }
if (-not $frontendPortFree) { Write-Output '  - port 3000 is still bound' }
if (-not $backendPortFree) { Write-Output '  - port 8000 is still bound' }
Write-Output 'Session pilot variables were cleared, but inspect the survivors above'
Write-Output 'before starting another pilot. Their PID files were deliberately kept.'
exit 1
