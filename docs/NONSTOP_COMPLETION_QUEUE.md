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
full backend suite                1687 passed, 2 skipped, 0 FAILED
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
| 4 | User Management (full CRUD + delete) | `NOT STARTED` | backend lifecycle exists and is tested; **hard delete, dependency summary and the frontend forms do not exist** |
| 5 | Store Management (full CRUD + delete) | `NOT STARTED` | lifecycle exists; **hard delete, dependency rules and the frontend dialogs do not exist** |
| 5 | Receiver Device onboarding UI | `NOT STARTED` | backend endpoints exist; token countdown, states and safe refusal categories do not |
| 7 | `EchoCastHQRuntime.exe` + HQ task | `NOT STARTED` | a new GUI supervisor executable; substantial |
| 8 | `EchoCastStoreSetup.exe` wizard | `NOT STARTED` | a new GUI application; substantial |
| 9 | Audio/WebSocket/queue audit | `NOT STARTED` | not examined this session |
| 10 | Security audit document | `NOT STARTED` | not examined this session |
| 12 | Load tests 2/5/10/20/40 | `NOT STARTED` | needs a running persistent server |
| 12 | Playwright / E2E | `BLOCKED` | the live pilot holds port 3000 |

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
