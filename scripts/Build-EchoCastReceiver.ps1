<#
.SYNOPSIS
    Build the standalone EchoCastReceiver package for Store desktops.

.DESCRIPTION
    Produces a one-folder package that runs on a Windows desktop with no Python,
    no pip, no virtual environment, no source checkout and no Node.

    FFmpeg is NOT downloaded. The path to an existing local ffmpeg.exe must be
    given, and its version and SHA-256 are recorded in the package manifest. A
    build step that fetches a binary from the Internet decides, silently and
    differently each time, what ends up on 44 Store computers.

.PARAMETER FfmpegPath
    An existing ffmpeg.exe to package. Defaults to whatever is on PATH, which is
    reported before use so it is never a surprise.

.EXAMPLE
    .\scripts\Build-EchoCastReceiver.ps1 -FfmpegPath 'C:\ffmpeg\bin\ffmpeg.exe'
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$FfmpegPath,
    [string]$Version = '1.0.0',
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot 'backend\.venv\Scripts\python.exe'
$spec = Join-Path $repositoryRoot 'receiver_agent.spec'
$outputRoot = Join-Path $repositoryRoot "artifacts\EchoCastReceiver-$Version"

Write-Output '=== building EchoCastReceiver ==='

if (-not (Test-Path $venvPython)) { throw "Python virtual environment not found at $venvPython." }
if (-not (Test-Path $spec)) { throw "Spec file not found at $spec." }

& $venvPython -m PyInstaller --version > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller is not installed in the build environment. Install it with: $venvPython -m pip install pyinstaller"
}

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

Write-Output "  ffmpeg source : $FfmpegPath"
Write-Output "  ffmpeg version: $ffmpegVersion"
Write-Output "  ffmpeg sha256 : $ffmpegHash"
Write-Output "  ffmpeg size   : $([Math]::Round((Get-Item $FfmpegPath).Length / 1MB, 1)) MB"
Write-Output "  output        : $outputRoot"

if ($DryRun -or -not $PSCmdlet.ShouldProcess($outputRoot, 'Build the Receiver package')) {
    Write-Output ''
    Write-Output 'Dry run: every input was checked. Nothing was built.'
    exit 0
}

Write-Output ''
Write-Output '--- PyInstaller ---'
& $venvPython -m PyInstaller $spec --noconfirm `
    --distpath (Join-Path $repositoryRoot 'build\pyi-dist') `
    --workpath (Join-Path $repositoryRoot 'build\pyi-work') | Select-Object -Last 1
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

New-Item -ItemType Directory -Force -Path $outputRoot, (Join-Path $outputRoot 'licenses') | Out-Null
Copy-Item (Join-Path $repositoryRoot 'build\pyi-dist\EchoCastReceiver\*') $outputRoot -Recurse -Force
Copy-Item $FfmpegPath (Join-Path $outputRoot 'ffmpeg.exe') -Force

# The licence notice has to name the actual build. gyan.dev "full" builds link
# GPL-only libraries, which is a distribution obligation, not a formality.
@"
FFmpeg
======

Version : $ffmpegVersion
SHA-256 : $ffmpegHash
Source  : the file named at build time; see manifest.json

If this is a gyan.dev "full" build it links GPL-only libraries (x264, x265 and
others) and the whole binary is covered by GPLv3.

Full licence text: https://www.gnu.org/licenses/gpl-3.0.txt
FFmpeg licensing:  https://ffmpeg.org/legal.html

EchoCast uses FFmpeg only to decode WebM/Opus through a pipe. It links no
FFmpeg library and ships the executable unmodified.

If this package is ever distributed outside the organisation that built it,
either honour the GPL obligations or rebuild against an LGPL FFmpeg without
GPL-only libraries. The decode path used here does not need them.
"@ | Set-Content -Encoding utf8 (Join-Path $outputRoot 'licenses\FFMPEG-LICENSE.txt')

$files = Get-ChildItem $outputRoot -Recurse -File |
         Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'manifest.json') }
$files | ForEach-Object {
    "{0}  {1}" -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(),
                  $_.FullName.Substring($outputRoot.Length + 1).Replace('\', '/')
} | Set-Content -Encoding utf8 (Join-Path $outputRoot 'SHA256SUMS.txt')

[PSCustomObject]@{
    product = 'EchoCastReceiver'
    version = $Version
    built_utc = (Get-Date).ToUniversalTime().ToString('o')
    format = 'PyInstaller one-folder'
    requires_python_on_target = $false
    requires_node_on_target = $false
    ffmpeg = @{
        version = $ffmpegVersion
        sha256 = $ffmpegHash
        source_path_at_build = $FfmpegPath
        resolved_at_runtime = 'relative to EchoCastReceiver.exe'
    }
    file_count = $files.Count
    total_bytes = ($files | Measure-Object Length -Sum).Sum
    secrets_in_package = 'none'
} | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 (Join-Path $outputRoot 'manifest.json')

Write-Output ''
Write-Output "  files  : $($files.Count)"
Write-Output "  size   : $([Math]::Round((($files | Measure-Object Length -Sum).Sum) / 1MB, 1)) MB"
Write-Output ''
Write-Output 'Built. Verify with .\scripts\Test-EchoCastReceiverPackage.ps1'
Write-Output 'This package is git-ignored and must not be committed.'
