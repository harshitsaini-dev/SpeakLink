# Rollback plan — HQ

What to do when a new HQ package is worse than the one it replaced.

**The premise this whole plan rests on:** application files are disposable and
data is not. The persistent root — database, keys, configuration, backups, logs
— is 44 Stores, every user account and every Device credential. Rolling back an
application is a five-minute operation. Losing that root is not recoverable by
any procedure in this document.

So: **no rollback step below deletes, moves, re-initializes or writes to the
persistent root.**

---

## Before you install anything

1. Note the package you are running now:

   ```powershell
   Get-Content "$env:LOCALAPPDATA\EchoCast-AI\hq-app\manifest.json" |
       ConvertFrom-Json | Select-Object version, source_commit_short
   ```

2. Confirm the previous package folder still exists in `artifacts\`. **Old
   evidence is never overwritten** — the builder refuses to write over an
   existing package directory — so it should.

3. Take a database backup and verify it is readable:

   ```powershell
   .\Test-EchoCastPersistentLanServer.ps1
   ```

If step 2 or 3 cannot be completed, do not install. There is no rollback
without something to roll back to.

---

## Rolling back

### 1. Stop the current runtime

```powershell
Stop-ScheduledTask -TaskName "EchoCast HQ Runtime"
```

Confirm it is gone:

```powershell
Get-CimInstance Win32_Process -Filter "Name = 'EchoCastHQRuntime.exe'"
```

### 2. Verify the package you are rolling back TO

```powershell
.\Test-EchoCastHQPackage.ps1 -PackagePath "artifacts\<previous-package>"
```

Expect `ECHOCAST_HQ_PACKAGE_VERIFIED`. A package that does not verify is not a
rollback target — it is a second problem.

### 3. Re-install it

```powershell
.\Install-EchoCastHQAutoStart.ps1 -PackagePath "artifacts\<previous-package>"
```

This replaces the application files and re-registers the task. It does not touch
the persistent root.

### 4. Start and verify

```powershell
Start-ScheduledTask -TaskName "EchoCast HQ Runtime"
.\Test-EchoCastHQAutoStart.ps1
```

Expect `ECHOCAST_HQ_AUTO_START_VERIFIED`, and the status file to read `READY`:

```powershell
Get-Content "$env:LOCALAPPDATA\EchoCast-AI\hq-runtime-status.json"
```

`READY` means both children answered over HTTP. A running process is not
evidence.

### 5. Verify a Store, not just the server

A rolled-back HQ that starts is not a working HQ. Confirm at least one Receiver
reaches `CONNECTED`, and run one test broadcast with a human listening.
`CONNECTED`, `PLAYBACK_CONFIRMED` and *somebody heard it* are three different
facts — do not merge them in the rollback log.

---

## If the roll-forward already changed the database

Application rollback does **not** undo a schema migration.

1. Stop the runtime (step 1 above).
2. Do **not** delete the current database. Move nothing.
3. Copy the pre-upgrade backup **beside** it, under a new name, and compare the
   two read-only:

   ```powershell
   python tools\compare_databases.py --left <current> --right <backup>
   ```

   `compare_databases.py` opens both `mode=ro&immutable=1` and recommends; it
   does not decide, and it cannot write.
4. Choosing which database HQ keeps for ever is an **operator decision with a
   typed confirmation**, not a script's decision. Read the comparison first.

## If the persistent root itself is damaged

Do not run `Initialize-EchoCastPersistentLanServer.ps1`. It creates a server;
running it against a damaged one is how an empty database that starts cleanly
replaces a real one.

```powershell
.\Test-EchoCastPersistentLanServer.ps1      # read-only diagnosis
.\Repair-EchoCastPersistentLanServer.ps1 -DryRun
```

Restore from `<persistent-root>\backups\` by copying a backup to a **new** path
and pointing at it deliberately.

---

## Uninstalling entirely

```powershell
.\Uninstall-EchoCastHQAutoStart.ps1
```

Removes the task and the application files. **Keeps every byte of data.**
Re-installing later brings HQ back with all its Stores, users and Devices.

`-RemovePersistentData` is declared and refuses to run. That is deliberate.

---

## What rollback cannot fix

| Situation | Why not |
|---|---|
| A rotated Receiver HMAC key container | Every Device credential was signed with the old one. Every Store must re-enrol |
| A replaced signing secret | Every session is invalid. Everybody signs in again (recoverable, but visible) |
| Stores enrolled against the new version | Enrolment is recorded in the database, not in the application |
| A deleted persistent root | Nothing in this document recovers it |
