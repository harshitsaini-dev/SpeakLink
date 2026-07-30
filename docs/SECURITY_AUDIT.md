# Security audit

| | |
|---|---|
| Date | 2026-07-30 |
| Commit audited | `9e6d83b` |
| Method | 14 independent read-only audits of the real code, then a separate adversarial pass instructed to **refute** every P0/P1 claim, defaulting to refuted |
| Files inspected | 398 file-visits across 14 areas |
| Findings | 1 **P0** · 6 P1 (3 distinct) · 45 P2 · 64 P3 · 15 accepted risks · 92 verified-correct |

Every P0/P1 below was re-verified by hand before anything was changed. Findings
the adversarial pass refuted are recorded as refuted, not quietly dropped.

---

## P0 — ACTION REQUIRED BY A HUMAN

### The current live JWT signing secret is in an archive in the repository folder

**`echocast-live.zip`** (repository root, dated 7 July) contains `backend/.env`.
The `JWT_SECRET`, `ADMIN_USERNAME` and `ADMIN_PASSWORD` inside it are
**byte-identical to the current live values** — confirmed by comparing SHA-256
fingerprints, never by printing them:

```
JWT_SECRET     : SAME VALUE = True   (fingerprint e78c272f53ed)
ADMIN_PASSWORD : SAME VALUE = True   (fingerprint af8f3e90f5e1)
ADMIN_USERNAME : SAME VALUE = True   (fingerprint 3d9a13ea8e39)
```

**Impact.** The signing secret *is* the authentication system. Anyone holding
this file can mint a valid JWT for any account — including OWNER — without
touching a password, and every rate limit, lockout and audit control is
irrelevant to a token that verifies. The admin password is in the same file.

**Why every existing scan missed it.** It was never committed. `.gitignore`
matches `*.zip` and `git ls-files` confirms it is untracked — and every
repository-wide secret scan enumerates through `git ls-files`. An ignored file is
invisible to all of them. *"Not in git"* is not *"not in the folder somebody zips
up and emails."*

**Not fixed automatically, deliberately.** Deleting an operator's archive is
irreversible and rotating a live credential is an operational decision. Both are
human checkpoints.

**Remediation, in this order:**

1. Rotate `JWT_SECRET` in `backend/.env` and in
   `%LOCALAPPDATA%\EchoCast-AI\persistent-lan-server\keys\jwt-secret.txt`.
   Every existing session is invalidated — that is the intended effect.
2. Change the `admin` account password.
3. Delete `echocast-live.zip`.
4. Review anywhere that folder has been copied, zipped, emailed or backed up
   since 7 July.

**Guard added:** [`test_no_secret_archives_in_tree.py`](../backend/tests/test_no_secret_archives_in_tree.py)
fails while any archive in the tree contains a `.env`, a database or a key
container. It names the entry and deletes nothing. **It is currently RED, and it
should stay RED until step 3 is done** — see *Gate status*.

> Related, lower severity: `backups/echocast_live-20260729-160359.db` is a full
> copy of the protected production database in the tree. Gitignored, so never
> committed, but it travels with the folder. Same class of exposure, no secrets
> of its own beyond the data.

---

## P1 — fixed in `9e6d83b`

### 1. The microphone uplink had no authorization at all

Reported independently by three of the fourteen areas.

**Was:** `/api/ws/broadcaster` redeemed a handshake ticket, **discarded the
returned user id**, and began accepting audio. No permission, no role lookup, no
re-read. `POST /api/auth/ws-ticket` was authenticated-only for every role and
minted a ticket carrying nothing but a user id, so **one ticket opened both the
dashboard and the uplink**.

**Impact.** A `VIEWER` — read-only by definition, refused by every broadcast HTTP
route — could mint a ticket over the ordinary API, connect to the uplink, and push
arbitrary WebM/Opus audio to the loudspeakers of every targeted Store. Or occupy
the single uplink slot and deny it to the operator who was allowed to use it. In a
retail announcement system that is unauthorised audio on a shop floor.

**Evidence:** `server.py:1981` bare `ws_ticket_store.redeem(ticket)` with the
return value unassigned, contrasted with `server.py:1956` in `ws_hq` which does
assign it; `ws_tickets.py:50-58` stored only `(user_id, expiry)`;
`rbac.py:107` VIEWER holds `VIEW_STATUS`/`VIEW_HISTORY` only.

**Fix.** Tickets are scoped to one socket (`AUDIENCE_HQ` / `AUDIENCE_BROADCASTER`),
`audience` is required with no default anywhere, and the permission is checked
**twice**: `START_BROADCAST` to mint, and again at the handshake against a freshly
loaded account. A right verified only at mint time is verified once, and an
operator can be demoted or disabled in the seconds before connecting. A mismatched
audience still **spends** the ticket — leaving it usable would make the mismatch a
free oracle.

**Tests:** 14 in [`test_ws_ticket_audience.py`](../backend/tests/test_ws_ticket_audience.py).
RED first (`ImportError: cannot import name 'AUDIENCE_BROADCASTER'`).

### 2. A Store could be locked out of enrolment permanently

**Was:** the per-Store outstanding-code cap counted every unredeemed code with no
expiry term (`receiver_enrollment_api.py:139-146`), and nothing in the codebase
prunes, deletes or marks an expired code — `redeemed_at_epoch` stays `NULL`
forever.

**Impact.** Three abandoned codes and that Store can never be enrolled again. An
ordinary sequence across 44 Stores: an admin clicks Generate during a failed setup
visit, again the next day, again a week later. The refusal even advised waiting
for them to expire, which could never help.

The intent was always live codes — the constant's own comment says *"a primary, a
standby and one spare **in flight**"* and warns of *"a haystack of **live**
credentials"*. Only the filter disagreed.

**Fix.** The count now includes `expires_at_epoch > time.time()`, and the message
says "live". **Tests:** 3, RED first.

### 3. A comment stopped the HQ server launching

**Was:** `Start-EchoCastPersistentLanServer.ps1:125` ended in a backtick
continuation followed by a `#` comment. The escaped newline joins them into one
logical line, so the comment swallowed every parameter after it.

Confirmed with the PowerShell parser: **zero parse errors**, `Start-Process` with
**three** elements (`-FilePath $python`), and a separate command literally named
`-ArgumentList`.

**Impact.** At runtime that launches a bare interactive Python REPL in a visible
window on the HQ desk. No uvicorn, no `--host`, no `--workers 1`, no log
redirection — on the documented operator path for starting the persistent server.
Valid PowerShell doing something entirely unlike what it reads as, which is why it
survived. The comment that broke it was explaining a *previous* launcher bug about
quoting `-ArgumentList` values.

**Fix.** Comment moved above the statement; `Start-Process` now parses with 14
elements. **Guard:** a structural check over all 42 scripts — any `CommandAst`
whose name begins with `-` is an orphaned parameter, whatever caused it.

### Refuted P1 claims (2)

Two claims were withdrawn by the adversarial pass with `file:line` proof that the
concern was already handled elsewhere. They are not listed as findings because
they were not findings.

### Not fixed: standby acknowledgements share the primary's snapshot

`server.py:1898-1902` calls `apply_receiver_payload(store_id, ...)` with no
`device_id` and no standby branch, so a primary and a standby in one Store write
to one snapshot (`ws_manager.py:347-363`). Their sequence counters are independent
and interleaved, so each rejects some of the other's messages with
`NON_MONOTONIC_SEQUENCE` — including the primary's `playback_confirmed`.

**Deferred, with reasons.** The correct fix keys health state by
`(store_id, device_id)` and aggregates through the already-written
`store_aggregate_state` — a change to the live status model that deserves its own
change with its own tests, not a corner of a security sprint. It only manifests
when a Store runs a primary *and* a standby simultaneously, which no Store does
today. **It must be fixed before any two-Device Store goes live**, and it is
recorded in the completion queue rather than in a commit message.

---

## Area-by-area result

Each row: what was inspected, and what came out. "Verified correct" means a
control was checked against the code and holds.

| # | Area | Files | Verified correct | P2 | P3 | Accepted |
|---|---|---|---|---|---|---|
| 1 | Password security | 25 | 7 | 3 | 6 | 1 |
| 2 | Authentication / JWT | 25 | 5 | 2 | 4 | 1 |
| 3 | Authorization / RBAC | 18 | 6 | 2 | 2 | 1 |
| 4 | Login abuse controls | 17 | 9 | 5 | 4 | 2 |
| 5 | Enrollment codes | 21 | 8 | 2 | 3 | 1 |
| 6 | Receiver credentials | 35 | 6 | 3 | 4 | 2 |
| 7 | WebSocket security | 25 | 8 | 2 | 4 | 2 |
| 8 | HTTP / API configuration | 27 | 2 | 2 | 3 | 1 |
| 9 | Database | 32 | 7 | 2 | 7 | 2 |
| 10 | Windows execution | 30 | 6 | 4 | 5 | 0 |
| 11 | Packaging | 26 | 10 | 7 | 3 | 0 |
| 12 | Frontend | 39 | 5 | 4 | 8 | 1 |
| 13 | Logging and audit | 33 | 6 | 7 | 5 | 1 |
| 14 | Dependencies / deployment | 45 | 9 | 6 | 6 | 0 |

### Confirmed strong

Worth recording, because an audit that lists only problems misrepresents the
system:

* **Password hashing** — bcrypt cost 12, per-password salt, ~0.22 s per hash,
  `checkpw` failing closed on a malformed hash (`auth.py:20-28`).
* **Timing-safe unknown usernames** — an unknown or inactive account still pays a
  full bcrypt comparison (`login_guard.py:132-149`), so timing does not enumerate.
* **One generic 401** for every login failure; throttle and lock share one 429
  carrying no counter or threshold.
* **`session_version`** compared on every request, and a token with no `sv` is
  rejected because the default is 0 and the stored minimum is 1 (`auth.py:94-96`).
* **Response models enumerate fields explicitly** rather than dumping the ORM
  object, so `password_hash` cannot appear by someone adding a column.
* **No raw token in any URL** — the JWT is header-only; the one URL credential is
  a single-use 20-second ticket.
* **Enrollment codes** — 24 bytes of `secrets`, SHA-256 verifier, redemption as a
  conditional `UPDATE` so two racing computers cannot both win.
* **Receiver credentials** — verifier-only storage, DPAPI sealing on the till, no
  API that reads a raw credential back.
* **SQL** — no string-formatted SQL anywhere; every dynamic value is bound.
* **No `shell=True`** anywhere in `tools/` or `backend/`.
* **Task and process safety** — ownership verified before modifying a Scheduled
  Task; PID command lines verified before signalling.

### Notable P2s to schedule

Not exploitable now, but each is a real defect:

* **Logout is a no-op** and the frontend never calls it (`server.py`) — sign-out
  is client-side token disposal only.
* **`redact()` does not match the real enrolment-code format**, so the
  handler-level safety net is inert for codes. The call sites are careful; the net
  behind them is not.
* **Enrolment refusal categories rarely match the messages the services raise**,
  so audit lines are mostly `INVALID_STATE`.
* **`hq_users` ids are reused after a hard delete**, so a stale token could
  authenticate as a different, newer account.
* **Username enumeration is not fully closed** — the account lock fires before the
  burst limiter, so only a real account can produce a 429 from a single client.
* **`register_failed_login` is a non-atomic read-modify-write**; concurrent
  failures are lost.
* **Revoke/disable does not revoke the Device credential** or close its socket.
* **The HQ package ships frontend source maps** (~1.9 MB of unminified client
  source).
* **The reverse SHA-256 direction** (unlisted-file-present) exists only for the HQ
  package, not the Receiver package or Store kit.
* **No request-body size limit** — a single worker buffers an entire
  unauthenticated POST body.
* **`requirements.txt` describes 86 pins not installed** in the running venv.
* **No code signing** on any shipped executable.

### Accepted risks (15)

Recorded as accepted because they are consequences of a deliberate, documented
posture rather than defects: private-LAN plain HTTP for the pilot, one Uvicorn
worker with process-local WebSocket state, in-memory rate limiting that resets on
restart, HQ requiring an interactive sign-in, and simulated Receivers never
proving audibility.

---

## Public deployment: BLOCKED

Unchanged by this audit, and none of it is implemented:

| Condition | Status |
|---|---|
| HTTPS | **Not implemented** — plain HTTP |
| WSS | **Not implemented** — plain `ws://` |
| Restricted CORS | Configurable; pilot allows the LAN origin |
| Secrets outside the source tree | **Violated** — see P0 |
| Rate limiting that survives restart | In-memory only |
| Firewall | Scripts exist, not verified in this audit |
| Encrypted backups | Not implemented |
| Monitoring / alerting | Not implemented |
| Credential rotation policy | Not documented |
| Incident response | Not documented |

`normalise_backend_url` already enforces the pilot boundary in code: plain HTTP is
refused unless the target is a literal RFC1918 address **and** matches the
operator's declared `--expected-hq-host`. A public address is refused always.

## Gate status

**The security gate does NOT pass**, and it must not be made to pass by editing a
test.

One mandatory test is RED —
[`test_no_secret_archives_in_tree.py`](../backend/tests/test_no_secret_archives_in_tree.py)
— because a live JWT signing secret is sitting in an archive in the working tree.
That is a true finding about the real state of the system. Deleting the file or
weakening the assertion would turn a genuine exposure into a green tick.

It goes green when the P0 remediation above is done. Nothing else is required.

## Re-running this audit

The audit is 14 independent read-only passes plus an adversarial refutation pass.
The instruction that made it useful was *"default to refuted=true"*: it removed 8
of 14 initial P0/P1 claims, and every survivor came with `file:line` proof I could
check myself. Two survivors were also mislabelled — the archive finding arrived as
**P2** and is P0 in practice, which is why severities were re-judged by hand
rather than taken from the report.
