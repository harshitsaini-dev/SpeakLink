<#
.SYNOPSIS
    Package SpeakLinkStoreSetup.exe as a versioned, hashed artifact.

.DESCRIPTION
    SpeakLinkStoreSetup.exe was being built into dist\ and shipped in nothing.
    It was absent from the Store kit, had no version, recorded no source commit
    and carried no SHA256SUMS - so there was no way for a Store to check that
    the wizard it received was the one that was built, and no way to tell two
    builds apart. The Receiver had all of that; the wizard that enrols the
    Receiver did not.

    This is deliberately a separate artifact rather than an addition to the
    Store kit. The kit is the Receiver payload and its contents are asserted by
    Test-SpeakLinkStorePilotKit.ps1; whether the wizard belongs inside it is an
    operational decision about how a Store is set up, not something to change
    quietly while fixing a packaging gap.

    Same guarantees as the HQ package: the executable must be WINDOWS_GUI, the
    tree must be clean unless a development package is asked for, the manifest
    names no build path and no person, and nothing that must not leave this
    machine is copied.
#>
[CmdletBinding()]
param(
    [string]$DistPath,
    # A verified SpeakLinkReceiver-* package. Mandatory in practice: a StoreSetup
    # package without the Receiver is a wizard that cannot install anything,
    # which is exactly what shipped last time.
    [Parameter(Mandatory)][string]$ReceiverPackagePath,
    [string]$OutputRoot,
    [string]$Version,
    [switch]$AllowDirtyTree
)

$ErrorActionPreference = 'Stop'

# Resolved in the body, not in param(). $PSScriptRoot is empty inside a param
# block when Windows PowerShell runs a script with -File, so a default built
# from it fails only under the invocation an automated test uses.
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = (Resolve-Path (Join-Path $scriptRoot '..')).Path
if (-not $DistPath) { $DistPath = Join-Path $repositoryRoot 'dist\SpeakLinkStoreSetup' }
if (-not $OutputRoot) { $OutputRoot = Join-Path $repositoryRoot 'artifacts' }

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

Write-Output '=== building an SpeakLink Store Setup package ==='

Push-Location $repositoryRoot
try {
    $commit = (& git rev-parse HEAD).Trim()
    $shortCommit = (& git rev-parse --short HEAD).Trim()
    $dirty = @(& git status --porcelain) | Where-Object { $_.Trim() }
} finally { Pop-Location }
if (-not $commit) { throw 'Not a git working tree. The package manifest must record a source commit.' }

$treeState = if ($dirty.Count -gt 0) { 'dirty' } else { 'clean' }
if ($dirty.Count -gt 0 -and -not $AllowDirtyTree) {
    throw ("The working tree has $($dirty.Count) uncommitted change(s), so this " +
           "package could not be rebuilt from commit $shortCommit. Commit first, " +
           'or pass -AllowDirtyTree to build a clearly-marked development package.')
}

if (-not $Version) { $Version = '1.0.0' }
$timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$packageName = "SpeakLinkStoreSetup-$Version-$shortCommit-$timestamp"

# ---------------------------------------------------------------------------
# The build, checked before anything is copied
# ---------------------------------------------------------------------------
if (-not (Test-Path $DistPath)) {
    throw ("No StoreSetup build at $DistPath. Build it first: " +
           'python -m PyInstaller --noconfirm store_setup.spec')
}
$DistPath = (Resolve-Path $DistPath).Path
$setupExe = Join-Path $DistPath 'SpeakLinkStoreSetup.exe'
if (-not (Test-Path $setupExe)) {
    throw "There is no SpeakLinkStoreSetup.exe in $DistPath."
}

$subsystem = Get-PeSubsystem $setupExe
if ($subsystem -ne 2) {
    throw ("That build is a console application (PE subsystem $subsystem, " +
           'expected 2 / WINDOWS_GUI). The wizard is what a Store person ' +
           'double-clicks; a black window behind it is not acceptable. ' +
           'Rebuild it with store_setup.spec.')
}

# The wizard must be newer than the sources it was built from, or this packages
# yesterday's executable with today's commit recorded against it.
$sourceInputs = @(
    (Join-Path $repositoryRoot 'tools\store_setup_gui.py'),
    (Join-Path $repositoryRoot 'tools\store_setup_core.py'),
    (Join-Path $repositoryRoot 'store_setup.spec')
) | Where-Object { Test-Path $_ }
$newestSource = ($sourceInputs | ForEach-Object { (Get-Item $_).LastWriteTime } |
                 Sort-Object -Descending | Select-Object -First 1)
$builtAt = (Get-Item $setupExe).LastWriteTime
if ($newestSource -and $builtAt -lt $newestSource) {
    throw ("SpeakLinkStoreSetup.exe was built at $builtAt but a source input " +
           "changed at $newestSource. This would ship a stale wizard with a " +
           'current commit recorded against it. Rebuild it first.')
}

Write-Output "  version    : $Version"
Write-Output "  commit     : $shortCommit ($treeState tree)"
Write-Output "  wizard     : WINDOWS_GUI (PE subsystem 2)"
Write-Output "  built      : $builtAt (newer than every source input)"
Write-Output "  package    : $packageName"

# ---------------------------------------------------------------------------
# Copy
# ---------------------------------------------------------------------------
$outputPath = Join-Path $OutputRoot $packageName
if (Test-Path $outputPath) { throw "$outputPath already exists. Old evidence is never overwritten." }
New-Item -ItemType Directory -Force -Path $outputPath | Out-Null

Copy-Item (Join-Path $DistPath '*') $outputPath -Recurse -Force

# ---------------------------------------------------------------------------
# The Receiver payload and the scripts, BESIDE the executable
# ---------------------------------------------------------------------------
# The first version of this script copied only $DistPath, so the package was the
# wizard and nothing else: no Receiver, no FFmpeg, none of the five PowerShell
# scripts. The wizard cannot install a Receiver it does not have, and an operator
# was told to hand-create _internal\artifacts and _internal\scripts - folders that
# only existed because the frozen path calculation was wrong.
#
# Both halves are fixed together on purpose. tools/resource_paths resolves
# BESIDE the executable; this puts the files exactly there. Neither is any use
# alone.
Write-Output '  --- Receiver payload ---'
$receiverTarget = Join-Path $outputPath 'Receiver'
# Created first. Copying a directory tree onto a path that does not exist yet
# makes Copy-Item treat the destination as a leaf, and it fails with
# "Container cannot be copied onto existing leaf item" on the first subfolder.
New-Item -ItemType Directory -Force -Path $receiverTarget | Out-Null
if (-not (Test-Path $ReceiverPackagePath)) {
    Remove-Item $outputPath -Recurse -Force
    throw "There is no Receiver package at $ReceiverPackagePath."
}
Copy-Item (Join-Path $ReceiverPackagePath '*') $receiverTarget -Recurse -Force
foreach ($required in @('SpeakLinkReceiver.exe', 'SpeakLinkReceiverBackground.exe',
                        'manifest.json', 'SHA256SUMS.txt')) {
    if (-not (Test-Path (Join-Path $receiverTarget $required))) {
        Remove-Item $outputPath -Recurse -Force
        throw ("The Receiver package at $ReceiverPackagePath has no $required. " +
               'The StoreSetup package was deleted rather than shipped incomplete.')
    }
}
$ffmpeg = @(Get-ChildItem $receiverTarget -Recurse -File -Filter 'ffmpeg.exe')
if ($ffmpeg.Count -eq 0) {
    Remove-Item $outputPath -Recurse -Force
    throw ('The Receiver package carries no ffmpeg.exe, so a Store PC would need ' +
           'FFmpeg installed by hand. The StoreSetup package was deleted.')
}
Write-Output ("    Receiver files : " + @(Get-ChildItem $receiverTarget -Recurse -File).Count)
Write-Output ("    ffmpeg         : " + $ffmpeg[0].FullName.Substring($outputPath.Length))

Write-Output '  --- Store scripts ---'
$scriptTarget = Join-Path $outputPath 'scripts'
New-Item -ItemType Directory -Force -Path $scriptTarget | Out-Null
# The same list tools/resource_paths.REQUIRED_SCRIPTS enforces at runtime. Kept
# deliberately explicit rather than a wildcard: a wildcard would ship whatever
# happened to be in scripts\ that day, including HQ-only scripts a Store must
# never run.
$requiredScripts = @(
    'Install-SpeakLinkStoreReceiver.ps1',
    'Repair-SpeakLinkStoreReceiver.ps1',
    'Test-SpeakLinkStoreReceiver.ps1',
    'Uninstall-SpeakLinkStoreReceiver.ps1',
    'Manage-SpeakLinkStoreReceiverTask.ps1',
    'SpeakLinkProcessTree.ps1'
)
foreach ($name in $requiredScripts) {
    $source = Join-Path $scriptRoot $name
    if (-not (Test-Path $source)) {
        Remove-Item $outputPath -Recurse -Force
        throw ("scripts\$name is missing from the repository, so the package would " +
               'ship a wizard that cannot install. The package was deleted.')
    }
    Copy-Item $source (Join-Path $scriptTarget $name) -Force
}
Write-Output ("    scripts copied : " + $requiredScripts.Count)

$quickStart = @"
SpeakLink Store Setup - quick start
=================================

Package : $packageName
Version : $Version
Commit  : $commit

WHAT THIS IS

SpeakLinkStoreSetup.exe sets this computer up as an SpeakLink Receiver. It asks
four things and then enrols the computer with HQ. You do not need Python and
you do not need to edit any file.

1. VERIFY WHAT YOU RECEIVED

   Compare the files against the list they were shipped with:

       Get-Content .\SHA256SUMS.txt

   If a line does not match, stop and ask HQ for the package again.

2. RUN IT

   Double-click SpeakLinkStoreSetup.exe.

   It will ask for:
     - the HQ address you were given
     - a name for this computer
     - which speaker or sound output to use
     - the enrolment code from HQ

   The enrolment code is single-use and expires. If it is refused, ask HQ for a
   new one - do not reuse an old one.

3. IF IT IS ALREADY SET UP

   Running it again shows what is already installed and asks before changing
   anything. It will not silently replace a working credential.

WHAT IT NEVER ASKS FOR

   It never asks for an HQ password, and never asks you to paste a secret into
   a chat or an email. The enrolment code is the only thing you type in.

PRIVATE NETWORK ONLY

   SpeakLink runs on the shop's own network. This package is not for use over
   the public internet.
"@
[IO.File]::WriteAllText((Join-Path $outputPath 'QUICK-START.txt'), $quickStart,
                        (New-Object System.Text.UTF8Encoding $false))

# ---------------------------------------------------------------------------
# Nothing that must not leave this machine
# ---------------------------------------------------------------------------
$leaked = @(Get-ChildItem $outputPath -Recurse -File -Force | Where-Object {
    $_.Name -match '(?i)^\.env' -or
    $_.Name -match '(?i)\.(db|db-wal|db-shm|sqlite|sqlite3|log|pem|key|pfx|p12|wav|mp3)$' -or
    $_.Name -match '(?i)(jwt-secret|hmac-keys)' -or
    $_.Name -match '(?i)^receiver-credential'
})
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

$manifest = [ordered]@{
    product = 'SpeakLink Store Setup'
    version = $Version
    source_commit = $commit
    source_commit_short = $shortCommit
    source_tree = $treeState
    built_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    package_name = $packageName
    file_count = $hashed.Count
    wizard = [ordered]@{
        name = 'SpeakLinkStoreSetup.exe'
        pe_subsystem = $subsystem
        pe_subsystem_meaning = 'WINDOWS_GUI - creates no console window'
        sha256 = (Get-FileHash $setupExe -Algorithm SHA256).Hash.ToLower()
    }
    contains = @('SpeakLinkStoreSetup.exe', '_internal/ (PyInstaller runtime)',
                 'Receiver/ (SpeakLinkReceiver.exe, SpeakLinkReceiverBackground.exe, FFmpeg)',
                 'scripts/ (install, repair, test, uninstall, task, process-tree)',
                 'QUICK-START.txt')
    receiver_source = (Split-Path -Leaf $ReceiverPackagePath)
    self_contained = $true
    excludes = @('any database', '.env', 'jwt-secret', 'hmac-keys',
                 'any Receiver credential', 'logs', 'recordings',
                 'node_modules', '.venv')
    private_lan_only = $true
    notes = @('The wizard asks for an enrolment code. It never asks for an HQ password.',
              'An enrolment code is single-use and expires. A refused code means ask HQ for a new one.',
              'Re-running it reports what is already installed and asks before changing anything.',
              'Plain HTTP on a private LAN. HTTPS and WSS are required before any public deployment.')
}
[IO.File]::WriteAllText((Join-Path $outputPath 'manifest.json'),
                        ($manifest | ConvertTo-Json -Depth 5),
                        (New-Object System.Text.UTF8Encoding $false))

Write-Output "  $($hashed.Count) files hashed"
Write-Output ''
Write-Output 'SPEAKLINK_STORE_SETUP_PACKAGE_BUILT'
Write-Output "Package: $outputPath"
Write-Output 'This package is git-ignored and must not be committed.'
