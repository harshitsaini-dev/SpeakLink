<#
.SYNOPSIS
    Assemble a self-contained Store pilot kit: the Receiver package plus the
    installer scripts the Store operator is told to run.

.DESCRIPTION
    THE GAP THIS CLOSES

    The Receiver package carried the executable and its FFmpeg, and the
    two-desktop runbook then told the Store operator to run

        .\scripts\Install-SpeakLinkReceiverLanPilot.ps1

    - a path that exists only in this repository. An operator who copied the
    package to the second desktop, exactly as instructed, would find no such
    script and no way to set up autorun. The instructions were correct about a
    machine that has the source checkout, and the whole point of the package is
    that the Store desktop does not.

    So the kit ships the installer scripts beside the package.

    WHAT IT NEVER DOES

    It downloads nothing. Every input is a file that already exists on this
    computer, named explicitly. A build step that fetches something from the
    Internet decides silently, and differently each time, what ends up on 44
    Store computers.

    It carries no credential and no enrolment code. Enrolment is a person
    typing a one-time code; a kit that contained one would be a kit that
    enrolled whoever copied it.

    It refuses a stale package. The first Receiver package in this repository
    was built nine minutes before the source it was supposed to contain, tested
    green, and was wrong - so both the STALE-DO-NOT-DEPLOY marker and the
    freshness of the manifest are checked here rather than trusted.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [string]$OutputRoot,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot

Write-Output '=== building the SpeakLink Store pilot kit ==='

# ---------------------------------------------------------------------------
# The Receiver package
# ---------------------------------------------------------------------------
if (-not (Test-Path $PackagePath)) { throw "No package at $PackagePath." }
$PackagePath = (Resolve-Path $PackagePath).Path

if (Test-Path (Join-Path $PackagePath 'STALE-DO-NOT-DEPLOY.txt')) {
    throw "The package at $PackagePath is marked STALE-DO-NOT-DEPLOY. It is diagnostic evidence, not something to ship."
}
if ((Split-Path -Leaf $PackagePath) -eq 'SpeakLinkReceiver-1.0.0') {
    throw 'That is the original stale package. Build a fresh one with .\scripts\Build-SpeakLinkReceiver.ps1.'
}

foreach ($required in @('SpeakLinkReceiver.exe', 'ffmpeg.exe', 'manifest.json', 'SHA256SUMS.txt', '_internal')) {
    if (-not (Test-Path (Join-Path $PackagePath $required))) {
        throw "The package has no $required. It is not a complete Receiver package."
    }
}

$packageManifest = Get-Content (Join-Path $PackagePath 'manifest.json') -Raw | ConvertFrom-Json
if (-not $packageManifest.source_commit) { throw 'The package manifest records no source commit.' }
if (-not $packageManifest.source_tree_clean) {
    throw ("The package was built from a dirty working tree, so it does not match the commit it " +
           'records. Rebuild from a clean tree before shipping it to a Store.')
}

# Every file must still match what the build hashed. A package that was edited
# after it was verified is a package nobody has verified.
$mismatched = @()
foreach ($line in (Get-Content (Join-Path $PackagePath 'SHA256SUMS.txt'))) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split '  ', 2
    $file = Join-Path $PackagePath ($parts[1] -replace '/', '\')
    if (-not (Test-Path $file)) { $mismatched += "$($parts[1]) (missing)"; continue }
    if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) { $mismatched += $parts[1] }
}
if ($mismatched.Count -gt 0) {
    throw "The package no longer matches its own SHA256SUMS.txt: $($mismatched -join ', ')."
}

Write-Output "  package        : $(Split-Path -Leaf $PackagePath)"
Write-Output "  package commit : $($packageManifest.source_commit_short) (clean tree)"
Write-Output '  package hashes : every file matches'

# ---------------------------------------------------------------------------
# The kit's own source commit
# ---------------------------------------------------------------------------
Push-Location $repositoryRoot
try {
    $commit = (git rev-parse HEAD 2>$null)
    $shortCommit = (git rev-parse --short HEAD 2>$null)
    $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
    $dirty = @(git status --porcelain 2>$null)
} finally { Pop-Location }
if (-not $commit) { throw 'Not a git working tree. The kit manifest must record a source commit.' }

Write-Output "  kit commit     : $shortCommit ($branch)$(if ($dirty.Count) { ' DIRTY' })"

$installerScripts = @(
    'Install-SpeakLinkReceiverLanPilot.ps1'
    'Test-SpeakLinkReceiverLanPilot.ps1'
    'Uninstall-SpeakLinkReceiverLanPilot.ps1'
)
foreach ($name in $installerScripts) {
    if (-not (Test-Path (Join-Path $repositoryRoot "scripts\$name"))) {
        throw "Installer script scripts\$name is missing."
    }
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
if (-not $OutputRoot) {
    $OutputRoot = Join-Path $repositoryRoot "artifacts\SpeakLink-Store-Pilot-$shortCommit-$stamp"
}
Write-Output "  output         : $OutputRoot"

if ($DryRun -or -not $PSCmdlet.ShouldProcess($OutputRoot, 'Assemble the Store pilot kit')) {
    Write-Output ''
    Write-Output 'Dry run: every input was checked. Nothing was assembled.'
    exit 0
}

# ---------------------------------------------------------------------------
# Assemble
# ---------------------------------------------------------------------------
$receiverDir = Join-Path $OutputRoot 'Receiver'
$installerDir = Join-Path $OutputRoot 'Installer'
New-Item -ItemType Directory -Force -Path $receiverDir, $installerDir | Out-Null

Copy-Item (Join-Path $PackagePath '*') $receiverDir -Recurse -Force
foreach ($name in $installerScripts) {
    Copy-Item (Join-Path $repositoryRoot "scripts\$name") (Join-Path $installerDir $name) -Force
}

@"
SpeakLink - Store pilot kit
===============================

Private LAN pilot. NOT a production release.

WHAT THIS IS
  Everything the Store computer needs. No Python, no Node, no FFmpeg install,
  no repository checkout, no Internet connection.

WHAT IT DOES NOT CONTAIN
  No credential and no enrolment code. Someone at HQ issues a one-time code and
  you type it once. From that moment this computer has its own sealed identity
  that no other computer can use.

BEFORE YOU START
  Ask HQ to confirm the backend is running at http://192.168.4.134:8000 and to
  create an enrolment code for this Store. The code is shown once. Write it on
  paper. Do not e-mail it, message it or save it in a file.

STEP 1 - open PowerShell in this folder

STEP 2 - check you can reach HQ

    Test-NetConnection 192.168.4.134 -Port 8000

  Expect TcpTestSucceeded : True. If it is False it is a network or firewall
  problem at HQ, not a problem with this kit. Stop and tell HQ.

STEP 3 - enrol this computer (you will be asked for the code; it is not shown
         as you type, and it never appears on the command line)

    .\Receiver\SpeakLinkReceiver.exe enrol ``
        --backend-url http://192.168.4.134:8000 ``
        --allow-insecure-private-lan --expected-hq-host 192.168.4.134 ``
        --device-name "<a name for this computer>"

STEP 4 - check what was stored

    .\Receiver\SpeakLinkReceiver.exe status

STEP 5 - run it once by hand, and watch it

    .\Receiver\SpeakLinkReceiver.exe run ``
        --backend-url http://192.168.4.134:8000 ``
        --allow-insecure-private-lan --expected-hq-host 192.168.4.134

  It prints a warning about plain HTTP. That warning is correct. This pilot
  sends the credential unencrypted, which is only ever acceptable on a private
  network you control. Press Ctrl+C to stop.

STEP 6 - make it start automatically when you log on

    .\Installer\Install-SpeakLinkReceiverLanPilot.ps1 -PackagePath .\Receiver

  This COPIES the Receiver to
      %LOCALAPPDATA%\SpeakLink\receiver-app
  and points the scheduled task at that copy - deliberately, so that taking
  away the USB stick or emptying Downloads does not stop the Store working
  weeks later.

STEP 7 - check the task

    .\Installer\Test-SpeakLinkReceiverLanPilot.ps1

  Expect SPEAKLINK_RECEIVER_TASK_SCHEDULER_VERIFIED.

STEP 8 - sign out and sign back in. Ask HQ whether this Store shows CONNECTED.

TO REMOVE EVERYTHING

    .\Installer\Uninstall-SpeakLinkReceiverLanPilot.ps1 -StopRunning
    .\Receiver\SpeakLinkReceiver.exe remove-local-credential

WHERE THINGS LIVE
  installed Receiver : %LOCALAPPDATA%\SpeakLink\receiver-app
  sealed credential  : %LOCALAPPDATA%\SpeakLink\receiver\device-credential.bin
  logs               : %LOCALAPPDATA%\SpeakLink\receiver\logs

  The credential is sealed to THIS Windows account on THIS computer. Copying
  the file to another machine gives that machine nothing.

IMPORTANT - WHAT THIS DOES NOT DO
  It starts when you LOG ON. It is not a Windows service. It does not start
  before anybody signs in, and it does not keep playing while the computer sits
  locked with nobody signed in. If this Store must announce with nobody logged
  in, this kit does not cover it - tell HQ.

  A green result here means the Receiver decoded audio and handed it to a sound
  device. It does not mean a speaker made a sound. Listen, and tell HQ what you
  actually heard.

Receiver package commit : $($packageManifest.source_commit_short)
Kit commit              : $shortCommit
Assembled (UTC)         : $((Get-Date).ToUniversalTime().ToString('o'))
"@ | Set-Content -Encoding ascii (Join-Path $OutputRoot 'README-FIRST.txt')

# ---------------------------------------------------------------------------
# Hash everything, then describe it
# ---------------------------------------------------------------------------
$kitFiles = Get-ChildItem $OutputRoot -Recurse -File |
            Where-Object { $_.Name -notin @('KIT-SHA256SUMS.txt', 'KIT-MANIFEST.json') }
$kitFiles | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(),
                  $_.FullName.Substring($OutputRoot.Length + 1).Replace('\', '/')
} | Set-Content -Encoding ascii (Join-Path $OutputRoot 'KIT-SHA256SUMS.txt')

# No absolute build path and no Windows username: a manifest that shipped
# "C:\Users\<someone>\..." would tell every Store who assembled it and where.
[PSCustomObject]@{
    product = 'SpeakLink Store Pilot Kit'
    purpose = 'private LAN pilot'
    production_release = $false
    kit_source_commit = $commit
    kit_source_commit_short = $shortCommit
    kit_source_branch = $branch
    kit_source_tree_clean = ($dirty.Count -eq 0)
    assembled_utc = (Get-Date).ToUniversalTime().ToString('o')
    receiver_package = @{
        name = (Split-Path -Leaf $PackagePath)
        source_commit = $packageManifest.source_commit
        source_commit_short = $packageManifest.source_commit_short
        built_utc = $packageManifest.built_utc
        executable_sha256 = $packageManifest.executable_sha256
        ffmpeg = $packageManifest.ffmpeg
    }
    installer_scripts = $installerScripts
    installs_to = '%LOCALAPPDATA%\SpeakLink\receiver-app'
    credential_path = '%LOCALAPPDATA%\SpeakLink\receiver\device-credential.bin'
    log_directory = '%LOCALAPPDATA%\SpeakLink\receiver\logs'
    requires_python_on_target = $false
    requires_node_on_target = $false
    requires_internet = $false
    secrets_in_kit = 'none'
    file_count = $kitFiles.Count
    total_bytes = ($kitFiles | Measure-Object Length -Sum).Sum
} | ConvertTo-Json -Depth 6 | Set-Content -Encoding ascii (Join-Path $OutputRoot 'KIT-MANIFEST.json')

Write-Output ''
Write-Output "  files : $($kitFiles.Count)"
Write-Output "  size  : $([Math]::Round((($kitFiles | Measure-Object Length -Sum).Sum) / 1MB, 1)) MB"
Write-Output ''
Write-Output 'SPEAKLINK_STORE_PILOT_KIT_BUILT'
Write-Output "Kit: $OutputRoot"
Write-Output "Verify with .\scripts\Test-SpeakLinkStorePilotKit.ps1 -KitPath `"$OutputRoot`""
Write-Output 'This kit is git-ignored and must not be committed.'
