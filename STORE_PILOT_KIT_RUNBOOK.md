# Store pilot kit — build, verify, hand over

Status: **private LAN pilot only.** Verdict remains `NOT_READY_FOR_PRODUCTION`.

---

## 1. Why this kit exists

The Receiver package carried the executable and its own FFmpeg. The two-desktop
runbook then told the Store operator to run:

```powershell
.\scripts\Install-SpeakLinkReceiverLanPilot.ps1
```

That path exists only in this repository. An operator who copied the package to
the Store desktop — exactly as instructed — would find no such script and no way
to set up autorun. The instructions were written for a machine with the source
checkout, and the entire point of the package is that the Store desktop does not
have one.

The kit ships the installer scripts **beside** the package, and its verifier
checks `README-FIRST.txt` names no path outside the kit.

---

## 2. What the kit contains

```
SpeakLink-Store-Pilot-<commit>-<timestamp>/
  Receiver/          SpeakLinkReceiver.exe, ffmpeg.exe, _internal/,
                     manifest.json, SHA256SUMS.txt, licenses/
  Installer/         Install- / Test- / Uninstall-SpeakLinkReceiverLanPilot.ps1
  README-FIRST.txt   the operator's instructions, self-contained
  KIT-MANIFEST.json  both source commits, FFmpeg version and hash, paths
  KIT-SHA256SUMS.txt every file in the kit
```

It contains **no credential and no enrolment code**. Enrolment is a person
typing a one-time code; a kit carrying one would enrol whoever copied it.

It downloads **nothing**. Every input is a file already on the build computer,
named explicitly. A build step that fetches from the Internet decides silently,
and differently each time, what lands on 44 Store computers.

The kit is written under `artifacts/`, which is git-ignored. **Never commit it.**

---

## 3. Build

```powershell
.\scripts\Build-SpeakLinkStorePilotKit.ps1 `
    -PackagePath "artifacts\SpeakLinkReceiver-1.0.0-<commit>-<stamp>"
```

`-DryRun` checks every input and assembles nothing.

It refuses to proceed if:

- the package is marked `STALE-DO-NOT-DEPLOY`, or is the original
  `SpeakLinkReceiver-1.0.0` folder;
- the package was built from a **dirty** working tree, so it does not match the
  commit it records;
- any file in the package no longer matches the package's own `SHA256SUMS.txt`
  — a package edited after verification is a package nobody has verified;
- any of the three installer scripts is missing.

Ends with `SPEAKLINK_STORE_PILOT_KIT_BUILT`.

---

## 4. Verify

```powershell
.\scripts\Test-SpeakLinkStorePilotKit.ps1 -KitPath "artifacts\SpeakLink-Store-Pilot-<commit>-<stamp>"
```

37 checks. Ends with `SPEAKLINK_STORE_PILOT_KIT_VERIFIED` and exit code `0`.

An unreadable check reports `UNKNOWN` and no marker is emitted.

Two things this verifier learned the hard way, both worth knowing before you
write another one:

- **A probe must not shadow its caller's variables.** The `Check` helper took a
  parameter called `$Name`; a caller loop that also used `$name` for the file
  under test silently received the check's *label* instead, because PowerShell
  resolves variables in the calling function's scope and is case-insensitive.
  Three installer scripts that were present the whole time were reported
  missing. The parameters are now called `$CheckLabel` and `$CheckBody`.
- **"No matches in no files" is not a pass.** The download scan globbed the
  Installer folder; with an empty folder it would find nothing and report
  success — passing hardest exactly when the kit is emptiest. It now asserts
  there was something to scan.

---

## 5. Hand it to the Store computer

Copy the **whole kit folder**. Not just the `.exe`, and not just `Receiver\`.

USB stick, network share, whatever the site allows. No Internet is needed at
either end.

Then the operator follows `README-FIRST.txt` inside the kit. They do not need
this document and they do not need the repository.

---

## 6. Where the installed Receiver ends up

The installer **copies** the Receiver to:

```
%LOCALAPPDATA%\SpeakLink\receiver-app
```

and points the scheduled task at that copy.

This is deliberate. A scheduled task stores an absolute path and runs it at
every logon for months. If that path is the USB stick, the Store stops working
the moment somebody takes the stick home. If it is `Downloads` or `Temp`, it
stops working the first time Windows Storage Sense tidies up — and the failure
arrives weeks after the cause, with nothing connecting the two.

The installer refuses `-RunInPlace` for a package on a removable drive, a
network drive, `%TEMP%` or `Downloads`.

After copying, every file in the installed copy is **re-hashed** against the
package manifest. Copying is where files get truncated or silently skipped, and
a Receiver that is 99% copied does not announce it — it fails later with a DLL
error nobody connects to the install.

Unchanged by any of this:

| What | Where |
|---|---|
| Sealed credential | `%LOCALAPPDATA%\SpeakLink\receiver\device-credential.bin` |
| Logs | `%LOCALAPPDATA%\SpeakLink\receiver\logs` |

The credential is sealed with DPAPI to **that Windows account on that computer**.
Copying the file elsewhere gives the other machine nothing.

---

## 7. What a verified kit proves

**Proved:** the kit is complete and self-contained; it carries no credential,
code, password or key; it needs no Python, Node, FFmpeg install or Internet; the
packaged executable and FFmpeg both run from it; both source commits are
recorded; every file matches its recorded hash; and the operator instructions
reference nothing outside the kit.

**Not proved:** anything about a second desktop actually running it, an
amplifier, an audible speaker, LinkGuard, or `SPEAKER_VERIFIED`. Verifying a kit
is not the same as an operator using one — see
[PRIVATE_LAN_TWO_DESKTOP_TEST_RUNBOOK.md](PRIVATE_LAN_TWO_DESKTOP_TEST_RUNBOOK.md).
