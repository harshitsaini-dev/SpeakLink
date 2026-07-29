<#
.SYNOPSIS
    Set up the persistent HQ data root once, from a named source database.

.DESCRIPTION
    PLANONLY BY DEFAULT. A first run changes nothing at all - it reads, checks
    and prints what it *would* do. Finding out what a tool intends should never
    be the same action as letting it do it.

    THE PROBLEM THIS SOLVES

    Start-EchoCastLanPilot.ps1 builds its data root from the clock:

        ...\lan-pilot\$(Get-Date -Format 'yyyyMMdd-HHmmss')

    so every start is a new, empty database with a new generated administrator.
    Correct for a throwaway pilot; wrong for a daily server. It is why the Store
    showed OFFLINE after a restart (its Device lives in an older pilot database)
    and why owneradmin could not sign in (that account is in
    backend/echocast_live.db, not in the pilot the backend was running).

    The persistent server uses ONE fixed root and reuses it for ever.

    WHAT IT WILL NOT DO

    It never deletes, moves or edits the source database - it copies from it
    using SQLite's backup API, which produces a consistent snapshot even if the
    source is in WAL mode. It refuses a throwaway pilot database as a source. It
    refuses to overwrite an existing persistent database unless told to, because
    that database is the thing everything else now depends on.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$SourceDatabase,
    [switch]$Apply,
    [switch]$ReplaceExisting
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw "Python virtual environment not found at $python." }

Write-Output '=== EchoCast persistent LAN server - initialization ==='
if (-not $Apply) {
    Write-Output '  MODE: PlanOnly. Nothing will be created or changed.'
    Write-Output '        Re-run with -Apply once the plan below looks right.'
} else {
    Write-Output '  MODE: APPLY. Changes will be made.'
}
Write-Output ''

# ---------------------------------------------------------------------------
# Where the persistent data will live. Resolved by the same module the server
# uses, so the two cannot disagree about the path.
# ---------------------------------------------------------------------------
$profileJson = & $python -c @"
import json, sys
sys.path.insert(0, r'$repositoryRoot')
from tools.persistent_lan_server import ServerProfile
p = ServerProfile.persistent()
print(json.dumps({'root': str(p.root), 'database': str(p.database),
                  'keys': str(p.key_container), 'backups': str(p.backups),
                  'logs': str(p.logs), 'config': str(p.config),
                  'lock': str(p.lock), 'mode': p.mode}))
"@
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the persistent server profile.' }
$serverProfile = $profileJson | ConvertFrom-Json

Write-Output "  mode      : $($serverProfile.mode)"
Write-Output "  root      : $($serverProfile.root)"
Write-Output "  database  : $($serverProfile.database)"
Write-Output "  keys      : $($serverProfile.keys)"
Write-Output "  backups   : $($serverProfile.backups)"
Write-Output ''

# ---------------------------------------------------------------------------
# The source, checked before anything is touched
# ---------------------------------------------------------------------------
if (-not (Test-Path $SourceDatabase)) { throw "No database at $SourceDatabase." }
$SourceDatabase = (Resolve-Path $SourceDatabase).Path

$sourceReport = & $python -c @"
import hashlib, json, sqlite3, sys
sys.path.insert(0, r'$repositoryRoot')
from tools.persistent_lan_server import is_throwaway_pilot_database
path = r'$SourceDatabase'
digest = hashlib.sha256(open(path,'rb').read()).hexdigest().upper()
con = sqlite3.connect('file:' + path + '?mode=ro', uri=True)
def one(sql, default='-'):
    try: return con.execute(sql).fetchone()[0]
    except sqlite3.Error: return default
report = {
    'throwaway': bool(is_throwaway_pilot_database(path)),
    'sha256': digest,
    'integrity': one('PRAGMA integrity_check'),
    'users': one('SELECT COUNT(*) FROM hq_users'),
    'stores': one('SELECT COUNT(*) FROM stores'),
    'devices': one('SELECT COUNT(*) FROM receiver_devices'),
    'sessions': one('SELECT COUNT(*) FROM broadcast_sessions'),
    'logs': one('SELECT COUNT(*) FROM system_logs'),
}
con.close()
print(json.dumps(report))
"@
if ($LASTEXITCODE -ne 0) { throw "The source database at $SourceDatabase could not be read." }
$source = $sourceReport | ConvertFrom-Json

Write-Output "  source    : $SourceDatabase"
Write-Output "    sha256      : $($source.sha256)"
Write-Output "    integrity   : $($source.integrity)"
Write-Output "    users       : $($source.users)"
Write-Output "    stores      : $($source.stores)"
Write-Output "    devices     : $($source.devices)"
Write-Output "    sessions    : $($source.sessions)"
Write-Output "    logs        : $($source.logs)"

if ($source.integrity -ne 'ok') {
    throw "The source database fails PRAGMA integrity_check ($($source.integrity)). Refusing."
}
if ($source.throwaway) {
    throw ("$SourceDatabase belongs to a throwaway pilot, which is rebuilt from " +
           'scratch on every start. Adopting it would make the persistent server ' +
           'forget everything the next time that folder is tidied. Name a real ' +
           'source database - normally backend\echocast_live.db.')
}

$targetExists = Test-Path $serverProfile.database
Write-Output ''
Write-Output "  target exists already : $targetExists"
if ($targetExists -and -not $ReplaceExisting) {
    Write-Output ''
    Write-Output '  A persistent database is already here, and everything now depends on it.'
    Write-Output '  This will NOT overwrite it. Pass -ReplaceExisting only if you are sure,'
    Write-Output '  and take your own copy first.'
    if ($Apply) { throw 'Refusing to replace an existing persistent database without -ReplaceExisting.' }
}

# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
Write-Output ''
Write-Output '  PLAN'
Write-Output "    1. create $($serverProfile.root) and its data/config/keys/logs/backups/runtime folders"
Write-Output "    2. back up the SOURCE with the SQLite backup API into backups\"
if ($targetExists) {
    Write-Output "    3. back up the EXISTING persistent database into backups\ as well"
    Write-Output "    4. copy the source into $($serverProfile.database) (replacing it)"
} else {
    Write-Output "    3. copy the source into $($serverProfile.database)"
}
Write-Output '    5. verify integrity and row counts of the result'
Write-Output '    6. write a migration report'
Write-Output '    NOT done: the source database is never deleted, moved or edited.'
Write-Output '    NOT done: no account is created and no password is set.'

if (-not $Apply) {
    Write-Output ''
    Write-Output 'PlanOnly complete. Nothing was created or changed.'
    Write-Output 'Re-run with -Apply to carry the plan out.'
    exit 0
}

if (-not $PSCmdlet.ShouldProcess($serverProfile.root, 'Initialize the persistent server')) { exit 0 }

# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------
foreach ($folder in @('data', 'config', 'keys', 'logs', 'backups', 'runtime', 'migration-reports')) {
    New-Item -ItemType Directory -Force -Path (Join-Path $serverProfile.root $folder) | Out-Null
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$applyReport = & $python -c @"
import hashlib, json, os, sqlite3, sys
source = r'$SourceDatabase'
target = r'$($serverProfile.database)'
backups = r'$($serverProfile.backups)'
stamp = '$stamp'

def sha(path):
    return hashlib.sha256(open(path,'rb').read()).hexdigest().upper()

def snapshot(src, dst):
    s = sqlite3.connect('file:' + src + '?mode=ro', uri=True)
    d = sqlite3.connect(dst)
    try: s.backup(d)
    finally:
        d.close(); s.close()

result = {}
# 1. back up the source, consistently
source_backup = os.path.join(backups, 'source-' + stamp + '.db')
snapshot(source, source_backup)
result['source_backup'] = source_backup

# 2. back up any existing target before replacing it
if os.path.exists(target):
    previous = os.path.join(backups, 'persistent-before-' + stamp + '.db')
    snapshot(target, previous)
    result['previous_backup'] = previous
    os.replace(target, target + '.replaced-' + stamp)
    result['replaced_to'] = target + '.replaced-' + stamp

# 3. copy source -> target
snapshot(source, target)
result['target'] = target
result['target_sha256'] = sha(target)

con = sqlite3.connect('file:' + target + '?mode=ro', uri=True)
def one(sql, default='-'):
    try: return con.execute(sql).fetchone()[0]
    except sqlite3.Error: return default
result['integrity'] = one('PRAGMA integrity_check')
result['users'] = one('SELECT COUNT(*) FROM hq_users')
result['stores'] = one('SELECT COUNT(*) FROM stores')
result['devices'] = one('SELECT COUNT(*) FROM receiver_devices')
result['sessions'] = one('SELECT COUNT(*) FROM broadcast_sessions')
result['logs'] = one('SELECT COUNT(*) FROM system_logs')
con.close()
print(json.dumps(result))
"@
if ($LASTEXITCODE -ne 0) { throw 'The persistent database could not be created. Nothing was deleted.' }
$applied = $applyReport | ConvertFrom-Json

Write-Output ''
Write-Output "  source backup : $($applied.source_backup)"
if ($applied.previous_backup) { Write-Output "  previous kept : $($applied.previous_backup)" }
Write-Output "  database      : $($applied.target)"
Write-Output "  sha256        : $($applied.target_sha256)"
Write-Output "  integrity     : $($applied.integrity)"
Write-Output "  users/stores/devices/sessions/logs : $($applied.users)/$($applied.stores)/$($applied.devices)/$($applied.sessions)/$($applied.logs)"

if ($applied.integrity -ne 'ok') { throw "The new persistent database fails integrity_check." }
if ([int]$applied.users -ne [int]$source.users) { throw 'User count changed during the copy.' }
if ([int]$applied.stores -ne [int]$source.stores) { throw 'Store count changed during the copy.' }

$report = Join-Path $serverProfile.root "migration-reports\initialize-$stamp.json"
$applied | ConvertTo-Json -Depth 4 | Set-Content -Encoding ascii $report
Write-Output "  report        : $report"

Write-Output ''
Write-Output 'ECHOCAST_PERSISTENT_SERVER_INITIALIZED'
Write-Output 'The source database was not deleted, moved or edited.'
Write-Output "Start it with .\scripts\Start-EchoCastPersistentLanServer.ps1"
