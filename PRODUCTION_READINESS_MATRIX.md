# SpeakLink Production Readiness Matrix

**Verdict: `NOT_READY_FOR_PRODUCTION`.**

One honest table for the whole system. Each row says what is actually true
today, not what is intended. Statuses mean exactly this:

| Status | Meaning |
| --- | --- |
| `COMPLETE` | Implemented and covered by automated tests |
| `TESTED_AUTOMATICALLY` | Proven by tests only — no hardware, no production environment |
| `TESTED_ON_REAL_HARDWARE` | Proven with real equipment and an operator present |
| `BLOCKED` | Cannot proceed until a named dependency is resolved |
| `NOT_IMPLEMENTED` | Does not exist |

Last updated: 2026-07-28, branch `release/production-readiness-candidate`.

Evidence behind this revision, all re-run on 2026-07-28:
backend 1490 passed / 1 skipped (twice in parallel, once serial),
Playwright Chromium 128 passed (twice), `yarn build` succeeds,
`SPEAKLINK_LAN_PILOT_FIREWALL_VERIFIED`,
`SPEAKLINK_RECEIVER_PACKAGE_VERIFIED`,
`SPEAKLINK_STORE_PILOT_KIT_VERIFIED`,
`PACKAGED_FFMPEG_CONFIRMED`,
`SPEAKLINK_RECEIVER_EXE_STAGING_SMOKE_PASSED`,
`RECEIVER_DEVICE_ENROLMENT_STAGING_SMOKE_PASSED`,
`LOCAL_PILOT_SMOKE_PASSED`,
`SPEAKLINK_RECEIVER_TASK_SCHEDULER_VERIFIED`,
`SPEAKLINK_PRIVATE_LAN_RECEIVER_EXE_CHECK_PASSED` (43/43).

---

## Authentication and secrets

| Capability | Status | Evidence / blocker |
| --- | --- | --- |
| HQ login, JWT issuance | `TESTED_AUTOMATICALLY` | backend suite |
| **A fresh install has a SUPER_ADMIN** | `COMPLETE` | Fixed here. The role migration ran *before* `seed_admin`, so on an empty database it found nobody to promote and the seeded account kept the legacy role `'admin'`. Measured on a fresh install: no SUPER_ADMIN existed at all, and every `require_super_admin` endpoint answered 403 to everyone — silently. Both migrations now run again after seeding |
| User lifecycle: create, edit, disable, enable, archive, restore | `COMPLETE` | `user_lifecycle.py`; 44 tests. Never deleted — a User is the author of broadcast history |
| Last-SUPER_ADMIN and self-action lock-out rules | `COMPLETE` | The last active SUPER_ADMIN cannot be disabled, archived or demoted; nobody may switch off their own account. A disabled or archived SUPER_ADMIN does not count as cover |
| Password change and administrator reset | `COMPLETE` | Current password required; a reset returns nothing reusable — no echoed password, no link, no token |
| **Offline password change, no running server** | `COMPLETE` | `tools/change_hq_user_password.py`; 31 tests. Added during the 2026-07-30 credential incident, when it turned out nothing could change a password without a running server and a signed-in session. getpass only — no flag, environment variable, file or stdin accepts a password; two named targets, no free-form path; refuses while a backend appears to be running, because a live server keeps serving sessions minted under the old password |
| **JWT secret rotation** | `COMPLETE` | `tools/rotate_jwt_secret.py`; 35 tests. Atomic write, redacted backup outside the repository, fingerprints only — never the value |
| **Secrets in ignored archives** | `COMPLETE` | `test_no_secret_archives_in_tree`; the 2026-07-30 P0 was invisible to every existing scan because they all walk `git ls-files` and the archive was gitignored. This one walks the disk |
| Immediate session revocation | `COMPLETE` | `session_version` bumped by disable, archive, role change, password change and reset; existing JWTs fail on their next request. Renaming somebody deliberately does not sign them out |
| RBAC enforced at the endpoint, not by hidden buttons | `COMPLETE` | 43 endpoint tests calling as the wrong role and expecting 403; `test_rbac_endpoint_matrix` walks the routing table and fails on any undeclared authenticated route |
| User responses carry no hash, session counter or token | `COMPLETE` | Response models written field by field, so adding a column cannot start publishing it. Asserted in both the backend suite and Playwright |
| No default credentials in UI | `COMPLETE` | `e18b525`; production bundle contains no default password |
| Fail-closed administrator bootstrap | `COMPLETE` | `e062e11`; 31 tests; verified live that a missing variable creates zero rows |
| Login rate limiting | `COMPLETE` | `007bd2f`; bounded in-process limiter, injectable clock |
| Account lockout | `COMPLETE` | `007bd2f`; persistent, survives restart |
| Username enumeration closed | `COMPLETE` | identical 401s; unknown-username path now performs a bcrypt comparison |
| HTTP auth is header-only | `COMPLETE` | `6c27833`; 31 tests; no route accepts a credential from a URL |
| WebSocket single-use tickets | `COMPLETE` | `f27bd08`; 19 tests |
| **Receiver HMAC key custody** | `COMPLETE` | `key_custody.py`; 30 tests; **real DPAPI round trip passed on this host** |
| Key container ACL policy and verification | `COMPLETE` | `key_custody_acl.py`; 22 tests; inheritance from `C:\ProgramData` rejected, service Full Control rejected, Administrators recovery required |
| Service-identity install and integration scripts | `COMPLETE` | `Install-SpeakLinkServiceIdentity.ps1` refuses without elevation; `Test-SpeakLinkKeyCustody.ps1` refuses under any other identity — both verified |
| DPAPI **under `SpeakLinkService`** | `BLOCKED` | The account does not exist on this machine, this session is not elevated, and nothing has run as it. Scope decided (`CURRENT_USER`), path decided (`C:\ProgramData\SpeakLink\keys\receiver-hmac-keys.bin`). **Operator gate** — run `Test-SpeakLinkKeyCustody.ps1` as the service account, then again after a service restart, then again after a reboot |
| Production HMAC key generation ceremony | `NOT_IMPLEMENTED` | Needs an approved host and named owners |
| Key backup and recovery rehearsal | `NOT_IMPLEMENTED` | ADR requires a backup separate from the database backup |

## Receiver identity

| Capability | Status | Evidence / blocker |
| --- | --- | --- |
| One-time enrolment codes | `COMPLETE` | `6281aa3`; 25 tests including a threaded race with exactly one winner |
| Receiver Device schema | `TESTED_AUTOMATICALLY` | Written in `migrations.py`; **never applied to any live database** |
| Migration status / preflight / apply tooling | `COMPLETE` | `6c42102`; 21 tests; backup verified by size, SHA-256 and reopen |
| Migration maintenance runbook | `COMPLETE` | `RECEIVER_MIGRATION_RUNBOOK.md`; every command executed against a copy of the pilot database while writing it |
| Migration applied to a real database | `BLOCKED` | Needs an approved maintenance window. Tool refuses the protected database with no override. Neither the protected nor the pilot database is migrated |
| Enrolment HTTP API | `COMPLETE` | `1472628`; six routes; claim-first-then-release ordering proven by a threaded race and a monkeypatched failure |
| Device credential issuance | `COMPLETE` | `1472628`; raw credential leaves the server exactly once; absent from every later read and from `system_logs` |
| Device credential rotation | `COMPLETE` | `d4115d9`; 31 tests; **no overlap window** — the old credential dies at the instant of rotation |
| Per-Device revocation and disable | `COMPLETE` | `d4115d9`, `1472628`; a revoked Device is not rescued by the legacy path — both authenticators must refuse |
| Windows Agent enrolment mode | `COMPLETE` | `8ae39a2`; `tools/receiver_agent.py`, 52 tests; a test found argparse prefix-matching turning `--credential` into `--credential-path` |
| Device credential secure local storage | `COMPLETE` | `8ae39a2`; `tools/receiver_credential_store.py`, 33 tests; **real Windows DPAPI round trip passed**; distinct DPAPI entropy makes crossing it with the HMAC key container impossible, proven both directions |
| Device runtime authentication | `COMPLETE` | `4dc602e`; `DualRuntimeAuthenticator` prefers the Device credential, falls back to the legacy Store token, identical refusal from either path |
| Enrolment allowed where the credential works | `COMPLETE` | `1e16359`; enrolment was pinned to `legacy_only` where credentials cannot be verified — a cut-over server could never enrol a new till. `backfilled` stays refused |
| Device administration UI | `COMPLETE` | `48aa8f5`; 26 Playwright tests; code and credential shown once, absent from URL, both browser storages, and after reload |
| End-to-end proof on a real server | `TESTED_AUTOMATICALLY` | `9902912`; 32-check staging smoke, real backend, real Agent subprocess, real DPAPI, `dual_verify` |
| Legacy `stores.receiver_token` retired | `NOT_IMPLEMENTED` | Still accepted during the explicit migration period. Cutover is deleting `DualRuntimeAuthenticator`; see `RECEIVER_ENROLMENT.md` |

## Broadcast path

| Capability | Status | Evidence / blocker |
| --- | --- | --- |
| HQ microphone capture, WebM/Opus | `TESTED_ON_REAL_HARDWARE` | One-Store amplifier test, 2026-07-26 |
| Bounded per-Store queue, drop-oldest | `TESTED_AUTOMATICALLY` | 0 dropped at 5/10/20/40 synthetic Stores |
| FFmpeg decode to a Windows output device | `TESTED_ON_REAL_HARDWARE` | 533 chunks, 159.42 s decoded, return code 0 |
| Audible on an amplifier and speakers | `TESTED_ON_REAL_HARDWARE` | `OPERATOR_LIVE_AUDIO_OBSERVATION = Haan, clear` — one Store only |
| READY gates the microphone | `COMPLETE` | Playwright: no `getUserMedia` while `ready_receivers` is empty |
| Honest play status | `COMPLETE` | `f533c06`; command-sent is never shown as Playing |
| Two Stores on real hardware | `NOT_IMPLEMENTED` | 1 of 44 Stores tested |
| Store vs Device status separation | `COMPLETE` | `6147d6e`; two Devices in one Store are two inventory records; the Store aggregate follows the primary, not the best of its Devices |
| Primary / standby Device policy | `TESTED_AUTOMATICALLY` | `6147d6e`; one primary enforced by a PRIMARY KEY, not by check-then-write; standby receives zero chunks — measured in the staging smoke at 17 chunks to the primary and 0 to a genuinely connected standby |
| Primary / standby **health attribution** | `TESTED_AUTOMATICALLY` | `3c3d945`; 16 tests. Audio isolation was always right; health was not — a standby's acks landed in the Store snapshot, so a standby heartbeat kept a switched-off primary reading online. Was recorded as one defect, was four. **Do not run a Store with primary + standby simultaneously until the two-machine manual acceptance passes** |
| No silent failover | `COMPLETE` | `6147d6e`; disabling or revoking the primary clears the role and promotes nothing; the dashboard warns instead |
| Synthetic load with Device credentials | `NOT_IMPLEMENTED` | `tools/load_test_receivers.py` still drives legacy Store tokens only. The 5/10/20/40 figures below predate the Device model |

## LinkGuard

| Capability | Status | Evidence / blocker |
| --- | --- | --- |
| Acoustic verification **message contract** | `COMPLETE` | `receiver_contract.py` — `TrustedSpeakerVerifiedEvent` with `source: Literal["linkguard"]`, parsed by a separate adapter |
| A Receiver cannot claim `speaker_verified` | `COMPLETE` | The trusted event is excluded from the ordinary acknowledgement union; a Receiver presenting one is rejected |
| Pause / resume **contract** | `COMPLETE` | `linkguard.py`; 19 contract tests against a fake adapter |
| Pause / resume **implementation** | `BLOCKED` | No LinkGuard executable, service or IPC surface exists to call. `NullLinkGuard` reports `UNAVAILABLE`, never `PAUSED` |
| Acoustic speaker verification in practice | `NOT_IMPLEMENTED` | The contract exists; nothing produces such an event. `SPEAKER_VERIFIED` is claimed by no report |

## Deployment

| Capability | Status | Evidence / blocker |
| --- | --- | --- |
| Exactly one Uvicorn worker | `COMPLETE` | Required; connection state is process-local |
| Loopback-only pilot | `TESTED_AUTOMATICALLY` | |
| Private-LAN pilot on `192.168.4.134` | `TESTED_AUTOMATICALLY` | `SPEAKLINK_PRIVATE_LAN_RECEIVER_EXE_CHECK_PASSED`, 43/43 — backend and frontend both bound to the LAN address, packaged EXE enrolling, broadcasting, rotating, revoking. **Same computer only**; no second desktop has run any of it |
| Standalone Receiver package (no Python, no FFmpeg install) | `TESTED_AUTOMATICALLY` | `SPEAKLINK_RECEIVER_PACKAGE_VERIFIED` (22/22), `SPEAKLINK_RECEIVER_FRESH_BUILD_VERIFIED`, `PACKAGED_FFMPEG_CONFIRMED` from the Agent's own process tree |
| Self-contained Store pilot kit | `TESTED_AUTOMATICALLY` | `SPEAKLINK_STORE_PILOT_KIT_VERIFIED` (37/37). Closes a real gap: the runbook told operators to run `.\scripts\…`, which exists only in this repository |
| HTTPS / WSS | `NOT_IMPLEMENTED` | Needs approved reverse proxy or termination layer. The LAN pilot sends the Device credential over plain HTTP and says so, loudly, at every start |
| Production CORS policy | `NOT_IMPLEMENTED` | Pilot configuration only |
| Shared rate-limit storage | `BLOCKED` | Required before any multi-worker deployment |
| Receiver auto-start at logon | `TESTED_AUTOMATICALLY` | `SPEAKLINK_RECEIVER_TASK_SCHEDULER_VERIFIED` (22/22). At-Logon only — **not** a service, **not** before logon, **not** as SYSTEM, **not** on a locked desktop with nobody signed in |
| Receiver recovery after a crash | `TESTED_AUTOMATICALLY` | A repetition schedule, bounded to one day, plus `MultipleInstances = IgnoreNew`. **Correction:** an earlier version of the runbook claimed `RestartCount` did this. It does not — Windows applies it to a task that fails to *start*, not to one whose program exits non-zero. Measured with `cmd /c exit 1`, `RestartCount 2`: never re-ran |
| One Receiver per credential | `COMPLETE` | Byte-range lock scoped to the credential path; duplicate launch exits `4`. Proven cross-process with a real killed holder, and through the packaged EXE on the LAN |
| Windows service / boot-before-logon | `NOT_IMPLEMENTED` | Needs a different design: the Receiver plays into a user session, and session 0 has no audio device |
| Structured audit events | `TESTED_AUTOMATICALLY` | Login and Receiver events exist; enrolment and Device events do not |
| Vercel frontend readiness | `NOT_IMPLEMENTED` | Build works; no rewrites, environment contract or deployment checklist |
| Staging deployment validation | `NOT_IMPLEMENTED` | |

## Process discipline

| Capability | Status | Evidence |
| --- | --- | --- |
| Protected database never touched | `COMPLETE` | `backend/speaklink_live.db`: 507,904 bytes, SHA-256 `8C858B13…BD2EF2AB`, `LastWriteTime` `2026-07-26 14:13:13.819`, WAL and SHM absent. Verified before and after every phase on 2026-07-28. **Correction:** a cleanup on 2026-07-27 checked `backend/speaklink.db`, which is not the protected file and does not exist. The check was passing on nothing. Every verification now names the exact path and compares the full hash |
| Complete process-tree shutdown | `COMPLETE` | `4aa9b2a`; verified live — 7 frontend and 3 backend processes stopped, both ports released |
| Secret-free repository | `COMPLETE` | `git grep` finds no historical default, JWT, bcrypt hash or key material |

---

## Mandatory gates that no test can pass on your behalf

1. DPAPI behaviour under the final dedicated Windows service identity, including ACLs.
2. Phase 1 migration against an operator-approved staging or production database.
3. Real HTTPS/WSS public staging.
4. LinkGuard pause/resume against actual LinkGuard. The contract is defined in
   `backend/linkguard.py`; **no pause/resume implementation, executable or IPC
   surface exists in this repository** to satisfy it. Needed from outside: how
   LinkGuard is invoked (process, service, HTTP, named pipe), how it
   acknowledges, its idempotency guarantees, and its behaviour across restarts.
5. LinkGuard acoustic verification.
6. Two real Store Receiver computers, two amplifiers, two speaker systems.
7. Bluetooth or wired stability over hours, and clarity under real store noise.
8. Vercel Preview connected to a real staging backend.
9. Production deployment approval.

## What this campaign changed

| Phase | Result |
| --- | --- |
| 1. HMAC key custody | **Delivered** — DPAPI container, 30 tests, real round trip passed |
| 2. Migration tooling | **Delivered** — status/preflight/apply, verified backup, 21 tests |
| 3. Enrolment HTTP API | **Delivered** — `1472628` |
| 4. Windows Agent enrolment | **Delivered** — `8ae39a2` |
| 5. Rotation and revocation | **Delivered** — `d4115d9` |
| 6. Store vs Device status | **Delivered** — `6147d6e` |
| 7. LinkGuard | **Contract delivered**, implementation blocked — no pause/resume surface exists to call. An acoustic-verification message contract already existed and was found only after an over-broad claim was caught by its own test |
| 8. Windows auto-start | Not built |
| 9. Backend deployment security | Partially pre-existing; not completed |
| 10. Audit logging | Partially pre-existing; not extended |
| 11. Frontend production completion | Partially pre-existing; not completed |
| 12. Vercel readiness | Not built |

Phases 1 and 2 were the keystone: nothing in phases 3, 4 or 5 can exist without
a key to sign credentials with and a schema to store them in. Everything since
was built test-first, with RED captured before the fix, and committed and pushed
per phase rather than accumulated as one large uncommitted change.

---

## Two measured findings that changed the architecture

Both were found by running the system rather than reading it, and both were
approved as explicit architecture decisions before anything was changed.

**Enrolment and authentication were mutually exclusive.**

| operation | `legacy_only` | `backfilled` | `dual_verify` | `hash_only` |
| --- | --- | --- | --- | --- |
| enrol a Device | was yes | no | **was no** | **was no** |
| rotate a credential | was yes | no | **was no** | **was no** |
| authenticate a Device | **no** | **no** | yes | yes |

`receiver_device_service` pinned enrolment to `legacy_only`; `receiver_auth_service`
verifies hashed Device credentials only in `dual_verify`, `hash_only` and
`raw_neutralized`. The sets were disjoint, so a server that had cut over could
never enrol a new till — the Device would be created, an operator would type in
the credential, and it would never connect, with no error pointing at the cause.
Enrolment now follows the states where the credential it issues can be used.
`backfilled` stays refused: hashed credentials are not verified there either, so
enrolling into it is the same trap one state along.

**Primary-plus-standby was arithmetically impossible.** Legacy backfill creates a
Device of its own, and the per-Store limit was two, leaving room for exactly one
enrolled Device on any backfilled Store. The limit is now three, holding exactly
the migration-period trio: legacy backfilled Device, primary, standby. It is
still a bound — the fourth is refused, including under four threads enrolling at
the same instant.

---

## Commit record note

Commit `e7670ee` carries three files but a message describing only the matrix.
A shell here-string containing quotes was mangled, the intended LinkGuard commit
failed, and the next commit swept all three files up. The content is correct; the
message under-describes it. It was already pushed, so the record is corrected
here rather than by rewriting shared history.

- `backend/linkguard.py` — the pause/resume contract
- `backend/tests/test_linkguard_contract.py` — 19 contract tests
- `PRODUCTION_READINESS_MATRIX.md` — this table

