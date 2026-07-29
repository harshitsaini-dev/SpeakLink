<#
.SYNOPSIS
    Verify an EchoCast HQ package before anybody installs it. Read-only.

.DESCRIPTION
    Most of this script's work is negative. Checking that the expected files are
    present and hash correctly is the easy half; the half that matters is going
    looking for what should not be there at all.

    THE CHECK THAT MAKES THE OTHERS WORTH RUNNING

    Every file is accounted for against SHA256SUMS.txt in BOTH directions. A
    verifier that only hashes the listed files will pass a package with a
    database dropped into it, because nobody listed the database and so nobody
    hashed it. Unlisted files are a failure here, not a curiosity.

    A check that cannot be read reports UNKNOWN - never PASS, never FAIL - and
    no marker is emitted while anything is unreadable.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [string]$ExpectedVersion
)

$ErrorActionPreference = 'Stop'

$script:results = @()
function Check {
    param([string]$CheckLabel, [scriptblock]$CheckBody)
    try { $verdict = & $CheckBody } catch { $verdict = 'UNKNOWN' }
    $outcome = switch ($verdict) { $true { 'PASS' } $false { 'FAIL' } default { [string]$verdict } }
    $script:results += [PSCustomObject]@{ Name = $CheckLabel; Verdict = $outcome }
    Write-Output ('  {0,-58}{1}' -f $CheckLabel, $outcome)
}

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

if (-not (Test-Path $PackagePath)) { throw "No package at $PackagePath." }
$PackagePath = (Resolve-Path $PackagePath).Path
$prefix = $PackagePath.TrimEnd('\') + '\'

Write-Output '=== EchoCast HQ package verification ==='
Write-Output "  package: $(Split-Path -Leaf $PackagePath)"
Write-Output ''

# ---- everything that should be there ---------------------------------------
$runtimeExe = Join-Path $PackagePath 'EchoCastHQRuntime.exe'
foreach ($expected in @('EchoCastHQRuntime.exe', 'manifest.json', 'SHA256SUMS.txt',
                        'QUICK-START.txt', 'frontend\index.html', 'backend\server.py',
                        'Install-EchoCastHQAutoStart.ps1', 'Test-EchoCastHQAutoStart.ps1',
                        'Repair-EchoCastHQAutoStart.ps1', 'Uninstall-EchoCastHQAutoStart.ps1')) {
    $target = Join-Path $PackagePath $expected
    Check "$expected is present" ({ Test-Path $target }.GetNewClosure())
}

Check 'the runtime creates NO console window' { (Get-PeSubsystem $runtimeExe) -eq 2 }

# ---- hashes, in both directions ---------------------------------------------
$sumsFile = Join-Path $PackagePath 'SHA256SUMS.txt'
$listed = @{}
if (Test-Path $sumsFile) {
    foreach ($line in (Get-Content $sumsFile)) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '  ', 2
        $listed[$parts[1].Replace('/', '\')] = $parts[0]
    }
}

Check 'the hash manifest is not empty' { $listed.Count -gt 0 }
Check 'every listed file is present and matches its hash' {
    if ($listed.Count -eq 0) { throw 'nothing listed' }
    $bad = 0
    foreach ($relative in $listed.Keys) {
        $file = Join-Path $PackagePath $relative
        if (-not (Test-Path $file)) { $bad++; continue }
        if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $listed[$relative]) { $bad++ }
    }
    $bad -eq 0
}

# The direction that catches a database somebody dropped in. A file nobody
# listed is a file nobody hashed.
$unlisted = @()
foreach ($file in (Get-ChildItem $PackagePath -Recurse -File -Force)) {
    $relative = $file.FullName.Substring($prefix.Length)
    if ($relative -in @('SHA256SUMS.txt', 'manifest.json')) { continue }
    if (-not $listed.ContainsKey($relative)) { $unlisted += $relative }
}
Check 'no unlisted file was added after the build' { $unlisted.Count -eq 0 }
if ($unlisted.Count -gt 0) {
    $unlisted | Select-Object -First 10 | ForEach-Object { Write-Output "      unlisted: $_" }
}

# ---- what must never ship ---------------------------------------------------
$forbidden = @{
    'no database'            = @('*.db', '*.db-wal', '*.db-shm', '*.sqlite', '*.sqlite3')
    'no environment file'    = @('.env', '.env.*')
    'no key or certificate'  = @('*.pem', '*.key', '*.pfx', '*hmac-keys*', '*jwt-secret*')
    'no log or recording'    = @('*.log', '*.wav', '*.webm', '*.mp3')
}
foreach ($entry in $forbidden.GetEnumerator()) {
    $patterns = $entry.Value
    Check "$($entry.Key) is in the package" ({
        @(Get-ChildItem $PackagePath -Recurse -File -Force -Include $patterns `
            -ErrorAction SilentlyContinue).Count -eq 0
    }.GetNewClosure())
}
Check 'no node_modules, __pycache__ or virtual environment' {
    @(Get-ChildItem $PackagePath -Recurse -Directory -Force |
      Where-Object { $_.Name -in @('node_modules', '__pycache__', '.venv', 'venv') }).Count -eq 0
}
Check 'no test suite shipped with the backend' {
    -not (Test-Path (Join-Path $PackagePath 'backend\tests'))
}

# ---- no unexpected executable ------------------------------------------------
# An extra .exe in a package is either a mistake or somebody else's program, and
# both are reasons not to install it.
$allowedExecutables = @('EchoCastHQRuntime.exe')
$strayExecutables = @(Get-ChildItem $PackagePath -Recurse -File -Force -Include '*.exe', '*.msi', '*.bat', '*.cmd' `
                        -ErrorAction SilentlyContinue |
                      Where-Object { $_.Name -notin $allowedExecutables })
Check 'no unexpected executable' { $strayExecutables.Count -eq 0 }
if ($strayExecutables.Count -gt 0) {
    $strayExecutables | Select-Object -First 5 | ForEach-Object { Write-Output "      stray: $($_.Name)" }
}

# ---- the frontend is a production build, not a dev server --------------------
Check 'the frontend is a production build' {
    (Test-Path (Join-Path $PackagePath 'frontend\index.html')) -and
    -not (Test-Path (Join-Path $PackagePath 'frontend\package.json'))
}
Check 'no development server command anywhere in the package' {
    # react-scripts start is a watcher with a compiler attached. It is not what
    # an unattended HQ should depend on at seven in the morning.
    @(Get-ChildItem $PackagePath -Recurse -File -Include '*.ps1', '*.txt', '*.json', '*.py' |
      Select-String -Pattern 'react-scripts start', 'yarn start', 'npm start' `
        -ErrorAction SilentlyContinue).Count -eq 0
}

# ---- the task scripts point at the runtime, not at a wrapper -----------------
Check 'the install script runs EchoCastHQRuntime.exe directly' {
    # The action passes a variable, not a literal, so both halves are checked:
    # what the action executes, and what that variable was built from. Reading
    # only the 400 characters after New-ScheduledTaskAction reported FAIL on a
    # correct script, which is the kind of check that gets switched off.
    $body = Get-Content (Join-Path $PackagePath 'Install-EchoCastHQAutoStart.ps1') -Raw
    $action = $body.Substring($body.IndexOf('New-ScheduledTaskAction'))
    $action = $action.Substring(0, [Math]::Min(400, $action.Length))
    ($action -match '-Execute \$installedExe') -and
    ($body -match "\`$installedExe = Join-Path \`$InstallRoot 'EchoCastHQRuntime\.exe'") -and
    ($action -notmatch '(?i)(powershell|pwsh|cmd|python)\.exe') -and
    ($action -notmatch '(?i)\.(ps1|bat|cmd)\b')
}

# ---- nothing secret in any text the package carries --------------------------
Check 'no credential, code or token in any packaged text' {
    @(Get-ChildItem $PackagePath -Recurse -File -Include '*.ps1', '*.txt', '*.json', '*.py', '*.md' |
      Select-String -Pattern 'echocast_rcv_v1\.', 'ECHO(-[A-Z0-9]{4}){2,}', 'eyJ[A-Za-z0-9_-]{20,}' `
        -ErrorAction SilentlyContinue).Count -eq 0
}

# ---- the manifest describes this package ------------------------------------
$manifestFile = Join-Path $PackagePath 'manifest.json'
Check 'the manifest records a source commit' {
    (Get-Content $manifestFile -Raw | ConvertFrom-Json).source_commit -match '^[0-9a-f]{40}$'
}
Check 'the manifest records the runtime as windowed' {
    (Get-Content $manifestFile -Raw | ConvertFrom-Json).runtime.pe_subsystem -eq 2
}
Check 'the manifest runtime hash matches the packaged runtime' {
    $recorded = (Get-Content $manifestFile -Raw | ConvertFrom-Json).runtime.sha256
    $recorded -eq (Get-FileHash $runtimeExe -Algorithm SHA256).Hash.ToLower()
}
Check 'the manifest file count matches what is listed' {
    (Get-Content $manifestFile -Raw | ConvertFrom-Json).file_count -eq $listed.Count
}
Check 'the manifest names no build path and no person' {
    $text = Get-Content $manifestFile -Raw
    $text -notmatch '(?i)C:\\\\?Users\\\\?' -and $text -notmatch '(?i)AppData'
}
if ($ExpectedVersion) {
    Check "the package version is $ExpectedVersion" {
        (Get-Content $manifestFile -Raw | ConvertFrom-Json).version -eq $ExpectedVersion
    }
}
Check 'the package name matches the version and commit it records' {
    $manifest = Get-Content $manifestFile -Raw | ConvertFrom-Json
    $leaf = Split-Path -Leaf $PackagePath
    $leaf.StartsWith("EchoCastHQ-$($manifest.version)-$($manifest.source_commit_short)")
}

# ---- verdict ----------------------------------------------------------------
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
    Write-Output 'Result: INCOMPLETE. No marker while a check is unreadable.'
    exit 1
}

Write-Output ''
Write-Output "  $($script:results.Count) checks passed"
Write-Output ''
Write-Output 'ECHOCAST_HQ_PACKAGE_VERIFIED'
Write-Output ''
Write-Output 'Scope: the package is complete, unmodified since it was built, and'
Write-Output 'carries no database, key, secret or log. It says nothing about whether'
Write-Output 'HQ runs correctly on the machine you install it on - run'
Write-Output 'Test-EchoCastHQAutoStart.ps1 there.'
exit 0
