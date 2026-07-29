<#
.SYNOPSIS
    Stop the persistent HQ server. Stops nothing else.

.DESCRIPTION
    Only processes this server started, identified by the PID recorded in its
    lock file AND re-checked against the command line before anything is
    stopped. A recorded PID alone is not proof: Windows reuses process numbers,
    and a stale lock file can name a PID that now belongs to somebody's editor.

    The database, keys, logs and backups are left exactly where they are. That
    is the entire point of a persistent server.
#>
[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'

$profileJson = & $python -c @"
import json, sys
sys.path.insert(0, r'$repositoryRoot')
from tools.persistent_lan_server import ServerProfile
p = ServerProfile.persistent()
print(json.dumps({'lock': str(p.lock), 'database': str(p.database)}))
"@
$serverProfile = $profileJson | ConvertFrom-Json

Write-Output '=== stopping the EchoCast persistent LAN server ==='

if (-not (Test-Path $serverProfile.lock)) {
    Write-Output '  no lock file - this server does not appear to be running'
    Write-Output ''
    Write-Output 'ECHOCAST_PERSISTENT_SERVER_NOT_RUNNING'
    exit 0
}

$lock = Get-Content $serverProfile.lock -Raw | ConvertFrom-Json
Write-Output "  recorded backend PID : $($lock.backend_pid)"

$process = Get-CimInstance Win32_Process -Filter "ProcessId = $($lock.backend_pid)" -ErrorAction SilentlyContinue
if (-not $process) {
    Write-Output '  that PID no longer exists - removing the stale lock only'
    Remove-Item $serverProfile.lock -Force
    Write-Output ''
    Write-Output 'ECHOCAST_PERSISTENT_SERVER_NOT_RUNNING'
    exit 0
}

# Windows reuses PIDs. Before stopping anything, confirm it is really ours.
if ($process.CommandLine -notmatch 'uvicorn' -or $process.CommandLine -notmatch 'server:app') {
    Write-Output "  PID $($lock.backend_pid) is now '$($process.Name)', which is NOT our backend."
    Write-Output '  Refusing to stop it. Removing the stale lock file only.'
    Remove-Item $serverProfile.lock -Force
    Write-Output ''
    Write-Output 'ECHOCAST_PERSISTENT_SERVER_NOT_RUNNING'
    exit 0
}

if ($PSCmdlet.ShouldProcess("PID $($lock.backend_pid)", 'Stop the persistent backend')) {
    Stop-Process -Id $lock.backend_pid -Force -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Remove-Item $serverProfile.lock -Force
    Write-Output '  backend stopped'
}

Write-Output ''
Write-Output 'ECHOCAST_PERSISTENT_SERVER_STOPPED'
Write-Output "The database is untouched at $($serverProfile.database)"
Write-Output 'Starting again reuses it, with the same users, Stores and Devices.'
