# Windows Receiver Agent — installation and administration runbook

For the person standing at a till in a Store, and for the administrator at HQ
who talks them through it. Every command here has been run.

**Two tools, and they are not interchangeable.**

| Tool | What it is for |
| --- | --- |
| `tools/receiver_agent.py` | **Production.** One enrolled Device per computer, its own revocable credential |
| `tools/audio_receiver_pilot.py` | **Hardware test bench.** Shared per-Store token, used for the amplifier and output-device work. Unchanged, and staying |

---

## Before you start

- The computer needs Python and FFmpeg with Opus and WebM support on `PATH`.
- The Agent runs as the Windows account that will run it every day. The
  credential is sealed to that account by DPAPI: sealing it as one user and
  running as another will not work, and the error will say so.
- HQ must be reachable over **https://** in production. Plain HTTP is refused
  unless it is loopback *and* `--allow-insecure-loopback` is given, which exists
  for local testing and must never appear in a Store.

---

## 1. Enrol the computer (once)

**At HQ**, on the Store's Devices page (`Stores → the Store → Receiver Devices`):
press **Create enrolment code**. The code is displayed once. It cannot be shown
again — if it is lost, issue another.

**At the till**, in PowerShell:

```powershell
python tools\receiver_agent.py enrol `
    --backend-url https://hq.example.internal `
    --device-name "Store UN till 1"
```

It asks for the code with a hidden prompt. Type it and press Enter.

> The code is **never** a command argument. `tasklist`, Windows event logs, crash
> dumps and anyone watching the screen can all see process arguments. If a
> command line ever contains a code or a credential, stop and report it.

On success it prints the Device identifier, the Store, and where the credential
was sealed. It never prints the credential.

To read the code from a script instead of a prompt, pipe it:

```powershell
"ECHO-XXXX-XXXX" | python tools\receiver_agent.py enrol --backend-url https://... --device-name "..." --from-stdin
```

### If enrolling fails

| Message | What it means | What to do |
| --- | --- | --- |
| "HQ refused that enrolment code" | Unknown, expired, or already used | Ask HQ for a new code |
| "HQ is rate limiting" | Too many attempts from this computer | Wait, then retry with the same code |
| "HQ is not ready to enrol" | Server-side: no key container or no schema | The code is unused. Escalate to HQ |
| "HQ could not be reached" | Nothing was issued | Safe to retry with the same code |
| "already holds a Receiver Device credential" | This computer is enrolled | See *Re-enrolling* below |
| **"Device … was created at HQ, but its credential could not be stored"** | **The dangerous one.** The code is spent and a Device exists that nothing can use | Do **not** retry the code. Ask HQ to revoke the named Device and issue a new code |

---

## 2. Run it

```powershell
python tools\receiver_agent.py run --backend-url https://hq.example.internal
```

It loads the sealed credential, connects, and stays connected. There is no code
and no credential on this command line either.

What you should see, in order, and what each actually means:

| State | What it proves |
| --- | --- |
| `CONNECTED` | The socket is open and HQ accepted this Device's credential. **Nothing more.** |
| `READY` | FFmpeg is present, the Opus/WebM decode path works, and the output was opened |
| `AUDIO_RECEIVING` | Real audio bytes arrived for this session |
| `PLAYBACK_CONFIRMED` | The software player accepted and processed the audio |
| `STOPPED` | The broadcast ended cleanly |

`PLAYBACK_CONFIRMED` does **not** mean anybody heard anything. It means a sound
card accepted frames. Whether the amplifier was on, the right input was
selected, or the shop could hear it, this computer cannot know. Only EchoGuard
acoustic verification can establish that, and it is not implemented.

### When it stops trying

If HQ refuses the credential the Agent exits non-zero with
`AUTHENTICATION_REFUSED` rather than reconnecting forever. That covers a revoked
Device, a disabled Device and a wrong credential — HQ answers all three
identically on purpose, so the Agent cannot tell them apart and does not try.
None of the three will start working on its own.

Ordinary network faults retry with an exponential, jittered, capped backoff, and
the backoff resets after a stable connection. The jitter matters at 44 Stores:
when a shared link returns, they must not all reconnect in the same millisecond.

---

## 3. Check what a computer is

```powershell
python tools\receiver_agent.py status
```

Prints the Device identifier, Store, backend origin and when it was enrolled.
Never the credential.

---

## 4. Rotation

Rotate when a computer is replaced, a technician leaves, or a credential may
have been seen by someone. **There is no overlap window** — the old credential
stops working the instant the new one is issued, and that Device is offline
until you carry the new credential to it. That is deliberate: a grace period is
a period in which a leaked copy still works.

**At HQ:** Devices page → **Rotate** on that Device → confirm. The new
credential is displayed once.

**At the till:**

```powershell
python tools\receiver_agent.py rotate-local-credential
```

It asks for the new credential with a hidden prompt. Then start `run` again.

Rotation changes the secret, never which Device the computer is. Its identifier,
its history and its primary/standby role are all unchanged.

---

## 5. Primary and standby

A **Store** is what an announcement is sent to. A **Device** is one computer.

- Exactly one Device per Store is **primary**, and only it receives audio.
- Standbys connect, report health, and receive nothing. A standby that received
  audio "just in case" would be an echo in the shop.
- Promotion is always explicit: HQ → **Promote**.
- Disabling or revoking the primary leaves the Store **with no primary**, and the
  dashboard says so. Nothing is promoted automatically, because that would move
  the announcement onto a computer nobody has confirmed is connected to the
  amplifier, silently.

A Store may hold three active Devices during the migration: the legacy
backfilled Device, a primary and a standby.

---

## 6. Retiring a computer

**At HQ:** **Disable** to stop it temporarily (reversible), **Revoke** to retire
it permanently. Either way its Store and every other Device keep working.

**At the till**, to remove the local credential:

```powershell
python tools\receiver_agent.py remove-local-credential
```

It requires you to type `remove`. It does **not** revoke anything — the Device
still exists at HQ until an administrator retires it. Do both, or you leave a
Device that looks live and never connects.

### Re-enrolling a computer

`enrol` refuses if a credential is already stored, because a second enrolment
would strand the Device this computer is currently using. To genuinely
re-enrol: revoke the old Device at HQ, run `remove-local-credential`, then
`enrol` with a fresh code.

---

## Where the credential lives

```
%LOCALAPPDATA%\EchoCast-AI\receiver\device-credential.bin
```

Outside the repository, outside any database, sealed with Windows DPAPI
`CURRENT_USER`. Not `LOCAL_MACHINE`: that would let every account and service on
that computer read it, including whatever arrives later.

It carries distinct DPAPI **entropy** from the backend's HMAC key container, so
the two files cannot be opened by each other's code even by accident. Both
directions are covered by tests that run against real DPAPI.

Writes are atomic — a temporary file, then one replace — so an interrupted
rotation leaves the previous credential working rather than a computer that can
neither authenticate nor re-enrol.

---

## Legacy mode

`--legacy-pilot-mode` makes the Agent authenticate with the shared per-Store
token from `ECHOCAST_RECEIVER_TOKEN`. It is never the default, prints a warning
explaining that the token cannot be revoked for one computer, and exists only
for the migration period.

**No installation should use it.** Its removal, together with
`DualRuntimeAuthenticator` in `backend/receiver_runtime_auth.py`, is the
documented cutover. See `RECEIVER_ENROLMENT.md`.

---

## What has been proven, and what has not

`tools/receiver_device_staging_smoke.py` runs the whole chain against a real
backend with a throwaway database, and passes 32 checks: enrol, seal with real
DPAPI, reconnect, `CONNECTED → READY → AUDIO_RECEIVING → PLAYBACK_CONFIRMED →
STOPPED`, rotate, old credential refused, new credential reconnects, standby
enrolled and receiving zero chunks while the primary received 17, disable
refused, revoke refused, Store still active.

That is **software evidence**. Not proven by any of it: an amplifier, a
Bluetooth link, audible Store speakers, or `SPEAKER_VERIFIED`.
