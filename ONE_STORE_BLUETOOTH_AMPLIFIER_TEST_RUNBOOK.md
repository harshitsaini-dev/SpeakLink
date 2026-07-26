# One-Store Bluetooth Amplifier — Live Test Runbook

How to repeat the live announcement test through a Bluetooth amplifier for one
Store. Result of the first run:
[`ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md`](ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md).

This test plays real sound through real speakers. Do it when the store is empty
or the amplifier is turned down.

## What you need

- The Makook / BARROT Bluetooth USB adapter, plugged in
- The amplifier paired and **connected** in Windows Bluetooth settings
- FFmpeg and ffprobe on `PATH`
- `backend\.venv` created (see `README.md`)
- Yarn 1.22.x

## Rules that are not negotiable

- Never touch `backend\echocast_live.db`. Every step here uses the isolated
  pilot database under `%LOCALAPPDATA%\EchoCast-AI\local-pilot\`.
- Never change the Windows default output device or the system volume.
- Never pair, connect or disconnect a Bluetooth device from a script.
- Never paste a password, token or JWT into a chat, a log or a report.
- Never accept a bare `index:N` selector. Windows renumbers devices whenever one
  is added or removed. Always use `index:N@Exact Device Name`.
- `PLAYBACK_CONFIRMED` means the output device accepted PCM frames. It never
  means sound was audible. `SPEAKER_VERIFIED` does not exist.

---

## 1. Identify the amplifier endpoint — do not guess from names

Names lie. `Headphones ()` looks like a wired jack and is not one; it is a
WDM-KS view of the Bluetooth stack. Identify the endpoint by **difference**.

```powershell
.\scripts\List-EchoCastAudioDevices.ps1
```

Note every device. Then ask the operator to **unplug the Makook adapter**, and
list again. Every endpoint that disappeared belongs to that adapter. Ask them to
plug it back in and reconnect the amplifier from Windows Bluetooth settings.

To see the peer names behind the endpoints:

```powershell
Get-PnpDevice -Class AudioEndpoint | Format-Table -AutoSize Status, FriendlyName
```

Windows names endpoints `Headphones (<peer name> Stereo)`. On the pilot machine
the amplifier's peer is literally called `Bluetooth`, so its endpoint is
`Headphones (Bluetooth Stereo)`.

**Reject**: MME, Windows WASAPI (unless DirectSound fails), `Headphones ()`,
Hands-Free / AG Audio, Microsoft Sound Mapper, Primary Sound Driver,
NVIDIA / LG HDMI, and any TWS earbuds. Prefer **Windows DirectSound, 2 channels,
44100 Hz**.

Record the verified selector, for example:

```
index:4@Headphones (Bluetooth Stereo)
```

## 2. Prove the endpoint makes a sound, before any software

The chime must be run in an **interactive PowerShell window owned by the
operator**, who types `yes` themselves. Do not pipe or automate that answer.

```powershell
.\scripts\Test-EchoCastAudioOutput.ps1 -Seconds 4
```

A Bluetooth A2DP endpoint can take a second or more to wake its DAC, so use a
longer tone than the 1.5 s default. Loudness is fixed and not selectable — the
operator raises the amplifier, not the software.

- **Heard clearly** → record `OPERATOR_CHIME_OBSERVATION = HEARD_CLEARLY` and continue.
- **Exit code 0 but nothing heard** → this is `SOFTWARE_OUTPUT_WITHOUT_AUDIBILITY`.
  Do **not** continue to the browser test. Re-check the amplifier input source,
  its volume, and whether Windows still shows the endpoint as connected.

Never run DirectSound and WASAPI tests at the same time.

## 3. Start the pilot

Set session-only credentials. Never store them, never print them.

```powershell
$env:ADMIN_USERNAME = 'pilot-operator'
$env:ADMIN_PASSWORD = Read-Host 'Temporary pilot password' -AsSecureString | `
    ForEach-Object { [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($_)) }
$env:JWT_SECRET     = [Guid]::NewGuid().ToString('N') + [Guid]::NewGuid().ToString('N')

.\scripts\Start-EchoCastLocalPilot.ps1
```

The username matters. `seed_admin` looks up by username and creates a second row
for a different one — a mismatch here produces a 401 that looks like a broken
password. If login fails, check which username actually exists before resetting
anything.

Verify:

```powershell
Get-NetTCPConnection -State Listen | Where-Object { $_.LocalPort -in 8000,3000 }
```

Expect `127.0.0.1:8000` (loopback only, exactly one worker) and port 3000.

## 4. Start exactly one Receiver in hardware mode

Copy the Store credential from Store Management. It travels in the environment,
never in a command argument or a URL.

```powershell
$env:ECHOCAST_AUDIO_SINK_MODE     = 'windows'
$env:ECHOCAST_AUDIO_OUTPUT_DEVICE = 'index:4@Headphones (Bluetooth Stereo)'   # your verified selector
$env:ECHOCAST_RECEIVER_TOKEN      = '<paste, never echo>'

.\scripts\Start-EchoCastWindowsAudioReceiverPilot.ps1
```

Confirm it stays up. The Receiver sends a heartbeat every 5 s; without one the
backend closes an idle socket after 30 s.

```powershell
Start-Sleep -Seconds 45
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match 'audio_receiver_pilot' } |
    Select-Object ProcessId
```

Then confirm Store UN reads `online` in the Receiver Status page. If the UI says
OFFLINE, believe the UI and investigate — it was right last time.

## 5. Drive the browser, one action at a time

Open `http://localhost:3000` and sign in.

1. **Receiver Status** — confirm your Store shows ONLINE and no other Store does.
2. **Broadcast Console** — confirm **Target Mode = Selected Stores**.
3. Check **Broadcast Targets**. If `Selected` is anything but `0`, click
   **Clear** first. A stale selection of all 44 Stores is easy to miss.
4. Type the Store's full name into the **Search box** — the full name, not the
   code. Searching `UN` matches ten Stores; `Uttam Nagar Old` matches one.
   `Uttam Nagar ASR` is a different Store with a deliberately similar name.
5. Tick that one row's checkbox. Expect `Selected 1 / Online 1 / Offline 0`.
6. Enter a campaign name. The Start button stays disabled until both a name and
   a Store exist — that is the safety lock, so do this last.
7. Click **Start Live Broadcast**. This only opens a confirmation dialog.
8. Read the dialog. It shows Campaign, Target Mode and `1 (1 online, 0 offline)`
   — the last chance to catch a wrong configuration. Then click **Go Live**.
9. The microphone permission prompt appears **after** a Receiver reports READY.
   Click Allow. Do not use the **Mic Test** button: it opens the microphone
   early and breaks the ordering this test exists to prove.
10. Say one sentence, once.

Live audio is at full scale and will be **much louder than the chime**, which is
deliberately quiet at gain 0.08. Leave the amplifier where it was.

## 6. Watch the real evidence, not the UI alone

The honest per-Store indicator is the **Play Status** column, which shows
`PENDING → AUDIO RECEIVING → PLAYBACK CONFIRMED`. The summary card counts
Receiving / Confirmed / Errors from actual acknowledgements.

To watch the backend directly, read the isolated pilot database read-only:

```powershell
$db = Join-Path $env:LOCALAPPDATA 'EchoCast-AI\local-pilot\data\echocast_local_pilot.db'
.\backend\.venv\Scripts\python.exe -c @"
import os, sqlite3
con = sqlite3.connect('file:' + os.environ['DB'] + '?mode=ro', uri=True)
con.execute('PRAGMA query_only = ON')
for row in con.execute('SELECT event_type, event_time FROM receiver_events ORDER BY id DESC LIMIT 8'):
    print(row)
"@
```

Required order, each proven separately:

```
CONNECTED  ->  READY  ->  AUDIO_RECEIVING  ->  PLAYBACK_CONFIRMED
```

If the order is violated, stop and investigate. Never infer one from another.

## 7. Ask the operator, and record exactly what they say

> Live phrase amplifier ke connected speakers par kaisi sunai di?

Allowed answers: `Haan, clear` / `Haan, distorted` / `Wrong output par` /
`Bilkul nahi` / `Pata nahi`.

Record it verbatim as `OPERATOR_LIVE_AUDIO_OBSERVATION`. **Never** infer it, and
never convert it into `SPEAKER_VERIFIED`.

## 8. Stop and verify

Click **Stop Broadcast** (not Emergency Stop). Then check:

- Receiver sent `stopped`; session status is `ended`
- queue depth zero and `dropped_chunks: 0`
- FFmpeg exited with return code 0
- the output stream closed and no sound continues

The Receiver self-exits after a session stop and writes its secret-free report:

```
%LOCALAPPDATA%\EchoCast-AI\local-pilot\logs\windows-audio-receiver-report.json
```

That file carries the real evidence — chunks, bytes, decoded duration, PCM
frames written, dropped chunks and the device actually used. **Do not
force-kill the Receiver mid-session**: Windows has no SIGTERM, so a kill loses
the report.

Then stop everything:

```powershell
.\scripts\Stop-EchoCastWindowsAudioReceiverPilot.ps1
.\scripts\Stop-EchoCastLocalPilot.ps1
```

Both stop the **whole owned process tree**, verify every PID is gone, verify
ports 8000 and 3000 are released, and exit non-zero if anything survived. Do not
trust a "stopped" message alone — confirm:

```powershell
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 8000,3000 }
```

Empty output is the pass.

## 9. Confirm the protected database is untouched

```powershell
Get-Item backend\echocast_live.db | Select-Object Length, LastWriteTimeUtc
Test-Path backend\echocast_live.db-wal
Test-Path backend\echocast_live.db-shm
```

Length and timestamp must be unchanged; both sidecars must be `False`.

---

## Choosing the outcome

Exactly one, based only on evidence:

| Outcome | When |
| --- | --- |
| `BLUETOOTH_AMPLIFIER_LIVE_TEST_PASSED` | CONNECTED, READY, AUDIO_RECEIVING and PLAYBACK_CONFIRMED all observed; operator heard the phrase clearly; clean STOPPED; queue zero; FFmpeg exited; stream closed |
| `BLUETOOTH_AMPLIFIER_SOFTWARE_ONLY` | every software state passed but the operator did **not** hear a clear phrase |
| `BLUETOOTH_AMPLIFIER_LIVE_TEST_BLOCKED` | prepare, codec, device open, audio, playback or cleanup failed |

Never convert B or C into A. Always record:

```
OPERATOR_CHIME_OBSERVATION      = <what the operator said>
OPERATOR_LIVE_AUDIO_OBSERVATION = <what the operator said>
SPEAKER_VERIFIED                = NOT_IMPLEMENTED
NOT_READY_FOR_PRODUCTION
```

## Related regression coverage

Run these before and after any change to the audio path:

```powershell
.\backend\.venv\Scripts\python.exe -m pytest backend/tests -q
.\backend\.venv\Scripts\python.exe -m compileall -q backend tools
.\scripts\Run-EchoCastAudioSmoke.ps1 -PilotRoot <a scratch directory>
cd frontend; yarn e2e; yarn build
```

Pass `-PilotRoot` to the smoke run. Without it, the smoke re-seeds the shared
pilot database and re-aligns the operator's password hash, which breaks the
login you are about to use.

Multi-Store fan-out, with synthetic Receivers only:

```powershell
.\backend\.venv\Scripts\python.exe tools\load_test_receivers.py `
    --stores 5 --stores 10 --stores 20 --stores 40 --pilot-root <a scratch directory>
```
