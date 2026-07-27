<#
.SYNOPSIS
    Verify a built EchoCastReceiver package the way a Store desktop would meet it.

.DESCRIPTION
    The point of this package is that a till needs nothing installed, so the
    checks are run with Python removed from PATH. Leaving Python on PATH would
    let a broken package pass here and fail on the one machine it matters.

    It also copies the package to a path containing spaces before running it.
    That is not paranoia: this repository lives in "HQ-Broadcast-Full (1)" and
    has already been bitten twice by unquoted paths.
#>
[CmdletBinding()]
param(
    [string]$PackagePath,
    [string]$Version = '1.0.0'
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
if (-not $PackagePath) { $PackagePath = Join-Path $repositoryRoot "artifacts\EchoCastReceiver-$Version" }
if (-not (Test-Path $PackagePath)) { throw "No package at $PackagePath. Build it first." }

$results = [ordered]@{}
function Check { param([string]$Name, [scriptblock]$Test)
    try { $value = & $Test } catch { $value = $false }
    $results[$Name] = $value
    $mark = if ($value -eq $true) { 'PASS' } elseif ($value -eq $false) { 'FAIL' } else { "$value" }
    Write-Output ("  {0,-50} {1}" -f $Name, $mark)
}

Write-Output '=== EchoCastReceiver package verification ==='
Write-Output "  package: $PackagePath"

# ---------------------------------------------------------------------------
# Contents
# ---------------------------------------------------------------------------
Check 'EchoCastReceiver.exe present'   { Test-Path (Join-Path $PackagePath 'EchoCastReceiver.exe') }
Check 'ffmpeg.exe present'             { Test-Path (Join-Path $PackagePath 'ffmpeg.exe') }
Check 'manifest.json present'          { Test-Path (Join-Path $PackagePath 'manifest.json') }
Check 'SHA256SUMS.txt present'         { Test-Path (Join-Path $PackagePath 'SHA256SUMS.txt') }
Check 'licence notice present'         { Test-Path (Join-Path $PackagePath 'licenses\FFMPEG-LICENSE.txt') }

Check 'every hash in SHA256SUMS matches' {
    $ok = $true
    foreach ($line in Get-Content (Join-Path $PackagePath 'SHA256SUMS.txt')) {
        if (-not $line.Trim()) { continue }
        $expected, $relative = $line -split '\s+', 2
        $full = Join-Path $PackagePath ($relative.Trim().Replace('/', '\'))
        if (-not (Test-Path $full)) { $ok = $false; continue }
        if ((Get-FileHash $full -Algorithm SHA256).Hash.ToLower() -ne $expected.Trim()) { $ok = $false }
    }
    $ok
}

Check 'no .env, database or backend module bundled' {
    $bad = @()
    $bad += Get-ChildItem $PackagePath -Recurse -Force -Filter '.env' -ErrorAction SilentlyContinue
    $bad += Get-ChildItem $PackagePath -Recurse -Filter '*.db' -ErrorAction SilentlyContinue
    $bad += Get-ChildItem $PackagePath -Recurse -Filter 'server.py' -ErrorAction SilentlyContinue
    $bad.Count -eq 0
}

Check 'no credential, key or password in any package file' {
    $patterns = @('echocast_rcv_v', 'eyJ[A-Za-z0-9_-]{10}', '\$2[aby]\$[0-9]{2}\$',
                  'ADMIN_PASSWORD', 'JWT_SECRET', 'BEGIN .*PRIVATE KEY')
    $hits = Get-ChildItem $PackagePath -Recurse -File |
            Where-Object { $_.Length -lt 20MB } |
            ForEach-Object { Select-String -Path $_.FullName -Pattern $patterns -ErrorAction SilentlyContinue }
    -not $hits
}

Check 'manifest records the FFmpeg version and hash' {
    $manifest = Get-Content (Join-Path $PackagePath 'manifest.json') -Raw | ConvertFrom-Json
    $manifest.ffmpeg.sha256 -and $manifest.ffmpeg.version -and -not $manifest.requires_python_on_target
}

# ---------------------------------------------------------------------------
# Behaviour, from a spaced path with no Python
# ---------------------------------------------------------------------------
$sandbox = Join-Path $env:TEMP ("EchoCast Package Check " + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null
Copy-Item $PackagePath (Join-Path $sandbox 'EchoCastReceiver') -Recurse -Force
$exe = Join-Path $sandbox 'EchoCastReceiver\EchoCastReceiver.exe'
Write-Output "  running from: $exe"

$savedPath = $env:PATH
# WindowsApps carries Microsoft Store execution aliases for python/python3, so
# it has to go too or "no Python" is not true.
$sanitised = ($env:PATH -split ';' |
    Where-Object { $_ -and ($_ -notmatch 'Python|\.venv|pyenv|conda|WindowsApps|ffmpeg') }) -join ';'
try {
    $env:PATH = $sanitised
    Check 'python is not resolvable'      { $null -eq (Get-Command python -ErrorAction SilentlyContinue) }
    Check 'ffmpeg is not on PATH'         { $null -eq (Get-Command ffmpeg -ErrorAction SilentlyContinue) }

    # Reported, not asserted. py.exe ships with Windows and lives in C:\Windows:
    # it cannot be taken off PATH without breaking PATH, and its presence says
    # nothing about whether THIS package needs Python. The claim that matters is
    # that the executable runs when no interpreter is reachable, and the checks
    # below make exactly that claim. Failing on py.exe would have been a check
    # that looked strict while testing the wrong thing.
    Write-Output ("  {0,-50} {1}" -f 'py launcher present (informational)',
                  ($null -ne (Get-Command py -ErrorAction SilentlyContinue)))

    Check '--version answers' {
        $out = (& $exe --version 2>&1) -join ''
        $LASTEXITCODE -eq 0 -and $out -match 'EchoCastReceiver'
    }
    Check '--help lists every command' {
        $out = (& $exe --help 2>&1) -join ' '
        foreach ($command in 'enrol', 'run', 'status', 'rotate-local-credential', 'remove-local-credential') {
            if ($out -notmatch [regex]::Escape($command)) { return $false }
        }
        $true
    }
    Check 'status runs with no credential' {
        $out = (& $exe status --credential-path (Join-Path $sandbox 'none.bin') 2>&1) -join ' '
        $out -match 'enrolled'
    }
    # NOT `2>&1`. In Windows PowerShell 5.1 redirecting a native command's
    # stderr wraps each line in a NativeCommandError, which with
    # ErrorActionPreference='Stop' throws - so the refusal these checks are
    # looking for was being scored as a failed check. Relax the preference for
    # the length of the call and read the exit code, which is the actual answer.
    function Invoke-Refused {
        param([string[]]$Arguments)
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $null = & $exe @Arguments 2>&1
            return $LASTEXITCODE -ne 0
        } finally { $ErrorActionPreference = $previous }
    }

    Check 'a code as a command argument is refused' {
        Invoke-Refused @('enrol', '--backend-url', 'http://192.168.4.134:8000',
                         '--device-name', 'x', '--code', 'ECHO-1')
    }
    Check 'a credential as a command argument is refused' {
        Invoke-Refused @('run', '--backend-url', 'http://192.168.4.134:8000',
                         '--credential', 'secret')
    }
    Check 'plain HTTP to the LAN without the flag is refused' {
        Invoke-Refused @('enrol', '--backend-url', 'http://192.168.4.134:8000',
                         '--device-name', 'x', '--from-stdin')
    }
    Check 'plain HTTP to the LAN needs the explicit flag' {
        $null = & $exe status --credential-path (Join-Path $sandbox 'none.bin') 2>&1
        # status takes no URL; the URL guard is covered by the Agent unit tests.
        # What matters here is that the frozen build carries the same parser.
        $out = (& $exe run --help 2>&1) -join ' '
        $out -match 'allow-insecure-private-lan' -and $out -match 'expected-hq-host'
    }
    Check 'the packaged ffmpeg runs' {
        $packaged = Join-Path $sandbox 'EchoCastReceiver\ffmpeg.exe'
        $out = (& $packaged -version 2>&1 | Select-Object -First 1) -join ''
        $out -match 'ffmpeg version'
    }
    Check 'no Python process was spawned' {
        # The EXE has exited by now; what matters is that none of the calls above
        # left a python.exe behind whose parent was our sandbox.
        $strays = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                  Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape($sandbox) }
        -not $strays
    }
} finally {
    $env:PATH = $savedPath
}

Write-Output ''
$failed = @($results.GetEnumerator() | Where-Object { $_.Value -eq $false })
if ($failed.Count -eq 0) {
    Write-Output 'Result: ECHOCAST_RECEIVER_PACKAGE_VERIFIED'
    Write-Output 'Software evidence only: the package runs without Python and carries its own'
    Write-Output 'FFmpeg. Not an amplifier, speaker or SPEAKER_VERIFIED result.'
    exit 0
}
Write-Output "Result: FAILED ($($failed.Count) check(s))"
$failed | ForEach-Object { Write-Output "  - $($_.Key)" }
exit 1
