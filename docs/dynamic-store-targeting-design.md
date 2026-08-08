# Dynamic single-Store targeting — architecture spec

Design only. No product code exists for any of this yet.

Scope: **Add, Pause, Resume, Remove** one Store during an already-live physical
Broadcast. Zone and bulk actions are explicitly out of scope.

---

## 1. Receiver protocol delta

### The options, compared

| | A: `pause` + `resume` | B: `stand_down` + reuse `prepare` | C: make `stop` non-terminal |
|---|---|---|---|
| Protocol clarity | Clear, but `resume` restates everything `prepare` already says | Clear: one new verb meaning "end this segment, keep the socket" | Poor — one message with two meanings decided by a flag |
| Backward compatibility | 1.5.0 ignores both silently | 1.5.0 ignores it silently | **Dangerous**: 1.5.0 reads the flag it does not know and still exits |
| Implementation risk | Two new handlers, one duplicating `_on_prepare` | One new handler; Resume reuses the most-exercised path in the agent | Rewrites the shutdown path every Broadcast already depends on |
| Stale-message risk | Two verbs to generation-check | One verb to generation-check | A stale `stop` that used to end a Broadcast could now be read as a pause |
| Restoration safety | Fine | Fine — `prepare` already captures the snapshot | Restoration is entangled with the terminal path |
| Crash recovery | Unchanged | Unchanged | Changes what an unfinished `stop` means |
| 1.5.0 fails closed? | No — silently keeps playing | No — silently keeps playing | **No, and worse**: may exit when we meant pause |
| HQ capability detection | Needed | Needed | Needed, and least trustworthy |

### Chosen: **Option B**

One new HQ→Receiver command:

```
{ "type": "stand_down",
  "session_id": <int>,
  "target_generation": <int>,
  "reason": "pause" | "remove" }
```

The Receiver ends the current participation segment — stop feeding the decoder,
tear down decoder/queue/PCM sink, **restore the Windows endpoint baseline for
this generation**, stop the endpoint observer — and then **returns to the
session loop instead of returning out of it**. The socket stays open and the
Receiver stays enrolled and connected.

Resume is the **existing `prepare`**, carrying the next `target_generation`.
`_on_prepare` already does exactly what Resume needs: capture the endpoint
snapshot, build the decoder, queue and sink, and send `receiver_ready`.

New Receiver→HQ acknowledgement:

```
{ "type": "stood_down",
  "session_id": <int>,
  "target_generation": <int>,
  "endpoint_restored": <bool>,
  "restore_error": <str|null> }
```

`endpoint_restored: false` is a first-class outcome, not an exception. It is how
Pause reports "the audio stopped but I could not put the mixer back".

Every existing session-bearing command and ack gains an optional
`target_generation`. Absent means generation 1, which is what a Broadcast that
never uses dynamic targeting always has.

**1.5.0 does not fail closed on its own** — it ignores unknown control messages,
so it would keep playing while HQ believed it paused. HQ therefore refuses to
offer Pause/Resume unless the Receiver has *declared* the capability. Failing
closed is HQ policy, enforced before the command is ever sent.

---

## 2. Target state machine

```
NOT_TARGETED
   │ add
   ▼
ADDING ──► PREPARING ──► ACTIVE ──► PAUSING ──► PAUSED
   │           │            │                     │
   │           │            │  remove             │ resume
   ▼           ▼            ▼                     ▼
FAILED      FAILED      REMOVING ◄────────────  PREPARING
                            │                    (generation + 1)
                            ▼
                        REMOVED
```

- `ADDING` — request validated, lease being acquired.
- `PREPARING` — lease held, `prepare` sent, waiting for `receiver_ready`.
- `ACTIVE` — ready acknowledged and the audio pump is running. **This is
  delivery truth, not audibility.**
- `PAUSING` — `stand_down(reason=pause)` sent, awaiting `stood_down`.
- `PAUSED` — stood down, lease **retained**, mixer left alone.
- `REMOVING` → `REMOVED` — stood down, lease released, generation retired.
- `FAILED` — terminal for that generation; lease released; a fresh Add creates
  a new generation.

Playback truth (`audio_receiving`, `playback_confirmed`, `playback_error`,
`device_error`) stays a **separate axis** on the same target. `ACTIVE` never
means "Playing", and no state here ever means `SPEAKER_VERIFIED`.

---

## 3. Generation model

`target_generation`: a monotonic integer per `(session_id, store_id)`, starting
at 1, incremented **on every entry into `PREPARING`** — so Add gives 1, the
first Resume gives 2, the next Resume 3, and a re-Add after Remove continues the
sequence rather than restarting it.

Validated at the **same choke point that already validates `session_id`** —
`_validate_active_session` in `backend/receiver_contract.py:393`. An ack whose
`(session_id, target_generation)` does not match the Receiver's current
participation is dropped exactly as a wrong-session ack is today.

- Late **command** from an old generation → Receiver drops it (it knows its own
  current generation).
- Late **ack** from an old generation → HQ drops it.
- Late **audio** after Pause/Remove → cannot occur: the pump task for that
  generation is cancelled and its queue destroyed before `stand_down` is sent.
- **Reconnect** → the Receiver reports its last known `(session_id,
  target_generation)` in `receiver_ready`; HQ accepts only if it matches the
  live target, otherwise it stands the Receiver down.

## 4. Participation segment

**A segment and a generation are the same thing**, deliberately. One generation
= one participation segment = one Windows restoration baseline. Introducing two
terms for one boundary is how the volume snapshot got confusing in the first
place.

```
Broadcast session 50, Store BP
  generation 1:  ADD → ACTIVE → PAUSE          baseline A (captured at ADD)
  generation 2:  RESUME → ACTIVE → REMOVE      baseline B (captured at RESUME)
```

Baseline A is restored at the Pause. Baseline B — which reflects whatever the
Store did to its mixer while paused — is restored at the Remove.

---

## 5. Windows restoration lifecycle

| Moment | Action |
|---|---|
| `PREPARING` (Add or Resume) | Read current volume+mute → this generation's baseline → **write the record** → only then apply the broadcast level |
| `ACTIVE` | SpeakLink controls the endpoint; telemetry reports actual state; no enforcement |
| `PAUSING` | Restore this generation's baseline → **clear the record** → stop the observer |
| `PAUSED` | SpeakLink does not touch the mixer and does not report it as controlled |
| `RESUMING` | Capture the **current** state as the new baseline (start of the table again) |
| Remove / Stop / Emergency / disconnect | Restore the **current active** generation's baseline |

### Crash-recovery record

**`RECORD_VERSION` stays 1.** This is not laziness — `read_record`
(`tools/windows_endpoint_restore.py:119`) *raises* on any version mismatch, and
`recover_on_startup` then returns `recovered: false` **without restoring and
without clearing**. A Receiver 1.5.0 that ever met a v2 record — after a Kit
rollback, or a downgraded Store — would leave that shop at announcement volume
indefinitely. That directly violates "never leave a Store permanently at
SpeakLink-controlled volume".

So the record stays v1-readable and grows **optional** keys:

```json
{ "version": 1,
  "session_id": 50,
  "endpoint_id": "{0.0.0...}",
  "original_volume_percent": 20,
  "original_muted": true,
  "written_at_utc": "...",
  "target_generation": 2 }
```

The five v1 keys **always describe the currently active generation's baseline**.
A 1.5.0 Receiver reading this restores that baseline — which is exactly the
correct value. It simply cannot tell you which segment it came from.

Writes stay atomic and single-slot (write temp + replace). One record per
install remains correct because one Receiver serves one Store and participates
in one Broadcast at a time.

- **Power fails while PAUSED** → no record (cleared at the pause) → nothing to
  restore. Correct: SpeakLink was not controlling the mixer.
- **Power fails after Resume capture, before mutation** → the record holds a
  baseline equal to the endpoint's present state → recovery re-applies it and it
  is a no-op. Safe.
- **Power fails mid-ACTIVE** → record present → next start restores it.

---

## 6. Crash recovery per state

| State at crash | Windows state | Record | Receiver on startup | HQ reconciliation | Lease | Auto-rejoin |
|---|---|---|---|---|---|---|
| ADDING | untouched | none | nothing to do | lease released, target FAILED | released | no |
| PREPARING | untouched or just mutated | present | restore + clear | target FAILED | released | no |
| ACTIVE | SpeakLink level | present | restore + clear | target STOPPED, session ended if orphaned | released | no |
| PAUSING | baseline may be half-applied | present | restore + clear | target PAUSED→REMOVED | released | no |
| PAUSED | Store's own | **none** | nothing to restore | target REMOVED on session cleanup | released | no |
| RESUMING | untouched or just mutated | present (new baseline) | restore + clear | target FAILED | released | no |
| REMOVING | baseline may be half-applied | present | restore + clear | target REMOVED | released | no |

Existing `broadcast_reconciliation.py` already releases leases for orphaned
sessions on restart; per-target reconciliation extends it rather than replacing
it. **No Store ever auto-rejoins after a crash** — rejoining is an operator act.

---

## 7. Late-join audio

### Recommendation: migrate **all** Store fanout to framed Clusters

Frame once per session with the existing `WebmStreamFramer` — the same object
the web relay already uses — and fan **frames** out to Stores, not raw socket
bytes. One parser, one code path, no per-Store FFmpeg on HQ.

A Store queue is then fed:

1. the cached **init segment** (everything before the first Cluster), then
2. a **bounded** live-edge Cluster bootstrap (reuse `DEFAULT_LIVE_EDGE_CLUSTERS`,
   a small ring, not history), then
3. every subsequent **whole** Cluster.

FFmpeg decodes this because it is a structurally valid WebM stream: EBML header
and Segment/Tracks metadata first, then Cluster elements on element boundaries —
never a partial Cluster and never a Cluster without a header. This is the same
byte sequence the web path puts through real MediaSource in production today.

**Why migrate everyone rather than special-case late joiners:** a path used only
by late joiners is a path that is rarely exercised and quietly rots. Today's
"Stores get raw bytes from byte 0" correctness is accidental — it holds only
because every Store is prepared before the first byte. Making the framed path
the *only* path means every Broadcast exercises it.

**The risk, stated plainly:** this changes the byte stream delivered to every
Store, including BP, which has just passed physical acceptance. It must be
gated on the real-decoder late-join test **and** a physical re-acceptance on BP
before it ships. If that re-acceptance is not acceptable, the fallback is
bootstrap-for-late-joiners-only, and I would rather be told that now than
discover it after a rebuild.

---

## 8. Fanout and reconnect invariant

**Invariant:** at most one live pump per `(session_id, store_id, generation,
connection_id)`, and a pump's death marks its target as *needing re-bootstrap*
— never as permanently finished.

`_started_stores: set` becomes
`_pumps: dict[store_id, PumpHandle(generation, connection_id, task)]`.

- Starting a pump for a store that already has a live handle with the same
  generation and connection is a no-op.
- A pump whose send fails cancels itself, clears its handle, and records the
  reason. It does **not** poison the store.
- A Receiver reconnecting arrives with a new `connection_id`; the old handle is
  discarded, the queue is **recreated empty** (never inherited — that is where
  stale chunks would come from), and the store is re-bootstrapped at the live
  edge.

This repairs the pre-existing defect found in the audit: today a reconnecting
Receiver rejoins the target set, gets a `play`, and never gets a pump.

---

## 9. Database model

**Minimum additive change. No new table, no destructive rewrite.**

Two additive columns on `broadcast_targets`:

- `lifecycle_state TEXT NOT NULL DEFAULT 'active'` — the target state machine.
- `current_generation INTEGER NOT NULL DEFAULT 1`.

Existing rows read as `active`/`1`, which is exactly what a Broadcast that never
used dynamic targeting means. `play_status` is untouched and keeps meaning
playback truth.

Audit history — added, paused, resumed, removed, failed — goes to the
**existing `system_logs`** event architecture, one row per operator action.
Nothing is written per chunk, per ack, or per telemetry reading.

Migration is two `ALTER TABLE ... ADD COLUMN` with defaults, backed by a
migration test that a pre-migration database still opens and still reports its
existing Broadcasts correctly.

---

## 10. Lease semantics

**Pause KEEPS the lease.** Agreed, and it fits the current architecture: the
partial unique index is on `(store_id) WHERE released_at IS NULL`, so holding
the lease through a pause is exactly what stops a second Broadcast seizing a
Store that is temporarily quiet. The operational consequence — a paused Store is
unavailable to other Broadcasts until removed or the Broadcast ends — is the
correct one; the alternative is a Resume that can fail because someone else took
the Store while it was paused.

New function, alongside the existing session-scoped release:

```python
release_store_lease(engine, *, session_id: int, store_id: int) -> bool
```

Scoped to **both** ids. The existing docstring warns against a `store_id`-only
release, which is a genuinely different and dangerous thing: this cannot free a
Store another session holds.

---

## 11. Add sequence

1. RBAC: `broadcast.store_delivery` **and** control authority over this session.
2. Store Scope covers the Store.
3. Session still live; **not** Only With Link.
4. Store not already `ADDING`/`PREPARING`/`ACTIVE`/`PAUSED` in this session.
5. Receiver connected and its capabilities recorded.
6. **Acquire lease** `(session, store)` atomically. Conflict → truthful refusal,
   nothing else touched.
7. Create the target runtime object, `generation += 1`, state `ADDING`.
8. Create a bounded queue; state `PREPARING`; send `prepare(session,
   generation)`.
9. Receiver captures its endpoint baseline, writes the record, applies the
   broadcast level, replies `receiver_ready`.
10. On ready: bootstrap the queue with **init segment + live-edge Clusters**,
    start the pump, state → `ACTIVE`.
11. `audio_receiving` and `playback_confirmed` update **playback truth only**.

Any failure at 5–9: state `FAILED`, queue destroyed, **lease released**, Receiver
told to stand down if it may have mutated the endpoint. Other Stores are never
touched.

**UI may say `ACTIVE` only at step 10** — pump running after a real
`receiver_ready`. It may say **Playback confirmed only on the ack of that name**.
A command that has been sent is `Preparing…`, never `Playing`.

---

## 12. Pause sequence

**HQ stops the queue first, then commands the Receiver.** In that order, so no
chunk can cross the boundary and arrive after the Receiver has closed its
decoder.

1. State `ACTIVE` → `PAUSING` under the session lock.
2. Cancel the pump task, **destroy** the queue and discard everything in it.
   No pause buffer, no catch-up.
3. Send `stand_down(session, generation, reason=pause)`.
4. Receiver: stop decoding, tear down decoder/queue/sink, **restore this
   generation's baseline**, clear the record, stop the observer, return to the
   session loop, reply `stood_down`.
5. On `stood_down` with `endpoint_restored: true` → state `PAUSED`.
6. On `stood_down` with `endpoint_restored: false` → state `PAUSED` **with a
   surfaced restoration error**. The audio genuinely stopped; the mixer did not
   go back, and pretending otherwise is worse than saying so. The record is
   deliberately **not** cleared, so the next Receiver start retries it.
7. No `stood_down` within the timeout → state `FAILED` with "Receiver did not
   confirm pause", audio already stopped, lease retained.

Lease is retained. Volume telemetry for this Store stops — HQ must not present a
paused Store's last reading as current.

---

## 13. Resume sequence

1. Session still live; target `PAUSED`; Receiver still connected and current.
2. `generation += 1`; state `PREPARING`.
3. Send `prepare(session, new_generation)`.
4. Receiver captures the **current** endpoint state as the new baseline, writes
   the record, applies the broadcast level, builds a **fresh** decoder, queue and
   sink, restarts the observer, replies `receiver_ready`.
5. HQ creates a **fresh** bounded queue — never the old one — bootstraps with
   init segment + live-edge Clusters, starts a new pump, state `ACTIVE`.
6. Volume telemetry resumes; the two-way contract applies unchanged.

Nothing from the paused period exists anywhere to be played.

---

## 14. Remove sequence

| From | Behaviour |
|---|---|
| `ACTIVE` | Pause sequence steps 1–4 with `reason=remove` (queue destroyed, baseline restored), then release lease, retire generation, state `REMOVED` |
| `PAUSED` | No endpoint restoration — the Store owns its mixer already and overwriting it would undo a legitimate change. Release lease, retire generation, `REMOVED` |
| `PREPARING` | Cancel the prepare wait, stand the Receiver down (it may have mutated the endpoint), release lease, `REMOVED` |
| `FAILED` | Lease already released; retire generation, `REMOVED` |

Retiring a generation makes every later ack, `endpoint_state` and pump callback
from it inert. The Broadcast keeps running; other Stores are untouched; history
rows are kept.

---

## 15. Race matrix

Every dynamic operation takes the **per-session lock** and re-reads target state
after acquiring it. Global Stop takes the same lock and sets a session-level
`ending` flag that every operation checks first.

| Race | Winner | Cleanup | Lease | Windows |
|---|---|---|---|---|
| Stop vs Add | **Stop** | prepare abandoned, queue destroyed, Store never plays | released | baseline restored if prepare mutated it |
| Stop vs Pause | **Stop** | pause folded into session end | released | restored once |
| Stop vs Resume | **Stop** | resume abandoned before pump start | released | restored if captured |
| Stop vs Remove | **Stop** | remove folded into session end | released | restored per current generation |
| Pause vs Resume | **first to take the lock**; the second sees the new state and is refused | — | retained | one transition only |
| Resume vs Remove | **Remove** if it wins the lock; otherwise Resume completes and Remove then runs from `ACTIVE` | — | released | restored once |
| double Add | second is **idempotent no-op** (already `ADDING`/`ACTIVE`) | — | one lease | one baseline |
| double Pause | second refused (`PAUSING`/`PAUSED`) | — | retained | one restore |
| double Remove | second is a no-op (`REMOVING`/`REMOVED`) | — | one release | one restore |

Invariants: never two leases, never two queues, never two pumps, never a
resurrected removed generation, never two baselines for one generation.

---

## 16. RBAC

**No new permission.** Live target mutation requires:

- `broadcast.store_delivery` — the existing physical-delivery gate, enforced at
  the existing choke point `_require_physical_delivery`; **and**
- control authority over this Broadcast — the same ownership rule the existing
  stop route uses (own session, or `broadcast.stop_any` with full Store Scope
  coverage); **and**
- Store Scope covering the Store being added.

Adding a fourth permission would let a user hold "can start physical Broadcasts"
and "can mutate live targets" separately, which is a distinction nobody has
asked for and one more cell in every matrix test.

---

## 17. UI

Smallest possible Console change. No new page, no route, no layout change —
fixed sidebar, single-scroll shell, three-card layout, Recording Player and Web
Audience panel all untouched.

**Add Store** button on the live Console, shown only when the Broadcast is
physical (never Only With Link) and the user holds store delivery. It opens the
existing Store picker, filtered to exclude Stores already participating or
adding, showing offline Stores truthfully and respecting Store Scope.

| Target state | Shown as | Buttons |
|---|---|---|
| ADDING | `Adding…` | — |
| PREPARING | `Preparing Receiver…` | Remove |
| ACTIVE + no playback ack | `Ready` | Pause, Remove |
| ACTIVE + `audio_receiving` | `Audio receiving` | Pause, Remove |
| ACTIVE + `playback_confirmed` | `Playback confirmed` | Pause, Remove |
| PAUSING | `Pausing…` | — |
| PAUSED | `Paused` | Resume, Remove |
| PAUSED + restore error | `Paused — volume not restored` | Resume, Remove |
| REMOVING | `Removing…` | — |
| REMOVED | `Removed` | — |
| FAILED | the real error | Retry (fresh generation), Remove |
| Receiver offline | `Receiver offline` | Remove |
| Receiver 1.5.0 | `Pause needs Receiver 1.6.0` | Remove only |

Pause and Resume are **disabled with a reason**, never hidden, when the Receiver
cannot support them — a missing button reads as a bug, a disabled one with a
reason reads as an upgrade.

---

## 18. Capability negotiation

Extend the existing `ReceiverCapabilities` block, which already has exactly the
right semantics: **absent means old**, and the fields default to `False` rather
than optimistically to `True`.

```python
class ReceiverCapabilities(BaseModel):
    output_volume: bool = False
    output_mute: bool = False
    output_control_status: Literal[...] = "unknown"
    stand_down_resume: bool = False        # new
```

HQ keys entirely off `stand_down_resume`, **never off the version string**. A
version string is a claim; a capability flag is what the build actually declared
about itself. (This also side-steps the `AGENT_VERSION` class of defect, where
the reported version was wrong for every build ever made.)

---

## 19. Mixed fleet, 1.5.0 and 1.6.0

- A Broadcast **starts normally to both**. Nothing about session start changes.
- **Add works on 1.5.0**, provided the framing decision in §7 holds — Add needs
  only `prepare`, which 1.5.0 already implements. A 1.5.0 Store can be added
  mid-Broadcast and will play.
- **Pause and Resume are refused for 1.5.0** at the API — not merely hidden in
  the UI — because 1.5.0 ignores `stand_down` silently and would keep playing
  while HQ showed `Paused`. That is the one failure mode worth being strict
  about.
- **Remove works on 1.5.0** using the existing terminal `stop`: the Receiver
  restores its endpoint and closes the session socket, then the agent reconnects
  idle. Slightly heavier than a stand-down, and correct.

**No fleet-wide upgrade is required.** Stores upgrade when they want Pause.

---

## 20. Implementation slices

| # | Slice | Files expected | Tests first | Receiver package | Manual acceptance |
|---|---|---|---|---|---|
| 1 | Target runtime object, generation, **reconnect pump repair** | `broadcast_runtime.py`, `audio_streaming.py`, `ws_manager.py` | pump identity, reconnect, no duplicate pump, no inherited queue | no | none (repairs an existing defect) |
| 2 | Shared framing + live-edge bootstrap for Stores | `webm_stream.py` (shared), `audio_streaming.py`, `server.py` | real-decoder late-join, existing-Store equivalence | no | **BP re-acceptance of ordinary Broadcast audio** |
| 3 | Add | `server.py`, `broadcast_reservation.py`, `models.py` + migration | Add matrix, lease conflict, failure paths, RBAC | no | needs a **second Store** |
| 4 | Receiver `stand_down` + per-generation snapshot | `audio_receiver_pilot.py`, `windows_endpoint_restore.py`, `receiver_contract.py` | stand-down, snapshot lifecycle, crash recovery, frozen-exe | **1.6.0 / Kit 1.9.0** | test Store first, then BP |
| 5 | Pause + Resume | `server.py`, runtime | pause/resume matrix, volume regression, no catch-up | no | BP |
| 6 | Remove | `server.py`, runtime | remove-from-each-state, stale rejection, re-add | no | BP |
| 7 | Frontend | `BroadcastConsole.jsx`, new `StoreTargetActions.jsx` | Jest state/button table, Playwright lifecycle | no | operator |
| 8 | Races, restart reconciliation, 5/10/20/40 load | runtime, `broadcast_reconciliation.py` | race matrix, restart matrix, load | no | — |

Slices 1–3 ship without touching the Receiver. Slice 4 is the only one that
rebuilds the Kit.

---

## 21. Tests per slice

Slice 1 — pump identity, reconnect rebootstrap, no duplicate pump, no inherited
queue, no cross-generation mutation.
Slice 2 — **real FFmpeg late-join decode**, resume decode, existing-Store byte
equivalence, bounded bootstrap, no backlog.
Slice 3 — the 30-point Add matrix: online, offline, duplicate, lease conflict,
prepare timeout, readiness failure, one queue, live edge, Link-only refusal,
RBAC, scope, crafted request.
Slice 4 — stand-down restores and returns to the loop; snapshot per generation;
every crash-recovery row in §6; frozen-exe pycaw + COM callback.
Slices 5–6 — pause/resume/remove matrices, volume two-way regression across
generations, change-while-paused untouched, stale ack and stale telemetry
rejection, re-add generation.
Slice 8 — full race matrix, restart reconciliation, 5/10/20/40 with queue depth,
drops, latency, and audio/recording/Web-Audience continuity.

Throughout: recording lifecycle unchanged, Web Audience unchanged, HQ
MediaRecorder never restarted, Store A/B and Broadcast A/B isolation.

---

## 22. Physical pilot plan

1. **Slice 2 on BP alone** — ordinary Broadcast, start to stop. This proves the
   framing migration did not change what BP hears. Nothing dynamic yet.
2. **Slice 3 needs a second enrolled Store.** Add can only be proven by adding a
   *different* Store to a Broadcast BP is already in — that way a failed Add
   cannot disturb BP, which is the whole safety property being tested.
3. **Slice 4–6 on the second Store first**, then BP: Pause → check BP's mixer
   returned to its pre-Broadcast value → change the volume by hand while paused
   → Resume → confirm the hand-set value became the new baseline → Remove →
   confirm that value came back, not the original.
4. Only then Zone/bulk work.

---

## 23. Unresolved risks

1. **Framing migration touches every Store**, BP included. Mitigated by the real
   decoder test and a BP re-acceptance, but it is a real change to a
   just-accepted path. The alternative — bootstrap only for late joiners — is
   safer today and rots faster.
2. **Add cannot be physically proven without a second Store.** Software tests
   can prove the lifecycle; only a second Store proves it against real hardware.
3. **`stood_down` with `endpoint_restored: false`** leaves a Store audibly quiet
   but at SpeakLink's level until its Receiver restarts. The design surfaces this
   honestly rather than hiding it; there is no way to force a mixer that is
   refusing.
4. **The single-slot record** assumes one Receiver, one Store, one Broadcast at a
   time. Still true, and worth stating because dynamic targeting is the first
   feature that makes multiple participations per install imaginable.
5. **1.5.0 Remove uses terminal `stop`**, so that Store's socket cycles. Harmless
   but visible in logs as a reconnect.
