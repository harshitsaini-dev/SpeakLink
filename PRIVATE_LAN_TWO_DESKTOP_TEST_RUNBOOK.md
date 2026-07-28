# Two-desktop private LAN test — manual runbook

Status: **private LAN pilot only.** Overall verdict remains
`NOT_READY_FOR_PRODUCTION`. Nothing here is a production deployment procedure.

Two Windows computers on one private network:

| Role | Machine | Address |
|---|---|---|
| **HQ** | the computer holding this repository | `192.168.4.134` (fixed) |
| **Store** | a second Windows desktop | any private address on the same LAN |

The Store desktop needs **no Python, no pip, no Node, no repository checkout**
and no FFmpeg installed — the package carries its own.

> **Do not expose any of this to the Internet.** The pilot sends a Device
> credential over plain HTTP, which is only ever acceptable on a network you
> control. Production requires HTTPS and WSS.

---

## Before the operator does anything

Each step below is **one action**. Do them in order and check the stated result
before moving on. Do not batch them, and do not assume what the second desktop
showed — read it.

---

## Part A — On the HQ computer

**A1.** Confirm the address is still the fixed one.

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where-Object IPAddress -eq '192.168.4.134'
```

Expect exactly one row. If nothing comes back, stop — every later step depends
on this address.

**A2.** Confirm the network profile is `Private`.

```powershell
Get-NetConnectionProfile | Select-Object Name, NetworkCategory
```

Expect `Private`. On a `Public` profile Windows blocks inbound connections and
the Store desktop will simply time out.

**A3.** Install the firewall rules (needs an elevated PowerShell).

```powershell
.\scripts\Install-SpeakLinkLanPilotFirewall.ps1
```

TCP 3000 and 8000, Private profile only, LocalSubnet only.

**A4.** Verify them.

```powershell
.\scripts\Test-SpeakLinkLanPilotFirewall.ps1
```

If it says a rule **cannot be read**, that is not the same as absent — rerun it
elevated before concluding anything.

**A5.** Start the LAN pilot backend.

```powershell
.\scripts\Start-SpeakLinkLanPilot.ps1
```

This creates a **temporary** database, key ring and administrator. It never
touches the protected database.

**A6.** Verify the backend.

```powershell
.\scripts\Test-SpeakLinkLanPilot.ps1
```

**A7.** Open `http://192.168.4.134:3000` on the HQ computer and sign in.

**A8.** Create an enrolment code for the Store you are testing, in
**Receiver Devices**. Note two things about the code:

- it is shown **once** and never again;
- it is single-use and short-lived.

Write it on paper if you must. Do not e-mail it, do not put it in a chat, and
do not save it to a file — from that moment it is a credential in somebody
else's system.

---

## Part B — Getting the package onto the Store desktop

**B1.** On the HQ computer, build a fresh package.

```powershell
.\scripts\Build-SpeakLinkReceiver.ps1
```

Expect `SPEAKLINK_RECEIVER_FRESH_BUILD_VERIFIED`.

**B2.** Verify it.

```powershell
.\scripts\Test-SpeakLinkReceiverPackage.ps1 -PackagePath "artifacts\SpeakLinkReceiver-1.0.0-<commit>-<stamp>"
```

Expect `SPEAKLINK_RECEIVER_PACKAGE_VERIFIED`.

> **Never use `artifacts\SpeakLinkReceiver-1.0.0`.** It is marked
> `STALE-DO-NOT-DEPLOY` and kept only as evidence of the stale-package problem.
> It was built nine minutes *before* the source it was supposed to contain, and
> every test it had passed anyway.

**B2a.** Wrap it into a Store pilot kit, and verify that.

```powershell
.\scripts\Build-SpeakLinkStorePilotKit.ps1 -PackagePath "artifacts\SpeakLinkReceiver-1.0.0-<commit>-<stamp>"
.\scripts\Test-SpeakLinkStorePilotKit.ps1 -KitPath "artifacts\SpeakLink-Store-Pilot-<commit>-<stamp>"
```

Expect `SPEAKLINK_STORE_PILOT_KIT_VERIFIED`.

**This step is not optional, and it is not tidiness.** The package alone
contains the executable and FFmpeg but none of the installer scripts, so an
operator who copied only the package would reach step F1 below and find no
`Install-SpeakLinkReceiverLanPilot.ps1` anywhere on the machine. That was a real
gap in an earlier version of this runbook. See
[STORE_PILOT_KIT_RUNBOOK.md](STORE_PILOT_KIT_RUNBOOK.md).

**B3.** Copy the whole **kit** folder to the Store desktop — for example to
`C:\SpeakLink\Kit`. Copy the folder, not just the `.exe`; the executable alone
will not run.

**B4.** On the Store desktop, confirm the copy is complete.

```powershell
Get-ChildItem C:\SpeakLink\Receiver | Measure-Object
```

Expect 39 items, including `SpeakLinkReceiver.exe` and `ffmpeg.exe`.

**B5.** Confirm the Store desktop really has no Python.

```powershell
Get-Command python -ErrorAction SilentlyContinue
```

Expect nothing. If something answers, the test still works — but it no longer
proves the package is self-contained on a clean machine.

---

## Part C — Enrolling the Store desktop

**C1.** Confirm the Store desktop can reach HQ.

```powershell
Test-NetConnection 192.168.4.134 -Port 8000
```

Expect `TcpTestSucceeded : True`. If it is `False`, go back to A2 and A3 — it is
a firewall or profile problem, not a Receiver problem.

**C2.** Enrol. The code is typed at a hidden prompt and never appears on the
command line or in the console.

```powershell
cd C:\SpeakLink\Receiver
.\SpeakLinkReceiver.exe enrol `
    --backend-url http://192.168.4.134:8000 `
    --allow-insecure-private-lan --expected-hq-host 192.168.4.134 `
    --device-name "Store front counter"
```

Expect `Enrolled.` with a Device id, a Store id and a sealed path.

**C3.** Confirm what was stored.

```powershell
.\SpeakLinkReceiver.exe status
```

The credential is sealed with Windows DPAPI under **this Windows account on this
computer**. Another account cannot read it, and neither can this account on
another machine. That is deliberate: a stolen file is not a working credential.

**C4.** In the HQ Receiver Devices page, confirm the new Device appears.

---

## Part D — A broadcast

**D1.** On the Store desktop, start the Receiver in the foreground for the first
run, so you can watch it.

```powershell
.\SpeakLinkReceiver.exe run `
    --backend-url http://192.168.4.134:8000 `
    --allow-insecure-private-lan --expected-hq-host 192.168.4.134
```

It prints a plain-HTTP warning. That warning is correct and must not be removed.

**D2.** On the HQ page, confirm the Store shows `CONNECTED`, then `READY`.

**D3.** Start a broadcast to that Store from HQ.

**D4.** Read the Store status on the HQ page. Expect `AUDIO_RECEIVING`, then
`PLAYBACK_CONFIRMED`.

**D5.** Ask the operator at the Store desktop **what they actually heard**, and
write down their words. Do not infer it from the status.

`PLAYBACK_CONFIRMED` means the Receiver decoded audio and handed it to a sound
device. It does **not** mean a speaker made a sound, that an amplifier was on,
or that the volume was up. Only a person in the room knows that, and only
LinkGuard hardware produces `SPEAKER_VERIFIED`.

**D6.** Stop the broadcast at HQ. Expect `STOPPED` on the Store.

**D7.** Stop the Receiver with `Ctrl+C`. Expect `Stopped.` and exit code `0`.

---

## Part E — Only one Receiver

**E1.** Start the Receiver again (as in D1) and leave it running.

**E2.** Open a **second** PowerShell window on the Store desktop and run exactly
the same command.

**E3.** Expect the second one to refuse immediately:

```
Another SpeakLink Receiver is already running for this Device on this computer.
```

**E4.** Confirm its exit code.

```powershell
$LASTEXITCODE
```

Expect `4` — distinct from every failure code, so an autorun policy does not
treat a working Receiver as something to relaunch.

**E5.** Confirm the first Receiver is still connected on the HQ page. It must be
unaffected. Two Receivers on one credential would otherwise fight for the socket
and the Store would flicker between them.

**E6.** Stop the first Receiver.

---

## Part F — Autorun at logon

**F1.** Install the task on the Store desktop.

```powershell
.\scripts\Install-SpeakLinkReceiverLanPilot.ps1 -PackagePath C:\SpeakLink\Receiver
```

**F2.** Verify it.

```powershell
.\scripts\Test-SpeakLinkReceiverLanPilot.ps1
```

Expect `SPEAKLINK_RECEIVER_TASK_SCHEDULER_VERIFIED`.

**F3.** Sign out of the Store desktop and sign back in.

**F4.** Confirm on the HQ page that the Store reaches `CONNECTED` without
anybody starting anything.

**F5.** Read the log on the Store desktop.

```
%LOCALAPPDATA%\SpeakLink\receiver\logs\receiver.log
```

**F6.** Search that log for anything secret. Expect to find none:

```powershell
Select-String -Path "$env:LOCALAPPDATA\SpeakLink\receiver\logs\receiver.log" `
              -Pattern 'speaklink_rcv_v1\.', 'Bearer ', 'ECHO-'
```

**F7.** Do **not** conclude anything about behaviour before logon. Signing in is
what started it. See section 1 of `RECEIVER_TASK_SCHEDULER_RUNBOOK.md`.

---

## Part G — Putting everything back

**G1.** Remove the task on the Store desktop.

```powershell
.\scripts\Uninstall-SpeakLinkReceiverLanPilot.ps1 -StopRunning
```

**G2.** Remove the Device credential, if the desktop is not being kept.

```powershell
.\SpeakLinkReceiver.exe remove-local-credential
```

It asks for typed confirmation.

**G3.** On HQ, stop the pilot backend.

```powershell
.\scripts\Stop-SpeakLinkLanPilot.ps1
```

**G4.** Remove the firewall rules (elevated).

```powershell
.\scripts\Uninstall-SpeakLinkLanPilotFirewall.ps1
```

**G5.** Confirm the protected database was never touched: it must have the same
size and hash as before the test, with no `-wal` or `-shm` beside it.

---

## What a completely green run does and does not prove

**Proved:** the packaged Receiver runs on a Windows desktop with no Python and
no FFmpeg installed; it enrols over a private LAN; the credential is sealed per
user per machine; only one Receiver runs per credential; it starts at logon;
audio is decoded through the *packaged* FFmpeg; and no secret reaches a log, a
console, a task definition or a URL.

**Not proved:** anything about HTTPS/WSS, the Internet, more than two computers,
44 Stores at once, boot-before-logon, service or SYSTEM operation, sustained
multi-hour running, or any loudspeaker, amplifier, LinkGuard or
`SPEAKER_VERIFIED` result.
