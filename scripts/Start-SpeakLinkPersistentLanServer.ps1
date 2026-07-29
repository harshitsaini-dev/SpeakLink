<#
.SYNOPSIS
    Start the persistent HQ server. Same database, same keys, every time.

.DESCRIPTION
    The difference from Start-SpeakLinkLanPilot.ps1 is the whole point:

      pilot       ...\lan-pilot\<timestamp>\lan-pilot.db   + a new random admin
      persistent  ...\persistent-lan-server\data\speaklink.db  + the accounts already in it

    This script builds no dated path, generates no administrator and creates no
    database. If the persistent database is not there it refuses and tells you
    to initialize, rather than helpfully producing an empty one that looks like
    it is working until somebody notices every Store has vanished.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$HqAddress = '192.168.4.134',
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "Python virtual environment not found at $python." }

Write-Output '=== SpeakLink PERSISTENT LAN SERVER ==='

if ($HqAddress -notmatch '^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') {
    throw "Refusing to bind $HqAddress. This pilot serves a private LAN only."
}

$profileJson = & $python -c @"
import json, sys
sys.path.insert(0, r'$repositoryRoot')
from tools.persistent_lan_server import ServerProfile, describe_profile
p = ServerProfile.persistent()
print(json.dumps({'root': str(p.root), 'database': str(p.database),
                  'keys': str(p.key_container), 'logs': str(p.logs),
                  'lock': str(p.lock), 'mode': p.mode}))
"@
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the persistent server profile.' }
$serverProfile = $profileJson | ConvertFrom-Json

Write-Output "  mode      : $($serverProfile.mode)"
Write-Output "  database  : $($serverProfile.database)"
Write-Output "  logs      : $($serverProfile.logs)"
Write-Output "  bind      : $HqAddress`:$BackendPort  (frontend $FrontendPort)"

# The refusal that matters. Creating an empty database here is what turned a
# missing-data problem into a "the Store disappeared" problem.
if (-not (Test-Path $serverProfile.database)) {
    throw ("There is no persistent database at $($serverProfile.database). " +
           'This script will NOT create an empty one. Run ' +
           '.\scripts\Initialize-SpeakLinkPersistentLanServer.ps1 -SourceDatabase ' +
           '"backend\speaklink_live.db" first.')
}

# One server, not two fighting over one SQLite file and one port.
$busy = @(Get-NetTCPConnection -State Listen -LocalPort $BackendPort -ErrorAction SilentlyContinue)
if ($busy.Count -gt 0) {
    throw ("Port $BackendPort is already in use, so a server is already running - " +
           'possibly the throwaway pilot. Stop it first with ' +
           '.\scripts\Stop-SpeakLinkPersistentLanServer.ps1 or ' +
           '.\scripts\Stop-SpeakLinkLanPilot.ps1.')
}

$health = & $python -c @"
import json, sqlite3
path = r'$($serverProfile.database)'
con = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
def one(sql, default='-'):
    try: return con.execute(sql).fetchone()[0]
    except sqlite3.Error: return default
print(json.dumps({'integrity': one('PRAGMA integrity_check'),
                  'users': one('SELECT COUNT(*) FROM hq_users'),
                  'stores': one('SELECT COUNT(*) FROM stores'),
                  'devices': one('SELECT COUNT(*) FROM receiver_devices')}))
con.close()
"@
$state = $health | ConvertFrom-Json
Write-Output "  integrity : $($state.integrity)"
Write-Output "  contains  : $($state.users) user(s), $($state.stores) Store(s), $($state.devices) Device(s)"
if ($state.integrity -ne 'ok') { throw 'The persistent database fails integrity_check. Refusing to start.' }

if ($DryRun -or -not $PSCmdlet.ShouldProcess($serverProfile.database, 'Start the persistent server')) {
    Write-Output ''
    Write-Output 'Dry run: every input was checked. Nothing was started.'
    exit 0
}

New-Item -ItemType Directory -Force -Path $serverProfile.logs | Out-Null

# A JWT signing secret that survives restarts, so a token minted before a
# restart is not silently invalidated by one. Generated once, kept beside the
# keys, never printed and never committed.
$secretFile = Join-Path (Split-Path -Parent $serverProfile.keys) 'jwt-secret.txt'
if (-not (Test-Path $secretFile)) {
    $generated = & $python -c "import secrets; print(secrets.token_urlsafe(48))"
    Set-Content -Path $secretFile -Value $generated -Encoding ascii -NoNewline
    Write-Output '  jwt secret: created (value never printed)'
} else {
    Write-Output '  jwt secret: reused'
}
$jwtSecret = (Get-Content $secretFile -Raw).Trim()

$backendLog = Join-Path $serverProfile.logs 'backend.log'
$backendErr = Join-Path $serverProfile.logs 'backend.err.log'

$environment = @{
    SPEAKLINK_DB_PATH       = $serverProfile.database
    SPEAKLINK_KEY_CONTAINER = $serverProfile.keys
    SPEAKLINK_KEY_PROTECTOR = 'dpapi'
    SPEAKLINK_SERVER_MODE   = $serverProfile.mode
    JWT_SECRET             = $jwtSecret
    CORS_ORIGINS           = "http://$HqAddress`:$FrontendPort"
}
foreach ($name in $environment.Keys) { Set-Item -Path "Env:$name" -Value $environment[$name] }

Write-Output ''
Write-Output '  starting backend (one Uvicorn worker)...'
$backend = Start-Process -FilePath $python `
    # Every interpolated value is quoted. PowerShell joins -ArgumentList with
    # spaces and does NOT quote them, so a value containing a space silently
    # truncates the argument - which is how an earlier launcher failed to start
    # the Receiver at all. There is a repository test that enforces this.
    -ArgumentList @('-m', 'uvicorn', 'server:app', '--host', "`"$HqAddress`"",
                    '--port', "`"$BackendPort`"", '--workers', '1', '--no-access-log') `
    -WorkingDirectory (Join-Path $repositoryRoot 'backend') `
    -RedirectStandardOutput $backendLog -RedirectStandardError $backendErr `
    -WindowStyle Hidden -PassThru

@{ backend_pid = $backend.Id; started_utc = (Get-Date).ToUniversalTime().ToString('o')
   database = $serverProfile.database; mode = $serverProfile.mode } |
    ConvertTo-Json | Set-Content -Encoding ascii $serverProfile.lock

Start-Sleep -Seconds 4
if ($backend.HasExited) {
    throw "The backend exited immediately (code $($backend.ExitCode)). Read $backendErr."
}

Write-Output "  backend running, PID $($backend.Id)"
Write-Output ''
Write-Output 'SPEAKLINK_PERSISTENT_SERVER_STARTED'
Write-Output "  API       : http://$HqAddress`:$BackendPort"
Write-Output "  database  : $($serverProfile.database)  (the SAME one next restart)"
Write-Output "  logs      : $($serverProfile.logs)"
Write-Output ''
Write-Output 'Serve the dashboard separately from frontend\build against this API.'
Write-Output 'This is the PERSISTENT server. It is not the throwaway pilot, and it'
Write-Output 'does not create a new database or a new administrator on each start.'
