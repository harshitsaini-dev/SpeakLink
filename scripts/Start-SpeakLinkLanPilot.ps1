<#
.SYNOPSIS
    Start a throwaway SpeakLink staging stack on the private LAN.

.DESCRIPTION
    Binds the backend and the HQ dashboard to a fixed private address so another
    Windows desktop on the same network can reach them.

    That binding is the whole reason this script is careful. Loopback is
    unreachable from anywhere else by construction; a LAN address is not. So the
    address is checked rather than assumed, the network profile must be Private,
    the database and administrator are created fresh under a throwaway root, and
    CORS names exact origins.

    Nothing here is production. Production still requires HTTPS and WSS.

.NOTES
    The temporary administrator password is prompted for as a SecureString and
    passed to the preparation step on stdin. It is never a command argument,
    never printed, and never written to the manifest.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    # Deliberately not a switch with a default. Starting a LAN-reachable HTTP
    # stack has to be something somebody typed on purpose.
    [Parameter(Mandatory = $true)]
    [switch]$LanPilot,

    [string]$HqAddress = '192.168.4.134',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repositoryRoot 'backend'
$frontendDir = Join-Path $repositoryRoot 'frontend'
$venvPython = Join-Path $backendDir '.venv\Scripts\python.exe'

function Write-Banner {
    Write-Output ''
    Write-Output '  ############################################################'
    Write-Output '  #  INSECURE PRIVATE LAN PILOT                              #'
    Write-Output '  #  DO NOT USE ON PUBLIC NETWORKS                           #'
    Write-Output '  #  DO NOT PORT-FORWARD                                     #'
    Write-Output '  #  DO NOT DEPLOY TO THE INTERNET                           #'
    Write-Output '  #                                                          #'
    Write-Output '  #  Plain HTTP over a private LAN, for a pilot only.        #'
    Write-Output '  #  Production still requires HTTPS and WSS.                #'
    Write-Output '  ############################################################'
    Write-Output ''
}

# ---------------------------------------------------------------------------
# Preflight. Every one of these fails closed.
# ---------------------------------------------------------------------------
Write-Output '=== SpeakLink private LAN pilot ==='
Write-Banner

if (-not (Test-Path $venvPython)) { throw "Python virtual environment not found at $venvPython." }
if ($null -eq (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw 'ffmpeg was not found on PATH. The Receiver needs FFmpeg with Opus and WebM support.'
}

$address = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
           Where-Object { $_.IPAddress -eq $HqAddress }
if (-not $address) {
    throw "$HqAddress is not assigned to any adapter on this computer. Refusing to bind a different address."
}
$adapter = Get-NetAdapter -InterfaceIndex $address.InterfaceIndex -ErrorAction SilentlyContinue
$profile = Get-NetConnectionProfile -InterfaceIndex $address.InterfaceIndex -ErrorAction SilentlyContinue

if ($adapter.Status -ne 'Up') { throw "The adapter holding $HqAddress is '$($adapter.Status)', not Up." }
if ($profile.NetworkCategory -eq 'Public') {
    throw "The network profile for $HqAddress is Public. Refusing: a Public profile means Windows treats this network as untrusted, and this pilot serves plain HTTP."
}

Write-Output "  interface        : $($address.InterfaceAlias)"
Write-Output "  address          : $($address.IPAddress)/$($address.PrefixLength)"
Write-Output "  adapter status   : $($adapter.Status)"
Write-Output "  network category : $($profile.NetworkCategory)"

foreach ($port in 3000, 8000) {
    $listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    if ($listener) {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener[0].OwningProcess)" -ErrorAction SilentlyContinue
        throw "Port $port is already owned by PID $($listener[0].OwningProcess) ($($owner.Name)). Not stopping it - stop it yourself if it is yours."
    }
}
Write-Output '  ports 3000, 8000 : free'

$pilotRoot = Join-Path $env:LOCALAPPDATA "SpeakLink\lan-pilot\$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Write-Output "  pilot root       : $pilotRoot"

if ($DryRun -or -not $PSCmdlet.ShouldProcess($HqAddress, 'Start the private LAN pilot')) {
    Write-Output ''
    Write-Output 'Dry run: every preflight check passed. Nothing was started and nothing was created.'
    exit 0
}

# ---------------------------------------------------------------------------
# Temporary credentials. Generated here, used, and never written down.
# ---------------------------------------------------------------------------
$adminUsername = "lan-pilot-$(-join ((48..57) + (97..122) | Get-Random -Count 6 | ForEach-Object { [char]$_ }))"
Write-Output ''
Write-Output "A temporary SUPER_ADMIN will be created as: $adminUsername"
$securePassword = Read-Host -Prompt 'Choose a password for it (not shown, not stored)' -AsSecureString
$plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword))
if ([string]::IsNullOrWhiteSpace($plainPassword) -or $plainPassword.Length -lt 12) {
    throw 'That password is shorter than 12 characters. Refusing.'
}

$bytes = New-Object byte[] 48
(New-Object System.Security.Cryptography.RNGCryptoServiceProvider).GetBytes($bytes)
$jwtSecret = [Convert]::ToBase64String($bytes)

Write-Output ''
Write-Output '--- preparing a fresh temporary database, key ring and administrator ---'
$manifestJson = $plainPassword | & $venvPython (Join-Path $repositoryRoot 'tools\lan_pilot.py') `
    prepare --pilot-root $pilotRoot --hq-address $HqAddress `
    --admin-username $adminUsername --password-from-stdin
if ($LASTEXITCODE -ne 0) { throw "Pilot preparation failed with exit code $LASTEXITCODE." }
$manifest = $manifestJson | ConvertFrom-Json

$logsDir = Join-Path $pilotRoot 'logs'
$environment = @{
    SPEAKLINK_DB_PATH        = $manifest.database_path
    SPEAKLINK_KEY_CONTAINER  = $manifest.key_container
    SPEAKLINK_KEY_PROTECTOR  = 'fake'
    JWT_SECRET              = $jwtSecret
    ADMIN_USERNAME          = $adminUsername
    ADMIN_PASSWORD          = $plainPassword
    CORS_ORIGINS            = ($manifest.cors_origins -join ',')
}
foreach ($pair in $environment.GetEnumerator()) { Set-Item "env:$($pair.Key)" $pair.Value }

Write-Output '--- starting one Uvicorn worker ---'
$backend = Start-Process -FilePath $venvPython -PassThru -WindowStyle Hidden `
    -WorkingDirectory $backendDir `
    -ArgumentList @('-m','uvicorn','server:app','--host',"`"$HqAddress`"",'--port','8000','--workers','1','--no-access-log') `
    -RedirectStandardOutput (Join-Path $logsDir 'backend.log') `
    -RedirectStandardError  (Join-Path $logsDir 'backend.err.log')

Write-Output '--- starting the HQ dashboard ---'
$env:HOST = $HqAddress
$env:PORT = '3000'
$env:BROWSER = 'none'
$env:REACT_APP_BACKEND_URL = $manifest.backend_url
$env:DANGEROUSLY_DISABLE_HOST_CHECK = 'true'
$frontend = Start-Process -FilePath 'cmd.exe' -PassThru -WindowStyle Hidden `
    -WorkingDirectory $frontendDir `
    -ArgumentList @('/c','yarn','start') `
    -RedirectStandardOutput (Join-Path $logsDir 'frontend.log') `
    -RedirectStandardError  (Join-Path $logsDir 'frontend.err.log')

# Non-secret process metadata only.
[PSCustomObject]@{
    started_utc   = (Get-Date).ToUniversalTime().ToString('o')
    pilot_root    = $pilotRoot
    database_path = $manifest.database_path
    hq_address    = $HqAddress
    backend_url   = $manifest.backend_url
    frontend_url  = $manifest.frontend_url
    cors_origins  = $manifest.cors_origins
    backend_pid   = $backend.Id
    frontend_pid  = $frontend.Id
    ports         = @(8000, 3000)
    admin_username = $adminUsername
    secrets_in_this_file = 'none'
} | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $pilotRoot 'pilot-processes.json')

$plainPassword = $null
Remove-Variable plainPassword -ErrorAction SilentlyContinue

Write-Output ''
Write-Output '--- waiting for the backend ---'
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        if ((Invoke-WebRequest -Uri "$($manifest.backend_url)/docs" -TimeoutSec 2 -UseBasicParsing).StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}
Write-Output "  backend live: $ready"

Write-Banner
Write-Output "HQ URL:"
Write-Output "  $($manifest.frontend_url)"
Write-Output ""
Write-Output "API URL:"
Write-Output "  $($manifest.backend_url)"
Write-Output ""
Write-Output "Username:"
Write-Output "  $adminUsername"
Write-Output ""
Write-Output "The password is the one you just typed. It is not stored anywhere."
Write-Output "Pilot root (logs, manifest, temporary database):"
Write-Output "  $pilotRoot"
Write-Output ""
Write-Output "Stop it with:  .\scripts\Stop-SpeakLinkLanPilot.ps1 -PilotRoot '$pilotRoot'"
