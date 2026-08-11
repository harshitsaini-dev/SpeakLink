<#
.SYNOPSIS
    Repair the persistent HQ runtime without touching its data.

.DESCRIPTION
    Repair recreates the things that can be rebuilt and refuses to touch the
    things that cannot.

    REBUILT IF MISSING
        the folder layout, a missing logs or backups directory, a stale lock
        file left by a process that no longer exists.

    NEVER TOUCHED
        the database, the key ring, the JWT secret, users, Stores, Receiver
        Devices, history, migration reports and existing backups.

    That split is the whole design. A repair tool that can recreate a database
    is a repair tool that will one day recreate it over the real one, and the
    operator will find out when every Store has vanished. This one cannot: if
    the database is missing it says so and stops, because a missing database is
    a restore decision, not a repair.
#>
[CmdletBinding(SupportsShouldProcess)]
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "Python virtual environment not found at $python." }

$profileJson = & $python -c @"
import json, sys
sys.path.insert(0, r'$repositoryRoot')
from tools.persistent_lan_server import ServerProfile
p = ServerProfile.persistent()
print(json.dumps({'root': str(p.root), 'database': str(p.database),
                  'keys': str(p.key_container), 'logs': str(p.logs),
                  'backups': str(p.backups), 'lock': str(p.lock), 'mode': p.mode}))
"@
$serverProfile = $profileJson | ConvertFrom-Json

Write-Output '=== repairing the SpeakLink persistent LAN server ==='
Write-Output "  mode     : $($serverProfile.mode)"
Write-Output "  root     : $($serverProfile.root)"

if (-not (Test-Path $serverProfile.root)) {
    throw ("There is no persistent server at $($serverProfile.root). Repair cannot " +
           'create one - run Initialize-SpeakLinkPersistentLanServer.ps1 instead.')
}

# The refusal that matters most.
if (-not (Test-Path $serverProfile.database)) {
    throw ("The persistent database is missing from $($serverProfile.database). " +
           'Repair will NOT create one: an empty database that looks healthy is ' +
           'worse than an obvious absence. Restore the newest file from ' +
           "$($serverProfile.backups) or re-run Initialize with a named source.")
}

$actions = @()

foreach ($folder in @('data', 'config', 'keys', 'logs', 'backups', 'runtime', 'migration-reports')) {
    $path = Join-Path $serverProfile.root $folder
    if (-not (Test-Path $path)) { $actions += "create missing folder $folder" }
}

# A lock naming a PID that is gone, or one that now belongs to something else,
# stops the server starting for no reason. Windows reuses process numbers, so
# the command line is checked too.
if (Test-Path $serverProfile.lock) {
    $lock = Get-Content $serverProfile.lock -Raw | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($lock.backend_pid)" -ErrorAction SilentlyContinue
    if (-not $process) {
        $actions += "remove stale lock (PID $($lock.backend_pid) no longer exists)"
    } elseif ($process.CommandLine -notmatch 'uvicorn' -or $process.CommandLine -notmatch 'server:app') {
        $actions += "remove stale lock (PID $($lock.backend_pid) is now '$($process.Name)', not our backend)"
    } else {
        Write-Output "  a backend is running as PID $($lock.backend_pid) - leaving the lock alone"
    }
}

$integrity = & $python -c @"
import sqlite3
con = sqlite3.connect('file:' + r'$($serverProfile.database)' + '?mode=ro&immutable=1', uri=True)
print(con.execute('PRAGMA integrity_check').fetchone()[0]); con.close()
"@
$integrity = ($integrity | Select-Object -Last 1).Trim()
Write-Output "  database integrity : $integrity"
if ($integrity -ne 'ok') {
    throw ("The persistent database fails integrity_check. Repair will not touch it. " +
           "Restore from $($serverProfile.backups) and verify before starting.")
}

$counts = & $python -c @"
import json, sqlite3
con = sqlite3.connect('file:' + r'$($serverProfile.database)' + '?mode=ro&immutable=1', uri=True)
def one(sql):
    try: return con.execute(sql).fetchone()[0]
    except sqlite3.Error: return '-'
print(json.dumps({'users': one('SELECT COUNT(*) FROM hq_users'),
                  'stores': one('SELECT COUNT(*) FROM stores'),
                  'devices': one('SELECT COUNT(*) FROM receiver_devices'),
                  'sessions': one('SELECT COUNT(*) FROM broadcast_sessions'),
                  'logs': one('SELECT COUNT(*) FROM system_logs')}))
con.close()
"@
$state = ($counts | Select-Object -Last 1) | ConvertFrom-Json
Write-Output "  preserved : $($state.users) user(s), $($state.stores) Store(s), $($state.devices) Device(s), $($state.sessions) session(s), $($state.logs) log(s)"

Write-Output ''
if ($actions.Count -eq 0) {
    Write-Output '  nothing to repair'
} else {
    Write-Output '  PLAN'
    $actions | ForEach-Object { Write-Output "    - $_" }
}

if ($DryRun) {
    Write-Output ''
    Write-Output 'Dry run: nothing was changed.'
    exit 0
}

foreach ($folder in @('data', 'config', 'keys', 'logs', 'backups', 'runtime', 'migration-reports')) {
    $path = Join-Path $serverProfile.root $folder
    if (-not (Test-Path $path) -and $PSCmdlet.ShouldProcess($path, 'Create folder')) {
        New-Item -ItemType Directory -Force -Path $path | Out-Null
        Write-Output "  created $folder"
    }
}

if ($actions -match 'remove stale lock') {
    if ($PSCmdlet.ShouldProcess($serverProfile.lock, 'Remove the stale lock')) {
        Remove-Item $serverProfile.lock -Force
        Write-Output '  stale lock removed'
    }
}

Write-Output ''
Write-Output 'SPEAKLINK_PERSISTENT_SERVER_REPAIRED'
Write-Output 'The database, keys, users, Stores, Devices, history and backups were'
Write-Output 'not touched. Verify with .\scripts\Test-SpeakLinkPersistentLanServer.ps1'
