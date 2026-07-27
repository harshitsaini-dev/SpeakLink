# Receiver Device Enrolment

How a Windows Receiver computer is meant to get its own credential, what exists
today, and what is still blocked.

**Status: partially implemented. `NOT_READY_FOR_PRODUCTION`.**

## Why a Store token is not enough

A Store is a broadcast target. A Receiver Device is one Windows computer that
plays audio for that Store. They are different things, and today the system
conflates them.

Every Receiver for a Store presents the same secret — the raw 32-hex value in
`stores.receiver_token`. That has three consequences:

1. Two computers in the same shop are indistinguishable to the backend. The
   Receiver Status page can say "UN is online"; it cannot say *which machine*.
2. Revoking one Receiver revokes them all. Regenerating the Store token kicks
   every computer for that Store at once.
3. Getting the credential onto a machine meant copying it out of the UI. Store
   Management rendered `${origin}/receiver?token=<credential>` behind a Copy
   button, so a long-lived shared secret travelled through a clipboard, and
   through any chat message, browser history entry or log that saw the link.

Point 3 is fixed. Points 1 and 2 need the rest of this document.

## What exists today

### One-time enrolment codes — implemented

`backend/receiver_enrollment_codes.py`, table `receiver_enrollment_codes`
(created by `create_all`, no migration needed).

An administrator creates a code for one Store, reads it once, and types it into
one computer.

| Property | Behaviour |
| --- | --- |
| Material | 24 bytes from `secrets.token_urlsafe` |
| Stored | SHA-256 verifier only — never the code |
| Lifetime | 900 s (`CODE_TTL_SECONDS`) |
| Uses | exactly one |
| Concurrency | conditional `UPDATE`, so a race has exactly one winner |
| Refusal | never echoes the supplied value |
| Store state | refused for an unknown or inactive Store, including one disabled after the code was issued |

A plain SHA-256 verifier is correct here, unlike a password: the code is 24
bytes of urandom, so there is nothing to brute-force. What matters is that a
database copy cannot be replayed into an enrolment.

Covered by 25 tests in `backend/tests/test_receiver_enrollment_codes.py`,
including a threaded race proving exactly one redemption wins.

### Device and credential tables — written, never applied

`backend/migrations.py` `run_receiver_credential_phase_one` creates
`receiver_devices`, `receiver_credentials`, `receiver_credential_events` and
`receiver_credential_migration_state`, with foreign keys, CHECK constraints and
indexes. `backend/receiver_device_service.py` `enroll_receiver_device` issues one
device credential once, and `backend/receiver_credentials.py` defines the
versioned token format `speaklink_rcv_v1.<uuid>.<secret>` with HMAC-SHA256
verifiers and a key ring.

All of it is exercised by tests. **None of it has ever run against a live
database** — the pilot database contains only `stores`, `hq_users`,
`broadcast_sessions`, `broadcast_targets`, `receiver_events` and `system_logs`.

## What is blocking the second half

Redeeming a code must produce a device credential, and issuing one requires the
HMAC key ring. There is deliberately no runtime mechanism to supply it, because
`RECEIVER_HOSTING_KEY_STORAGE_ADR.md` already decided how that key must be held:

> DPAPI-protected versioned HMAC-key container outside Git and SQLite,
> restricted by ACL to the dedicated service identity … Only non-secret
> key-version metadata lives in normal application configuration.

Putting the key in an environment variable would contradict that approved
decision, and the ADR still lists the DPAPI protection-scope choice as an open
implementation prerequisite.

So the enrolment code layer stops where it should: it establishes **who may
ask**. Wiring redemption to `enroll_receiver_device` is the next branch, and it
starts with key custody, not with an endpoint.

## Status: built

Everything described below now exists and is proven end to end by
`tools/receiver_device_staging_smoke.py` — 32 checks against a real backend with
a real Agent subprocess and real Windows DPAPI. The operator-facing procedure is
`WINDOWS_RECEIVER_AGENT_RUNBOOK.md`.

## The cutover, and how to know it is safe

The migration period is explicit and has exactly one exit. During it,
`DualRuntimeAuthenticator` (`backend/receiver_runtime_auth.py`) tries the Device
credential **first** and falls back to the legacy per-Store token, so a Store
stops depending on its shared token the moment one of its computers presents a
real credential — no flag, no restart.

**Cutover is deleting that class**, along with `--legacy-pilot-mode` in
`tools/receiver_agent.py`. Do it when all three are true:

1. Every active Store has at least one enrolled Device, and one of them is
   promoted to primary. `GET /api/stores/{id}/receiver-devices/roles` per Store.
2. No connection has authenticated with `legacy_store_token` for a full trading
   week. The runtime records which transport proved each identity precisely so
   this question has an answer instead of an opinion.
3. The migration state has advanced past `dual_verify` to `hash_only`.

Until then, deleting it takes every Store still on a shared token off the air at
the moment of the switch.

### One constraint worth knowing before you plan the cutover

Enrolment and rotation are allowed in `legacy_only`, `dual_verify`, `hash_only`
and `raw_neutralized` — the states where the credential they issue can actually
be verified — and **refused in `backfilled`**, where hashed credentials are not
verified and a newly issued credential would silently fail to authenticate.

This was measured, not assumed: enrolment used to be pinned to `legacy_only`
alone, which meant a cut-over server could never enrol a new till.

## The flow

```
1. HQ administrator, authenticated          POST /api/receiver-devices/enrollment-codes
                                            -> { code, expires_in }   (code shown once)

2. Operator types the code into one Windows Receiver computer
   - through stdin or a local prompt, never a URL, never a command argument

3. Receiver agent                           POST /api/receiver-devices/enroll
   body: { code, device_name, hostname, software_version }
                                            -> { credential }         (returned once)

4. Agent stores the credential under %LOCALAPPDATA%\SpeakLink, outside Git

5. Runtime, every reconnect
   Authorization: Bearer <device credential>
   - no re-enrolment, no code, nothing in the URL
```

Device list and read APIs must never return the credential again.

## Browser Receiver page

`frontend/src/pages/Receiver.jsx` is **not routed**. It connected to
`/ws/receiver/{token}` — a backend route that does not exist; the real Receiver
socket is `/api/ws/receiver` and authenticates with an `Authorization` header —
and reaching it required a Store credential in the URL.

The component is kept rather than deleted, because a browser-based Receiver
harness is worth having. It needs rebuilding on top of device enrolment first,
so that it holds a credential belonging to one computer.

Store Management no longer renders a Receiver URL or a Copy button. Credential
rotation stays, because an operator must be able to revoke; what changed is that
the new value is never displayed. Five Playwright tests in
`frontend/e2e/store-management.spec.js` pin this.

## Legacy transition

Nothing has been removed. `stores.receiver_token` and
`LegacyStoreTokenRuntimeAuthenticator` remain the live authentication path, and
`MigrationAwareReceiverRuntimeAuthenticator` still has to be constructed
explicitly with an engine and a key ring — importing the module does not enable
it.

No Device may be enrolled with a legacy Store token, because no Device can be
enrolled at all yet.

## Remaining blockers

1. DPAPI key custody — the prerequisite for issuing any device credential.
2. Applying `run_receiver_credential_phase_one` to a real database, with a
   verified backup and a rehearsed rollback.
3. The two HTTP endpoints above.
4. Windows Receiver agent enrolment: read a code from stdin, store the
   credential under `%LOCALAPPDATA%\SpeakLink`, reconnect without
   re-enrolling. Windows secure storage for that credential is **not**
   implemented.
5. Retiring `stores.receiver_token` once every Store has enrolled Devices.
6. Rebuilding the browser Receiver harness on device credentials.
