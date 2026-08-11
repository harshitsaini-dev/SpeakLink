# One-Store Bluetooth Amplifier — Live Hardware Validation Result

**Outcome: `BLUETOOTH_AMPLIFIER_LIVE_TEST_PASSED`**

```
OPERATOR_CHIME_OBSERVATION      = HEARD_CLEARLY
OPERATOR_LIVE_AUDIO_OBSERVATION = Haan, clear   (heard clearly)
SPEAKER_VERIFIED                = NOT_IMPLEMENTED
NOT_READY_FOR_PRODUCTION
```

A live announcement spoken into the HQ browser microphone was heard clearly on
the speakers connected to the Bluetooth amplifier. That is an **operator audible
confirmation**, recorded because a person listened and said so. It is not
`SPEAKER_VERIFIED`: nothing measured the sound, and LinkGuard does not exist.

This covers **one Store**. It is not a 44-Store rollout, and it is not
production readiness — see [Production blockers](#production-blockers).

## When and where

| Field | Value |
| --- | --- |
| Date (UTC) | 2026-07-26 |
| Branch | `test/one-store-bluetooth-amplifier-live` |
| Base commit | `e9aed54` |
| Head commit | `26615a7` |
| Machine | Local development / pilot PC (Windows 10 Pro 19045) |
| Store | `UN` — Uttam Nagar Old (store_id 1, UN ZONE) |

## The audio path that was actually tested

```
HQ browser microphone (Chrome, MediaRecorder)
  -> React Broadcast Console
  -> FastAPI broadcaster WebSocket        ws://127.0.0.1:8000/api/ws/broadcaster
  -> bounded per-Store queue (capacity 24)
  -> one local Receiver                   tools/audio_receiver_pilot.py
  -> FFmpeg WebM/Opus decode
  -> Windows DirectSound output endpoint  index:4@Headphones (Bluetooth Stereo)
  -> BARROT / Makook Bluetooth USB adapter
  -> Bluetooth A2DP link
  -> amplifier Bluetooth input
  -> amplifier
  -> physically connected speakers
  -> operator heard the announcement
```

## Hardware identification

| Thing | What it actually is |
| --- | --- |
| `BARROT Bluetooth Adapter` | the Makook USB Bluetooth dongle |
| `Bluetooth` | the amplifier's own Bluetooth peer name |
| `Headphones (Bluetooth Stereo)` | the Windows playback endpoint for that amplifier |
| `Nirvana X TWS` | **separate earbuds. Never selected. Not part of this path.** |

The amplifier endpoint was identified by **differential enumeration**, not by
guessing from names: snapshot with the dongle connected (12 devices), operator
unplugged it, snapshot again (6 devices). Every Bluetooth endpoint disappeared,
which proved the Makook dongle *is* the PC's entire Bluetooth audio stack.
`Get-PnpDevice` then showed Windows' endpoint naming formula
`Headphones (<peer name> Stereo)` and confirmed two distinct peers.

### Correction to the previous validation document

`ONE_STORE_WINDOWS_OUTPUT_VALIDATION_RESULT.md` describes `Headphones ()`
(Windows WDM-KS) as *"the only candidate wired analog endpoint"* — at
`index:7`, later `index:18`.

**That is wrong.** `Headphones ()` is a **WDM-KS view of the Bluetooth stack**,
not a wired analog output. It vanished together with every other Bluetooth
endpoint when the Makook dongle was removed. Any instruction in that document or
in `ONE_STORE_WINDOWS_OUTPUT_TEST_RUNBOOK.md` to select
`index:18@Headphones ()` as a wired output should not be followed.

### Selected endpoint

```
SELECTOR     NAME                            HOST API              CH    RATE
index:4      Headphones (Bluetooth Stereo)   Windows DirectSound    2   44100
```

Verified selector used: **`index:4@Headphones (Bluetooth Stereo)`**. The bare
`index:N` form is not stable — Windows renumbers every device whenever one is
added or removed. The selector was re-enumerated immediately before the
broadcast and again after; it did not move.

Explicitly **rejected** (present in the device list, never selected): MME
`index:1` (the current system default), Windows WASAPI `index:6`,
`Headphones ()` `index:9` (WDM-KS), NVIDIA / LG HDMI, Microsoft Sound Mapper,
Primary Sound Driver, Hands-Free / AG Audio, and Nirvana X TWS.

The Windows default device was never changed. System volume was never changed.
No device was paired, connected or disconnected automatically.

## Status sequence — measured separately, never inferred

| State | Timestamp (UTC) | What proved it |
| --- | --- | --- |
| `CONNECTED` | `12:56:03.702` | authenticated Receiver WebSocket, `receiver_events` row |
| `READY` | `13:34:27.464` | `receiver_ready` acknowledgement after real FFmpeg, codec, queue and device-open checks |
| `AUDIO_RECEIVING` | `13:34:28.226` | `audio_receiving` acknowledgement — actual non-empty audio bytes arrived |
| `PLAYBACK_CONFIRMED` | `13:34:28.235` | `playback_confirmed` acknowledgement — the selected output stream accepted decoded PCM frames |
| `STOPPED` | `13:37:07.467` | `stopped` acknowledgement, target `play_status=stopped` |

Session `id=8`, campaign `One-Store Bluetooth Amplifier Live Test`,
`target_mode=selected`, `selected_store_count=1`, `online_store_count=1`.
Session went `live -> ended` at `13:37:07.443`. Ordering was never violated.

**No state was inferred from another.** ONLINE is not READY; READY is not
AUDIO_RECEIVING; AUDIO_RECEIVING is not PLAYBACK_CONFIRMED; PLAYBACK_CONFIRMED
is not SPEAKER_VERIFIED. `PLAYBACK_CONFIRMED` means the output device accepted
frames — it does not mean sound was audible.

## Receiver report

Written by `tools/audio_receiver_pilot.py` when its session loop ended.

```json
{
  "sink_mode": "windows",
  "selected_device_id": "index:4",
  "selected_device_name": "Headphones (Bluetooth Stereo)",
  "selected_device_host_api": "Windows DirectSound",
  "sample_rate": 44100,
  "channels": 2,
  "protocol_version": "1.0",
  "ffmpeg_available": true,
  "codec_supported": true,
  "bounded_queue_capacity": 24,
  "dropped_chunks": 0,
  "total_chunks": 533,
  "total_bytes": 606393,
  "ffmpeg_decoded_microseconds": 159420000,
  "ffmpeg_returncode": 0,
  "output_frames_written": 7030422,
  "output_stream_open": "ok",
  "speaker_verified": false,
  "states": ["CONNECTED","READY","AUDIO_RECEIVING","PLAYBACK_CONFIRMED","STOPPED"],
  "overall_result": "AUDIO_RECEIVER_PILOT_PASSED"
}
```

Cross-checks:

```
7 030 422 output frames / 44 100 Hz = 159.42 s   <- exactly ffmpeg_decoded_microseconds
606 393 bytes / 159.42 s            = 30.4 kbps  <- against 32 kbps requested
dropped_chunks 0; the 24-chunk bounded queue never overflowed
```

Every decoded frame reached the output stream. End-to-end, nothing was lost.

This report is also the retroactive proof of which device was really used. It
could not be verified from outside the running process beforehand, and that
limitation was stated at the time rather than glossed over.

## Clean stop

| Check | Result |
| --- | --- |
| Receiver `stopped` acknowledgement | yes, `13:37:07.467` |
| Session status | `ended` |
| Target `play_status` | `stopped` |
| Dropped chunks | 0 |
| FFmpeg exit | return code `0` |
| Output stream | closed (`output_stream_open: ok`, process exited) |
| Receiver process | self-exited cleanly and wrote its report |
| ffmpeg processes after | 0 |

Two things could not be verified from outside the browser and are recorded as
such: the `MediaRecorder.stop()` call and the release of the microphone tracks.
What is certain is that the Receiver disconnected 2 ms after `stopped`, so no
further chunk could have been delivered.

## Protected database

| Point | Length | LastWriteTimeUtc | WAL | SHM |
| --- | ---: | --- | --- | --- |
| Before | 507,904 | 2026-07-26 08:43:13 | absent | absent |
| After | 507,904 | 2026-07-26 08:43:13 | absent | absent |

`backend/speaklink_live.db` was never opened, copied or modified. Every run used
the isolated pilot database under
`%LOCALAPPDATA%\SpeakLink\local-pilot\`, which carries a `PILOT_ONLY` marker
and reconciles `EXACT_CANONICAL_MATCH` against `backend/store_catalog.py`
(44 Stores, 9 Zones).

## Defects found and fixed

Every one was captured as a failing test before it was fixed.

### 1. The amplifier test chime generated mono PCM into a stereo stream

`c77a4fb`. The chime built one sample per frame and wrote it to a 2-channel
stream, so PortAudio read a mono buffer as interleaved stereo: **0.75 s at
880 Hz instead of 1.5 s at 440 Hz**. A Bluetooth DAC can take a second to wake,
so the whole tone was swallowed and the operator heard nothing at all.

Never caught before because the previous hardware pilot was `BLOCKED` and no
sound had ever been played. The Receiver path was never affected — FFmpeg is
given `-ac {channels}`.

Also added a bounded `--seconds` option (max 10 s), because a slow Bluetooth
endpoint needs a longer diagnostic tone. Loudness stayed fixed and
operator-inaccessible.

### 2. PowerShell launchers did not quote paths containing spaces

`bd8e419`. `Start-Process -ArgumentList` joins elements with spaces without
quoting them. This repository lives at `...\HQ-Broadcast-Full (1)`, so Python
received a truncated path:

```
python.exe: can't open file 'C:\Users\admin\Desktop\SpeakLink\HQ-Broadcast-Full'
```

The Receiver died instantly — it never connected, and never reported
`DEVICE_ERROR` either. It simply was not there. Never caught because the
automated smoke launches the Receiver through a Python subprocess list, not
through PowerShell. `backend/tests/test_pilot_scripts.py` is the first test
coverage the `.ps1` scripts have ever had.

### 3. The Receiver sent no heartbeat and was closed after 30 seconds

`3ffd638`. `backend/server.py` waits `HEARTBEAT_INTERVAL_SECONDS` for a message
and closes an idle Receiver socket with code 4408 once its snapshot ages past
`OFFLINE_AFTER_SECONDS`. The pilot only ever reacted to inbound messages, so an
idle Receiver waiting for an operator-driven broadcast was closed exactly on
schedule. Observed: connected `12:47:09.764`, disconnected `12:47:39.790` — a
30.03 s gap.

Never caught because the automated smoke starts a broadcast within a second or
two, and the resulting acknowledgements kept the snapshot fresh.

**The frontend was right.** During this failure the Receiver Status page showed
Store UN as OFFLINE and was accused of a status mismatch. It was correct: the
Receiver really had died. After the fix, the Receiver held its connection for
**2 118 s** before the live test began.

### 4. Stopping the local pilot left the whole process tree running

`4aa9b2a`. `Stop-SpeakLinkLocalPilot.ps1` stopped only the PID recorded at
launch, then printed `Frontend : stopped.` On Windows, `yarn start` is a chain:

```
7688  cmd.exe    /c yarn start          <- the only PID in frontend.pid
+- 11884 node.exe   corepack yarn.js start
   +- 19904 cmd.exe    craco start
      +- 14304 node.exe   craco
         +- 2968  cmd.exe   node ...
            +- 16356 node.exe   <- actually listening on port 3000
```

Six processes survived every "successful" stop, and port 3000 stayed bound. The
same launcher/child shape affects the Receiver and the backend, where a venv
`python.exe` spawns the base interpreter as a child.

The ownership decision now lives in `tools/process_tree.py` so it can be tested
against a fixed process table; the termination stays in PowerShell so the
destructive step is visible in the script the operator reads. Ownership is
proven by command line, never by process name, and the recorded creation time
lets a recycled PID be refused.

Two further defects were found by *running* the fix rather than trusting its
unit tests:

- PowerShell prefixes a UTF-8 BOM when piping to a native command, so
  `json.loads` rejected the process table. Every unit test passed while the live
  pipe failed — during shutdown.
- Progress messages used `Write-Output` inside functions that return a bool.
  PowerShell folds uncaptured output into the return value, so `return $false`
  became a two-element array: non-empty, therefore always truthy. That silently
  defeated the very survivor check being added —
  `Test-SpeakLinkPortReleased` reported success for a port that was still bound.

Verified live: frontend 7 processes and backend 3 processes stopped, both ports
released, PID files removed only after the survivor check passed.

### 5. Console counters used states the backend never writes

`f533c06`. The target summary counted `play_status === "playing"` and
`=== "failed"`. `_persist_receiver_ack` only ever writes `pending`,
`audio_receiving`, `playback_confirmed`, `playback_error`, `device_error` and
`stopped`. Both counters were permanently zero — including during this live
test, where the header read `Currently Playing 0 / 1` while the Receiver was in
`PLAYBACK_CONFIRMED`.

This erred safe (a merely-sent command was never shown as Playing) but left the
operator with no live summary, and used a word for a state that does not exist.
The summary now counts Receiving / Confirmed / Errors from real acknowledgements
and says plainly that Confirmed does not mean audible.

### 6. A reusable JWT travelled in the WebSocket URL and was logged

`f27bd08`. The backend access log contained, in full:

```
INFO: ('127.0.0.1', 56087) - "WebSocket /api/ws/hq?token=<a complete JWT>" [accepted]
INFO: ('127.0.0.1', 57575) - "WebSocket /api/ws/broadcaster?token=<a complete JWT>" [accepted]
```

Both were real, reusable access tokens. A URL is the least private part of a
request: access logs, proxy logs, crash reports and browser history all keep
copies. Anyone able to read one log line could replay the session. The Receiver
socket was already correct — it uses an `Authorization` header.

A browser cannot set a header on a WebSocket handshake, so something must travel
in the URL. It is now worthless rather than reusable: `POST /api/auth/ws-ticket`
(authenticated over normal HTTP, JWT in a header) mints 32 bytes of urandom that
expire in 20 seconds, are redeemed exactly once, carry no claims, and fail
identically whether unknown, spent or expired.

`HQ_WEBSOCKET_TOKEN_EXPOSED_IN_URL_LOG` is closed. The token value was never
reproduced in any report, commit or document.

## Findings recorded but deliberately not changed on this branch

| Finding | Why it was left alone |
| --- | --- |
| `Login.jsx` pre-fills a default username and password and prints both on screen | A real production blocker, but changing the login form during a live-validation branch is the wrong moment. Tracked below. |
| `Receiver.jsx` connects to `/ws/receiver/{token}` — a route the backend does not have, with a Store credential in the URL path | Dead code for a feature not under test. Fixing it means designing browser-Receiver auth properly. |
| Per-Store Play Status disappears after a page refresh mid-broadcast | `playStatus` requires local selection state, which a refresh clears. Real observability gap; needs a small design decision, not a quick patch. |
| `framer-motion@11.18.0` is a dependency but is imported nowhere | Unused. Removing a dependency is not this branch's job. Motion work belongs on `feat/ui-motion-polish`. |
| `.gitignore` content is duplicated three times | Pre-existing. Deduplicating risks silently dropping a rule. |

## Test results

Baseline before this work: **581 passed, 1 skipped, 32 warnings**.

| Suite | Result |
| --- | --- |
| Complete backend suite | **666 passed, 1 skipped, 32 warnings** (+85) |
| Five consecutive backend suites | all five identical, 22.7–23.2 s, no flakiness |
| `compileall backend tools` | exit 0 |
| Playwright Chromium | **32 passed** |
| Frontend production build | compiled successfully |
| Null-sink audio smoke | `ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED`, sent 22 905 B = received 22 905 B, 0 dropped |

New tests added by this work:

| File | Tests | Covers |
| --- | ---: | --- |
| `backend/tests/test_windows_audio_output.py` | +9 | stereo chime, bounded duration, fixed gain |
| `backend/tests/test_pilot_scripts.py` | 20 | `.ps1` quoting, credential safety, stop-script guarantees |
| `backend/tests/test_receiver_heartbeat.py` | 7 | idle liveness without claiming readiness |
| `backend/tests/test_process_tree.py` | 30 | owned-tree discovery, refusals, PID reuse |
| `backend/tests/test_websocket_ticket_auth.py` | 19 | no credential in a WebSocket URL |
| `frontend/e2e/*.spec.js` | 32 | login, Receiver status, READY gating, honest play status |

### Five consecutive backend suites

| Run | Exit | Elapsed | Result |
| ---: | ---: | ---: | --- |
| 1 | 0 | 23.2 s | 666 passed, 1 skipped, 32 warnings |
| 2 | 0 | 23.1 s | 666 passed, 1 skipped, 32 warnings |
| 3 | 0 | 22.7 s | 666 passed, 1 skipped, 32 warnings |
| 4 | 0 | 22.8 s | 666 passed, 1 skipped, 32 warnings |
| 5 | 0 | 22.9 s | 666 passed, 1 skipped, 32 warnings |

## Synthetic load test

`tools/load_test_receivers.py`, N null-sink Receivers against a real loopback
backend, streaming the deterministic WebM/Opus fixture to all of them at once.

| Stores | connect p95 | READY p95 | first chunk p95 | dropped | full delivery | CPU | RSS after |
| ---: | ---: | ---: | ---: | ---: | :---: | ---: | ---: |
| 5 | 26.9 ms | 33.6 ms | 3.4 ms | 0 | 5/5 | 0.02 s | 73.5 MB |
| 10 | 37.5 ms | 27.5 ms | 4.4 ms | 0 | 10/10 | 0.09 s | 75.0 MB |
| 20 | 68.2 ms | 35.2 ms | 6.8 ms | 0 | 20/20 | 0.14 s | 77.7 MB |
| 40 | 125.6 ms | 55.4 ms | 11.4 ms | 0 | 40/40 | 0.23 s | 83.6 MB |

Every Receiver received all 21 chunks at every level. Fan-out throughput reached
1 058 kbps at 40 Stores. Backend memory grew roughly 0.27 MB per Store.

**Every Receiver in this test is synthetic.** None opens an audio device,
decodes with FFmpeg, or reaches a speaker. This covers fan-out, the bounded
per-Store queues and the acknowledgement path. **A one-Store hardware test does
not become a 40-Store rollout because this passed.**

## Production blockers

`NOT_READY_FOR_PRODUCTION` stands. All of the following are still open:

1. `frontend/src/pages/Login.jsx` pre-fills a default username and password into
   the sign-in form and prints both on screen to every visitor.
2. Password hashing, rate limiting and account lockout have not been reviewed.
3. Device enrolment and unique per-Receiver credentials are incomplete.
4. Receiver token hashing and rotation are not implemented.
5. No HTTPS/WSS. Everything here ran on loopback HTTP.
6. CORS is open to `http://localhost:3000` only by pilot configuration, not by policy.
7. No audit logging of broadcast actions beyond `system_logs`.
8. No restart recovery or Windows auto-start for Receivers.
9. LinkGuard pause/resume integration does not exist.
10. **Acoustic speaker verification does not exist.** `SPEAKER_VERIFIED` cannot be produced by any code path in this repository, and no report claims it.
11. The in-browser Receiver page targets a backend route that does not exist.
12. Multi-Store rollout is unproven on real hardware — 1 Store tested, 44 exist.

## What this validation does and does not prove

**Proves**, on one Store, on this machine, on this date:

- HQ browser microphone capture, WebM/Opus encoding at ~30 kbps, 250 ms chunks
- transport to the backend and fan-out into one bounded per-Store queue
- an authenticated Receiver reaching READY only after real FFmpeg, codec, queue and device-open checks
- FFmpeg decoding 159.42 s of audio with return code 0 and no dropped chunks
- 7 030 422 PCM frames accepted by `index:4@Headphones (Bluetooth Stereo)`
- the operator hearing the announcement clearly through the amplifier's speakers
- a clean stop with queue at zero, FFmpeg exited and the output stream closed

**Does not prove:** audio quality or intelligibility under real store noise;
behaviour of any Store other than UN; behaviour of any amplifier other than this
one; Bluetooth stability over hours; recovery from adapter loss mid-broadcast;
that sound reached the speakers of any Store automatically; anything at all
about production readiness.
