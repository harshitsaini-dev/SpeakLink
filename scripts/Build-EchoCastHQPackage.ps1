<#
.SYNOPSIS
    Build a versioned, hashed EchoCast HQ package that another machine can install.

.DESCRIPTION
    WHAT A PACKAGE IS FOR

    Everything an HQ computer needs to run EchoCast, and nothing that belongs to
    a particular HQ computer. The application, the production frontend, the
    backend source the runtime starts as a child, the four task scripts and a
    quick-start - built from a named commit, hashed file by file.

    WHAT IT MUST NEVER CONTAIN, AND WHY THAT IS THE HARD PART

    The package is the one artifact that leaves this machine. Every dangerous
    file in this system is one an over-helpful copy would sweep in: the
    persistent database (44 Stores, every user account), the Receiver HMAC key
    container (forge any Device credential), the signing secret (mint any
    session), a .env, a log with Store names in it. None of them are needed to
    run HQ elsewhere, and any one of them on a USB stick is a full compromise.

    So the copy is a whitelist, never a mirror of a directory, and
    Test-EchoCastHQPackage.ps1 then goes looking for what should not be there -
    including files the hash manifest never mentions, because a file nobody
    listed is a file nobody hashes.

    ABOUT __pycache__

    Excluded, and not only for tidiness. A .pyc compiled on this machine records
    the absolute source path it came from, which is a Windows username shipped
    to whoever receives the package.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RuntimePath,
    # Resolved in the body, not here. $PSScriptRoot is empty inside a param
    # block when Windows PowerShell runs the script with -File, so a default
    # built from it fails with "Cannot bind argument ... empty string" only
    # under the invocation an automated test uses - which is exactly the kind
    # of difference that ships.
    [string]$OutputRoot,
    [string]$Version,
    [string]$FrontendBuild,
    [switch]$AllowDirtyTree
)

$ErrorActionPreference = 'Stop'

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = (Resolve-Path (Join-Path $scriptRoot '..')).Path
if (-not $OutputRoot) { $OutputRoot = Join-Path $repositoryRoot 'artifacts' }
if (-not $FrontendBuild) { $FrontendBuild = Join-Path $repositoryRoot 'frontend\build' }

function Get-PeSubsystem {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $reader = New-Object System.IO.BinaryReader($stream)
        $stream.Position = 0x3C
        $peOffset = $reader.ReadInt32()
        $stream.Position = $peOffset + 4 + 20 + 68
        return $reader.ReadUInt16()
    } finally { $stream.Dispose() }
}

Write-Output '=== building an EchoCast HQ package ==='

# ---------------------------------------------------------------------------
# What it is being built from
# ---------------------------------------------------------------------------
Push-Location $repositoryRoot
try {
    $commit = (& git rev-parse HEAD).Trim()
    $shortCommit = (& git rev-parse --short HEAD).Trim()
    $dirty = @(& git status --porcelain) | Where-Object { $_.Trim() }
} finally { Pop-Location }
if (-not $commit) { throw 'Not a git working tree. The package manifest must record a source commit.' }

# Recorded either way. A package built from uncommitted work that says nothing
# about it is a package nobody can reproduce or trust.
$treeState = if ($dirty.Count -gt 0) { 'dirty' } else { 'clean' }
if ($dirty.Count -gt 0 -and -not $AllowDirtyTree) {
    throw ("The working tree has $($dirty.Count) uncommitted change(s), so this " +
           "package could not be rebuilt from commit $shortCommit. Commit first, " +
           'or pass -AllowDirtyTree to build a clearly-marked development package.')
}

if (-not $Version) {
    $Version = "0.1.0-rc"
}
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$packageName = "EchoCastHQ-$Version-$shortCommit-$timestamp"

# ---------------------------------------------------------------------------
# The pieces, each checked before anything is copied
# ---------------------------------------------------------------------------
if (-not (Test-Path $RuntimePath)) { throw "No runtime build at $RuntimePath." }
$RuntimePath = (Resolve-Path $RuntimePath).Path
$runtimeExe = Join-Path $RuntimePath 'EchoCastHQRuntime.exe'
if (-not (Test-Path $runtimeExe)) {
    throw "There is no EchoCastHQRuntime.exe in $RuntimePath."
}

$subsystem = Get-PeSubsystem $runtimeExe
if ($subsystem -ne 2) {
    throw ("That runtime is a console application (PE subsystem $subsystem, " +
           'expected 2 / WINDOWS_GUI). Packaging it would ship a black window ' +
           'to every HQ machine. Rebuild it with hq_runtime.spec.')
}

if (-not (Test-Path (Join-Path $FrontendBuild 'index.html'))) {
    throw ("There is no production frontend at $FrontendBuild. Run 'yarn build' " +
           "in frontend first. HQ does not ship a development server.")
}
$FrontendBuild = (Resolve-Path $FrontendBuild).Path

$backendSource = Join-Path $repositoryRoot 'backend'
if (-not (Test-Path (Join-Path $backendSource 'server.py'))) {
    throw "There is no backend/server.py to package."
}

$taskScripts = @('Install-EchoCastHQAutoStart.ps1', 'Test-EchoCastHQAutoStart.ps1',
                 'Repair-EchoCastHQAutoStart.ps1', 'Uninstall-EchoCastHQAutoStart.ps1')
foreach ($name in $taskScripts) {
    if (-not (Test-Path (Join-Path $scriptRoot $name))) {
        throw "The task script $name is missing from scripts\."
    }
}

Write-Output "  version   : $Version"
Write-Output "  commit    : $shortCommit ($treeState tree)"
Write-Output "  runtime   : WINDOWS_GUI (PE subsystem 2)"
Write-Output "  frontend  : $FrontendBuild"
Write-Output "  package   : $packageName"

# ---------------------------------------------------------------------------
# Copy. A whitelist, never a mirror.
# ---------------------------------------------------------------------------
$outputPath = Join-Path $OutputRoot $packageName
if (Test-Path $outputPath) { throw "$outputPath already exists. Old evidence is never overwritten." }
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

# 1. The runtime, whole. It is a PyInstaller one-folder build.
Copy-Item (Join-Path $RuntimePath '*') $outputPath -Recurse -Force

# 2. The production frontend, beside the executable - which is where the frozen
#    runtime looks for it.
$frontendTarget = Join-Path $outputPath 'frontend'
New-Item -ItemType Directory -Force -Path $frontendTarget | Out-Null
Copy-Item (Join-Path $FrontendBuild '*') $frontendTarget -Recurse -Force

# 3. The backend source. The runtime starts it as a child with the machine's
#    own Python, so the .py files travel and the database never does.
$backendTarget = Join-Path $outputPath 'backend'
New-Item -ItemType Directory -Force -Path $backendTarget | Out-Null
$excludedDirectories = @('__pycache__', 'node_modules', '.venv', 'venv', 'tests',
                         '.pytest_cache', 'backups', 'logs')
$backendPrefix = $backendSource.TrimEnd('\') + '\'
foreach ($file in (Get-ChildItem $backendSource -Recurse -File)) {
    $relative = $file.FullName.Substring($backendPrefix.Length)
    $parts = $relative.Split('\')
    if ($parts.Length -gt 1 -and ($parts[0..($parts.Length - 2)] |
            Where-Object { $excludedDirectories -contains $_ })) { continue }
    if ($file.Name -match '(?i)\.(db|db-wal|db-shm|sqlite|sqlite3|log|pem|key|pyc|pyo)$') { continue }
    if ($file.Name -match '(?i)^\.env') { continue }
    if ($file.Name -match '(?i)(jwt-secret|hmac-keys)') { continue }
    $target = Join-Path $backendTarget $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
    Copy-Item $file.FullName $target -Force
}
if (-not (Test-Path (Join-Path $backendTarget 'requirements.txt'))) {
    Write-Output '  note: backend\requirements.txt was not found to package'
}

# 4. The four task scripts, at the top level where an operator will look.
foreach ($name in $taskScripts) {
    Copy-Item (Join-Path $scriptRoot $name) (Join-Path $outputPath $name) -Force
}

# 5. A quick-start that fits on one screen.
$quickStart = @"
EchoCast HQ - quick start
=========================

Package : $packageName
Version : $Version
Commit  : $commit

BEFORE YOU START

HQ keeps its data in a persistent server root, and this package deliberately
contains none of it. On a new HQ computer, create that root first:

    .\Initialize-EchoCastPersistentLanServer.ps1

On an existing HQ computer, do NOT run Initialize again. The data is already
there and this package does not touch it.

1. VERIFY WHAT YOU RECEIVED

    .\Test-EchoCastHQPackage.ps1 -PackagePath .

   Expect: ECHOCAST_HQ_PACKAGE_VERIFIED

2. INSTALL THE AUTO-START (registers, does not start)

    .\Install-EchoCastHQAutoStart.ps1 -PackagePath . -DryRun
    .\Install-EchoCastHQAutoStart.ps1 -PackagePath .

   Expect: ECHOCAST_HQ_AUTO_START_INSTALLED

3. START IT

    Start-ScheduledTask -TaskName "EchoCast HQ Runtime"

4. CHECK IT

    .\Test-EchoCastHQAutoStart.ps1

   Expect: ECHOCAST_HQ_AUTO_START_VERIFIED

   The runtime writes what it is doing to
   %LOCALAPPDATA%\EchoCast-AI\hq-runtime-status.json. READY means the backend
   and the frontend both answered - not that a process started.

WHAT THIS DOES NOT DO

HQ starts when the HQ Windows user signs in. It does NOT run at the Windows
sign-in screen, and no setting in these scripts changes that. If the HQ machine
reboots unattended, somebody has to sign in.

This is a private-LAN deployment over plain HTTP. Do not expose it to the
internet: there is no HTTPS, no WSS, and tokens would travel in clear text.

IF SOMETHING IS WRONG

    .\Test-EchoCastHQAutoStart.ps1          what is installed and running
    .\Repair-EchoCastHQAutoStart.ps1 -PackagePath . -DryRun    what it would fix
    .\Uninstall-EchoCastHQAutoStart.ps1     remove the app, KEEP the data

Repair and Uninstall never touch the persistent database, keys, configuration,
backups or logs.
"@
[IO.File]::WriteAllText((Join-Path $outputPath 'QUICK-START.txt'), $quickStart,
                        (New-Object System.Text.UTF8Encoding $false))

# ---------------------------------------------------------------------------
# Nothing dangerous got in
# ---------------------------------------------------------------------------
$mustNeverShip = @('*.db', '*.db-wal', '*.db-shm', '*.sqlite', '*.sqlite3',
                   '*.log', '*.pem', '*.key', '*.pfx', '.env', '.env.*',
                   '*jwt-secret*', '*hmac-keys*', '*.wav', '*.webm')
$leaked = @(Get-ChildItem $outputPath -Recurse -File -Force -Include $mustNeverShip `
              -ErrorAction SilentlyContinue)
$leaked += @(Get-ChildItem $outputPath -Recurse -Directory -Force |
             Where-Object { $_.Name -in @('node_modules', '__pycache__', '.venv', 'venv') })
if ($leaked.Count -gt 0) {
    Remove-Item $outputPath -Recurse -Force
    throw ("The package would have shipped $($leaked.Count) file(s) that must " +
           "never leave this machine, starting with $($leaked[0].Name). The " +
           'package was deleted rather than left on disk.')
}

# ---------------------------------------------------------------------------
# Hashes, then the manifest that describes them
# ---------------------------------------------------------------------------
$prefix = $outputPath.TrimEnd('\') + '\'
$hashed = Get-ChildItem $outputPath -Recurse -File |
          Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'manifest.json') } |
          Sort-Object FullName
$lines = foreach ($file in $hashed) {
    $relative = $file.FullName.Substring($prefix.Length).Replace('\', '/')
    "$((Get-FileHash $file.FullName -Algorithm SHA256).Hash.ToLower())  $relative"
}
[IO.File]::WriteAllText((Join-Path $outputPath 'SHA256SUMS.txt'),
                        (($lines -join "`n") + "`n"),
                        (New-Object System.Text.UTF8Encoding $false))

# No absolute build path and no Windows username: a manifest that shipped
# "C:\Users\<someone>\..." names the person who built it to everyone who
# receives it.
$manifest = [ordered]@{
    product = 'EchoCast HQ'
    version = $Version
    source_commit = $commit
    source_commit_short = $shortCommit
    source_tree = $treeState
    built_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    package_name = $packageName
    file_count = $hashed.Count
    runtime = [ordered]@{
        name = 'EchoCastHQRuntime.exe'
        pe_subsystem = $subsystem
        pe_subsystem_meaning = 'WINDOWS_GUI - creates no console window'
        sha256 = (Get-FileHash $runtimeExe -Algorithm SHA256).Hash.ToLower()
    }
    contains = @('EchoCastHQRuntime.exe', 'frontend/ (production build)',
                 'backend/ (source)', 'the four HQ auto-start scripts',
                 'QUICK-START.txt')
    excludes = @('any database', '.env', 'jwt-secret', 'hmac-keys', 'logs',
                 'backups', 'node_modules', '.venv', 'recordings')
    private_lan_only = $true
    notes = @('HQ starts at the HQ user sign-in. It does not run at the Windows sign-in screen.',
              'Plain HTTP on a private LAN. HTTPS and WSS are required before any public deployment.')
}
[IO.File]::WriteAllText((Join-Path $outputPath 'manifest.json'),
                        ($manifest | ConvertTo-Json -Depth 5),
                        (New-Object System.Text.UTF8Encoding $false))

Write-Output "  $($hashed.Count) files hashed"
Write-Output ''
Write-Output 'ECHOCAST_HQ_PACKAGE_BUILT'
Write-Output "Package: $outputPath"
Write-Output "Verify : .\Test-EchoCastHQPackage.ps1 -PackagePath `"$outputPath`""
