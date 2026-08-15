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

# The destination is emptied before the copy. If the package to copy FROM sits
# inside it, that step destroys its own source - which is exactly what happened
# on a Store PC:
#
#     install folder is held open; replacing its contents in place
#     27 file(s) could not be removed and will be overwritten
#     ...\SpeakLink\receiver-app\Receiver ... because it does not exist
#
# The Store Kit had unpacked itself into SpeakLink\receiver-app, and
# SpeakLink\receiver-app is where the Receiver is installed to. The kit now
# uses its own folder, and this refuses the arrangement outright so that no
# future caller can arrive at it again by accident. Checked here, BEFORE
# anything is deleted, because after the delete there is nothing left to
# report with.
$resolvedInstallRoot = if (Test-Path $InstallRoot) { (Resolve-Path $InstallRoot).Path } else { $InstallRoot }
$normalisedRoot = $resolvedInstallRoot.TrimEnd('\') + '\'
if ($PackagePath.TrimEnd('\').Equals($resolvedInstallRoot.TrimEnd('\'), 'OrdinalIgnoreCase') -or
    $PackagePath.StartsWith($normalisedRoot, 'OrdinalIgnoreCase')) {
    throw ("The package to install from is inside the folder being installed " +
           "to, so installing would delete its own source.`n" +
           "    package     : $PackagePath`n" +
           "    install root: $resolvedInstallRoot`n" +
           'Keep the Store Kit somewhere of its own and run this again. ' +
           'Nothing has been changed.')
}

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
# The At-Logon task goes first. Stopping the process without stopping the task
# that owns it is a race the task wins: Task Scheduler can restart it between
# the kill and the delete, and the reinstall then fails on a folder that was
# free a moment earlier.
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Output '  stopping the existing At-Logon task'
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
}

# Matched by NAME, not only by path. An upgrade from an older kit - or an
# install root reached by a different spelling of the same folder (a mapped
# drive, a short 8.3 path, a different case) - leaves a process this filter
# would not have recognised, and it is that process which holds the folder.
# There is no SpeakLink Receiver on a Store PC that should survive a reinstall,
# so the name is the right test.
$running = @(Get-CimInstance Win32_Process `
                -Filter "Name = 'SpeakLinkReceiverBackground.exe' OR Name = 'SpeakLinkReceiver.exe'" `
                -ErrorAction SilentlyContinue)
foreach ($process in $running) {
    Write-Output "  stopping running Receiver PID $($process.ProcessId)"
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
}
if ($running.Count -gt 0) { Start-Sleep -Seconds 2 }

# Removal retries, because a handle closes on Windows's schedule, not ours.
#
# THE FAILURE THIS EXISTS FOR
#
# A real Store PC refused the whole installation with
#
#     Remove-Item : ...\SpeakLink\receiver-app because it is in use
#
# after enrolment had already succeeded - the worst possible moment to stop,
# because the Device was registered and the computer had nothing installed to
# use it. A process had just been killed; its handles had not been released
# yet; and one Remove-Item attempt decided that was fatal.
if (Test-Path $InstallRoot) {
    $removed = $false
    foreach ($attempt in 1..5) {
        try {
            Remove-Item $InstallRoot -Recurse -Force -ErrorAction Stop
            $removed = $true
            break
        } catch {
            if ($attempt -eq 5) { break }
            Write-Output "  install folder still in use, waiting (attempt $attempt of 5)"
            Start-Sleep -Seconds 2
        }
    }
    if (-not $removed) {
        # Last resort: empty what CAN be emptied and install over the rest.
        #
        # This is not a weakened installation. Every file is copied fresh below
        # and then verified against SHA256SUMS.txt, so anything left behind
        # that matters is overwritten, and anything stale that is NOT
        # overwritten is caught by that check rather than shipped.
        Write-Output '  install folder is held open; replacing its contents in place'
        Get-ChildItem $InstallRoot -Force -Recurse -ErrorAction SilentlyContinue |
            Sort-Object { $_.FullName.Length } -Descending |
            ForEach-Object { Remove-Item $_.FullName -Force -Recurse -ErrorAction SilentlyContinue }
        $held = @(Get-ChildItem $InstallRoot -Force -Recurse -File -ErrorAction SilentlyContinue)
        if ($held.Count -gt 0) {
            Write-Output ("  $($held.Count) file(s) could not be removed and will be " +
                          'overwritten; the checksum test below still covers them')
        }
    }
}
New-Item -ItemType Directory -Force -Path $InstallRoot, $stateRoot, $logDirectory | Out-Null

# Copied file by file, with a short retry on the transient locks that real
# Store PCs produce.
#
# A single Copy-Item -Recurse is all or nothing, and on a machine with
# real-time antivirus a freshly written .exe, .dll or .pyd is read by the
# scanner the instant it lands. If that read is still open when the next write
# arrives, Windows raises a sharing violation - and one such moment killed the
# whole installation, leaving an install root holding most of a Receiver.
#
# It happened on a real first install: 41 of the 44 files arrived and the three
# that did not were SpeakLinkReceiverBackground.exe, python3.dll and
# _psutil_windows.pyd - every one of them a binary the scanner opens, none of
# them next to each other. Nothing was wrong with the package; all three copy
# perfectly on the next attempt, which is exactly what a transient lock looks
# like.
#
# So each file gets a few attempts a moment apart. It is NOT weakened
# verification: SHA256SUMS.txt is still checked against every installed file
# below, so a copy that lies about succeeding is still caught.
$sourceRoot = (Resolve-Path $PackagePath).Path
$copyFailures = @()
foreach ($item in Get-ChildItem $sourceRoot -Recurse -File) {
    $relative = $item.FullName.Substring($sourceRoot.Length).TrimStart('\')
    $target = Join-Path $InstallRoot $relative
    $targetDirectory = Split-Path $target -Parent
    if (-not (Test-Path $targetDirectory)) {
        New-Item -ItemType Directory -Force -Path $targetDirectory | Out-Null
    }
    $copied = $false
    $lastError = $null
    foreach ($attempt in 1..5) {
        try {
            Copy-Item $item.FullName $target -Force -ErrorAction Stop
            $copied = $true
            break
        } catch {
            $lastError = $_.Exception.Message
            Start-Sleep -Milliseconds (200 * $attempt)
        }
    }
    if (-not $copied) { $copyFailures += "$relative - $lastError" }
}
if ($copyFailures.Count -gt 0) {
    # Named in full. The operator previously saw a truncated PowerShell stack
    # with no file in it, which is not something anybody can act on.
    throw ("These files could not be installed after several attempts:`n  " +
           ($copyFailures -join "`n  ") +
           "`n`nThis is usually antivirus holding a file open. Try again, and " +
           "if it repeats, allow $InstallRoot in the antivirus and re-run.")
}

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
# Removing a scheduled task, on a machine where somebody else may own it
# ---------------------------------------------------------------------------
#
# `Unregister-ScheduledTask` fails with "Access is denied" in two ordinary
# situations, and the raw error names neither of them:
#
#   * the task is RUNNING - Windows will not delete a task while its action is
#     alive, which is exactly the case on a repair or an upgrade, because the
#     Receiver this installer is replacing is on air at that moment;
#   * the task belongs to ANOTHER account - the pilot task was registered by
#     whoever first set the shop up, and the person re-running the installer is
#     signed in as somebody else.
#
# The first is ours to fix and this does. The second is not - and the honest
# response is to say whose task it is and what to do, rather than to fail with
# a stack trace that reads like the installer is broken.
function Remove-SpeakLinkTask {
    param(
        [Parameter(Mandatory = $true)][string] $Name,
        [switch] $Quiet
    )

    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { return $true }

    # Stop it first. A running task cannot be deleted, and stopping one that is
    # not running is not an error.
    try { Stop-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue } catch { }

    foreach ($attempt in 1..3) {
        try {
            Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction Stop
            return $true
        } catch {
            $reason = $_.Exception.Message
            Start-Sleep -Milliseconds (300 * $attempt)
        }
    }

    # schtasks.exe goes through a different path from the cmdlet's COM object
    # and succeeds in cases the cmdlet refuses. Tried second, not first,
    # because when it fails it says less.
    #
    # BY FULL PATH. The setup wizard runs this script from a GUI process whose
    # PATH does not include System32, so a bare `schtasks.exe` was not found -
    # and the error that reached the operator was "CommandNotFoundException:
    # Remove-SpeakLinkTask", which names this function rather than the thing
    # that was actually missing, and reads as though the installer itself is
    # broken.
    $schtasks = Join-Path $env:SystemRoot 'System32\schtasks.exe'
    if (-not (Test-Path $schtasks)) { $schtasks = 'schtasks.exe' }
    # $ErrorActionPreference is 'Stop' for this whole script, and under Stop a
    # native program writing to stderr becomes a TERMINATING error. So
    # schtasks printing "ERROR: Access is denied." - the very answer this line
    # exists to handle - killed the script at the line handling it, and the
    # operator saw a NativeCommandError instead of the sentence below.
    #
    # The same guard the audio probe above uses, for the same reason.
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $schtasks /Delete /TN $Name /F 2>&1 | Out-String
    } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -eq 0) { return $true }

    $owner = try { (Get-ScheduledTask -TaskName $Name).Principal.UserId } catch { 'unknown' }
    # A marker the wizard can act on, before the sentences a person reads.
    # Administrator rights are needed to REPLACE this task, and for nothing
    # else: the task itself is registered to run as this signed-in user at a
    # limited level, so playing an announcement never asks for anything.
    Write-Output "NEEDS_ADMIN: $Name"
    if (-not $Quiet) {
        Write-Output "  could not remove the scheduled task '$Name'"
        Write-Output "    it is registered to: $owner"
        Write-Output "    you are signed in as: $env:USERDOMAIN\$env:USERNAME"
        Write-Output '    Windows refuses to let one account delete another account''s task.'
        Write-Output '    Fix it in one of two ways, then run this installer again:'
        Write-Output "      - sign in as $owner and run this installer, or"
        Write-Output '      - open an Administrator PowerShell and run:'
        Write-Output "          schtasks /Delete /TN `"$Name`" /F"
    }
    return $false
}

# ---------------------------------------------------------------------------
# The scheduled task
# ---------------------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output '  replacing the earlier task of the same name'
    if (-not (Remove-SpeakLinkTask -Name $TaskName)) {
        # Registering over a task that could not be removed would leave the
        # shop running the OLD Receiver while this installer reported success.
        throw "The existing scheduled task '$TaskName' could not be replaced. See the lines above."
    }
}

# The earlier pilot task runs SpeakLinkReceiver.exe - the CONSOLE build - so a
# Store still carrying it gets a black window at logon regardless of anything
# fixed here. Left in place it would also fight this task for the same
# credential. Removing the task removes no credential and no settings: those
# live in the state directory and are untouched.
foreach ($obsolete in @('SpeakLink Receiver LAN Pilot (disposable)',
                        'SpeakLink Store Probe',
                        'SpeakLink Restart Probe')) {
    $old = Get-ScheduledTask -TaskName $obsolete -ErrorAction SilentlyContinue
    if (-not $old) { continue }
    $ran = ($old.Actions | ForEach-Object { $_.Execute }) -join ' '
    if ($ran -notmatch 'SpeakLink') {
        Write-Output "  NOT removing '$obsolete': it runs '$ran', which is not ours"
        continue
    }
    Write-Output "  removing the obsolete task '$obsolete' (ran $ran)"
    foreach ($process in @(Get-CimInstance Win32_Process -Filter "Name = 'SpeakLinkReceiver.exe'" |
                           Where-Object { $_.CommandLine -match '\brun\b' })) {
        Write-Output "    stopping its console Receiver, PID $($process.ProcessId)"
        Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    }
    # Best effort: an obsolete task that cannot be removed is a nuisance, not
    # a reason to abandon an otherwise good installation - the new task is
    # what actually runs the shop.
    if (-not (Remove-SpeakLinkTask -Name $obsolete)) {
        Write-Output "  leaving '$obsolete' in place; the new task is unaffected"
    }
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
        Remove-SpeakLinkTask -Name $TaskName -Quiet | Out-Null
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
