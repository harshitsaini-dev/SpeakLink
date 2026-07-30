# Completion queue

Updated 2026-07-29 · Branch `feature/persistent-hq-and-one-click-store-setup`

Status values: `NOT STARTED` · `IN PROGRESS` · `AUTOMATED PASS` ·
`OPERATOR CHECKPOINT` · `BLOCKED` · `COMPLETE`

---

## Automated phases

| # | Phase | Status | Evidence |
|---|---|---|---|
| 0 | Preflight and queue | `COMPLETE` | tree clean, source SHA measured, live pilot untouched |
| 1 | Persistent HQ | `AUTOMATED PASS` | initialized + `ECHOCAST_PERSISTENT_SERVER_VERIFIED` 11/11; Initialize/Start/Stop/Test/**Repair** all present |
| 2 | Protected baseline | `COMPLETE` | rebaselined under every authorized condition; chain recorded; suite green |
| 3 | Database source decision | `COMPLETE` | `tools/compare_databases.py`, 16 tests; recommends `backend/echocast_live.db` |
| 6 | Store recovery diagnostics | `AUTOMATED PASS` | `scripts/Test-EchoCastStoreRecovery.ps1`, read-only, exercised |
| 12 | Automated release gate | `AUTOMATED PASS` | see below |

### Release gate result

```
compileall backend tools          exit 0
python -m pip check               No broken requirements found
full backend suite                1758 passed, 2 skipped, 0 FAILED
Playwright (chromium)             155 passed, 0 FAILED
frontend production build         Done
Receiver package verification     ECHOCAST_RECEIVER_PACKAGE_VERIFIED
  background EXE = WINDOWS_GUI    PASS
  operator EXE   = WINDOWS_CUI    PASS
Store kit verification            43 checks passed
persistent server verification    11 checks passed
secret scan (tracked files)       clean
git diff --check / git status     clean
protected DB                      8A7E3413…B1A547CA, no WAL/SHM
```

---

## Phases NOT completed, and why

Stated plainly rather than left to read as passed.

| # | Phase | Status | Why |
|---|---|---|---|
| 1 | Persistent HQ frontend serving | `NOT STARTED` | needs the HQ runtime below |
| 4 | User hard delete + dependency summary | `AUTOMATED PASS` | `backend/deletion_safety.py`, `GET /api/users/{id}/dependencies`, permanent-delete route; 25 tests |
| 4 | User Management frontend | `AUTOMATED PASS` | Add/Edit/Reset/Enable/Disable/Archive/Restore + dependency dialog + typed-confirmation delete; 38 Playwright tests |
| 5 | Store hard delete + dependency summary | `AUTOMATED PASS` | same module, `GET /api/stores/{id}/dependencies`, permanent-delete route |
| 5 | Store Management frontend | `AUTOMATED PASS` | Add/Edit/Enable/Disable/Archive/Restore + dependency dialog + typed short-code delete; 8 Playwright tests |
| 5 | Receiver onboarding: refusal categories | `AUTOMATED PASS` | `backend/enrolment_refusal.py`; category logged, wire response stays generic; 17 tests |
| 5 | Receiver onboarding: code countdown/state | `AUTOMATED PASS` | live countdown, UNUSED/EXPIRED, value removed on expiry; 11 Playwright tests |
| 5 | Receiver onboarding: setup-progress + USED state | `NOT STARTED` | needs backend evidence the page does not receive |
| 7 | `EchoCastHQRuntime.exe` supervisor | `AUTOMATED PASS` | `tools/hq_runtime.py` + `hq_runtime.spec`; **entry point added** - the earlier build defined a supervisor and never called it; 54 tests |
| 7 | HQ auto-start scripts | `AUTOMATED PASS` | Install/Test/Repair/Uninstall-EchoCastHQAutoStart.ps1; 73 tests; dry-run and four refusal paths executed for real against an isolated task |
| 7 | HQ versioned package | `AUTOMATED PASS` | `Build-`/`Test-EchoCastHQPackage.ps1`; 40 tests; RC package built and verified 32/32 |
| 7 | HQ documentation | `AUTOMATED PASS` | `HQ_RUNTIME_DESIGN.md`, `HQ_AUTO_START.md`, `ROLLBACK_PLAN.md` |
| 7 | HQ live task installation | `OPERATOR CHECKPOINT` | deliberately not installed; registering and starting are separate decisions |
| 8 | `EchoCastStoreSetup.exe` wizard | `NOT STARTED` | a new GUI application; substantial |
| 9 | Audio/WebSocket/queue audit | `NOT STARTED` | not examined this session |
| 10 | Security audit document | `NOT STARTED` | not examined this session |
| 12 | Load tests 2/5/10/20/40 | `NOT STARTED` | needs a running persistent server |
| 12 | Playwright / E2E | `AUTOMATED PASS` | port 3000 freed; 144 passed |

**None of the above should be read as "passed". They were not done.**

---

## Operator checkpoints outstanding

| # | Checkpoint | Status |
|---|---|---|
| 1 | Review persistent PlanOnly | `COMPLETE` — reviewed and applied |
| 2 | Stop the throwaway pilot | `OPERATOR CHECKPOINT` |
| 3 | Start persistent HQ | `OPERATOR CHECKPOINT` |
| 4 | `owneradmin` browser login | `OPERATOR CHECKPOINT` |
| 5 | `admin` browser login | `OPERATOR CHECKPOINT` |
| 9 | Final Store A re-enrolment | `OPERATOR CHECKPOINT` |
| 10–22 | audible, recovery, reboot, two-Store, restore drill | `OPERATOR CHECKPOINT` |

---

## Verdict

**`GREEN_FOR_MANUAL_ACCEPTANCE` — partial scope.**

Every automated gate that exists is green, and the persistent HQ foundation is
built and verified. It is **not** `GREEN_FOR_CONTROLLED_TWO_STORE_PILOT`,
because Phases 4, 5, 7, 8, 9 and 10 have not been done, and no physical Store
test has been run.

---

## HQ phase — completed 2026-07-29

Commits `0efe7b8` (runtime entry point + auto-start scripts), `e0cc648`
(versioned package), and the documentation commit that follows them.

### P0/P1 found and fixed

| # | Severity | Finding | Found by |
|---|---|---|---|
| 1 | P0 | `EchoCastHQRuntime.exe` had **no entry point**. Built, packaged and correctly verified WINDOWS_GUI, it imported, defined classes and exited 0 — which Task Scheduler records as success. Green task history, no window, no HQ | writing the entry-point tests |
| 2 | P0 | The runtime refused a correctly initialized persistent profile because `keys\receiver-hmac-keys.bin` was absent, and told the operator to "restore it". **Nothing in this repository creates it before the first start** — the refusal could not be satisfied by any documented procedure | running the packaged `--check` against the real profile |
| 3 | P0 | The same refusal, again, in `Install-EchoCastHQAutoStart.ps1`. Fixed in Python and left in PowerShell | dry-running the installer against the real profile |
| 4 | P1 | `sys.executable` inside a frozen build is the supervisor itself, so the backend command would have relaunched `EchoCastHQRuntime.exe` with `-m uvicorn` — and the spec excludes uvicorn deliberately | reasoning about the frozen layout, confirmed by test |
| 5 | P1 | `Path(__file__).parents[1]` inside a frozen build is the unpacked bundle, so the packaged runtime looked for the React build inside itself | running the packaged `--check` |
| 6 | P1 | The installer and the runtime **disagreed** about where the frontend lives (`frontend\index.html` vs `frontend\build\index.html`), so a package that installed cleanly could not start | writing the test that holds them to one answer |
| 7 | P1 | `$PSScriptRoot` is empty inside a `param()` block under `powershell -File`, so package-builder defaults failed only under automated invocation | the package tests |
| 8 | P2 | The package verifier read 400 characters after `New-ScheduledTaskAction` looking for a literal executable name and reported FAIL on a correct installer that passes a variable | running the verifier on a real package |
| 9 | P2 | `tools/persistent_lan_server.py` opened with a docstring containing `\lan-pilot` — an invalid escape sequence warned about on every parse | the byte-order-mark suite's warning output |

**Findings 2 and 3 are the same defect in two languages.** Fixing one and not
searching for the other is how it survived; the rule now lives in one place
per concern — the installer requires the database and the `keys` folder, and
whether a *missing* container is normal or an emergency is decided by the
runtime at start, because only it can count the enrolled Devices.

### The rule that came out of finding 2

A new **signing secret** costs everybody one sign-in. A new **HMAC key
container** costs 44 Stores a re-enrolment, silently, while every Store still
looks enrolled. So the signing secret is created if absent and never replaced;
the key container is never created, and a missing one is refused only when
Devices are actually enrolled against it. An unreadable database refuses rather
than reporting zero — "I could not count them" must never become "there are
none".

### Still not done

| Phase | Status |
|---|---|
| `EchoCastStoreSetup.exe` | `NOT STARTED` |
| Store task/recovery tests (Phase 5 of the sprint brief) | `NOT STARTED` |
| Receiver onboarding: USED state + setup progress | `NOT STARTED` |
| Audio/WebSocket/queue P0/P1 audit | `NOT STARTED` |
| `docs/SECURITY_AUDIT.md` | `NOT STARTED` |
| Load tests 2/5/10/20/40 | `NOT STARTED` |
| Live HQ task installation, reboot/sign-in, locked desktop | `OPERATOR CHECKPOINT` |


---

## StoreSetup phase — started 2026-07-30

Commits `da9c0e8` (Receiver status file), `ed4b58b` (store_setup_core.py),
`5717306` (store_setup_gui.py + a real threading bug it caught), `8710ef1`
(EchoCastStoreSetup.exe built and verified).

| Item | Status | Evidence |
|---|---|---|
| Receiver status file (CONNECTED evidence) | `AUTOMATED PASS` | write_status/read_status deduplicated into receiver_agent.py; 5 tests |
| store_setup_core.py (connection/enrolment/audio/install logic) | `AUTOMATED PASS` | 25 tests, no GUI import |
| store_setup_gui.py (4 screens + Rerun) | `IN PROGRESS` | 13 tests; Rerun screen buttons are placeholders except Replace Device Identity |
| EchoCastStoreSetup.exe | `AUTOMATED PASS` | PE subsystem 2, started and showed a real window titled "EchoCast Store Setup" |
| Receiver package location (Phase 1 of this sprint) | `AUTOMATED PASS` | locate_verified_receiver_package(); built EchoCastReceiver-1.0.0-7e6d704, verified 2 ways (PowerShell 20+ checks + Python locator) |
| StoreSetup package (Build-/Test-EchoCastStoreSetupPackage.ps1) | `NOT STARTED` | |
| Store task/recovery automated tests (Phase 2 of the sprint brief) | `NOT STARTED` | |
| Receiver enrollment status evidence, USED state (Phase 3) | `NOT STARTED` | |
| Audio/WebSocket/queue audit (Phase 4) | `NOT STARTED` | |
| `docs/SECURITY_AUDIT.md` (Phase 5) | `NOT STARTED` | |
| Load tests 2/5/10/20/40 (Phase 7) | `NOT STARTED` | |
| Final Release Candidate artifacts (Phase 8) | `NOT STARTED` | |

### What "install" cannot yet do end to end

**RESOLVED.** `InstallScreen` now calls `core.locate_verified_receiver_package()`, which finds the newest `EchoCastReceiver-*` package under `artifacts/` and re-verifies it independently (PE subsystems, every hash, no forbidden file) before installing from it. A real package was built (`EchoCastReceiver-1.0.0-7e6d704-20260730-124134`) and verified both by `Test-EchoCastReceiverPackage.ps1` (20+ checks) and the new locator. `Build-EchoCastReceiver.ps1` itself had never been run end to end on this branch and its PyInstaller call aborted on PyInstaller's own stderr under PowerShell's ErrorActionPreference=Stop - fixed the same way every other chatty native-command call in this repository already is.

A real end-to-end enrollment through the wizard against a running HQ instance has still not been run - that needs a live HQ to enrol against, which is an operator/network setup step beyond this sprint's automatable scope.

### Rerun screen buttons not yet wired

Status, Repair, Change Audio Output, Test Sound (from the rerun screen),
Restart/Stop Receiver, Redacted Diagnostics, Export Redacted Diagnostics, and
Open Log Folder exist as real buttons (a test asserts they are all present) but
call `_not_yet_wired` - a placeholder, not a silent no-op that pretends to
work. Replace Device Identity is the one fully wired action.
