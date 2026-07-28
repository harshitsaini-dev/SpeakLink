<#
.SYNOPSIS
    Verify the disposable SpeakLink Receiver logon task, including the claims it
    is easy to make and hard to support.

.DESCRIPTION
    Every check here reads the registered task or exercises the packaged
    executable. None of it infers behaviour from the fact that a script ran.

    A check that cannot be read is reported as UNKNOWN, never as PASS and never
    as FAIL. An earlier firewall checker in this repository asked
    Get-NetFirewallRule without elevation, was told "Access is denied", and
    scored that as "the rule is not installed" - so it reported a missing
    firewall rule that was present the whole time. "I could not read this" and
    "this is wrong" are different answers and must look different.

    What this cannot verify, and does not pretend to:
      * behaviour before logon, as SYSTEM, or on a locked desktop;
      * anything about the second desktop;
      * that a loudspeaker made a sound.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'SpeakLink Receiver LAN Pilot (disposable)',
    [switch]$IncludeLiveInstanceCheck,
    [string]$LiveCredentialPath
)

$ErrorActionPreference = 'Stop'

$script:results = @()
function Check {
    param([string]$Name, [scriptblock]$Body)
    try {
        $verdict = & $Body
    } catch {
        $verdict = 'UNKNOWN'
        $script:lastDetail = $_.Exception.Message
    }
    $label = switch ($verdict) {
        $true   { 'PASS' }
        $false  { 'FAIL' }
        default { [string]$verdict }
    }
    $script:results += [PSCustomObject]@{ Name = $Name; Verdict = $label }
    Write-Output ('  {0,-56}{1}' -f $Name, $label)
}

Write-Output '=== SpeakLink Receiver logon task verification ==='

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    throw "No scheduled task named '$TaskName'. Install one with .\scripts\Install-SpeakLinkReceiverLanPilot.ps1."
}
$xml = Export-ScheduledTask -TaskName $TaskName
$action = $task.Actions | Select-Object -First 1
$exe = $action.Execute
$packagePath = Split-Path -Parent $exe

Write-Output "  task    : $TaskName"
Write-Output "  runs    : $exe"
Write-Output ''

# ---------------------------------------------------------------------------
# The definition
# ---------------------------------------------------------------------------
Check 'the task is registered and enabled' { $task.State -ne 'Disabled' }
Check 'it triggers at logon' { ($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -contains 'MSFT_TaskLogonTrigger' }
Check 'it runs as the interactive user, not SYSTEM' {
    $task.Principal.UserId -notmatch '(?i)(SYSTEM|LOCALSERVICE|NETWORKSERVICE)'
}
Check 'it stores no Windows password' { $task.Principal.LogonType -eq 'Interactive' }
Check 'it does not demand administrator rights' { $task.Principal.RunLevel -ne 'Highest' }
Check 'restart on failure is bounded' {
    $count = $task.Settings.RestartCount
    $count -gt 0 -and $count -le 10
}
Check 'a second launch is ignored rather than started' {
    $task.Settings.MultipleInstances -eq 'IgnoreNew'
}
Check 'it is not killed after a fixed run time' {
    $limit = $task.Settings.ExecutionTimeLimit
    -not $limit -or $limit -eq 'PT0S'
}

# ---------------------------------------------------------------------------
# Nothing secret in the definition
# ---------------------------------------------------------------------------
Check 'no Device credential in the task definition' { $xml -notmatch 'speaklink_rcv_v1\.' }
Check 'no enrolment code in the task definition' { $xml -notmatch 'ECHO(-[A-Z0-9]{4}){2,}' }
Check 'no password, bearer or secret in the task definition' {
    $xml -notmatch '(?i)(password|bearer|secret=)'
}
Check 'no credential in the command line query string' {
    ($action.Arguments) -notmatch '(?i)[?&](token|credential|password|secret)='
}
Check 'the backend address is private' {
    ($action.Arguments) -match '(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)'
}

# ---------------------------------------------------------------------------
# What it points at
# ---------------------------------------------------------------------------
Check 'the executable it names exists' { Test-Path $exe }
Check 'FFmpeg sits beside that executable' { Test-Path (Join-Path $packagePath 'ffmpeg.exe') }
Check 'the package carries a build manifest' { Test-Path (Join-Path $packagePath 'manifest.json') }
Check 'the package is not the one marked stale' {
    $exe -notmatch 'STALE' -and -not (Test-Path (Join-Path $packagePath 'STALE-DO-NOT-DEPLOY.txt'))
}
Check 'a log directory is given on the command line' {
    ($action.Arguments) -match '--log-directory'
}

# ---------------------------------------------------------------------------
# The executable's own behaviour
# ---------------------------------------------------------------------------
Check 'the executable answers --version' {
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { ((& $exe --version) -join ' ') -match '\d+\.\d+\.\d+' }
    finally { $ErrorActionPreference = $previous }
}
Check 'run --help documents the log directory' {
    $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
    try { ((& $exe run --help) -join ' ') -match 'log-directory' }
    finally { $ErrorActionPreference = $previous }
}

# ---------------------------------------------------------------------------
# One instance. Only when asked, because it starts a real Receiver.
# ---------------------------------------------------------------------------
# A real, enrolled credential is required, and this script must not create one.
# The first attempt at this check used a placeholder credential file: the first
# Agent was refused before it had anything to do, released the lock immediately,
# and the second Agent then started normally. Both exited 1 and the check read
# that as "the lock does not work". The lock was fine; the probe never held it.
#
# A single-instance guard can only be observed while an instance is genuinely
# running, so this needs a Receiver that can actually connect. That is the LAN
# pilot check, not this one. Here it reports UNKNOWN unless given a credential.
if ($IncludeLiveInstanceCheck) {
    Check 'a duplicate launch of the packaged EXE exits 4' {
        if (-not $LiveCredentialPath) {
            throw ('no -LiveCredentialPath given. A duplicate can only be observed ' +
                   'while a real Receiver is connected; run the private-LAN check for this.')
        }
        if (-not (Test-Path $LiveCredentialPath)) { throw "no credential at $LiveCredentialPath" }

        $previous = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
        try {
            $sandbox = Join-Path $env:TEMP ("SpeakLink Instance Check " + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

            $first = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden -ArgumentList (
                @('run') + ($action.Arguments -split ' +' | Select-Object -Skip 1) +
                @('--credential-path', $LiveCredentialPath, '--log-directory', $sandbox))
            Start-Sleep -Seconds 6
            if ($first.HasExited) {
                throw "the first Receiver exited (code $($first.ExitCode)) before a duplicate could be tried"
            }
            & $exe run --credential-path $LiveCredentialPath --log-directory $sandbox `
                       @($action.Arguments -split ' +' | Select-Object -Skip 1) | Out-Null
            $duplicate = $LASTEXITCODE
            Stop-Process -Id $first.Id -Force -ErrorAction SilentlyContinue
            if ($duplicate -eq 4) { $true } else { "FAIL (exit $duplicate, expected 4)" }
        } finally { $ErrorActionPreference = $previous }
    }
}

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
$failed = @($script:results | Where-Object { $_.Verdict -eq 'FAIL' -or $_.Verdict -like 'FAIL*' })
$unknown = @($script:results | Where-Object { $_.Verdict -eq 'UNKNOWN' })

Write-Output ''
if ($unknown.Count -gt 0) {
    Write-Output "  $($unknown.Count) check(s) could not be read. That is not a pass and not a failure:"
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
    Write-Output 'Result: INCOMPLETE. No verification token is emitted while a check is unreadable.'
    exit 1
}

Write-Output ''
Write-Output 'SPEAKLINK_RECEIVER_TASK_SCHEDULER_VERIFIED'
Write-Output ''
Write-Output 'Scope of this evidence: an At-Logon task for this Windows user on this'
Write-Output 'computer. It says nothing about behaviour before logon, as SYSTEM, on a'
Write-Output 'locked desktop, on the second desktop, or about any loudspeaker.'

# Explicit: without it PowerShell returns the exit code of whatever native
# command ran last, so a fully passing verification can still report failure.
exit 0
