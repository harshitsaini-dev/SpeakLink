# Completion queue

Updated 2026-07-29 · Branch `feature/persistent-hq-and-one-click-store-setup`

Status values: `NOT STARTED` · `IN PROGRESS` · `AUTOMATED PASS` ·
`OPERATOR CHECKPOINT` · `BLOCKED` · `COMPLETE`

---

## Automated phases

| # | Phase | Status | Evidence |
|---|---|---|---|
| 0 | Preflight and queue | `COMPLETE` | tree clean, source SHA measured, live pilot untouched |
| 1 | Persistent HQ | `AUTOMATED PASS` | initialized + `SPEAKLINK_PERSISTENT_SERVER_VERIFIED` 11/11; Initialize/Start/Stop/Test/**Repair** all present |
| 2 | Protected baseline | `COMPLETE` | rebaselined under every authorized condition; chain recorded; suite green |
| 3 | Database source decision | `COMPLETE` | `tools/compare_databases.py`, 16 tests; recommends `backend/speaklink_live.db` |
| 6 | Store recovery diagnostics | `AUTOMATED PASS` | `scripts/Test-SpeakLinkStoreRecovery.ps1`, read-only, exercised |
| 12 | Automated release gate | `AUTOMATED PASS` | see below |

### Release gate result

```
compileall backend tools          exit 0
python -m pip check               No broken requirements found
full backend suite                1758 passed, 2 skipped, 0 FAILED
Playwright (chromium)             155 passed, 0 FAILED
frontend production build         Done
Receiver package verification     SPEAKLINK_RECEIVER_PACKAGE_VERIFIED
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
| 7 | `SpeakLinkHQRuntime.exe` supervisor | `AUTOMATED PASS` | `tools/hq_runtime.py` + `hq_runtime.spec`; **entry point added** - the earlier build defined a supervisor and never called it; 54 tests |
| 7 | HQ auto-start scripts | `AUTOMATED PASS` | Install/Test/Repair/Uninstall-SpeakLinkHQAutoStart.ps1; 73 tests; dry-run and four refusal paths executed for real against an isolated task |
| 7 | HQ versioned package | `AUTOMATED PASS` | `Build-`/`Test-SpeakLinkHQPackage.ps1`; 40 tests; RC package built and verified 32/32 |
| 7 | HQ documentation | `AUTOMATED PASS` | `HQ_RUNTIME_DESIGN.md`, `HQ_AUTO_START.md`, `ROLLBACK_PLAN.md` |
| 7 | HQ live task installation | `OPERATOR CHECKPOINT` | deliberately not installed; registering and starting are separate decisions |
| 8 | `SpeakLinkStoreSetup.exe` wizard | `NOT STARTED` | a new GUI application; substantial |
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
| 1 | P0 | `SpeakLinkHQRuntime.exe` had **no entry point**. Built, packaged and correctly verified WINDOWS_GUI, it imported, defined classes and exited 0 — which Task Scheduler records as success. Green task history, no window, no HQ | writing the entry-point tests |
| 2 | P0 | The runtime refused a correctly initialized persistent profile because `keys\receiver-hmac-keys.bin` was absent, and told the operator to "restore it". **Nothing in this repository creates it before the first start** — the refusal could not be satisfied by any documented procedure | running the packaged `--check` against the real profile |
| 3 | P0 | The same refusal, again, in `Install-SpeakLinkHQAutoStart.ps1`. Fixed in Python and left in PowerShell | dry-running the installer against the real profile |
| 4 | P1 | `sys.executable` inside a frozen build is the supervisor itself, so the backend command would have relaunched `SpeakLinkHQRuntime.exe` with `-m uvicorn` — and the spec excludes uvicorn deliberately | reasoning about the frozen layout, confirmed by test |
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
| `SpeakLinkStoreSetup.exe` | `NOT STARTED` |
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
(SpeakLinkStoreSetup.exe built and verified).

| Item | Status | Evidence |
|---|---|---|
| Receiver status file (CONNECTED evidence) | `AUTOMATED PASS` | write_status/read_status deduplicated into receiver_agent.py; 5 tests |
| store_setup_core.py (connection/enrolment/audio/install logic) | `AUTOMATED PASS` | 25 tests, no GUI import |
| store_setup_gui.py (4 screens + Rerun) | `IN PROGRESS` | 13 tests; Rerun screen buttons are placeholders except Replace Device Identity |
| SpeakLinkStoreSetup.exe | `AUTOMATED PASS` | PE subsystem 2, started and showed a real window titled "SpeakLink Store Setup" |
| Receiver package location (Phase 1 of this sprint) | `AUTOMATED PASS` | locate_verified_receiver_package(); built SpeakLinkReceiver-1.0.0-7e6d704, verified 2 ways (PowerShell 20+ checks + Python locator) |
| StoreSetup package (Build-/Test-SpeakLinkStoreSetupPackage.ps1) | `NOT STARTED` | |
| Store task/recovery automated tests (Phase 2 of the sprint brief) | `NOT STARTED` | |
| Receiver enrollment status evidence, USED state (Phase 3) | `NOT STARTED` | |
| Audio/WebSocket/queue audit (Phase 4) | `NOT STARTED` | |
| `docs/SECURITY_AUDIT.md` (Phase 5) | `NOT STARTED` | |
| Load tests 2/5/10/20/40 (Phase 7) | `NOT STARTED` | |
| Final Release Candidate artifacts (Phase 8) | `NOT STARTED` | |

### What "install" cannot yet do end to end

**RESOLVED.** `InstallScreen` now calls `core.locate_verified_receiver_package()`, which finds the newest `SpeakLinkReceiver-*` package under `artifacts/` and re-verifies it independently (PE subsystems, every hash, no forbidden file) before installing from it. A real package was built (`SpeakLinkReceiver-1.0.0-7e6d704-20260730-124134`) and verified both by `Test-SpeakLinkReceiverPackage.ps1` (20+ checks) and the new locator. `Build-SpeakLinkReceiver.ps1` itself had never been run end to end on this branch and its PyInstaller call aborted on PyInstaller's own stderr under PowerShell's ErrorActionPreference=Stop - fixed the same way every other chatty native-command call in this repository already is.

A real end-to-end enrollment through the wizard against a running HQ instance has still not been run - that needs a live HQ to enrol against, which is an operator/network setup step beyond this sprint's automatable scope.

### Rerun screen buttons not yet wired

Status, Repair, Change Audio Output, Test Sound (from the rerun screen),
Restart/Stop Receiver, Redacted Diagnostics, Export Redacted Diagnostics, and
Open Log Folder exist as real buttons (a test asserts they are all present) but
call `_not_yet_wired` - a placeholder, not a silent no-op that pretends to
work. Replace Device Identity is the one fully wired action.


---

## StoreSetup completion sprint - 2026-07-30

Commits `9657558`, `11a95c6`, `bbab632`, `34f6a6e`.

| Item | Status | Evidence |
|---|---|---|
| Rerun-screen actions wired (no placeholders) | `AUTOMATED PASS` | `_not_yet_wired` gone; 21 GUI tests; a test greps for the string so it cannot return |
| Task start/stop/status seam | `AUTOMATED PASS` | `Manage-SpeakLinkStoreReceiverTask.ps1`; 9 tests, incl. staged Start/Stop both refusing an impostor task |
| StoreSetup end-to-end vs real backend | `AUTOMATED PASS` | 12 tests, real FastAPI routes + real credential store; 3 mutations proved they can fail |
| Store Scheduled Task requirements in CI | `AUTOMATED PASS` | 49 tests; previously only checked against a live installed task |
| Receiver reconnect / backoff / revocation | `AUTOMATED PASS` (pre-existing) | `test_receiver_agent.py` - deliberately not duplicated |
| `SpeakLinkStoreSetup.exe` rebuilt from current source | `AUTOMATED PASS` | PE subsystem 2; launched, real window, 0 new conhost processes |
| Enrollment USED state + setup progress (Phase 4) | `NOT STARTED` | |
| Audio/WebSocket bounded-queue audit (Phase 5) | `NOT STARTED` | |
| `docs/SECURITY_AUDIT.md` (Phase 6) | `NOT STARTED` | |
| Playwright / frontend build this sprint | `NOT RUN` | not re-run; no frontend code changed |
| Load tests 2/5/10/20/40 (Phase 7) | `NOT STARTED` | |
| Final Release Candidate artifacts (Phase 8) | `NOT STARTED` | |

### P0/P1 found and fixed this sprint

| # | Sev | Finding | Found by |
|---|---|---|---|
| 1 | P0 | `_replace_identity` passed `core.CONFIRMATION_WORD` as the answer to its own typed-confirmation check, so the check could never fail | driving the window |
| 2 | P0 | The modal guarding it returned `True` with nothing typed - the headless/automated environment fires the dialog's default button. Measured: credential deleted with no input | running a script against the real app |
| 3 | P1 | Every `tk.StringVar`/`BooleanVar` had no master, binding to tkinter's global `_default_root`; after repeated root creation the next `tk.Tk()` failed. Intermittent setup error, full parallel suite only | the parallel suite |
| 4 | P2 | `Uninstall-...ps1 -RemoveCredential` never said the HQ Device is not revoked, though the Python helper always had. A Store that looks enrolled on the dashboard and is silent | the new task tests |
| 5 | P2 | `stop_receiver` returned `InstallState.CONNECTED` to mean "the stop succeeded" | writing it |

Findings 1+2 compounded: either alone would have destroyed a Store's Device
identity on a stray click; together they made an unconfirmable destructive
action look carefully guarded.

### Still an operator checkpoint

A real end-to-end enrollment against a *running* HQ with a *real* code, on real
Store hardware. Everything up to that is automated, including the whole chain
against the real backend routes.

---

## Enrollment evidence and queue audit - 2026-07-30 (later)

Commits `1981f78`, `8788628`, `2cac9b6`.

| Item | Status | Evidence |
|---|---|---|
| Enrollment USED state - backend | `AUTOMATED PASS` | `GET /api/stores/{id}/enrollment-codes`; 24 tests; migration proven on a legacy table |
| Enrollment USED state - frontend | `AUTOMATED PASS` | panel polls the record; 9 new Playwright tests; Playwright 164 passed fresh |
| Evidence-backed setup progress | `AUTOMATED PASS` | 5 stages, each on its own evidence; no stage inferred from elapsed time |
| Bounded per-Store queues | `AUTOMATED PASS` (pre-existing) | `audio_streaming.py` + 29 tests in `test_audio_protocol.py` - not rewritten |
| Queue high-water mark | `AUTOMATED PASS` | `max_depth` added; 11 new tests incl. five Stores and no-stale-audio-across-sessions |
| Emergency Stop clears queues | `AUTOMATED PASS` (pre-existing) | `_end_session` calls `stop_audio_fanout()` before clearing live state |
| Playwright Chromium | `AUTOMATED PASS` | 164 passed, 0 failed - run fresh this sprint |
| Frontend production build | `AUTOMATED PASS` | Done - run fresh this sprint |
| `docs/SECURITY_AUDIT.md` (Phase 6) | `NOT STARTED` | |
| Load tests 2/5/10/20/40 (Phase 7) | `NOT STARTED` | needs a running persistent server |
| Final Release Candidate artifacts (Phase 8) | `NOT STARTED` | |

### P0/P1 found and fixed

| # | Sev | Finding | Found by |
|---|---|---|---|
| 1 | P1 | `except Exception` swallowed a `NameError` from a missing `text` import, so the public-id→device-id lookup silently returned `{}` and `DEVICE_CONNECTED` was **unreachable**. The test asserting its absence passed for the wrong reason | adding the test that proves the stage CAN appear |
| 2 | P1 | `_enrollment_progress` gated `PRIMARY_ASSIGNED` (a **stored** fact) behind `DEVICE_CONNECTED` (a **live** one), hiding a promotion that had definitely happened | a test |
| 3 | P1 | Role comparison used `"primary"` against `DeviceRole.PRIMARY` (`"PRIMARY"`) - silently always false | a test |
| 4 | P2 | Queue metrics had no `max_depth`; sampled `depth` cannot distinguish "never queued" from "filled and drained" | the audit |

### Deliberately NOT added

**No REVOKED state.** Nothing in this schema can revoke an enrollment code - no
column, no service function, no route. The label would have nothing behind it.
Device revocation is a different thing and already exists on the Device.

### Still an operator checkpoint

A real enrollment against a running HQ with a real code, on real Store hardware.


---

## Security audit, load tests and RC rebuild - 2026-07-30 (final)

Commits `9e84dca`, `4eb865f`, `9e6d83b`, and the audit-document commit.

| Item | Status | Evidence |
|---|---|---|
| Security audit (14 areas) | `COMPLETE` | [docs/SECURITY_AUDIT.md](SECURITY_AUDIT.md) - 398 file-visits, adversarial refutation pass |
| P1: broadcaster uplink authorization | `FIXED` | ticket audience + double permission check; 14 tests |
| P1: enrolment cap counted dead codes | `FIXED` | expiry term added; 3 tests |
| P1: HQ start script orphaned -ArgumentList | `FIXED` | parser-verified; structural guard over 42 scripts |
| **P0: live JWT_SECRET in speaklink-live.zip** | **`OPERATOR CHECKPOINT`** | guard test RED by design; rotate + delete required |
| P1: standby acks share the primary snapshot | `DEFERRED` | see below - must be fixed before any two-Device Store |
| Audio metrics endpoint | `AUTOMATED PASS` | `GET /api/broadcast/audio-metrics`, VIEW_STATUS, 9 tests |
| Load tests 2/5/10/20/40 | `COMPLETE` | [docs/LOAD_TEST_REPORT.md](LOAD_TEST_REPORT.md) - all five levels clean |
| Fresh artifacts (HQ, Receiver, StoreSetup, Store kit) | `AUTOMATED PASS` | all four rebuilt from source and verified |
| Playwright chromium | `AUTOMATED PASS` | 164 passed, run fresh |
| Frontend production build | `AUTOMATED PASS` | Done, run fresh |

### DEFERRED, and why it is written here rather than lost in a commit

**Standby Device acknowledgements are applied to the primary's Store snapshot.**
`server.py:1898-1902` passes only `store_id` to `apply_receiver_payload`, with no
`device_id` and no standby branch, so a primary and a standby in one Store write
to a single snapshot whose sequence check then rejects roughly half of each
other's messages - including the primary's `playback_confirmed`.

It only manifests when a Store runs a primary AND a standby at the same time,
which no Store does today. The correct fix keys health state by
`(store_id, device_id)` and aggregates through the existing
`store_aggregate_state` - a change to the live status model that deserves its own
commit and its own tests.

**This must be fixed before any Store runs two Devices.**

### The gate does not pass, deliberately

One mandatory test is RED: `test_no_secret_archives_in_tree.py`, because the
current live JWT signing secret is in an archive in the working tree. That is a
true statement about the system. It goes green when the archive is removed and
the credential rotated - not by editing the test.
