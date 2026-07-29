<#
.SYNOPSIS
    Verify the installed SpeakLink HQ auto-start. Read-only.

.DESCRIPTION
    A check that cannot be read reports UNKNOWN - never PASS, never FAIL - and
    no marker is emitted while anything is unreadable. An earlier firewall
    checker in this repository asked Get-NetFirewallRule without elevation, was
    told "Access is denied", and scored that as "the rule is not installed",
    reporting a missing rule that had been there the whole time.

    This script changes nothing. A verifier that repairs is a verifier whose
    PASS means nothing.

    WHAT IT CANNOT SEE, AND DOES NOT PRETEND TO

    Behaviour after a reboot, after sign-out and sign-in, or on a locked
    desktop. Those need a person at the machine and live in the manual
    acceptance runbook. In particular this cannot show that HQ comes back after
    an unattended restart, because it does not: HQ starts when the HQ user
    signs in.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'SpeakLink HQ Runtime',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\hq-app'),
    [string]$PersistentRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\persistent-lan-server'),
    [string]$StatusFile = (Join-Path $env:LOCALAPPDATA 'SpeakLink\hq-runtime-status.json'),
    # Only used to read the database integrity, never to write anything.
    [string]$Python = 'python'
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

Write-Output '=== SpeakLink HQ auto-start verification ==='
Write-Output "  task        : $TaskName"
Write-Output "  install root: $InstallRoot"
Write-Output ''

# ---- what is installed ------------------------------------------------------
$installedExe = Join-Path $InstallRoot 'SpeakLinkHQRuntime.exe'
Check 'the install root exists' { Test-Path $InstallRoot }
Check 'SpeakLinkHQRuntime.exe is installed' { Test-Path $installedExe }
Check 'the production frontend is installed' {
    Test-Path (Join-Path $InstallRoot 'frontend\index.html')
}
Check 'no development server is packaged beside it' {
    -not (Test-Path (Join-Path $InstallRoot 'frontend\package.json')) -and
    -not (Test-Path (Join-Path $InstallRoot 'node_modules'))
}
Check 'every installed file matches its build hash' {
    $bad = 0
    foreach ($line in (Get-Content (Join-Path $InstallRoot 'SHA256SUMS.txt'))) {
        if (-not $line.Trim()) { continue }
        $parts = $line -split '  ', 2
        $file = Join-Path $InstallRoot ($parts[1] -replace '/', '\')
        if (-not (Test-Path $file)) { $bad++; continue }
        if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) { $bad++ }
    }
    $bad -eq 0
}
Check 'the installed runtime creates NO console window' {
    (Get-PeSubsystem $installedExe) -eq 2
}
Check 'no database was shipped into the application folder' {
    @(Get-ChildItem $InstallRoot -Recurse -File -Include '*.db', '*.sqlite', '*.sqlite3' `
        -ErrorAction SilentlyContinue).Count -eq 0
}
Check 'no key or environment file was shipped with it' {
    @(Get-ChildItem $InstallRoot -Recurse -File -Force `
        -Include '.env', '*.pem', '*.key', 'jwt-secret.txt', '*-hmac-keys.bin' `
        -ErrorAction SilentlyContinue).Count -eq 0
}

# ---- the task ---------------------------------------------------------------
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Check 'the scheduled task is registered' { [bool]$task }
if ($task) {
    Check 'the task is enabled' { $task.State -ne 'Disabled' }
    Check 'it triggers at logon' {
        ($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -contains 'MSFT_TaskLogonTrigger'
    }
    Check 'it has a separate periodic recovery trigger' {
        # Separate on purpose: repetition attached only to a logon trigger
        # begins only at a logon, so a runtime that dies mid-morning would wait
        # for the next sign-in.
        @($task.Triggers | Where-Object {
            $_.CimClass.CimClassName -ne 'MSFT_TaskLogonTrigger' -and
            $_.Repetition -and $_.Repetition.Interval
        }).Count -ge 1
    }
    Check 'the recovery repetition is bounded in time' {
        [bool]($task.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Duration })
    }
    Check 'it runs SpeakLinkHQRuntime.exe directly' {
        ($task.Actions | ForEach-Object { $_.Execute }) -match 'SpeakLinkHQRuntime\.exe'
    }
    Check 'it runs no shell, script or interpreter wrapper' {
        $ran = ($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join ' '
        $ran -notmatch '(?i)(powershell|pwsh|cmd|python|pythonw)\.exe' -and
        $ran -notmatch '(?i)\.(ps1|bat|cmd)\b'
    }
    Check 'it has a working directory' {
        [bool]($task.Actions | Where-Object { $_.WorkingDirectory })
    }
    # Without this, a correctly-configured task that runs a DIFFERENT
    # installation - an older copy still sitting on disk - verifies as PASS.
    Check 'it runs from the expected install root' {
        $executed = ($task.Actions | ForEach-Object { $_.Execute }) -join ' '
        $working = ($task.Actions | ForEach-Object { $_.WorkingDirectory }) -join ' '
        $executed.Trim('"').StartsWith($InstallRoot, 'OrdinalIgnoreCase') -and
        $working.Trim('"').TrimEnd('\') -ieq $InstallRoot.TrimEnd('\')
    }
    Check 'it runs as the interactive user, not SYSTEM' {
        $task.Principal.UserId -notmatch '(?i)(SYSTEM|LOCALSERVICE|NETWORKSERVICE)'
    }
    Check 'it stores no Windows password' { $task.Principal.LogonType -eq 'Interactive' }
    Check 'it does not demand administrator rights' { $task.Principal.RunLevel -ne 'Highest' }
    Check 'it starts when available after a missed run' {
        $task.Settings.StartWhenAvailable -eq $true
    }
    Check 'a second launch is ignored rather than started' {
        $task.Settings.MultipleInstances -eq 'IgnoreNew'
    }
    Check 'it has no execution time limit' {
        $task.Settings.ExecutionTimeLimit -in @('PT0S', $null, '')
    }
    Check 'it restarts on failure, a bounded number of times' {
        $task.Settings.RestartCount -gt 0 -and $task.Settings.RestartCount -le 10
    }
    Check 'the task definition carries nothing secret' {
        $xml = Export-ScheduledTask -TaskName $TaskName
        $xml -notmatch 'speaklink_rcv_v1' -and $xml -notmatch 'ECHO(-[A-Z0-9]{4}){2,}' -and
        $xml -notmatch '(?i)(password|bearer)' -and $xml -notmatch 'eyJ[A-Za-z0-9_-]{10,}'
    }
}

# ---- the persistent profile it depends on -----------------------------------
$persistentDatabase = Join-Path $PersistentRoot 'data\speaklink.db'
Check 'the persistent database is present' { Test-Path $persistentDatabase }

# A database file that EXISTS is not a database that reads. SQLite opens a
# truncated file without complaint and fails on the first real query an hour
# later - the same "it is there, so it works" claim this project keeps finding
# in other clothes. Opened mode=ro so this verifier cannot write to HQ's data,
# and an interpreter that cannot be run reports UNKNOWN rather than FAIL.
Check 'the persistent database passes an integrity check' {
    if (-not (Test-Path $persistentDatabase)) { throw 'no database to check' }
    $uri = 'file:///' + ($persistentDatabase -replace '\\', '/') + '?mode=ro'
    $probe = & $Python -c "import sqlite3,sys;c=sqlite3.connect(sys.argv[1],uri=True);print(c.execute('PRAGMA integrity_check').fetchone()[0])" $uri 2>&1
    if ($LASTEXITCODE -ne 0) { throw 'the integrity check could not be run' }
    ($probe -join '').Trim() -eq 'ok'
}
Check 'the keys folder exists' { Test-Path (Join-Path $PersistentRoot 'keys') }

# "Has HQ ever actually started here?" - and the honest answer is not "is there
# a status file". A status file recording CONFIG_ERROR exists exactly when the
# runtime REFUSED, which is precisely when the two files below legitimately do
# not exist yet. Keying off the file let that through and reported FAIL on a
# correct installation. Existence is not evidence of success.
# CONFIG_ERROR is a refusal. CONFIG_OK is a configuration CHECK, which starts
# nothing - and the first version of this treated a passed check as a start,
# reported FAIL for a key file the backend had never had a chance to create,
# and was the third appearance of the same mistake in one phase.
function Test-HQHasStarted {
    if (-not (Test-Path $StatusFile)) { return $false }
    $recorded = (Get-Content $StatusFile -Raw | ConvertFrom-Json).state
    return $recorded -and $recorded -notin @('CONFIG_ERROR', 'CONFIG_OK')
}
# Both of these are created at the FIRST start - the backend mints the HMAC
# container, the runtime mints the signing secret - so before HQ has ever run
# their absence is normal and reporting FAIL would send an operator looking for
# a backup that was never taken. Unreadable-yet is UNKNOWN, which is what a
# throw here produces.
Check 'the Receiver key container is present' {
    if (-not (Test-HQHasStarted)) { throw 'HQ has not started successfully yet' }
    Test-Path (Join-Path $PersistentRoot 'keys\receiver-hmac-keys.bin')
}
Check 'the signing secret is present' {
    if (-not (Test-HQHasStarted)) { throw 'HQ has not started successfully yet' }
    Test-Path (Join-Path $PersistentRoot 'keys\jwt-secret.txt')
}

# ---- what is actually running -----------------------------------------------
$live = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkHQRuntime.exe'" |
          Where-Object { $_.ExecutablePath -and
                         $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
Check 'at most one HQ runtime is running' { $live.Count -le 1 }

# The status file is the only thing a windowed process can say out loud. It is
# read here rather than inferred from the process existing, because a process
# that exists is not a backend that answers - the same claim that once let a
# silent Receiver look like a working one.
Check 'the runtime recorded what state it is in' {
    if (-not (Test-Path $StatusFile)) { throw 'no status file yet' }
    [bool](Get-Content $StatusFile -Raw | ConvertFrom-Json).state
}
Check 'the recorded state is not a configuration refusal' {
    if (-not (Test-Path $StatusFile)) { throw 'no status file yet' }
    (Get-Content $StatusFile -Raw | ConvertFrom-Json).state -ne 'CONFIG_ERROR'
}
Check 'the status file carries nothing secret' {
    if (-not (Test-Path $StatusFile)) { throw 'no status file yet' }
    $text = Get-Content $StatusFile -Raw
    $text -notmatch 'speaklink_rcv_v1' -and $text -notmatch 'ECHO(-[A-Z0-9]{4}){2,}' -and
    $text -notmatch '(?i)(password|bearer)'
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
Write-Output 'SPEAKLINK_HQ_AUTOSTART_VERIFIED'
Write-Output ''
Write-Output 'Scope: the task is registered correctly and the runtime is installed'
Write-Output 'windowed. It says NOTHING about behaviour after a reboot, after'
Write-Output 'sign-out and sign-in, or on a locked desktop. HQ starts when the HQ'
Write-Output 'user signs in - it does not run at the Windows sign-in screen.'
exit 0
