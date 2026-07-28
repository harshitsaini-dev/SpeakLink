<#
.SYNOPSIS
    Verify a Store pilot kit is complete, self-contained and secret-free.

.DESCRIPTION
    The question this answers is narrow and specific: if this folder were the
    ONLY thing a Store computer ever received, could the operator follow
    README-FIRST.txt to the end?

    That is why it checks the installer scripts are present and not merely
    referenced. The gap that produced this kit was a runbook confidently telling
    an operator to run a script that existed only in the repository.

    A check that cannot be read reports UNKNOWN, never PASS and never FAIL, and
    no marker is emitted while anything is unreadable.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$KitPath
)

$ErrorActionPreference = 'Stop'

$script:results = @()
function Check {
    # The parameters are deliberately awkward names. They were $Name and $Body,
    # and a caller loop that used $name for the file it was checking silently
    # got the check's own LABEL instead - PowerShell resolves variables in the
    # calling function's scope and is case-insensitive, so `$name` inside the
    # scriptblock meant "Installer\Install-....ps1 present". Three files that
    # were present the whole time were reported missing. A probe that shadows
    # its caller's variables reports on itself.
    param([string]$CheckLabel, [scriptblock]$CheckBody)
    try { $verdict = & $CheckBody } catch { $verdict = 'UNKNOWN' }
    $outcome = switch ($verdict) { $true { 'PASS' } $false { 'FAIL' } default { [string]$verdict } }
    $script:results += [PSCustomObject]@{ Name = $CheckLabel; Verdict = $outcome }
    Write-Output ('  {0,-58}{1}' -f $CheckLabel, $outcome)
}

if (-not (Test-Path $KitPath)) { throw "No kit at $KitPath." }
$KitPath = (Resolve-Path $KitPath).Path
$receiver = Join-Path $KitPath 'Receiver'
$installer = Join-Path $KitPath 'Installer'

Write-Output '=== SpeakLink Store pilot kit verification ==='
Write-Output "  kit: $KitPath"
Write-Output ''

# ---------------------------------------------------------------------------
# Structure
# ---------------------------------------------------------------------------
Check 'Receiver folder present' { Test-Path $receiver }
Check 'Installer folder present' { Test-Path $installer }
Check 'README-FIRST.txt present' { Test-Path (Join-Path $KitPath 'README-FIRST.txt') }
Check 'KIT-MANIFEST.json present' { Test-Path (Join-Path $KitPath 'KIT-MANIFEST.json') }
Check 'KIT-SHA256SUMS.txt present' { Test-Path (Join-Path $KitPath 'KIT-SHA256SUMS.txt') }

foreach ($required in @('SpeakLinkReceiver.exe', 'ffmpeg.exe', 'manifest.json', 'SHA256SUMS.txt', '_internal', 'licenses')) {
    Check "Receiver\$required present" { Test-Path (Join-Path $receiver $required) }
}

# The gap this kit exists to close.
foreach ($name in @('Install-SpeakLinkReceiverLanPilot.ps1',
                    'Test-SpeakLinkReceiverLanPilot.ps1',
                    'Uninstall-SpeakLinkReceiverLanPilot.ps1')) {
    Check "Installer\$name present" { Test-Path (Join-Path $installer $name) }
}

# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------
Check 'every file matches KIT-SHA256SUMS.txt' {
    $bad = 0
    foreach ($line in (Get-Content (Join-Path $KitPath 'KIT-SHA256SUMS.txt'))) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '  ', 2
        $file = Join-Path $KitPath ($parts[1] -replace '/', '\')
        if (-not (Test-Path $file)) { $bad++; continue }
        if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) { $bad++ }
    }
    $bad -eq 0
}
Check 'the inner Receiver package still matches its own hashes' {
    $bad = 0
    foreach ($line in (Get-Content (Join-Path $receiver 'SHA256SUMS.txt'))) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '  ', 2
        $file = Join-Path $receiver ($parts[1] -replace '/', '\')
        if (-not (Test-Path $file)) { $bad++; continue }
        if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) { $bad++ }
    }
    $bad -eq 0
}

$kitManifest = Get-Content (Join-Path $KitPath 'KIT-MANIFEST.json') -Raw | ConvertFrom-Json
Check 'the kit records its own source commit' { [bool]$kitManifest.kit_source_commit }
Check 'the kit records the Receiver package commit' { [bool]$kitManifest.receiver_package.source_commit }
Check 'the kit records the FFmpeg version and hash' {
    [bool]$kitManifest.receiver_package.ffmpeg.sha256 -and [bool]$kitManifest.receiver_package.ffmpeg.version
}
Check 'the kit does not claim to be a production release' { $kitManifest.production_release -eq $false }
Check 'the kit installs to LOCALAPPDATA, not to wherever it was unzipped' {
    $kitManifest.installs_to -match 'LOCALAPPDATA'
}

# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------
Check 'the packaged Receiver is not marked stale' {
    -not (Test-Path (Join-Path $receiver 'STALE-DO-NOT-DEPLOY.txt'))
}
Check 'the Receiver package was built from a clean tree' {
    (Get-Content (Join-Path $receiver 'manifest.json') -Raw | ConvertFrom-Json).source_tree_clean -eq $true
}
Check 'the executable is newer than nothing it should predate' {
    $exeTime = (Get-Item (Join-Path $receiver 'SpeakLinkReceiver.exe')).LastWriteTime
    $newestInternal = (Get-ChildItem (Join-Path $receiver '_internal') -Recurse -File |
                       Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
    $exeTime -ge $newestInternal.AddMinutes(-5)
}

# ---------------------------------------------------------------------------
# Nothing secret, nothing that phones home
# ---------------------------------------------------------------------------
Check 'no credential, code, password or key anywhere in the kit' {
    $patterns = @('speaklink_rcv_v1\.', 'ECHO(-[A-Z0-9]{4}){2,}', 'BEGIN [A-Z ]*PRIVATE KEY',
                  'password\s*=\s*\S', 'Authorization:\s*Bearer')
    $hits = @(Get-ChildItem $KitPath -Recurse -File -Include *.txt, *.json, *.ps1, *.md |
              Select-String -Pattern $patterns -ErrorAction SilentlyContinue)
    $hits.Count -eq 0
}
Check 'no .env, database or backend module in the kit' {
    @(Get-ChildItem $KitPath -Recurse -File -Include '.env', '*.db', '*.db-wal', '*.db-shm', 'server.py', 'db.py').Count -eq 0
}
Check 'the kit declares it needs no Internet' { $kitManifest.requires_internet -eq $false }
Check 'no installer script downloads anything' {
    $scripts = @(Get-ChildItem $installer -File -Filter *.ps1)
    # Assert there was something to scan. "I found no downloads in no files" is
    # not the same answer as "these scripts download nothing", and a check that
    # cannot tell them apart passes hardest exactly when the kit is emptiest.
    if ($scripts.Count -eq 0) { throw 'there are no installer scripts to scan' }
    @($scripts | Select-String -Pattern 'Invoke-WebRequest', 'Invoke-RestMethod', 'curl ', 'wget ', 'Start-BitsTransfer' -ErrorAction SilentlyContinue).Count -eq 0
}

# ---------------------------------------------------------------------------
# The instructions are followable from inside the kit
# ---------------------------------------------------------------------------
$readme = Get-Content (Join-Path $KitPath 'README-FIRST.txt') -Raw
Check 'README-FIRST names only paths inside the kit' {
    # The original defect: instructions pointing at .\scripts\ in a repository
    # the Store computer does not have.
    $readme -notmatch '\.\\scripts\\'
}
Check 'README-FIRST gives the enrol command' { $readme -match 'SpeakLinkReceiver\.exe enrol' }
Check 'README-FIRST gives the install command' { $readme -match 'Installer\\Install-SpeakLinkReceiverLanPilot\.ps1' }
Check 'README-FIRST warns that plain HTTP is pilot-only' { $readme -match '(?i)plain HTTP' }
Check 'README-FIRST states this is not a service' { $readme -match '(?i)not a Windows service' }
Check 'README-FIRST refuses to claim a speaker was heard' {
    $readme -match '(?i)does not mean a speaker'
}
Check 'README-FIRST contains no credential or code' {
    $readme -notmatch 'speaklink_rcv_v1\.' -and $readme -notmatch 'ECHO(-[A-Z0-9]{4}){2,}'
}

# ---------------------------------------------------------------------------
# The executable actually runs from the kit
# ---------------------------------------------------------------------------
Check 'the packaged executable answers --version from the kit' {
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { ((& (Join-Path $receiver 'SpeakLinkReceiver.exe') --version) -join ' ') -match '\d+\.\d+\.\d+' }
    finally { $ErrorActionPreference = $previous }
}
Check 'the packaged FFmpeg runs from the kit' {
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { ((& (Join-Path $receiver 'ffmpeg.exe') -version | Select-Object -First 1) -join '') -match 'ffmpeg version' }
    finally { $ErrorActionPreference = $previous }
}

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
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
if ($unknown.Count -gt 0) {
    Write-Output ''
    Write-Output 'Result: INCOMPLETE. No marker is emitted while a check is unreadable.'
    exit 1
}

Write-Output ''
Write-Output "  $($script:results.Count) checks passed"
Write-Output ''
Write-Output 'SPEAKLINK_STORE_PILOT_KIT_VERIFIED'
Write-Output ''
Write-Output 'Software evidence only: the kit is complete, self-contained, carries no'
Write-Output 'secret and runs without Python or an FFmpeg install. It is not evidence'
Write-Output 'of a second desktop, an amplifier, an audible speaker or SPEAKER_VERIFIED.'

# Explicit, because without it PowerShell returns the exit code of the last
# native command it happened to run - and `ffmpeg -version | Select -First 1`
# closes the pipe early and exits 255. This script printed its success marker
# and exited 255, which any caller would read as a failed verification.
exit 0
