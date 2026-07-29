<#
.SYNOPSIS
    Install the SpeakLink Receiver as a background Store runtime.

.DESCRIPTION
    THE ARCHITECTURE, AND WHY IT IS NOT A WINDOWS SERVICE

    The Receiver plays audio through WASAPI. A Windows service runs in session
    0, which has no audio endpoint - a service can start, authenticate, decode
    and write PCM into nothing at all, which is a more convincing kind of
    silence than the one this project already shipped once. So the Receiver
    runs inside the Store user's own interactive session, started at logon by
    Task Scheduler.

    A service-plus-agent split was considered and rejected. It would add a
    second process, a second failure domain and a local IPC channel, and it
    still could not play a sound before somebody logs in - because the audio
    endpoint does not exist until then. It buys nothing for the requirement
    that was actually stated, which is that staff sign in and then the Store
    works without them touching anything.

    What that means honestly: **announcements need the Store user to be signed
    in.** A locked screen with the user still signed in is fine. A machine
    sitting at the login screen is not, and no configuration here changes that.

    WHY THERE ARE TWO EXECUTABLES

    The scheduled task runs SpeakLinkReceiverBackground.exe, which is built
    windowed, so Windows never gives it a console. Task Scheduler's "hidden"
    setting hides the task in its own UI; it does nothing about a console
    application putting a black window on the counter. SpeakLinkReceiver.exe -
    the console one - stays for the commands a person runs and reads.

    WHAT IS PRESERVED

    The DPAPI Device credential is never touched. Installing over an existing
    installation keeps the Store enrolled, so an upgrade is not a re-enrolment.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$PackagePath,
    [Parameter(Mandatory)][string]$BackendUrl,
    [string]$ExpectedHqHost,
    [Parameter(Mandatory)][string]$AudioOutputDevice,
    [ValidateSet('null', 'windows')][string]$AudioSink = 'windows',
    [string]$TaskName = 'SpeakLink Store Receiver',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver-app'),
    # Where the settings, credential and logs live. A parameter so a test
    # installation can be fully isolated instead of writing over the settings a
    # real Store is using.
    [string]$StateRoot = (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver'),
    [int]$RepetitionMinutes = 5,
    [int]$RepetitionDurationDays = 1,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$stateRoot = $StateRoot
$configPath = Join-Path $stateRoot 'config.json'
$credentialPath = Join-Path $stateRoot 'device-credential.bin'
$logDirectory = Join-Path $stateRoot 'logs'

Write-Output '=== installing the SpeakLink Store Receiver ==='

# ---------------------------------------------------------------------------
# Inputs, all checked before anything is written
# ---------------------------------------------------------------------------
if (-not (Test-Path $PackagePath)) { throw "No package at $PackagePath." }
$PackagePath = (Resolve-Path $PackagePath).Path

$consoleExe = Join-Path $PackagePath 'SpeakLinkReceiver.exe'
$backgroundExe = Join-Path $PackagePath 'SpeakLinkReceiverBackground.exe'
foreach ($required in @($consoleExe, $backgroundExe, (Join-Path $PackagePath 'ffmpeg.exe'),
                        (Join-Path $PackagePath 'SHA256SUMS.txt'),
                        (Join-Path $PackagePath 'manifest.json'))) {
    if (-not (Test-Path $required)) {
        throw "The package is incomplete: $(Split-Path -Leaf $required) is missing."
    }
}
if (Test-Path (Join-Path $PackagePath 'STALE-DO-NOT-DEPLOY.txt')) {
    throw 'That package is marked STALE-DO-NOT-DEPLOY. Build a fresh one.'
}

if ($BackendUrl -match '^http://' -and
    $BackendUrl -notmatch '^http://(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)') {
    throw "Refusing plain HTTP to a non-private address: $BackendUrl"
}
if ($BackendUrl -match '(?i)[?&](token|credential|password|secret)=') {
    throw 'The backend URL carries a credential in its query string.'
}
if ($AudioSink -eq 'windows' -and -not $AudioOutputDevice) {
    throw ("-AudioSink windows needs -AudioOutputDevice. Run " +
           "'$consoleExe list-audio-devices' on THIS computer and copy a verified " +
           "'index:N@Name' selector.")
}
if (-not $ExpectedHqHost -and $BackendUrl -match '^http://([^:/]+)') {
    $ExpectedHqHost = $Matches[1]
}

$manifest = Get-Content (Join-Path $PackagePath 'manifest.json') -Raw | ConvertFrom-Json

Write-Output "  package     : $(Split-Path -Leaf $PackagePath) ($($manifest.source_commit_short))"
Write-Output "  install to  : $InstallRoot"
Write-Output "  state       : $stateRoot"
Write-Output "  backend     : $BackendUrl"
Write-Output "  audio       : $AudioSink -> $AudioOutputDevice"
Write-Output "  task        : $TaskName (At-Logon, $env:USERDOMAIN\$env:USERNAME)"
Write-Output "  runs        : SpeakLinkReceiverBackground.exe (windowed - no console window)"
Write-Output "  NOT a service: announcements need this Windows user to be signed in"

# The device selector is validated against the sound cards actually present on
# THIS computer, before anything is registered. A task that starts and then
# refuses is a Store that looks installed and is silent.
if ($AudioSink -eq 'windows' -and -not $DryRun) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $probe = & $consoleExe run --backend-url $BackendUrl --audio-sink windows `
                    --audio-output-device $AudioOutputDevice `
                    --credential-path (Join-Path $env:TEMP 'speaklink-install-probe.bin') `
                    --config-path (Join-Path $env:TEMP 'speaklink-install-probe.json') `
                    @(if ($BackendUrl -match '^http://') { '--allow-insecure-private-lan'; '--expected-hq-host'; $ExpectedHqHost }) 2>&1 | Out-String
    } finally { $ErrorActionPreference = $previous }
    if ($probe -match 'Refused:.*(ambiguous|no output device)') {
        throw ("That audio selector was refused on this computer:`n" +
               ($probe -split "`n" | Where-Object { $_ -match 'Refused:' } | Select-Object -First 1))
    }
    Write-Output '  audio selector resolves on this computer                    PASS'
}

if ($DryRun -or -not $PSCmdlet.ShouldProcess($TaskName, 'Install the Store Receiver')) {
    Write-Output ''
    Write-Output 'Dry run: every input was checked. Nothing was installed.'
    exit 0
}

# ---------------------------------------------------------------------------
# Stop anything running from the install root, then replace it
# ---------------------------------------------------------------------------
$running = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe' OR Name = 'SpeakLinkReceiver.exe'" |
             Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
foreach ($process in $running) {
    Write-Output "  stopping running Receiver PID $($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($running.Count -gt 0) { Start-Sleep -Seconds 2 }

if (Test-Path $InstallRoot) { Remove-Item $InstallRoot -Recurse -Force }
New-Item -ItemType Directory -Force -Path $InstallRoot, $stateRoot, $logDirectory | Out-Null
Copy-Item (Join-Path $PackagePath '*') $InstallRoot -Recurse -Force

# Copying is where files get truncated or silently skipped, and a Receiver that
# is 99% copied fails weeks later with a DLL error nobody connects to install.
$mismatched = @()
foreach ($line in (Get-Content (Join-Path $InstallRoot 'SHA256SUMS.txt'))) {
    if (-not $line.Trim()) { continue }
    $parts = $line -split '  ', 2
    $file = Join-Path $InstallRoot ($parts[1] -replace '/', '\')
    if (-not (Test-Path $file)) { $mismatched += "$($parts[1]) (missing)"; continue }
    if ((Get-FileHash $file -Algorithm SHA256).Hash.ToLower() -ne $parts[0]) { $mismatched += $parts[1] }
}
if ($mismatched.Count -gt 0) {
    throw "The installed copy does not match the package manifest: $($mismatched -join ', ')."
}
Write-Output '  every installed file matches its build hash                 PASS'

# ---------------------------------------------------------------------------
# Settings. Non-secret only - the credential stays sealed in its own file.
# ---------------------------------------------------------------------------
[PSCustomObject]@{
    backend_url = $BackendUrl
    expected_hq_host = $ExpectedHqHost
    allow_insecure_private_lan = [bool]($BackendUrl -match '^http://')
    audio_sink = $AudioSink
    audio_output_device = $AudioOutputDevice
    log_directory = $logDirectory
    installed_version = $manifest.version
    source_commit = $manifest.source_commit
} | ConvertTo-Json -Depth 3 | Set-Content -Encoding utf8 $configPath

foreach ($pattern in @('speaklink_rcv_v1', 'ECHO-[A-Z0-9]{4}-', '(?i)"password"', '(?i)"credential"')) {
    if ((Get-Content $configPath -Raw) -match $pattern) {
        Remove-Item $configPath -Force
        throw "The configuration matched /$pattern/ and was deleted rather than left on disk."
    }
}
Write-Output "  settings saved (no secret)                                  $configPath"

if (Test-Path $credentialPath) {
    Write-Output '  existing Device credential PRESERVED - this Store stays enrolled'
} else {
    Write-Output '  no Device credential yet - enrol before the Receiver can connect:'
    Write-Output "    & `"$InstallRoot\SpeakLinkReceiver.exe`" enrol --backend-url $BackendUrl ``"
    Write-Output "        --allow-insecure-private-lan --expected-hq-host $ExpectedHqHost ``"
    Write-Output '        --device-name "<a name for this computer>"'
}

# ---------------------------------------------------------------------------
# The scheduled task
# ---------------------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output '  replacing the earlier task of the same name'
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# Short and boring on purpose: everything else is in the config file, so the
# task definition carries no device name to go stale and nothing secret. The
# state root is named only when it is not the default, so a normal Store task
# stays a bare "run".
$arguments = 'run'
if ($StateRoot -ne (Join-Path $env:LOCALAPPDATA 'SpeakLink\receiver')) {
    $arguments += " --config-path `"$configPath`" --credential-path `"$credentialPath`""
}

$action = New-ScheduledTaskAction -Execute (Join-Path $InstallRoot 'SpeakLinkReceiverBackground.exe') `
                                  -Argument $arguments -WorkingDirectory $InstallRoot
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$triggers = @($logonTrigger)
if ($RepetitionMinutes -gt 0) {
    # Recovery is NOT RestartCount. Windows applies that when a task fails to
    # START, not when the program it started exits - measured with
    # `cmd /c exit 1` and RestartCount 2, which never re-ran. A repetition
    # schedule plus MultipleInstances=IgnoreNew is what actually brings a dead
    # Receiver back, and it sits on its own time-based trigger because
    # repetition attached to a logon trigger only begins at a logon.
    $repeating = New-ScheduledTaskTrigger -Once -At (Get-Date) `
        -RepetitionInterval (New-TimeSpan -Minutes $RepetitionMinutes) `
        -RepetitionDuration (New-TimeSpan -Days $RepetitionDurationDays)
    $logonTrigger.Repetition = $repeating.Repetition
    $triggers += $repeating
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings `
    -Description 'SpeakLink Store Receiver. Starts at logon, runs with no window.' | Out-Null

$xml = Export-ScheduledTask -TaskName $TaskName
foreach ($pattern in @('speaklink_rcv_v1', 'ECHO(-[A-Z0-9]{4}){2,}', '(?i)password', '(?i)bearer')) {
    if ($xml -match $pattern) {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        throw "The registered task matched /$pattern/ and was removed rather than left in place."
    }
}
Write-Output '  task definition carries no credential, code or password      PASS'

# ---------------------------------------------------------------------------
# Start it now, so the operator does not have to sign out and back in
# ---------------------------------------------------------------------------
if (Test-Path $credentialPath) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 6
    $live = @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiverBackground.exe'" |
              Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($InstallRoot, 'OrdinalIgnoreCase') })
    if ($live.Count -gt 0) {
        Write-Output "  Receiver started, PID $($live[0].ProcessId), no window                 PASS"
    } else {
        Write-Output '  Receiver did not stay running. Read the log:'
        Write-Output "    $logDirectory\receiver.log"
    }
}

Write-Output ''
Write-Output 'SPEAKLINK_STORE_RECEIVER_INSTALLED'
Write-Output "Verify   : .\Test-SpeakLinkStoreReceiver.ps1 -TaskName `"$TaskName`""
Write-Output "Diagnose : & `"$InstallRoot\SpeakLinkReceiver.exe`" diagnose"
Write-Output "Logs     : $logDirectory"
Write-Output ''
Write-Output 'The Store user does not need to open PowerShell again. The Receiver'
Write-Output 'starts at their next sign-in and runs with no window.'
Write-Output 'It does NOT run before anybody signs in, and this is not evidence of'
Write-Output 'boot-time, service or SYSTEM operation.'
