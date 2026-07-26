# One-Store Windows Output — Hardware Validation Result

**Outcome: `HARDWARE_PILOT_BLOCKED`**

**`SPEAKER_VERIFIED` remains `NOT_IMPLEMENTED`.**

No sound was played during this validation. No audio device was ever opened.
No operator audible observation was obtained, so nothing here claims that
anything was heard.

## When and where

| Field | Value |
| --- | --- |
| Date (UTC) | 2026-07-26 |
| Starting branch | `feat/one-store-windows-output-pilot` |
| Starting commit | `6d922b2dbc309bbb899221a0a016df77036cbaec` |
| Validation branch | `test/one-store-windows-output-hardware-validation` |
| Machine | Local development / pilot PC (Windows 10 Pro 19045) |

## Verified environment

| Component | Version |
| --- | --- |
| Python | 3.12.10 |
| FFmpeg | 8.1.2-full_build (gyan.dev) |
| ffprobe | 8.1.2-full_build |
| sounddevice | 0.5.2 |
| PortAudio | V19.7.0-devel |
| CFFI | 2.1.0 |

Baseline before any hardware work: complete backend suite **556 passed,
1 skipped, 32 warnings**; `compileall` OK; frontend `yarn build` compiled and
`yarn test` reported no test files; null-sink smoke returned
`ONE_STORE_AUDIO_SOFTWARE_PILOT_PASSED` with `sink_mode: null` and
`speaker_verified: False`.

## Protected database

| Point | Length | LastWriteTimeUtc | WAL | SHM |
| --- | ---: | --- | --- | --- |
| Before | 507,904 | 2026-07-26 08:43:13 | absent | absent |
| After | 507,904 | 2026-07-26 08:43:13 | absent | absent |

Opened: **no**. Copied: **no**. Modified: **no**.

## Why the pilot is blocked

The operator confirmed:

- **No amplifier is available**, and no cable to an amplifier AUX / LINE IN.
- Only headphones were available.

The task requires the physical path
`Store PC → wired adapter → amplifier AUX → Store speakers`, and
`HARDWARE_PILOT_PASSED` requires the operator to confirm hearing audio
*through that intended amplifier/speaker path*. That path does not exist yet,
so the run cannot pass and was not forced.

The operator did authorise testing over the Bluetooth TWS earbuds instead.
That was recorded as an explicit scope deviation, but it was **not executed**:
the task forbids using a Bluetooth endpoint to finish quickly, TWS earbuds do
not represent the Store amplifier path, and the outcome could not have been a
pass either way. Nothing was played.

## Device enumeration (read-only, nothing opened)

Enumeration was run twice, and the second run is the most important evidence in
this document.

**First enumeration** listed indices `0,1,2,3,4,5,7,8`, with the wired analog
endpoint at `index:7` — `Headphones ()`, Windows WDM-KS, 2 channels, 44100 Hz.

**Second enumeration, minutes later**, listed indices
`2,3,4,5,8,9,10,11,12,13,14,16,18,19`. A Bluetooth TWS earbud set
("Nirvana X TWS") had connected, which:

- renumbered **every** device,
- became the Windows default device, and
- moved the wired analog endpoint from `index:7` to `index:18`.

A resolution attempt against the previously chosen `index:7` correctly **failed
closed** rather than opening whatever now sat nearby.

## Defects found and fixed

Hardware validation found four real defects **before any sound was played**.
All four were fixed test-first, and 16 new automated tests cover them. Every
test uses an injected fake audio backend, so no test opens a real device.

### 1. Output format was hardcoded, not negotiated

`SinkConfiguration` forced 48000 Hz / 1 channel regardless of the device. The
real `index:7` advertised **44100 Hz / 2 channels** under WDM-KS, which is
strict about formats, so the open could have failed or produced wrong audio.

Fixed: the sink now adopts the device's advertised sample rate and channel
count (capped at 2), and the FFmpeg command resamples and re-channels to match.

### 2. The test chime crashed instead of refusing

Running the chime from a non-interactive shell raised a raw `EOFError`
traceback. The confirmation gate correctly prevented playback, but the failure
was not controlled.

Fixed: a non-interactive shell now produces a clear refusal explaining that the
chime must be run in an interactive terminal.

### 3. `index:N` was documented as a "stable selector" — it is not

Windows renumbers every audio device when one is added or removed. A saved
`index:7` could later point at a completely different endpoint, including a
Bluetooth one, which this pilot forbids.

Fixed: a **verified selector** form `index:N@<exact name>` was added. It pins
the index to the exact name that was present when the operator chose it and
fails closed after a renumber, naming what is actually at that index now. The
device table now prints a verified selector for every device and states plainly
that bare indices are not stable.

### 4. Bluetooth detection missed A2DP endpoints

`Headphones (Nirvana X TWS Stereo)` is a Bluetooth A2DP endpoint but was not
flagged, because only the hands-free variants contained an obvious marker. A
pilot that forbids Bluetooth would have shown it as an ordinary candidate.

Fixed: the heuristic now also matches `tws`, `a2dp`, `airpods`, `wireless`,
`earbud` and `handsfree`, and the flag is printed as **`wireless?`** so it
reads as a hint rather than a guarantee. PortAudio does not expose the
transport, so the operator must still confirm the endpoint is the wired one.

## Current device inventory after the fixes

```text
SELECTOR   NAME                                          HOST API              CH    RATE  FLAGS
index:2    Microsoft Sound Mapper - Output               MME                    2   44100
index:3    Headphones (Nirvana X TWS Stere               MME                    2   44100  current-default,wireless?
index:4    Headset (Nirvana X TWS Hands-Fr               MME                    1   44100  wireless?
index:5    LG IPS QHD-1 (NVIDIA High Defin               MME                    2   44100
index:8    Primary Sound Driver                          Windows DirectSound    2   44100
index:9    Headphones (Nirvana X TWS Stereo)             Windows DirectSound    2   44100  wireless?
index:10   Headset (Nirvana X TWS Hands-Free AG Audio)   Windows DirectSound    1   44100  wireless?
index:11   LG IPS QHD-1 (NVIDIA High Definition Audio)   Windows DirectSound    2   44100
index:12   Headset (Nirvana X TWS Hands-Free AG Audio)   Windows WASAPI         1   16000  wireless?
index:13   LG IPS QHD-1 (NVIDIA High Definition Audio)   Windows WASAPI         2   48000
index:14   Headphones (Nirvana X TWS Stereo)             Windows WASAPI         2   44100  wireless?
index:16   Headset (@System32\drivers\bthhfenum.sys...)  Windows WDM-KS         1   16000  wireless?
index:18   Headphones ()                                 Windows WDM-KS         2   44100
index:19   Output (NVIDIA High Definition Audio)         Windows WDM-KS         2   44100
```

The only candidate wired analog endpoint is:

```text
index:18@Headphones ()
```

It is not flagged wireless. Whether anything is physically plugged into that
3.5 mm jack was **not** verified, because no sound was played.

> **CORRECTION (2026-07-26).** The paragraph above is wrong and must not be
> followed. `Headphones ()` is **not a wired analog endpoint**. It is a
> Windows WDM-KS view of the *Bluetooth* stack.
>
> This was proven by differential enumeration during the amplifier validation:
> with the Makook / BARROT USB adapter connected the machine listed 12 output
> devices; with it unplugged, 6. `Headphones ()` disappeared along with every
> other Bluetooth endpoint. The heuristic that misled this document is that the
> name carries no peer, and the `wireless?` flag is a name match only — it
> cannot see an endpoint whose name happens to be empty.
>
> The real amplifier endpoint is
> `index:4@Headphones (Bluetooth Stereo)` on **Windows DirectSound**. See
> [`ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md`](ONE_STORE_BLUETOOTH_AMPLIFIER_VALIDATION_RESULT.md).

## What was and was not observed

| Item | Result |
| --- | --- |
| Device enumeration | Completed twice, read-only, nothing opened |
| Device selection | `index:7` chosen, then correctly refused after renumbering |
| Device opened | **No** |
| Test chime software result | **Not run** (no amplifier path; refused non-interactively as designed) |
| Operator chime observation | **None** |
| Receiver device-open result | **Not attempted** |
| CONNECTED | Not attempted in hardware mode |
| READY | Not attempted in hardware mode |
| AUDIO_RECEIVING | Not attempted in hardware mode |
| PLAYBACK_CONFIRMED | Not attempted in hardware mode |
| STOPPED | Not attempted in hardware mode |
| Operator microphone-audio observation | **None** |
| Windows default device | Never changed |
| System volume | Never changed |
| Bluetooth | Never paired or configured by this task |

The null-sink software path was re-verified and remains green; that is software
evidence only.

## Process and port cleanup

No backend, frontend, Receiver, FFmpeg or ffplay process was left running. No
pilot port remained listening. No output device stream was ever opened, so
none needed releasing.

## Honest readiness

- `HARDWARE_PILOT_BLOCKED` — no wired amplifier path was available.
- `SOFTWARE_OUTPUT_CONFIRMED_BUT_AUDIBILITY_UNPROVEN` still describes the
  software state: the null-sink path passes end to end.
- `SPEAKER_VERIFIED` = **NOT_IMPLEMENTED**.
- `NOT_READY_FOR_PRODUCTION`.

## To complete the hardware pilot later

1. Connect a wired output to the amplifier:
   `PC 3.5 mm or USB adapter → 3.5 mm cable → Amplifier AUX / LINE IN`.
2. Select the amplifier's AUX input manually and set volume **low**.
3. Re-list devices — **indices will have changed again**:
   ```powershell
   .\scripts\List-EchoCastAudioDevices.ps1
   ```
4. Copy the **verified** selector for the wired endpoint, for example
   `index:18@Headphones ()`.
5. Run the chime in an interactive PowerShell window and type `yes`:
   ```powershell
   $env:ECHOCAST_AUDIO_SINK_MODE     = 'windows'
   $env:ECHOCAST_AUDIO_OUTPUT_DEVICE = 'index:18@Headphones ()'
   .\scripts\Test-EchoCastAudioOutput.ps1
   ```
6. Then follow `ONE_STORE_WINDOWS_OUTPUT_TEST_RUNBOOK.md` for the browser
   microphone test and record the operator observation form.

Even when sound is heard, that is **operator observation**, not
`SPEAKER_VERIFIED`.
