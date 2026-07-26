# EchoCast One-Store Windows Output-Device Test Runbook

Status: **local hardware pilot for one Store only.** This runbook does not
authorize a Store deployment or a rollout.

## What this test proves

- Windows output devices can be enumerated safely (read-only).
- One device can be selected **explicitly and unambiguously**.
- That exact device can be opened by the Receiver.
- Decoded PCM frames are accepted by that device.
- The Receiver reports READY only after the device actually opens.
- `PLAYBACK_CONFIRMED` reflects frames the device accepted.
- Stop and cleanup release the device and the FFmpeg child.

## What this test does **not** prove

- That any sound was **audible**
- That the amplifier is powered on
- That the correct AUX input is selected
- That the cable is good
- Bluetooth behaviour
- `SPEAKER_VERIFIED`, `AMPLIFIER_VERIFIED`, `ECHO_GUARD_VERIFIED`
- Production readiness or multi-Store readiness

> **Operator hearing sound is NOT `SPEAKER_VERIFIED`.**
> It is useful pilot evidence and nothing more. `SPEAKER_VERIFIED` requires
> EchoGuard acoustic detection, which does not exist yet.

## Wired connection (preferred)

```text
  Store PC
     │  USB
     ▼
  USB audio adapter          (or the PC's 3.5 mm line-out)
     │  3.5 mm cable
     ▼
  Amplifier  AUX / LINE IN   ← select this input manually on the amplifier
     │
     ▼
  Store speakers
```

Bluetooth is **not** used in this milestone. A Bluetooth endpoint may appear in
the device list, but it is flagged and is never selected automatically.

## Safety rules this pilot follows

- The default sink stays `null`. Automated tests never open a real device.
- Hardware mode requires an explicit selector; there is no fallback.
- The Windows **default device is never used and never changed**.
- System volume is never changed.
- No audio driver is installed, and Bluetooth is never paired automatically.

## Step 1 - List output devices (read-only)

```powershell
Set-Location 'C:\Users\admin\Desktop\EchoCast-AI\HQ-Broadcast-Full (1)'
.\scripts\List-EchoCastAudioDevices.ps1
```

Example output from this machine:

```text
SELECTOR     NAME                                           HOST API                CH    RATE  FLAGS
index:1      LG IPS QHD-1 (NVIDIA High Defin                MME                      2   44100  current-default
index:3      LG IPS QHD-1 (NVIDIA High Definition Audio)    Windows DirectSound      2   44100
index:4      LG IPS QHD-1 (NVIDIA High Definition Audio)    Windows WASAPI           2   48000
index:5      Headset (@System32\drivers\bthhfenum.sys...    Windows WDM-KS           1    8000  bluetooth
```

Note that `index:3` and `index:4` share an **identical name**. That is exactly
why a bare name is not a safe selector and why the pilot refuses ambiguous
names.

**Bare indices are not stable either.** Hardware validation proved this:
connecting a Bluetooth earbud set renumbered every device and moved the
endpoint from `index:7` to `index:18`. Always copy the **verified selector**
the list prints, which pins the index to the exact name:

```text
index:18@Headphones ()
```

> **CORRECTION (2026-07-26).** The selector shown above is only an example of
> the *format*. Do **not** use `Headphones ()` as an output device: it is a
> WDM-KS view of the Bluetooth stack, not a wired analog jack. It vanishes when
> the Bluetooth adapter is unplugged. Identify your endpoint by differential
> enumeration as described in
> [`ONE_STORE_BLUETOOTH_AMPLIFIER_TEST_RUNBOOK.md`](ONE_STORE_BLUETOOTH_AMPLIFIER_TEST_RUNBOOK.md).

If the device is renumbered later, that selector fails closed and tells you
what is actually at that index now, instead of opening the wrong endpoint.

The `wireless?` flag is a **name heuristic only**. It can miss a wireless
endpoint, so confirm yourself that the device is the wired one.

Pick the selector of your **USB audio adapter or 3.5 mm output** — not the
monitor, not the Bluetooth headset.

## Step 2 - Configure the pilot (process-scoped)

```powershell
$env:ECHOCAST_AUDIO_SINK_MODE     = 'windows'
$env:ECHOCAST_AUDIO_OUTPUT_DEVICE = 'index:18@Headphones ()'   # your verified selector
```

These live only in this PowerShell window. Nothing is written to Git, to a
repository `.env`, or to the protected database.

## Step 3 - Optional short test chime

Turn the amplifier volume **down** first.

```powershell
.\scripts\Test-EchoCastAudioOutput.ps1
```

It prints the exact device and waits for you to type `yes`. It plays a quiet
~1.5 s tone once, then stops. Record what you observed:

- [ ] heard clearly
- [ ] heard with distortion
- [ ] not heard
- [ ] wrong device (heard somewhere else)
- [ ] amplifier / AUX input issue

Whatever you tick, this is **operator observation**, not `SPEAKER_VERIFIED`.

## Step 4 - Start backend and frontend

```powershell
$env:ADMIN_USERNAME = 'pilot-operator'
$env:ADMIN_PASSWORD = 'choose-a-temporary-pilot-only-value'
$env:JWT_SECRET     = 'choose-another-temporary-pilot-only-value'
.\scripts\Start-EchoCastLocalPilot.ps1
```

## Step 5 - Start the hardware-mode Receiver

Copy the Store's receiver credential from **Store Management**, then in a
**second** PowerShell window:

```powershell
Set-Location 'C:\Users\admin\Desktop\EchoCast-AI\HQ-Broadcast-Full (1)'
$env:ECHOCAST_AUDIO_SINK_MODE     = 'windows'
$env:ECHOCAST_AUDIO_OUTPUT_DEVICE = 'index:18@Headphones ()'
$env:ECHOCAST_RECEIVER_TOKEN      = '<paste-the-store-credential>'
.\scripts\Start-EchoCastWindowsAudioReceiverPilot.ps1
```

If that device cannot be opened, the Receiver reports **DEVICE_ERROR** and
never claims READY.

## Step 6 - Browser checklist

1. Open `http://localhost:3000` and log in with the pilot-only credentials.
2. Open **Receiver Status**; your Store should show **online** (CONNECTED).
3. Open **Broadcast Console**, Target Mode **Selected Stores**, choose
   **exactly one** Store - the one whose Receiver you started.
4. Enter a campaign name and click **Start Live Broadcast**.
5. The console shows *waiting for receiver readiness*. The microphone is not
   opened until the Receiver acknowledges **READY**, which in hardware mode
   also means the device opened.
6. Grant microphone permission.
7. Say a short test phrase.
8. Watch the Receiver window: `CONNECTED -> READY -> AUDIO_RECEIVING ->
   PLAYBACK_CONFIRMED`.
9. Record your observation:
   - [ ] heard clearly
   - [ ] heard with distortion
   - [ ] not heard
   - [ ] wrong device
   - [ ] amplifier / AUX input issue
10. Click **Stop Broadcast**; confirm the Receiver reports **STOPPED**.

## Step 7 - Stop everything

```powershell
.\scripts\Stop-EchoCastWindowsAudioReceiverPilot.ps1
.\scripts\Stop-EchoCastLocalPilot.ps1
```

## Step 8 - Confirm cleanup

```powershell
Get-CimInstance Win32_Process |
    Where-Object { $_.Name -in 'python.exe','node.exe','ffmpeg.exe','ffplay.exe' -and
                   $_.CommandLine -match 'uvicorn|craco|audio_receiver' } |
    Select-Object ProcessId, Name

foreach ($f in 'backend\echocast_live.db','backend\echocast_live.db-wal','backend\echocast_live.db-shm') {
    if (Test-Path $f) { Get-Item $f | Select-Object Name, Length, LastWriteTimeUtc } else { "$f : absent" }
}
```

Nothing should be listed, and the protected database must be unchanged.

## Operator observation form

| Field | Value |
| --- | --- |
| Date / time (local) | |
| Selected device selector | |
| Selected device name | |
| Host API | |
| Adapter type (USB / 3.5 mm) | |
| Amplifier input used | |
| Amplifier volume setting | |
| Chime heard? | clearly / distorted / not heard |
| Microphone audio heard? | clearly / distorted / not heard |
| Delay noticed (approx.) | |
| Anything unexpected | |

> This form records **operator observation**. It never becomes
> `SPEAKER_VERIFIED`.

## Common errors

| Symptom | What it means |
| --- | --- |
| `ECHOCAST_AUDIO_OUTPUT_DEVICE is not set` | Hardware mode needs an explicit selector. There is no default. |
| `matches N output devices, so it is ambiguous` | You used a name that appears under several host APIs. Use the `index:N` selector. |
| `no output device is named exactly ...` | Partial or differently-cased names are refused on purpose. |
| `no output device has index N` | The device list was renumbered (a device was added or removed). List again. |
| `index N is no longer ...` | A verified selector caught a renumber and refused rather than opening the wrong endpoint. List again and copy the new verified selector. |
| `could not be opened` / device busy | Another application holds it exclusively, or it was removed. |
| Receiver reports `DEVICE_ERROR` | The selected device did not open. READY is correctly withheld. |
| Receiver reports `PLAYBACK_ERROR` | The device stopped accepting frames mid-session. |
| `PLAYBACK_CONFIRMED` but no sound | Frames were accepted, so check amplifier power, AUX input selection, cable and volume. This is exactly why it is not `SPEAKER_VERIFIED`. |
| Distorted sound | Lower the amplifier gain first; check the adapter's sample rate in the device list. |
| Sound came from the monitor | You selected an HDMI/monitor endpoint. Re-list and pick the adapter. |
| Bluetooth endpoint used by accident | The list flags these. Choose the wired adapter instead. |

## Rolling back to the safe null sink

```powershell
Remove-Item Env:ECHOCAST_AUDIO_SINK_MODE     -ErrorAction SilentlyContinue
Remove-Item Env:ECHOCAST_AUDIO_OUTPUT_DEVICE -ErrorAction SilentlyContinue
```

With no sink mode set, the pilot is back to `null` and cannot open any device.
The automated smoke (`.\scripts\Run-EchoCastAudioSmoke.ps1`) always uses the
null sink regardless of these variables.

## Honest readiness after this test

- ✅ `READY_FOR_ONE_STORE_WINDOWS_OUTPUT_TEST`
- ❌ `NOT_READY_FOR_SPEAKER_TEST` — `SPEAKER_VERIFIED` remains **unavailable**
- ❌ `NOT_READY_FOR_PRODUCTION`

`SPEAKER_VERIFIED` will only become possible when EchoGuard acoustic detection
is implemented and independently confirms sound in the Store.
