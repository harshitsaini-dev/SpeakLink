# Running the EchoCast Receiver automatically — Task Scheduler runbook

Status: **private LAN pilot only.** Overall verdict remains
`NOT_READY_FOR_PRODUCTION`.

This describes an **At-Logon scheduled task** that starts the packaged Receiver
when a Windows user signs in to a Store desktop.

---

## 1. What this does not do

Read this first, because the gap between it and what a 44-Store rollout needs is
the main thing to understand.

| Claim | True here? |
|---|---|
| Starts when the operator logs on | Yes |
| Restarts a small, bounded number of times after a crash | Yes |
| Writes a log you can read afterwards | Yes |
| Only one Receiver per Device credential | Yes |
| Starts **before** anybody logs on | **No** |
| Runs as a Windows **service** | **No** |
| Runs as **SYSTEM** | **No** |
| Keeps playing while the desktop is **locked with nobody signed in** | **No** |

The last four are not oversights to be patched later with a flag. The Receiver
plays audio into a Windows user session, and session 0 — where services run —
has no audio device. Boot-time operation needs a different design: a service
that hands audio to a session, or an auto-logon kiosk account. Neither has been
built, and neither should be assumed from a green run of this runbook.

A Store that must announce with nobody logged in is **not** covered by this.

---

## 2. Before you start

- The HQ backend is running on the private LAN at `192.168.4.134:8000`
  (`.\scripts\Start-EchoCastLanPilot.ps1`).
- A fresh Receiver package exists and passed
  `ECHOCAST_RECEIVER_PACKAGE_VERIFIED`. Never use
  `artifacts\EchoCastReceiver-1.0.0` — it is marked `STALE-DO-NOT-DEPLOY`.
- This computer has already enrolled a Device
  (`EchoCastReceiver.exe enrol …`) and `status` shows a sealed credential.
  **The task never enrols.** Enrolment needs a one-time code typed by a person,
  and a task that could enrol would need that code stored on disk.

---

## 3. Install

```powershell
.\scripts\Install-EchoCastReceiverLanPilot.ps1 `
    -PackagePath "artifacts\EchoCastReceiver-1.0.0-<commit>-<stamp>"
```

Add `-DryRun` first if you want every input checked and nothing registered.

Expect to end with `ECHOCAST_RECEIVER_TASK_INSTALLED`.

What it registers, and why each choice:

| Setting | Value | Why |
|---|---|---|
| Trigger | At logon of this user | The only session with a sound card |
| Principal | Current user, `Interactive` | No Windows password stored anywhere |
| Run level | `Limited` | The Receiver needs no administrator rights |
| Restart | 3 times, 1 minute apart | Bounded — see below |
| Multiple instances | `IgnoreNew` | Belt and braces with the Agent's own lock |
| Execution time limit | none | A Receiver is meant to run all day |

### Why the restart count is small

An unbounded restart policy turns a Receiver that will *never* authenticate —
a revoked Device, a wrong backend — into a machine that reconnects for ever. It
looks like a flaky network, it fills the HQ log, and it buries the actual
refusal. Three attempts, then the task stops and the Receiver's own log says
why.

### What the task definition contains

A backend URL, a log directory and a file path. No credential, no enrolment
code, no token. The installer exports the registered XML and refuses to leave
the task in place if any of those appear — anyone who can open Task Scheduler
can read a task definition.

---

## 4. Verify

```powershell
.\scripts\Test-EchoCastReceiverLanPilot.ps1
```

Ends with `ECHOCAST_RECEIVER_TASK_SCHEDULER_VERIFIED` when all 20 checks pass.

A check that cannot be **read** reports `UNKNOWN`, and no verification token is
emitted while any check is unreadable. This matters: an earlier firewall checker
in this repository asked `Get-NetFirewallRule` without elevation, was told
"Access is denied", and scored that as "the rule is not installed" — reporting a
missing rule that was present the whole time. *"I could not read this"* and
*"this is wrong"* are different answers.

To also prove the packaged executable refuses a duplicate launch, pass a real
enrolled credential:

```powershell
.\scripts\Test-EchoCastReceiverLanPilot.ps1 -IncludeLiveInstanceCheck `
    -LiveCredentialPath "$env:LOCALAPPDATA\EchoCast-AI\receiver\device-credential.bin"
```

This starts a real Receiver, so run it against the LAN pilot, not a Store that
is on air.

---

## 5. Logs

Default location:

```
%LOCALAPPDATA%\EchoCast-AI\receiver\logs\receiver.log
```

Ten files of one megabyte, rotating — about 10 MB total, for ever. A Store
desktop that fills its own disk with logs stops working for a reason nobody
will guess.

Timestamps are **UTC**. 44 Stores that each report their own local time cannot
be read side by side.

Nothing secret is written. Credentials, enrolment codes, bearer headers and URL
query strings are removed by a filter on the log handler, not by asking each
call site to remember. Device public ids and Store codes are deliberately kept —
they are what makes a log useful, and a redactor that eats them is one an
operator switches off.

---

## 6. Reading the exit code

Task Scheduler shows the **Last Run Result** for the task.

| Code | Meaning | What to do |
|---|---|---|
| `0` | Stopped normally | Nothing |
| `1` | Refused — bad argument, no credential, unsafe URL | Read the message; it is a configuration problem |
| `2` | **Authentication refused.** The Device credential will never work | Re-enrol at HQ. Restarting will not help |
| `3` | Network problem after bounded retries | Check the LAN and the HQ backend |
| `4` | **Already running.** A Receiver for this credential is live | Nothing. This is not a failure |

Code `4` is separate on purpose. If a duplicate launch reported an ordinary
failure, the restart policy would keep relaunching something already working
perfectly.

Note that Task Scheduler treats any non-zero result as a failure for restart
purposes, including `4`. `MultipleInstances = IgnoreNew` is what actually stops
a duplicate from being started by the scheduler; the exit code is for the
operator reading the history.

---

## 7. Remove

```powershell
.\scripts\Uninstall-EchoCastReceiverLanPilot.ps1 -StopRunning
```

Ends with `ECHOCAST_RECEIVER_TASK_REMOVED`, or `ECHOCAST_RECEIVER_TASK_ABSENT`
if there was nothing there. Running it twice is safe.

It refuses to remove a task whose action does not point at an
`EchoCastReceiver` executable — the task name is a parameter, and a typo could
otherwise name somebody else's task.

`-StopRunning` stops only processes whose **full executable path** matches the
one the task names. It does not stop anything else on the machine: not a
language server, not an unrelated Python or Node process. "Stop anything that
looks like ours" is how a cleanup script kills an editor mid-save.

The DPAPI credential is **left alone**. Removing an autorun task is not a
decision to un-enrol a Device. To do that, deliberately:

```powershell
EchoCastReceiver.exe remove-local-credential
```

which asks for typed confirmation first.

---

## 8. What still has no evidence

- Boot-before-logon, service or SYSTEM operation — not built.
- Anything on the second desktop — see the two-desktop runbook.
- Any loudspeaker, amplifier, EchoGuard or `SPEAKER_VERIFIED` result. Every
  check in this runbook is software-only. `PLAYBACK_CONFIRMED` means the
  Receiver decoded audio and handed it to a sink; it does not mean a person
  heard anything.
