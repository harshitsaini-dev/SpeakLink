<#
.SYNOPSIS
    Prove the Receiver HMAC key container works under the identity that will
    actually run the backend. Run this AS SpeakLinkService.

.DESCRIPTION
    This is the integration gate that no unit test can pass on your behalf.
    DPAPI CURRENT_USER binds a container to the account that sealed it, so the
    only way to know it works is to seal and open one as that account, on that
    host.

    It checks, in order:

      1. which identity is running - and refuses if it is not the service account
      2. the ACL on the key directory, judged by backend/key_custody_acl.py
      3. creating the container, if it does not exist
      4. opening it and reading the active key version
      5. that the raw key is absent from the file on disk
      6. rotation, and that the previous version survives for verification

    Run it a second time after a service restart, and a third time after a
    reboot. Steps 4 and 6 are what prove persistence; nothing else can.

    No key material is printed. The container is never deleted.

.NOTES
    Expected to FAIL loudly when run as the wrong account. That is the point:
    a container that any account can open is not protected by CURRENT_USER
    scope at all.
#>
[CmdletBinding()]
param(
    [string] $AccountName = 'SpeakLinkService',
    [string] $KeyFile = 'C:\ProgramData\SpeakLink\keys\receiver-hmac-keys.bin',
    [switch] $AllowAnyIdentity
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) { throw "Python virtual environment not found at $venvPython." }

Write-Output '=== SpeakLink Receiver key custody, under the running identity ==='

# --- 1. Identity -----------------------------------------------------------
$current = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Write-Output "  running as : $current"
$expected = "$env:COMPUTERNAME\$AccountName"
if ($current -ne $expected) {
    if (-not $AllowAnyIdentity) {
        throw @"
This must run as $expected, not $current.

DPAPI CURRENT_USER binds the container to the account that sealed it, so a
result obtained under any other account proves nothing about the service.

Run it through the service account, for example:
  psexec -u $expected -p <password> powershell -File $PSCommandPath
or as a scheduled task configured to run as $expected.

Pass -AllowAnyIdentity only to rehearse the mechanics; the result is then
NOT evidence for the production gate.
"@
    }
    Write-Output '  WARNING: -AllowAnyIdentity was supplied. This run is a rehearsal,'
    Write-Output '           NOT evidence that the service account can open the key.'
}

# --- 2. ACL ----------------------------------------------------------------
$keyDirectory = Split-Path -Parent $KeyFile
Write-Output ''
Write-Output "  ACL on $keyDirectory :"
& icacls $keyDirectory

$aclCheck = @"
import sys, json
sys.path.insert(0, r'$repositoryRoot\backend')
from key_custody_acl import read_acl, verify_acl
verdict = verify_acl(read_acl(r'$keyDirectory'), service_account=r'$expected')
print(json.dumps({'acceptable': verdict.acceptable, 'problems': verdict.problems}, indent=2))
sys.exit(0 if verdict.acceptable else 3)
"@
$aclCheck | & $venvPython -
$aclOk = ($LASTEXITCODE -eq 0)
Write-Output "  ACL acceptable: $aclOk"

# --- 3-6. The container ----------------------------------------------------
$custody = @"
import sys, pathlib
sys.path.insert(0, r'$repositoryRoot\backend')
from key_custody import (
    DpapiProtector, ProtectionScope, create_key_container, load_key_ring, rotate_signing_key,
    KeyContainerMissing,
)

path = pathlib.Path(r'$KeyFile')
protector = DpapiProtector(scope=ProtectionScope.CURRENT_USER)

created = False
try:
    ring = load_key_ring(path, protector=protector)
except KeyContainerMissing:
    create_key_container(path, protector=protector)
    ring = load_key_ring(path, protector=protector)
    created = True

print(f'  container      : {"created now" if created else "opened (already existed)"}')
print(f'  active version : {ring.active_version}')
print(f'  all versions   : {ring.versions()}')

raw = path.read_bytes()
leaked = any(ring.key(v) in raw for v in ring.versions())
print(f'  key present in file bytes : {leaked}   (must be False)')
if leaked:
    sys.exit(4)

before = {v: ring.key(v) for v in ring.versions()}
new_version = rotate_signing_key(path, protector=protector)
after = load_key_ring(path, protector=protector)
kept = all(after.key(v) == before[v] for v in before)
print(f'  rotated to version : {new_version}')
print(f'  previous versions still readable : {kept}   (must be True)')
sys.exit(0 if kept else 5)
"@
Write-Output ''
$custody | & $venvPython -
$custodyExit = $LASTEXITCODE

Write-Output ''
if ($custodyExit -eq 0 -and $aclOk -and ($current -eq $expected)) {
    Write-Output 'RESULT: KEY_CUSTODY_VERIFIED_UNDER_SERVICE_ACCOUNT'
    Write-Output 'Run again after a service restart, and again after a reboot.'
    Write-Output 'Only those repeat runs prove the container survives them.'
    exit 0
}

Write-Output 'RESULT: NOT_VERIFIED'
if ($current -ne $expected) { Write-Output '  - not running as the service account' }
if (-not $aclOk)            { Write-Output '  - the key directory ACL was rejected' }
if ($custodyExit -ne 0)     { Write-Output "  - the container check failed (exit $custodyExit)" }
exit 1
