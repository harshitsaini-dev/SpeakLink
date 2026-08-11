# HQ quick start — persistent LAN server

One page. Every command runs from the repository root:

```
C:\Users\admin\Desktop\SpeakLink
```

---

## What changed, and why it matters

The old `Start-SpeakLinkLanPilot.ps1` built its data folder from the clock and
created a new random `lan-pilot-xxxxxx` administrator **every single start**. So
every restart threw away all Stores, Devices, users and history.

That one thing caused both problems you saw:

* the Store showed **OFFLINE** after a restart — its Device was in a database
  the server no longer used;
* **`owneradmin` could not sign in** — that account was in a different database.

The persistent server uses **one fixed folder, for ever**:

```
%LOCALAPPDATA%\SpeakLink\persistent-lan-server\
    data\speaklink.db      <- same file every restart
    keys\                 <- same keys every restart
    logs\  backups\  runtime\  migration-reports\
```

---

## Already done

The persistent server has been initialized from `backend\speaklink_live.db`:

| | |
|---|---|
| users | `admin` (ADMIN), `owneradmin` (OWNER) |
| Stores | 13 |
| sessions / logs | 17 / 194 |
| integrity | ok |
| source backup | `…\persistent-lan-server\backups\source-20260729-190540.db` |

The source database was **not** moved, edited or deleted.

---

## Daily commands

### Start

```powershell
.\scripts\Start-SpeakLinkPersistentLanServer.ps1
```

Expect `SPEAKLINK_PERSISTENT_SERVER_STARTED` and the API on
`http://192.168.4.134:8000`.

It **refuses** to start if the database is missing — it will not quietly create
an empty one — and refuses if port 8000 is already busy.

### Check

```powershell
.\scripts\Test-SpeakLinkPersistentLanServer.ps1
```

Expect `SPEAKLINK_PERSISTENT_SERVER_VERIFIED` and 11 checks passed.

### Stop

```powershell
.\scripts\Stop-SpeakLinkPersistentLanServer.ps1
```

Stops only its own backend, checked by process command line — Windows reuses
process numbers, so a recorded PID alone is not proof.

### Repair

```powershell
.\scripts\Repair-SpeakLinkPersistentLanServer.ps1 -DryRun
.\scripts\Repair-SpeakLinkPersistentLanServer.ps1
```

Rebuilds missing folders and clears a stale lock. It **never** touches the
database, keys, users, Stores, Devices or history. A missing database stops it —
that is a restore decision, not a repair.

---

## Signing in

`owneradmin` and `admin` both exist in the persistent database with the
passwords you already set. You will **not** need a `lan-pilot-xxxxxx` username
again.

**OWNER** and **ADMIN** both have full operational access. The only difference:
only an OWNER can change another OWNER, and the last enabled OWNER cannot be
disabled, archived or demoted — otherwise nobody could administer SpeakLink ever
again.

---

## Important honest limits

* This is still **private LAN, plain HTTP**. Production needs HTTPS and WSS.
* The Store's old Receiver Device is in an old pilot database. Its credential is
  verified with that pilot's own key ring, so it **cannot** be copied across.
  Plan **one** final re-enrolment into this persistent server. After that, a
  normal restart never needs another.
* **HQ auto-start now exists** — see [HQ_AUTO_START.md](HQ_AUTO_START.md). It is
  built and verified but **not installed on this machine**. Installing the live
  task is an operator decision.

---

## Starting HQ without a PowerShell window (new)

Build once, install once, and the HQ user never opens a terminal again.

```powershell
# 1. verify the package you were given
.\scripts\Test-SpeakLinkHQPackage.ps1 -PackagePath "artifacts\SpeakLinkHQ-<version>-<commit>-<time>"
#    expect: SPEAKLINK_HQ_PACKAGE_VERIFIED

# 2. see exactly what would be registered - changes nothing
.\scripts\Install-SpeakLinkHQAutoStart.ps1 -PackagePath "artifacts\<package>" -DryRun

# 3. register it (this does NOT start it)
.\scripts\Install-SpeakLinkHQAutoStart.ps1 -PackagePath "artifacts\<package>"

# 4. start it deliberately, when the ports are free
Start-ScheduledTask -TaskName "SpeakLink HQ Runtime"

# 5. check it
.\scripts\Test-SpeakLinkHQAutoStart.ps1
#    expect: SPEAKLINK_HQ_AUTOSTART_VERIFIED
```

`SpeakLinkHQRuntime.exe` is a **windowed** application (PE subsystem 2), so there
is no black window on the HQ desk at any point.

### What "it is running" actually means

```powershell
Get-Content "$env:LOCALAPPDATA\SpeakLink\hq-runtime-status.json"
```

| `state` | What is true |
|---|---|
| `READY` | the backend **and** the frontend both answered over HTTP |
| `DEGRADED` | one of them would not stay healthy; the recovery trigger will retry |
| `CONFIG_ERROR` | it refused to start. `detail` says why, in one sentence |
| `STOPPED` | not running |

A process existing in Task Manager is **not** evidence that HQ works. That is
the whole reason this file exists.

### Two limits worth repeating

1. **HQ starts when the HQ user signs in.** After an unattended reboot, somebody
   has to sign in. No setting changes this.
2. Uninstalling keeps every byte of data. Re-installing brings back all 44
   Stores, every user and every Device.
