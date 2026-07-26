<#
.SYNOPSIS
    Start SpeakLink in LOCAL PILOT MODE against the isolated pilot database.

.DESCRIPTION
    Starts one Uvicorn worker bound to 127.0.0.1 only, plus the existing React
    dev server. Runtime artifacts (PID files, logs) live outside the repository
    under the pilot root.

    This is local software pilot mode. It proves startup, login, the canonical
    catalog and Receiver authentication. It proves nothing about live audio,
    FFmpeg playback, amplifiers or audible Store speakers.

    The protected application database backend\speaklink_live.db is never used.

.NOTES
    The pilot password is read from the current session's ADMIN_PASSWORD
    environment variable and inherited by the child processes. It is never
    placed in a command argument, printed, or written to disk by this script.
#>
[CmdletBinding()]
param(
    [int]$BackendPort = 8000,
    [string]$PilotRoot
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repositoryRoot 'backend'
$frontendDir = Join-Path $repositoryRoot 'frontend'
$venvPython = Join-Path $backendDir '.venv\Scripts\python.exe'
$pilotTool = Join-Path $repositoryRoot 'tools\local_pilot.py'

if (-not $PilotRoot) {
    $PilotRoot = Join-Path $env:LOCALAPPDATA 'SpeakLink\local-pilot'
}
$pilotData = Join-Path $PilotRoot 'data\speaklink_local_pilot.db'
$pilotRuntime = Join-Path $PilotRoot 'runtime'
$pilotLogs = Join-Path $PilotRoot 'logs'
$backendPidFile = Join-Path $pilotRuntime 'backend.pid'
$frontendPidFile = Join-Path $pilotRuntime 'frontend.pid'

# Write-SpeakLinkPidFile records the process creation time next to the PID, so
# the stop script can refuse a PID that Windows has since recycled.
. (Join-Path $PSScriptRoot 'SpeakLinkProcessTree.ps1')

Write-Output '=== SpeakLink LOCAL PILOT MODE ==='

# --- Preconditions -------------------------------------------------------
if (-not (Test-Path $venvPython)) {
    throw "Python virtual environment not found at $venvPython. Create it first (see README.md)."
}
$yarn = Get-Command yarn -ErrorAction SilentlyContinue
if ($null -eq $yarn) {
    throw 'Yarn was not found on PATH. Install Yarn 1.22.x before starting the pilot frontend.'
}
if (-not (Test-Path $pilotData)) {
    throw "The pilot database is not prepared at $pilotData. Run the prepare command first (see LOCAL_PILOT_TEST_RUNBOOK.md)."
}
if ([string]::IsNullOrWhiteSpace($env:ADMIN_PASSWORD)) {
    throw 'ADMIN_PASSWORD is not set for this PowerShell session. Set a temporary pilot-only value before starting. It is never stored by this script.'
}
if ([string]::IsNullOrWhiteSpace($env:JWT_SECRET)) {
    throw 'JWT_SECRET is not set for this PowerShell session. Set a temporary pilot-only value before starting. It is never stored by this script.'
}

$existing = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -eq $BackendPort }
if ($existing) {
    throw "Port $BackendPort is already in use by process $($existing.OwningProcess). Choose another port with -BackendPort or stop that process yourself."
}

foreach ($directory in @($pilotRuntime, $pilotLogs)) {
    if (-not (Test-Path $directory)) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
}

# --- Point this session at the isolated pilot database -------------------
$env:SPEAKLINK_DB_PATH = $pilotData
if ([string]::IsNullOrWhiteSpace($env:ADMIN_USERNAME)) { $env:ADMIN_USERNAME = 'pilot-operator' }
$env:CORS_ORIGINS = 'http://localhost:3000'
$backendUrl = "http://127.0.0.1:$BackendPort"
$env:REACT_APP_BACKEND_URL = $backendUrl

# --- Backend: loopback only, exactly one Uvicorn worker ------------------
$backendOut = Join-Path $pilotLogs 'backend.out.log'
$backendErr = Join-Path $pilotLogs 'backend.err.log'
$backend = Start-Process -FilePath $venvPython `
    -ArgumentList @('-m', 'uvicorn', 'server:app', '--host', '127.0.0.1', '--port', "`"$BackendPort`"", '--workers', '1') `
    -WorkingDirectory $backendDir `
    -RedirectStandardOutput $backendOut `
    -RedirectStandardError $backendErr `
    -PassThru
Write-SpeakLinkPidFile -PidFile $backendPidFile -ProcessId $backend.Id

# --- Frontend: existing Yarn script --------------------------------------
$frontend = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/c', 'yarn', 'start') `
    -WorkingDirectory $frontendDir `
    -PassThru
Write-SpeakLinkPidFile -PidFile $frontendPidFile -ProcessId $frontend.Id

Write-Output ''
Write-Output 'Started in LOCAL PILOT MODE (isolated database, loopback only).'
Write-Output "  Backend URL      : $backendUrl"
Write-Output '  Frontend URL     : http://localhost:3000'
Write-Output "  Pilot database   : $pilotData"
Write-Output "  Backend PID file : $backendPidFile"
Write-Output "  Frontend PID file: $frontendPidFile"
Write-Output "  Backend logs     : $backendOut"
Write-Output '  Uvicorn workers  : 1 (required; connection state is process-local)'
Write-Output ''
Write-Output 'The protected database backend\speaklink_live.db is NOT in use.'
Write-Output 'This proves local software behaviour only - not live audio, not'
Write-Output 'FFmpeg playback, not amplifiers, and not audible Store speakers.'
Write-Output ''
Write-Output 'Stop it with: .\scripts\Stop-SpeakLinkLocalPilot.ps1'
