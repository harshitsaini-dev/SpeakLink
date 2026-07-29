# EchoCast codebase audit

Date: 2026-07-29 · Branch: `feature/persistent-hq-and-one-click-store-setup`

**Scope honesty first.** The brief asked for a full audit plus Phases 1–8 in two
hours. That is not achievable, and pretending otherwise would produce a document
that reads complete and is not. This audit covers what was **measured**, and
says plainly what was not looked at.

---

## 1. Executive summary

One P0 was found, reproduced live, and fixed. It is the single cause of both
symptoms the operator reported as separate bugs.

| | |
|---|---|
| **P0 fixed** | HQ created a new empty database on every start |
| **P1 open** | Store Device identity lives in an older pilot database |
| **P1 open** | `owneradmin` is in the protected DB, not in the running server |
| **Blocked gates** | protected-database baseline moved (expected, needs a decision) |
| **Verdict** | `BLOCKED` — see §9 |

---

## 2. Active runtime truth (measured, not remembered)

| question | answer | evidence |
|---|---|---|
| DB the live backend uses | `...\lan-pilot\20260729-181918\lan-pilot.db` | newest WAL activity; two `uvicorn` processes on `192.168.4.134:8000` |
| DB containing `owneradmin` | `backend\echocast_live.db` | inventory: users = `admin` (ADMIN), `owneradmin` (OWNER) |
| DB containing the working Store Device | `...\lan-pilot\20260729-115328\lan-pilot.db` | Device `1f5a6c77… 'AYUSH'` |
| Current server kind | **THROWAWAY PILOT** | root path carries a timestamp |
| Databases found on this machine | 14 | all `integrity_check: ok` |

No password hash, Device credential or token verifier was read or printed at any
point.

---

## 3. P0 findings

### P0-1 — HQ forgets everything on every start · **FIXED**

* **Evidence** — `scripts/Start-EchoCastLanPilot.ps1`
  * L92 `$pilotRoot = ...\lan-pilot\$(Get-Date -Format 'yyyyMMdd-HHmmss')`
  * L104 `$adminUsername = "lan-pilot-$(6 random chars)"`
* **Measured** — eight pilot roots exist, each with a different generated
  administrator. The root changed again *between two consecutive investigations
  during this sprint*.
* **Impact** — every restart discards all Stores, Devices, users and history.
  This is the sole cause of both reported symptoms:
  * Store OFFLINE after restart — its Device is not in the current database;
  * `owneradmin` cannot sign in — that account is not in the current database.
* **Fix** — `tools/persistent_lan_server.py` plus four operator scripts. One
  fixed root, no date, no generated administrator, and a refusal to create an
  empty database.
* **Test** — `backend/tests/test_persistent_lan_server.py`, 29 tests, RED first.
* **Status** — FIXED in code. **The operator has not yet run `-Apply`**, so the
  running server is still the throwaway pilot.

---

## 4. P1 findings

| id | finding | status |
|---|---|---|
| P1-1 | Store Device identity is in an older pilot DB; adopting it needs proven schema/key compatibility or one final re-enrolment | **DEFERRED** — needs the operator's decision, see §9 |
| P1-2 | `owneradmin` exists but in a different database from the running server | **RESOLVED BY P0-1** once the persistent server is initialized and started |
| P1-3 | Protected-database baseline moved after `create_owner` succeeded | **OPEN** — deliberately not auto-rebaselined |
| P1-4 | Unquoted interpolated values in a `Start-Process -ArgumentList` in my own new script | **FIXED** — caught by an existing repository guard |

---

## 5. What was NOT audited

Stated plainly rather than left to be assumed:

* frontend source, tests and E2E — not read this sprint
* User Management create/edit/delete flows beyond what already has tests
* Store hard-delete dependency rules — **not implemented, not designed**
* Receiver Device audit (PRIMARY/STANDBY, rotation, revoke) beyond existing tests
* WebSocket queue bounds, heartbeat and status accuracy
* microphone/broadcast console flow
* security sweep (CORS, JWT, rate limiting, dependency versions)
* `EchoCastHQRuntime.exe`, `EchoCastStoreSetup.exe` — **not built**
* HQ auto-start scheduled task — **not implemented**
* synthetic Receiver load runs
* frontend production build and Playwright — not run this sprint

None of the above should be read as "passed". They were not examined.

---

## 6. Test gate

```
compileall backend tools                 exit 0
new persistent-server tests              29 passed (RED captured first)
full backend suite                       1666 passed, 2 skipped, 1 FAILED
git diff --check                         clean
```

The one failure is `test_the_protected_database_matches_its_recorded_baseline`.
It moved because `create_owner` succeeded against that database and added
`owneradmin` plus the `session_version` column. Content verified intact:
`integrity_check ok`, `admin` + `owneradmin`, 13 Stores, 17 sessions, 194 logs.
**Not auto-rebaselined** — that is the operator's call.

---

## 7. Files inspected this sprint

`scripts/Start-EchoCastLanPilot.ps1` · `tools/lan_pilot.py` ·
`backend/tests/test_pilot_scripts.py` · `backend/tests/test_protected_database_isolation.py` ·
every `.db` under the repository and `%LOCALAPPDATA%\EchoCast-AI` (read-only) ·
plus the files carried forward from previous sprints
(`tools/receiver_agent.py`, `tools/audio_receiver_pilot.py`, `backend/rbac.py`,
`backend/user_lifecycle.py`, `backend/user_schema.py`, `tools/create_owner.py`).

---

## 8. Files added

* `tools/persistent_lan_server.py`
* `scripts/Initialize-EchoCastPersistentLanServer.ps1`
* `scripts/Start-EchoCastPersistentLanServer.ps1`
* `scripts/Stop-EchoCastPersistentLanServer.ps1`
* `scripts/Test-EchoCastPersistentLanServer.ps1`
* `backend/tests/test_persistent_lan_server.py`
* this document

---

## 9. Verdict

**`BLOCKED`**

Blocking items:

1. The persistent server has been built but **not initialized or started**.
   Until `-Apply` runs, HQ is still the throwaway pilot.
2. The Store's working Device is in an older pilot database. Either it is
   imported with proven schema and key compatibility, or the Store is enrolled
   once more into the persistent database. **That decision is the operator's**,
   and this audit does not make it silently.
3. The protected-database baseline needs an explicit decision again.
4. Everything in §5 is unexamined.

No claim is made here that any Store reconnects, that any sound was heard, or
that this is deployable.
