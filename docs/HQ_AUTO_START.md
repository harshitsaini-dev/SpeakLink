# HQ auto-start — Scheduled Task design

Four scripts register, verify, repair and remove the Windows Scheduled Task that
starts `SpeakLinkHQRuntime.exe`.

| Script | What it does |
|---|---|
| [`Install-SpeakLinkHQAutoStart.ps1`](../scripts/Install-SpeakLinkHQAutoStart.ps1) | Verify a package, install it, register the task. Does **not** start it unless `-StartNow` |
| [`Test-SpeakLinkHQAutoStart.ps1`](../scripts/Test-SpeakLinkHQAutoStart.ps1) | Read-only verification, 30+ checks |
| [`Repair-SpeakLinkHQAutoStart.ps1`](../scripts/Repair-SpeakLinkHQAutoStart.ps1) | Replace broken application files, re-register a wrong task. Never touches data |
| [`Uninstall-SpeakLinkHQAutoStart.ps1`](../scripts/Uninstall-SpeakLinkHQAutoStart.ps1) | Remove task and application files. Keeps data |

Tests: [`backend/tests/test_hq_auto_start.py`](../backend/tests/test_hq_auto_start.py)

---

## THE LIMIT, STATED FIRST

**HQ starts when the HQ Windows user signs in. It does not run at the Windows
sign-in screen.**

No setting in these scripts changes that, and none of them pretend to. A locked
screen with the HQ user still signed in is fine — the session exists, the
process runs, the network works. A machine sitting at the sign-in screen after
an unattended reboot is not.

The fix, if it is ever needed, is Windows automatic sign-in, which stores a
password in the registry. It is **not** configured here and should not be
configured without a decision about physical access to the HQ machine.

**Lock-screen behaviour is not verified by any automated test in this
repository.** It needs a person at the machine. See the manual acceptance
runbook.

## Why a Scheduled Task and not a Windows service

The Store Receiver cannot be a service because session 0 has no audio endpoint.
HQ has a different reason for the same answer: a service would run as SYSTEM or
as an account whose Windows password is stored, and it would then own the
persistent database. An interactive task owned by the HQ user needs neither.

---

## The task definition

| Setting | Value | Why |
|---|---|---|
| Action | `<InstallRoot>\SpeakLinkHQRuntime.exe`, **no arguments** | Everything configurable lives in the persistent profile, so the task cannot go stale and has nowhere to carry a secret |
| Working directory | `<InstallRoot>` | |
| Wrapper | **none** — no `powershell.exe`, `pwsh.exe`, `cmd.exe`, no `.ps1`/`.bat`/`.cmd`, no Python interpreter | A wrapper is how a console window gets onto an HQ desk after all the work spent making the executable windowed |
| Principal | the HQ user, `LogonType Interactive`, `RunLevel Limited` | No stored Windows password, no administrator rights, never SYSTEM |
| Trigger 1 | `AtLogOn` for that user | The normal path |
| Trigger 2 | `Once` + `RepetitionInterval` (default 10 min, `ValidateRange(5,60)`) + `RepetitionDuration` (default 1 day) | The recovery path |
| `StartWhenAvailable` | on | Runs a missed occurrence |
| `MultipleInstances` | `IgnoreNew` | Two triggers must never produce two runtimes |
| `ExecutionTimeLimit` | `[TimeSpan]::Zero` | It is meant to run all day |
| `RestartCount` / `RestartInterval` | 3 / 2 min | Bounded |

### Why the two triggers are separate

**A repetition setting attached only to an `AtLogOn` trigger begins only after a
logon.** A runtime that dies at 11am would then wait until the next morning.
That was measured on the Store Receiver task and is fixed the same way here: the
repetition sits on its own time-based trigger.

Related, and also measured: **Task Scheduler's `RestartCount` applies when a
task fails to _start_, not when the program it started exits.** Tested with
`cmd /c exit 1` and `RestartCount 2` — it never re-ran. The repetition schedule
plus `IgnoreNew` is what actually brings a dead runtime back.

### How this composes with `DEGRADED`

The runtime gives up after bounded attempts and exits 3 rather than respawning a
broken backend for ever. The periodic trigger starts it again at the next
interval; `IgnoreNew` means a healthy runtime already running is left alone. The
two halves are designed together — see
[HQ_RUNTIME_DESIGN.md](HQ_RUNTIME_DESIGN.md).

---

## Install

```powershell
.\Install-SpeakLinkHQAutoStart.ps1 -PackagePath . -DryRun     # check everything
.\Install-SpeakLinkHQAutoStart.ps1 -PackagePath .             # register
Start-ScheduledTask -TaskName "SpeakLink HQ Runtime"          # start deliberately
```

Refuses, before writing anything:

- a package missing the runtime, the frontend, `manifest.json` or `SHA256SUMS.txt`
- a package marked `STALE-DO-NOT-DEPLOY`
- a runtime whose **PE subsystem is not 2** — read from the header of the file
  about to be installed, not from the build log
- a persistent root with no `data\speaklink.db` or no `keys` folder
- `-HqUser` naming SYSTEM, LOCAL SERVICE or NETWORK SERVICE
- an existing task of the same name that runs something that is not ours
- an `-InstallRoot` inside the persistent data root

After copying it re-verifies every file against `SHA256SUMS.txt`, re-reads the
installed executable's PE subsystem, and exports the registered task XML to
confirm Windows stored nothing secret — because checking the variables is not
checking the task.

**Registering and starting are separate decisions.** Installing during business
hours must not take the running pilot's ports, so starting is opt-in
(`-StartNow`) or a deliberate `Start-ScheduledTask`.

`-TaskName`, `-InstallRoot` and `-PersistentRoot` exist so an automated test can
be fully isolated from anything real.

> **What the installer does NOT require:** `keys\receiver-hmac-keys.bin` and
> `keys\jwt-secret.txt`. Nothing creates them until the first start — the
> backend mints the HMAC container, the runtime mints the signing secret.
> Demanding them refused a correctly initialized HQ with an instruction no
> procedure here could carry out. Whether a *missing* container is normal or an
> emergency depends on how many Devices are enrolled, and only the runtime can
> count those, so it makes that call at start.

## Verify

```powershell
.\Test-SpeakLinkHQAutoStart.ps1
```

Read-only — a verifier that repairs is a verifier whose PASS means nothing.
A check that cannot be read reports `UNKNOWN`, never PASS and never FAIL, and no
marker is emitted while anything is unreadable. (An earlier firewall checker in
this repository asked `Get-NetFirewallRule` without elevation, was told "Access
is denied", and scored that as "the rule is not installed".)

It reads the runtime's **status file** rather than inferring health from a
process existing.

Emits `SPEAKLINK_HQ_AUTOSTART_VERIFIED`.

Its scope, stated in its own output: the task is registered correctly and the
runtime is installed windowed. It says nothing about behaviour after a reboot,
after sign-out and sign-in, or on a locked desktop.

## Repair

```powershell
.\Repair-SpeakLinkHQAutoStart.ps1 -PackagePath . -DryRun
```

**Application files and the task. Nothing else.**

- Replaces files whose hash does not match the package manifest
- Re-registers the task only after confirming the existing one runs
  `SpeakLinkHQRuntime.exe`
- **Preserves** the database, keys, configuration, backups and logs — reports
  each as preserved and never writes to any of them
- **Refuses** if HQ's own data is missing, rather than repairing around it. The
  obvious "helpful" repair is to re-initialize, and re-initializing is precisely
  how a persistent server becomes an empty one that starts cleanly and has lost
  every Store
- Never creates a database, resets a user, rotates a key or re-enrols a Store

## Uninstall

```powershell
.\Uninstall-SpeakLinkHQAutoStart.ps1
```

- Removes the task **only** if its action runs `SpeakLinkHQRuntime.exe`
- Stops **only** processes whose `ExecutablePath` is under the install root; a
  runtime running from somewhere else is reported and left alone
- Stops the uvicorn / `http.server` children by **command line**, not by image
  name — stopping every `python.exe` on an HQ desk would be a rude way to end an
  uninstall
- Removes application files (`-KeepApplicationFiles` to keep them)
- **Keeps all persistent data.** `-RemovePersistentData` is declared so that
  keeping it reads as a decision rather than an oversight, and **refuses**:
  deleting every Store, user account and Device credential needs a verified
  backup and a typed confirmation, and is not something this sprint should be
  able to do by flag

---

## Rollback

See [ROLLBACK_PLAN.md](ROLLBACK_PLAN.md).

## Operator checkpoints that cannot be automated

1. Installing the live task on the real HQ machine
2. Signing out and back in
3. Rebooting and signing in
4. Locked-desktop behaviour
5. Any audible confirmation
