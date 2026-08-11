# SpeakLink One-Store Live-Audio Software Test Runbook

Status: **local software test only**. This runbook does not authorize a Store
deployment, and it never asks you to switch on a Store amplifier.

Everything runs on your own machine, on loopback, against the disposable pilot
database. The protected application database `backend\speaklink_live.db` is
never used, opened, copied or modified.

## What the synthetic test proves

- Valid WebM/Opus data traverses the whole software pipeline.
- One Receiver authenticates and reaches CONNECTED.
- The Receiver reports READY only after real FFmpeg and Opus/WebM checks.
- The Receiver receives real audio bytes and reports AUDIO_RECEIVING.
- FFmpeg genuinely decodes the audio (its own progress counter advances).
- The Receiver reports PLAYBACK_CONFIRMED from that evidence.
- STOP, queue cleanup and process cleanup all work.

## What the microphone test additionally proves

- Browser microphone permission works.
- Browser MediaRecorder produces WebM/Opus.
- Real microphone chunks flow at roughly 250 ms.
- The real-time start/stop lifecycle works.

## What **neither** test proves

- The correct Windows output device is selected
- Amplifier behaviour
- Bluetooth connection
- **Audible Store speakers**
- LinkGuard pause/resume
- `SPEAKER_VERIFIED`
- Production deployment readiness

The Receiver decodes to a **null sink** on purpose, so nothing can be played
through the wrong Windows device. FFmpeg succeeding means the bytes were valid
and decodable. It says nothing about sound leaving a speaker.

## Audio format used

| Property | Value |
| --- | --- |
| Container | WebM |
| Codec | Opus |
| Channels | mono (1) |
| Target bitrate | ~32 kbps |
| Chunk duration | ~250 ms |
| Browser MIME | `audio/webm;codecs=opus` |

If the browser cannot produce WebM/Opus, the dashboard shows an honest codec
error and sends nothing. It never silently substitutes another format.

## Bounded queue

Each targeted Store gets its own bounded queue of **24 chunks** (about six
seconds at 250 ms). On overflow the **oldest** chunk is dropped so live audio
stays current, and a per-Store `dropped` counter records it. One slow Store can
never block another Store or the broadcaster read loop, and no audio chunk is
ever written to a database or a log.

## Prerequisites

1. Python virtual environment at `backend\.venv`.
2. FFmpeg **and** ffprobe on `PATH`, with Opus and WebM support.
3. Node.js and Yarn 1.22.x for the browser test.
4. No SpeakLink process already running:
   ```powershell
   Get-CimInstance Win32_Process |
       Where-Object { $_.Name -in 'python.exe','node.exe','ffmpeg.exe' -and $_.CommandLine -match 'uvicorn|craco|audio_receiver' } |
       Select-Object ProcessId, Name
   ```
5. Record the protected database and its sidecars (never open them):
   ```powershell
   foreach ($f in 'backend\speaklink_live.db','backend\speaklink_live.db-wal','backend\speaklink_live.db-shm') {
       if (Test-Path $f) { Get-Item $f | Select-Object Name, Length, LastWriteTimeUtc } else { "$f : absent" }
   }
   ```

## Part A - Automated synthetic audio test

### A1. Set pilot-only credentials

Process-scoped: they live only in this window and are never written to Git, a
repository `.env`, a report or a log.

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink\HQ-Broadcast-Full (1)'
$env:ADMIN_USERNAME = 'pilot-operator'
$env:ADMIN_PASSWORD = 'choose-a-temporary-pilot-only-value'
$env:JWT_SECRET     = 'choose-another-temporary-pilot-only-value'
```

### A2. Run the automated smoke

```powershell
.\scripts\Run-SpeakLinkAudioSmoke.ps1
```

Or directly:

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink\HQ-Broadcast-Full (1)\backend'
& .\.venv\Scripts\python.exe ..\tools\local_audio_pilot.py prepare
& .\.venv\Scripts\python.exe ..\tools\local_audio_pilot.py smoke
```

Expected (values will differ):

```text
One-Store audio pilot: ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED
  backend_host: 127.0.0.1
  ffmpeg_decoded_microseconds: 4000000
  ffmpeg_returncode: 0
  observed_connected: True
  observed_ready: True
  observed_audio_receiving: True
  observed_playback_confirmed: True
  observed_stopped: True
  receiver_dropped_chunks: 0
  receiver_total_chunks: 17
  selected_store_code: UN
  sink_mode: null
  speaker_verified: False
  uvicorn_workers: 1
```

The smoke test starts and stops its own backend and Receiver on a random free
loopback port. Nothing is left running.

### Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Passed |
| `1` | Input, safety or startup failure |
| `2` | Audio assertion failed |
| `3` | Cleanup or shutdown failure |

## Part B - Manual browser microphone test

Only do this after Part A passes.

### B1. Start the pilot backend and frontend

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink\HQ-Broadcast-Full (1)'
.\scripts\Start-SpeakLinkLocalPilot.ps1
```

### B2. Start one audio Receiver

Open **Store Management** in the browser, copy the receiver credential for the
Store you will test, then in a **second** PowerShell window:

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink\HQ-Broadcast-Full (1)'
$env:SPEAKLINK_RECEIVER_TOKEN = '<paste-the-store-credential>'
.\scripts\Start-SpeakLinkAudioReceiverPilot.ps1
```

### B3. Browser checklist

1. Open `http://localhost:3000` and log in with the pilot-only credentials.
2. Open **Receiver Status**. Your Store should show **online** (CONNECTED).
3. Open **Broadcast Console**.
4. Target Mode: **Selected Stores**. Select **exactly one** Store - the same
   Store whose Receiver you started.
5. Enter a campaign name.
6. Click **Start Live Broadcast** and confirm.
7. Watch the broadcaster status. It shows *waiting for receiver readiness*
   first. The microphone is **not** opened until the Receiver acknowledges
   READY.
8. Grant microphone permission when the browser asks.
9. Speak a short test phrase for a few seconds.
10. In the Receiver window, watch the state line advance:
    `CONNECTED -> READY -> AUDIO_RECEIVING -> PLAYBACK_CONFIRMED`.
11. Confirm the Receiver never prints `SPEAKER_VERIFIED`. It cannot.
12. Click **Stop Broadcast**.
13. Confirm the Receiver reports `STOPPED` and exits cleanly.

### B4. Stop everything

```powershell
.\scripts\Stop-SpeakLinkAudioReceiverPilot.ps1
.\scripts\Stop-SpeakLinkLocalPilot.ps1
```

### B5. Confirm cleanup

```powershell
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -in 'python.exe','node.exe','ffmpeg.exe' -and $_.CommandLine -match 'uvicorn|craco|audio_receiver' } |
    Select-Object ProcessId, Name
```

Then re-check the protected database and sidecars from the Prerequisites step.
They must be unchanged.

## Common errors

| Message / symptom | What it means |
| --- | --- |
| `ffmpeg was not found on PATH` | Install FFmpeg with Opus/WebM support. |
| `ADMIN_PASSWORD is not set` | Step A1 was skipped, or this is a new window. |
| `SPEAKLINK_RECEIVER_TOKEN is not set` | The Receiver needs the Store credential in its environment. |
| `No Receiver reported READY` | The Receiver is not running, or FFmpeg is missing on it. No audio was sent. |
| `This browser cannot record WebM/Opus audio` | Use a current Chrome or Edge. Nothing is sent in another format. |
| `the Receiver did not report both AUDIO_RECEIVING and PLAYBACK_CONFIRMED` | Audio did not reach the Receiver, or FFmpeg could not decode it. |
| `POST .../stop returned HTTP 400` | The session already ended, usually because the broadcaster socket closed first. |
| `Port 8000 is already in use` | Stop the other process or pass `-BackendPort`. |
| `the protected SpeakLink database was refused` | A path resolved to the real database. The safety net worked. |
| `a WAL file sits beside the snapshot` | Stop the backend, then re-run prepare. |

## Files produced (all outside Git)

```text
%LOCALAPPDATA%\SpeakLink\local-pilot\
    audio\pilot-tone.webm              deterministic synthetic fixture
    data\speaklink_local_pilot.db       disposable pilot database
    logs\audio-backend.log             pilot backend log
    logs\audio-receiver.log            Receiver log
    logs\audio-receiver-report.json    secret-free Receiver report
    logs\audio-smoke-report.json       secret-free smoke result
    runtime\audio-receiver.pid         present only while running
```

Nothing above is committed. To reset only the pilot database:

```powershell
Set-Location 'C:\Users\admin\Desktop\SpeakLink\HQ-Broadcast-Full (1)\backend'
& .\.venv\Scripts\python.exe ..\tools\local_pilot.py reset --reset-pilot-db
```

The audio fixture can simply be deleted from the `audio\` folder; it is
regenerated deterministically on the next run.

Never delete the repository, and never delete `backend\speaklink_live.db` or its
`-wal`/`-shm` files.

## Honest readiness after this test

- ✅ `READY_FOR_ONE_STORE_LIVE_AUDIO_SOFTWARE_TEST`
- ❌ `NOT_READY_FOR_SPEAKER_TEST`
- ❌ `NOT_READY_FOR_PRODUCTION`

`SPEAKER_VERIFIED` requires LinkGuard acoustic detection, which is not part of
this milestone and is never reported by this Receiver.
