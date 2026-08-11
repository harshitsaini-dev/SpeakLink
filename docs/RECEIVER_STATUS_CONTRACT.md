# SpeakLink Receiver Status and Acknowledgement Contract

Version: `1.0`

Status: pure contract implemented; runtime integration is pending

This contract defines how SpeakLink describes receiver health without confusing
network connection, device readiness, software playback, or acoustic speaker
verification. The implementation in `backend/receiver_contract.py` has no
FastAPI, WebSocket, database, frontend, Receiver Agent, or audio-streaming side
effects.

## Independent state axes

A receiver snapshot is composed of four independent axes. A more advanced state
must never be inferred from a less advanced one.

| Axis | States |
| --- | --- |
| Connection | `OFFLINE`, `CONNECTED`, `NETWORK_ERROR` |
| Readiness | `UNKNOWN`, `READY`, `DEVICE_ERROR` |
| Playback | `STOPPED`, `AUDIO_RECEIVING`, `PLAYBACK_CONFIRMED`, `PLAYBACK_ERROR` |
| Acoustic | `UNVERIFIED`, `SPEAKER_VERIFIED` |

`UNKNOWN` and `UNVERIFIED` are explicit non-healthy states. They must not be
rendered as READY, PLAYBACK_CONFIRMED, or SPEAKER_VERIFIED.

## State meanings

- `OFFLINE`: the server has no usable authenticated receiver connection.
- `CONNECTED`: the server accepted an authenticated receiver WebSocket.
- `NETWORK_ERROR`: the connection has not produced a validated message within
  the stale threshold.
- `UNKNOWN`: readiness has not been reported for the current connection or was
  cleared by stale/error recovery.
- `READY`: required software checks and output-device checks passed.
- `DEVICE_ERROR`: a receiver reported a bounded, typed device failure.
- `STOPPED`: no software playback is currently confirmed for the active session.
- `AUDIO_RECEIVING`: the receiver reports that live audio chunks are arriving.
- `PLAYBACK_CONFIRMED`: the receiver software pipeline reports that it processed
  audio. This is not proof that a physical speaker produced sound.
- `PLAYBACK_ERROR`: the active software playback pipeline failed.
- `UNVERIFIED`: no trusted acoustic evidence exists for the active session.
- `SPEAKER_VERIFIED`: LinkGuard, through the separate trusted-verifier path,
  reported acoustic speaker output for the active session.

Bluetooth or output-device connection is not acoustic proof.

## Trust boundaries

- The server derives `OFFLINE` and `NETWORK_ERROR`.
- Only authenticated WebSocket acceptance establishes `CONNECTED`.
- Ordinary receiver messages cannot assert `CONNECTED`, `OFFLINE`,
  `NETWORK_ERROR`, or `SPEAKER_VERIFIED`.
- `speaker_verified` is excluded from `parse_receiver_ack` and is accepted only
  by the separate trusted-verifier schema and reducer function.
- Receiver `occurred_at` values are informational. Server-generated UTC
  `received_at` values control ordering and freshness.
- PLAY or START command dispatch changes no receiver-state axis.
- No password, JWT, receiver token, or other credential belongs in an
  acknowledgement, snapshot, exception, or log.

## Ordinary receiver acknowledgement envelope

Every ordinary acknowledgement contains:

| Field | Rule |
| --- | --- |
| `protocol_version` | Must equal `1.0` |
| `type` | Must be one of the supported receiver types below |
| `message_id` | UUID used for bounded per-connection deduplication |
| `occurred_at` | Timezone-aware UTC; non-UTC and naive values are rejected |
| `sequence` | Non-negative and strictly increasing per connection |
| `session_id` | Positive integer required for session-scoped media messages |

Supported ordinary receiver types:

- `receiver_ready`
- `audio_receiving`
- `playback_confirmed`
- `playback_error`
- `device_error`
- `stopped`
- `heartbeat`

All schemas reject unexpected fields.

### Type-specific fields

- `receiver_ready` requires `software_checks_passed=true` and
  `output_device_checks_passed=true`.
- `audio_receiving`, `playback_confirmed`, `playback_error`, and `stopped`
  require the matching active `session_id`.
- Error messages require an uppercase machine-readable `error_code` of at most
  64 characters and printable `details` of at most 512 characters.
- `recoverable` is an optional error boolean and defaults to false.
- `stopped.reason` is optional, printable, and limited to 128 characters.

Control characters are rejected rather than rewritten, preventing multiline
log injection if details are logged by a future integration.

## Trusted acoustic verifier envelope

`speaker_verified` uses a separate Pydantic schema and parser. It requires the
common envelope, a matching `session_id`, and `source="linkguard"`. Ordinary
receiver parsing rejects this message type. This reservation defines no
LinkGuard transport or API.

## Allowed transitions

### Connection

```text
OFFLINE -> CONNECTED
CONNECTED -> NETWORK_ERROR
NETWORK_ERROR -> CONNECTED
CONNECTED -> OFFLINE
NETWORK_ERROR -> OFFLINE
```

### Readiness

```text
UNKNOWN -> READY
UNKNOWN -> DEVICE_ERROR
READY -> DEVICE_ERROR
DEVICE_ERROR -> READY
disconnect/stale -> UNKNOWN
```

### Playback

```text
STOPPED -> AUDIO_RECEIVING
AUDIO_RECEIVING -> PLAYBACK_CONFIRMED
AUDIO_RECEIVING -> PLAYBACK_ERROR
PLAYBACK_CONFIRMED -> PLAYBACK_ERROR
any playback state -> STOPPED, only for a matching stopped acknowledgement
```

`AUDIO_RECEIVING` requires a fresh READY acknowledgement. Playback errors clear
readiness to UNKNOWN, so a new `receiver_ready` acknowledgement is required
before audio reception can begin again.

### Acoustic verification

```text
UNVERIFIED -> SPEAKER_VERIFIED
```

Only the trusted verifier path can perform this transition. A stale connection
or disconnect resets acoustic state to UNVERIFIED for the live snapshot.

Invalid, duplicate, out-of-order, skipped, wrong-session, and non-UTC inputs
raise explicit typed exceptions or Pydantic validation errors. They never
silently advance state.

## Heartbeat and stale-state policy

| Policy | Value |
| --- | ---: |
| Receiver heartbeat interval | 5 seconds |
| Stale boundary | 15 seconds |
| Offline boundary | 30 seconds |

The boundaries are inclusive and use server receipt time:

- Before 15 seconds: retain the current connection state.
- At exactly 15 seconds: set connection to `NETWORK_ERROR` and clear readiness,
  playback, and acoustic health claims.
- At exactly 30 seconds: set connection to `OFFLINE` and clear the active
  session and all health claims.
- A validated message can recover `NETWORK_ERROR` to `CONNECTED`, but it cannot
  restore READY, PLAYBACK_CONFIRMED, or SPEAKER_VERIFIED implicitly.
- Heartbeat refreshes connection freshness only.

The reducer does not persist heartbeats or audio chunks. Runtime persistence
and coalescing policy remain a separate integration decision.

## Non-goals for version 1.0

- No changes to `server.py`, `ws_manager.py`, models, or SQLite.
- No frontend status changes.
- No Windows Receiver Agent.
- No audio streaming or playback implementation.
- No LinkGuard API or transport design.
- No claim that receiver playback or store speakers currently work.
