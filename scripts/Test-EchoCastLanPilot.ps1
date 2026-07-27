<#
.SYNOPSIS
    Verify a private LAN pilot: the address, the profile, reachability and CORS.

.DESCRIPTION
    Two modes.

    -PreflightOnly checks what must be true BEFORE anything is started, and
    needs no running pilot. This is what to run first, and it is what the Start
    script's own -DryRun repeats.

    With -PilotRoot it also checks a running pilot from the outside: that the
    backend and dashboard answer on the LAN address rather than only on
    loopback, that the approved origin is allowed and an unapproved one is not,
    and that the protected database is untouched.

    The CORS check is the one worth reading. An API that sends credentials and
    answers every origin is open to any page a browser can be pointed at, and
    the symptom of getting it wrong is nothing at all.
#>
[CmdletBinding()]
param(
    [string]$HqAddress = '192.168.4.134',
    [string]$PilotRoot,
    [switch]$PreflightOnly
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'

$results = [ordered]@{}
function Check {
    param([string]$Name, [scriptblock]$Test)
    try { $value = & $Test } catch { $value = $false }
    $results[$Name] = $value
    $mark = if ($value -eq $true) { 'PASS' } elseif ($value -eq $false) { 'FAIL' } else { "$value" }
    Write-Output ("  {0,-46} {1}" -f $Name, $mark)
}

Write-Output '=== EchoCast private LAN pilot verification ==='
Write-Output '--- preflight ---'

$address = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -eq $HqAddress }
Check 'the fixed address is assigned'          { $null -ne $address }
if ($address) {
    $adapter = Get-NetAdapter -InterfaceIndex $address.InterfaceIndex -ErrorAction SilentlyContinue
    $profile = Get-NetConnectionProfile -InterfaceIndex $address.InterfaceIndex -ErrorAction SilentlyContinue
    Check 'the adapter is up'                  { $adapter.Status -eq 'Up' }
    Check 'the network profile is Private'     { $profile.NetworkCategory -eq 'Private' }
    Check 'interface alias'                    { $address.InterfaceAlias }
    Check 'prefix length'                      { "/$($address.PrefixLength)" }
}
# In Windows PowerShell 5.1, redirecting a native command's stderr AT ALL -
# `2>&1` or `2>$null` - wraps each line in a NativeCommandError. With
# ErrorActionPreference='Stop' that throws, so a guard which correctly refused
# an address was reported as a failed check: the refusal message itself became
# the failure. The preference is relaxed for the length of the call instead, and
# the exit code is what is read.
function Invoke-AddressCheck {
    param([string]$Candidate)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $null = & $venvPython (Join-Path $repositoryRoot 'tools\lan_pilot.py') `
            check-address --hq-address $Candidate 2>&1
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Test-AddressRefused {
    param([string]$Candidate)
    return (Invoke-AddressCheck $Candidate) -ne 0
}

Check 'the address is private and routable'    { (Invoke-AddressCheck $HqAddress) -eq 0 }
Check 'a public address would be refused'      { Test-AddressRefused '8.8.8.8' }
Check 'loopback would be refused'              { Test-AddressRefused '127.0.0.1' }
Check 'a link-local address would be refused'  { Test-AddressRefused '169.254.1.1' }
Check 'a hostname would be refused'            { Test-AddressRefused 'hq.example.internal' }

$protected = Join-Path $repositoryRoot 'backend\echocast_live.db'
Check 'protected database size unchanged'      { (Get-Item $protected).Length -eq 507904 }
Check 'protected database sidecars absent'     { -not ((Test-Path "$protected-wal") -or (Test-Path "$protected-shm")) }

if ($PreflightOnly -or -not $PilotRoot) {
    Write-Output ''
    Write-Output 'Preflight only. Supply -PilotRoot to also verify a running pilot.'
    $failed = @($results.GetEnumerator() | Where-Object { $_.Value -eq $false })
    exit $(if ($failed.Count -eq 0) { 0 } else { 1 })
}

Write-Output ''
Write-Output '--- running pilot ---'
$manifestPath = Join-Path $PilotRoot 'manifest.json'
$processPath  = Join-Path $PilotRoot 'pilot-processes.json'
if (-not (Test-Path $manifestPath)) { throw "No manifest at $manifestPath." }
$manifest = Get-Content $manifestPath -Raw | ConvertFrom-Json

Check 'the manifest holds no secret'           {
    $raw = Get-Content $manifestPath -Raw
    -not ($raw -match 'password|jwt|hmac|receiver_token|credential"\s*:\s*"[A-Za-z0-9]')
}

if (Test-Path $processPath) {
    $processes = Get-Content $processPath -Raw | ConvertFrom-Json
    Check 'the recorded backend PID is real'   { $null -ne (Get-Process -Id $processes.backend_pid -ErrorAction SilentlyContinue) }
    Check 'the recorded frontend PID is real'  { $null -ne (Get-Process -Id $processes.frontend_pid -ErrorAction SilentlyContinue) }
    Check 'no secret in any owned command line' {
        $clean = $true
        foreach ($id in @($processes.backend_pid, $processes.frontend_pid)) {
            $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$id" -ErrorAction SilentlyContinue
            if ($proc -and $proc.CommandLine -match 'password|secret|token|credential|phc_') { $clean = $false }
        }
        $clean
    }
}

$outsideChecks = & $venvPython (Join-Path $repositoryRoot 'tools\lan_pilot.py') verify --manifest $manifestPath | ConvertFrom-Json
Check 'backend answers on the LAN address'     { $outsideChecks.backend_reachable_on_lan }
Check 'dashboard answers on the LAN address'   { $outsideChecks.frontend_reachable_on_lan }
Check 'the approved origin is allowed'         { $outsideChecks.approved_origin_allowed }
Check 'an unapproved origin is refused'        { $outsideChecks.unapproved_origin_refused }
Check 'protected database still unchanged'     { $outsideChecks.protected_database_unchanged }
Check 'protected sidecars still absent'        { $outsideChecks.protected_sidecars_absent }

Write-Output ''
$failed = @($results.GetEnumerator() | Where-Object { $_.Value -eq $false })
if ($failed.Count -eq 0) {
    Write-Output 'Result: ECHOCAST_PRIVATE_LAN_PILOT_VERIFIED'
    Write-Output 'Software and network configuration only. Not an amplifier, speaker or'
    Write-Output 'SPEAKER_VERIFIED result, and not yet proof that a second desktop connected.'
    exit 0
}
Write-Output "Result: FAILED ($($failed.Count) check(s))"
$failed | ForEach-Object { Write-Output "  - $($_.Key)" }
exit 1
