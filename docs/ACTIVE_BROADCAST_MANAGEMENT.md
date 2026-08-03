# Active Broadcast Management

The supervision page for concurrent broadcasts, and the four independent
permissions that shape it.

## Why it is a separate page

Broadcast Console used to carry a cross-user list of every live broadcast.
That was readable with one concurrent broadcast and unusable with twenty: the
page where somebody speaks into a microphone grew without bound behind the
controls they came for. With 50 simultaneous broadcasts the Start button was
below the fold.

Supervision now lives at `/active-broadcasts`. The Console keeps a single
compact badge — `Active Broadcasts: 17  View →` — whose height does not depend
on how many broadcasts are live.

## The six capabilities

| Code | Means | Notes |
|---|---|---|
| `broadcast.active_view` | May open the supervision page | Without it: nav hidden, route refused, list API 403, and the Console badge count is `null` rather than hidden client-side |
| `broadcast.view_ownership` | May see WHO is broadcasting | Existing permission, meaning unchanged |
| `broadcast.view_targets` | May see WHICH Stores | New. Gates the detail endpoint, Store search and the Store filter |
| `broadcast.stop` | Stop your OWN session | Existing, unchanged. Never widened to other owners |
| `broadcast.stop_any` | Stop ONE named session belonging to somebody else | Requires `active_view` + full Store Scope coverage |
| `broadcast.emergency_stop` | Stop EVERY session estate-wide | Existing, unchanged, and independent of all the above |

### No permission implies another

Deliberately. A supervisor may be trusted to end a broadcast on Stores they
administer without being told which campaign or which colleague it belonged
to; an auditor may need to see which Stores are in use with no power to
intervene. `stop_any` reveals no Store names and no owner identity;
`view_targets` reveals no owner; `emergency_stop` reveals nothing at all.

## Default role assignments

| Role | active_view | view_ownership | view_targets | stop_any | emergency_stop |
|---|---|---|---|---|---|
| OWNER | YES | YES | YES | YES | YES |
| ADMIN | YES | YES | YES | YES | YES |
| BROADCASTER | no | no | no | no | no |
| VIEWER | no | no | no | no | no |

Per-user ALLOW/DENY overrides apply on top, through the existing rights editor
in User Management — the catalog is rendered from
`GET /users/{id}/permissions`, so the four codes appear there automatically
with the labels `Active Broadcasts — View Page / View Broadcaster / View
Stores / Stop Other Broadcast`. No SQLite editing is required to assign them.

## Redaction is server-side

Every hidden field is hidden by NOT BEING BUILT. A field that reaches the
browser has been disclosed whatever the interface does with it afterwards: it
is in the network tab, the response cache and any proxy log that records
bodies. The tests assert on the serialized JSON for that reason.

The subtler rule is that **the shape of an answer is a disclosure too**.
Search only looks at fields the caller may see — otherwise typing `Alice` and
getting one result would reveal that Alice is broadcasting even though no
owner field was serialized. An unauthorized filter is refused with 403 rather
than ignored, because a silently-dropped filter returns an unfiltered list the
caller will misread as the answer.

`total` counts only what the caller may know about, and `target_store_count`
counts Scope survivors rather than the real total — a count that reported the
truth would let a scoped Admin measure exactly how much they cannot see.

## Store Scope and Stop

Scope applies underneath every permission. For a cross-owner Stop it is
checked against **every** target of the session, not merely the visible ones:
Stop ends the whole session, so a supervisor scoped to two of its three Stores
would otherwise silence a third Store they have no authority over — and one
they never saw on screen. Partial stops are not offered; a broadcast
continuing on some Stores and not others is a state no operator asked for and
every listener would experience as a fault.

## API

    GET  /api/broadcast/active-management                  list, metadata only
    GET  /api/broadcast/active-management/{id}/stores      exact Stores
    POST /api/broadcast/active-management/{id}/stop        one named session

The list deliberately does not carry targets even for a `view_targets` holder:
50 sessions × every target is the payload this design exists to avoid. Paging
is server-side (20 default, 50 selectable), and refresh is bounded polling on
the existing convention — no second WebSocket architecture.

All three read the SAME active truth as `GET /api/broadcast/active`: session
ids from `BroadcastRuntime`, rows from `broadcast_sessions`, occupancy from
`broadcast_store_leases`. There is no competing active-state calculation.

## One defect closed on the way

`GET /api/broadcast/active` used to return `sessions[].target_store_ids` to
anybody holding `view_ownership`, which made ownership visibility a back door
to target visibility. The ids now require `broadcast.view_targets`; the count
remains, because a Broadcaster already learns occupancy from
`busy_store_ids`.

## Audit

A cross-owner stop is privileged and is recorded through the existing system
log: actor id and username, target session id, target owner id, target Store
count and the result. Refusals (out-of-scope) and failures are recorded too.
Ids and counts only — no password, token, Device credential or audio.

A stop is never reported as succeeded merely because a command was sent. If
cleanup fails the caller gets `STOP_FAILED` and is told the broadcast may
still be live, because an operator who believes a Store is silent when it is
not is worse off than one who sees an error.
