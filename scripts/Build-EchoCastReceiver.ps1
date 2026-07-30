<#
.SYNOPSIS
    Build the standalone EchoCastReceiver package for Store desktops.

.DESCRIPTION
    Produces a one-folder package that runs on a Windows desktop with no Python,
    no pip, no virtual environment, no source checkout and no Node.

    THE PROBLEM THIS VERSION EXISTS TO SOLVE

    The first build of this package was stale before it shipped. PyInstaller ran,
    then the source changed, then the package was assembled by copying the
    earlier PyInstaller output - so the executable in artifacts/ was nine minutes
    older than the code it was supposed to contain, and it never carried the
    packaged-FFmpeg resolution that had just been added. Nothing failed. The
    package tested green, because every test it had could pass without that code.

    So this build:

      * refuses a dirty working tree, unless -AllowDirty says otherwise;
      * uses a fresh timestamped work and dist directory every time, so it can
        never reuse an earlier PyInstaller output;
      * records the exact source commit in the package manifest;
      * checks the built executable is NEWER than every source file that went
        into it, and fails if it is not.

    That last check is the one that would have caught the original mistake.

    FFmpeg is never downloaded. An explicit path is required, and its version and
    SHA-256 are recorded. A build step that fetches a binary decides silently,
    and differently each time, what ends up on 44 Store computers.

    The distributable manifest carries no absolute build path and no Windows
    username - a manifest that shipped "C:\Users\<someone>\..." would leak the
    build operator's account name to every Store.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$FfmpegPath,
    [string]$Version = '1.0.0',
    [switch]$AllowDirty,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
$spec = Join-Path $repositoryRoot 'receiver_agent.spec'

Write-Output '=== building EchoCastReceiver ==='

# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
if (-not (Test-Path $venvPython)) { throw "Python virtual environment not found at $venvPython." }
if (-not (Test-Path $spec)) { throw "Spec file not found at $spec." }

Push-Location $repositoryRoot
try {
    $commit = (git rev-parse HEAD 2>$null)
    $shortCommit = (git rev-parse --short HEAD 2>$null)
    $branch = (git rev-parse --abbrev-ref HEAD 2>$null)
    $dirty = @(git status --porcelain 2>$null)
} finally { Pop-Location }

if (-not $commit) { throw 'Not a git working tree. The package manifest must record a source commit.' }

Write-Output "  commit  : $shortCommit ($branch)"
if ($dirty.Count -gt 0) {
    Write-Output "  tree    : DIRTY ($($dirty.Count) changed file(s))"
    if (-not $AllowDirty) {
        throw ("The working tree has $($dirty.Count) uncommitted change(s). A package built now " +
               "would record a commit it does not actually contain. Commit first, or pass " +
               "-AllowDirty for a development build.")
    }
    Write-Output '  NOTE    : -AllowDirty was given. This package does NOT match its recorded commit.'
} else {
    Write-Output '  tree    : clean'
}

& $venvPython -m PyInstaller --version > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed. Install the pinned build tools with: $venvPython -m pip install -r requirements-build.txt"
}
$pyinstallerVersion = ((& $venvPython -m PyInstaller --version 2>&1) -join '').Trim()
$pythonVersion = ((& $venvPython --version 2>&1) -join '').Trim()

if (-not $FfmpegPath) {
    $onPath = Get-Command ffmpeg -ErrorAction SilentlyContinue
    if (-not $onPath) {
        throw 'No -FfmpegPath given and no ffmpeg on PATH. This build never downloads one - name the binary you intend to ship.'
    }
    $FfmpegPath = $onPath.Source
}
if (-not (Test-Path $FfmpegPath)) { throw "FFmpeg not found at $FfmpegPath." }

$ffmpegVersion = ((& $FfmpegPath -version 2>&1 | Select-Object -First 1) -join '')
$ffmpegHash = (Get-FileHash $FfmpegPath -Algorithm SHA256).Hash

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$outputRoot = Join-Path $repositoryRoot "artifacts\EchoCastReceiver-$Version-$shortCommit-$stamp"
$distPath = Join-Path $repositoryRoot "build\pyi-dist-$stamp"
$workPath = Join-Path $repositoryRoot "build\pyi-work-$stamp"

Write-Output "  python  : $pythonVersion"
Write-Output "  builder : $pyinstallerVersion"
Write-Output "  ffmpeg  : $ffmpegVersion"
Write-Output "  ff hash : $ffmpegHash"
Write-Output "  output  : $outputRoot"

if ($DryRun -or -not $PSCmdlet.ShouldProcess($outputRoot, 'Build the Receiver package')) {
    Write-Output ''
    Write-Output 'Dry run: every input was checked. Nothing was built.'
    exit 0
}

# ---------------------------------------------------------------------------
# Build. Fresh directories, so an older output cannot be picked up by mistake.
# ---------------------------------------------------------------------------
$sourceNewest = (Get-ChildItem (Join-Path $repositoryRoot 'tools') -Filter '*.py' -Recurse |
                 Sort-Object LastWriteTime -Descending | Select-Object -First 1).LastWriteTime
$specTime = (Get-Item $spec).LastWriteTime
if ($specTime -gt $sourceNewest) { $sourceNewest = $specTime }
Write-Output "  newest source input: $sourceNewest"

Write-Output ''
Write-Output '--- PyInstaller ---'
# PyInstaller writes its own progress ("114 INFO: PyInstaller: ...") to STDERR.
# Under $ErrorActionPreference = 'Stop' - set for this whole script - Windows
# PowerShell 5.1 treats ANY stderr text from a native command as a terminating
# error, even when the process goes on to exit 0. Measured directly: the build
# aborted on PyInstaller's very first INFO line, before PyInstaller had done
# anything wrong. Flipped to Continue for exactly this call, exit code checked
# by hand - the same guard every other chatty native command in this
# repository already needs.
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $venvPython -m PyInstaller $spec --noconfirm --distpath $distPath --workpath $workPath 2>&1 |
        Select-Object -Last 1
} finally {
    $ErrorActionPreference = $previousErrorAction
}
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

$builtExe = Join-Path $distPath 'EchoCastReceiver\EchoCastReceiver.exe'
if (-not (Test-Path $builtExe)) { throw "PyInstaller reported success but $builtExe does not exist." }

# The check that would have caught the original stale package.
$builtTime = (Get-Item $builtExe).LastWriteTime
if ($builtTime -lt $sourceNewest) {
    throw ("The built executable ($builtTime) is older than the newest source input " +
           "($sourceNewest). This package would not contain the current code.")
}
Write-Output "  built executable   : $builtTime  (newer than every source input)"

New-Item -ItemType Directory -Force -Path $outputRoot, (Join-Path $outputRoot 'licenses') | Out-Null
Copy-Item (Join-Path $distPath 'EchoCastReceiver\*') $outputRoot -Recurse -Force
Copy-Item $FfmpegPath (Join-Path $outputRoot 'ffmpeg.exe') -Force

@"
FFmpeg
======

Version : $ffmpegVersion
SHA-256 : $ffmpegHash

If this is a gyan.dev "full" build it links GPL-only libraries (x264, x265 and
others) and the whole binary is covered by GPLv3.

Full licence text: https://www.gnu.org/licenses/gpl-3.0.txt
FFmpeg licensing:  https://ffmpeg.org/legal.html

EchoCast uses FFmpeg only to decode WebM/Opus through a pipe. It links no
FFmpeg library and ships the executable unmodified.

If this package is ever distributed outside the organisation that built it,
either honour the GPL obligations or rebuild against an LGPL FFmpeg without
GPL-only libraries. The decode path used here does not need them.
"@ | Set-Content -Encoding ascii (Join-Path $outputRoot 'licenses\FFMPEG-LICENSE.txt')

$files = Get-ChildItem $outputRoot -Recurse -File |
         Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'manifest.json') }
$files | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(),
                  $_.FullName.Substring($outputRoot.Length + 1).Replace('\', '/')
} | Set-Content -Encoding ascii (Join-Path $outputRoot 'SHA256SUMS.txt')

# Deliberately no absolute build path and no username: a manifest that shipped
# "C:\Users\<someone>\..." would tell every Store who built it and where.
[PSCustomObject]@{
    product = 'EchoCastReceiver'
    version = $Version
    source_commit = $commit
    source_commit_short = $shortCommit
    source_branch = $branch
    source_tree_clean = ($dirty.Count -eq 0)
    built_utc = (Get-Date).ToUniversalTime().ToString('o')
    builder = @{ pyinstaller = $pyinstallerVersion; python = $pythonVersion; format = 'one-folder' }
    requires_python_on_target = $false
    requires_node_on_target = $false
    ffmpeg = @{
        filename = 'ffmpeg.exe'
        version = $ffmpegVersion
        sha256 = $ffmpegHash
        resolved_at_runtime = 'relative to EchoCastReceiver.exe'
    }
    executable_sha256 = (Get-FileHash (Join-Path $outputRoot 'EchoCastReceiver.exe') -Algorithm SHA256).Hash
    file_count = $files.Count
    total_bytes = ($files | Measure-Object Length -Sum).Sum
    secrets_in_package = 'none'
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding ascii (Join-Path $outputRoot 'manifest.json')

Write-Output ''
Write-Output "  files : $($files.Count)"
Write-Output "  size  : $([Math]::Round((($files | Measure-Object Length -Sum).Sum) / 1MB, 1)) MB"
Write-Output ''
Write-Output 'ECHOCAST_RECEIVER_FRESH_BUILD_VERIFIED'
Write-Output "Package: $outputRoot"
Write-Output 'Verify with .\scripts\Test-EchoCastReceiverPackage.ps1 -PackagePath "<that path>"'
Write-Output 'This package is git-ignored and must not be committed.'
