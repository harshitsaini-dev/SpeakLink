<#
.SYNOPSIS
    Verify the persistent HQ server. Read-only.

.DESCRIPTION
    A check that cannot be read reports UNKNOWN, never PASS and never FAIL, and
    no marker is emitted while anything is unreadable.

    The check that matters most is the one that would have caught the original
    problem: the database this server uses must NOT be a throwaway pilot file,
    and the path must not contain a timestamp.
#>
[CmdletBinding()]
param([string]$HqAddress = '192.168.4.134', [int]$BackendPort = 8000)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'

$script:results = @()
function Check {
    param([string]$CheckLabel, [scriptblock]$CheckBody)
    try { $verdict = & $CheckBody } catch { $verdict = 'UNKNOWN' }
    $outcome = switch ($verdict) { $true { 'PASS' } $false { 'FAIL' } default { [string]$verdict } }
    $script:results += [PSCustomObject]@{ Name = $CheckLabel; Verdict = $outcome }
    Write-Output ('  {0,-56}{1}' -f $CheckLabel, $outcome)
}

$profileJson = & $python -c @"
import json, sys
sys.path.insert(0, r'$repositoryRoot')
from tools.persistent_lan_server import ServerProfile, PERSISTENT_MARKER
p = ServerProfile.persistent()
print(json.dumps({'root': str(p.root), 'database': str(p.database),
                  'keys': str(p.key_container), 'logs': str(p.logs),
                  'lock': str(p.lock), 'backups': str(p.backups),
                  'mode': p.mode, 'marker': PERSISTENT_MARKER}))
"@
$serverProfile = $profileJson | ConvertFrom-Json

Write-Output '=== SpeakLink persistent LAN server verification ==='
Write-Output "  mode     : $($serverProfile.mode)"
Write-Output "  database : $($serverProfile.database)"
Write-Output ''

Check 'the data root exists' { Test-Path $serverProfile.root }
Check 'the persistent database exists' { Test-Path $serverProfile.database }

# The check that would have caught the original problem.
Check 'the database path contains no timestamp' {
    $serverProfile.database -notmatch '\d{8}-\d{6}'
}
Check 'the database is not a throwaway pilot file' {
    $verdict = & $python -c @"
import sys
sys.path.insert(0, r'$repositoryRoot')
from tools.persistent_lan_server import is_throwaway_pilot_database
print('THROWAWAY' if is_throwaway_pilot_database(r'$($serverProfile.database)') else 'PERSISTENT')
"@
    $verdict.Trim() -eq 'PERSISTENT'
}

Check 'the database passes integrity_check' {
    if (-not (Test-Path $serverProfile.database)) { throw 'no database yet' }
    $result = & $python -c @"
import sqlite3
con = sqlite3.connect('file:' + r'$($serverProfile.database)' + '?mode=ro', uri=True)
print(con.execute('PRAGMA integrity_check').fetchone()[0]); con.close()
"@
    $result.Trim() -eq 'ok'
}

Check 'it holds at least one enabled administrator' {
    # Roles are compared in Python rather than in SQL. The first version put an
    # IN ('OWNER','ADMIN',...) list inside a double-quoted PowerShell here-string
    # and the nested quoting broke, so the check reported UNKNOWN against a
    # database that was completely fine. A quoting accident should not look like
    # a missing administrator.
    if (-not (Test-Path $serverProfile.database)) { throw 'no database yet' }
    $count = & $python -c @"
import sqlite3
con = sqlite3.connect('file:' + r'$($serverProfile.database)' + '?mode=ro', uri=True)
privileged = {'OWNER', 'ADMIN', 'SUPER_ADMIN', 'admin'}
total = 0
for role, active in con.execute('SELECT role, is_active FROM hq_users'):
    if active and str(role) in privileged:
        total += 1
print(total)
con.close()
"@
    [int]($count | Select-Object -Last 1).Trim() -ge 1
}

Check 'the keys directory exists' { Test-Path (Split-Path -Parent $serverProfile.keys) }
Check 'a backups directory exists' { Test-Path $serverProfile.backups }

Check 'no throwaway pilot is running on the same port' {
    $listening = @(Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue)
    if ($listening.Count -eq 0) { return $true }   # nothing running at all is fine here
    $owners = $listening | ForEach-Object {
        (Get-CimInstance Win32_Process -Filter "ProcessId = $($_.OwningProcess)" -ErrorAction SilentlyContinue).CommandLine
    }
    # A pilot backend and a persistent one cannot both own this port.
    -not ($owners -join ' ' -match 'lan-pilot')
}

Check 'the lock file, if present, names a live backend' {
    if (-not (Test-Path $serverProfile.lock)) { return 'NOT RUNNING' }
    $lock = Get-Content $serverProfile.lock -Raw | ConvertFrom-Json
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $($lock.backend_pid)" -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    # PIDs are reused; confirm it is really ours.
    [bool]($process.CommandLine -match 'uvicorn' -and $process.CommandLine -match 'server:app')
}

Check 'no secret is written into the lock file' {
    if (-not (Test-Path $serverProfile.lock)) { return $true }
    $text = Get-Content $serverProfile.lock -Raw
    $text -notmatch '(?i)(password|secret|token|bearer|speaklink_rcv_v1)'
}

$failed = @($script:results | Where-Object { $_.Verdict -like 'FAIL*' })
$unknown = @($script:results | Where-Object { $_.Verdict -eq 'UNKNOWN' })

Write-Output ''
if ($unknown.Count -gt 0) {
    Write-Output "  $($unknown.Count) check(s) could not be read - not a pass, not a failure:"
    $unknown | ForEach-Object { Write-Output "    - $($_.Name)" }
}
if ($failed.Count -gt 0) {
    Write-Output ''
    Write-Output "Result: FAILED ($($failed.Count) check(s))"
    $failed | ForEach-Object { Write-Output "  - $($_.Name)" }
    exit 1
}
if ($unknown.Count -gt 0) { Write-Output ''; Write-Output 'Result: INCOMPLETE.'; exit 1 }

Write-Output ''
Write-Output "  $($script:results.Count) checks passed"
Write-Output ''
Write-Output 'SPEAKLINK_PERSISTENT_SERVER_VERIFIED'
Write-Output ''
Write-Output 'Scope: this database is fixed, readable and not a throwaway pilot file.'
Write-Output 'It says nothing about whether a Store reconnects or any sound was heard.'
exit 0
