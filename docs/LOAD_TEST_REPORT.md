# Load test report — synthetic Receivers

**Read the limitations section before quoting any number from this document.**

| | |
|---|---|
| Date | 2026-07-30 |
| Commit | `9e84dca` |
| Tool | [`tools/load_test_receivers.py`](../tools/load_test_receivers.py) (existing; extended to read server-side queue counters) |
| Machine | Windows 10 Pro 19045, AMD64, 12 logical cores |
| Python | 3.12.10 |
| Backend | Uvicorn, **one worker** |
| Database | temporary SQLite per level, fresh pilot root per level |
| Protected database | never opened — `8A7E3413…B1A547CA` unchanged |

---

## What was measured, and what it means

Each level starts a real backend on a free loopback port against a **temporary**
database with temporary users and keys, enrols N synthetic Receivers over real
WebSockets, streams the deterministic WebM/Opus fixture to all of them at ~250 ms
per chunk (what the browser's MediaRecorder actually uses), then stops.

Queue counters are read from the server's own
`GET /api/broadcast/audio-metrics` **mid-broadcast**. That matters: a Receiver
cannot see a chunk the server dropped before sending it, so counting drops from
the Receiver side infers them. These are the server's own numbers.

## Results

| Receivers | Backend CPU | Backend RSS | All connected | Fanout | READY p95 | READY | Full delivery | Queue max_depth | Dropped | Within capacity | Queues after stop | Errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2 | 0.02 s | 74.9 MB | 27.7 ms | 52.7 kbps | 18.3 ms | 2/2 | 2/2 | 1 / 24 | 0 | yes | 0 | 0 |
| 5 | 0.06 s | 76.0 MB | 38.3 ms | 132.0 kbps | 12.1 ms | 5/5 | 5/5 | 1 / 24 | 0 | yes | 0 | 0 |
| 10 | 0.09 s | 77.6 MB | 56.8 ms | 263.9 kbps | 16.5 ms | 10/10 | 10/10 | 1 / 24 | 0 | yes | 0 | 0 |
| 20 | 0.19 s | 80.7 MB | 92.2 ms | 533.0 kbps | 17.7 ms | 20/20 | 20/20 | 1 / 24 | 0 | yes | 0 | 0 |
| **40** | **0.27 s** | **86.1 MB** | **174.4 ms** | **1072.4 kbps** | **41.9 ms** | **40/40** | **40/40** | **1 / 24** | **0** | **yes** | **0** | **0** |

CPU is total processor seconds consumed by the backend process tree across a
~6.9 second broadcast. At 40 Receivers that is 0.27 s of CPU for 6.9 s of
wall clock — roughly 4 % of one core.

Scaling is linear and shallow: 20× the Receivers (2 → 40) costs 13× the CPU,
1.15× the memory, and 20× the throughput, with no drops.

## Pass criteria, and whether they were met

| Criterion | Result |
|---|---|
| Every Receiver authenticates and reaches READY | **PASS** at all five levels |
| Every Receiver receives every chunk | **PASS** — full delivery at all five levels |
| No queue exceeds its capacity | **PASS** — `every_queue_within_capacity: true` |
| Dropped chunks | **PASS** — 0, server-side counter |
| No queue survives the stop | **PASS** — `store_count: 0` after every level |
| No orphan sender task | **PASS** — implied by the above; `stop_all()` removes queue and task together |
| Receiver errors | **PASS** — 0 |

**Highest stable level: 40 simulated Receivers.** 40 was run only after 20 was
clean, per the sequencing rule. There is no sign of a ceiling at 40 — CPU and
memory are both far from saturation — so the limit of this evidence is the
number tested, not a measured breaking point.

---

## Limitations — what these numbers do NOT show

**`max_depth` was 1 of 24 at every level, and that is the most important caveat
in this document.** It means the fan-out was never backlogged in this scenario:
synthetic Receivers on loopback accept a chunk essentially instantly. So this
run demonstrates the queues stay bounded and drop nothing when nothing is slow.
**It does not exercise the overflow path at all.** Overflow, drop-oldest
ordering and the dropped counter are proven separately by unit tests
([`test_audio_protocol.py`](../backend/tests/test_audio_protocol.py),
[`test_audio_queue_scale.py`](../backend/tests/test_audio_queue_scale.py)),
which force a Store to stall and assert the behaviour directly.

**Nothing here touches audio hardware.** Every Receiver is synthetic: no FFmpeg
decode, no WASAPI device, no amplifier, no speaker. `receivers_playback_confirmed`
counts simulators that accepted the bytes.

The three claims must stay separate, and this report supports only the first:

| Claim | Supported by this report? |
|---|---|
| The fan-out, bounded queues and acknowledgement path hold at N Stores | **Yes** |
| Decoded audio reached a software output device | No — needs a real Receiver |
| A human heard it from the Store's speakers | No — needs a person in the shop |
| `SPEAKER_VERIFIED` | No — needs EchoGuard/acoustic evidence |

**Loopback is not a shop.** No real network: no Wi-Fi, no contended uplink, no
packet loss, no NAT, no 44 physical machines. A Store on a congested link is
exactly the case that would exercise `max_depth`, and it is not represented here.

**One broadcast per level.** No soak test, no repeated sessions over hours, no
memory-growth measurement across many sessions.

## Recommended rollout limit

**The software path is not the constraint.** On this evidence a single HQ machine
with one Uvicorn worker handles 40 Stores with ~4 % of one core and 86 MB.

The constraint is physical, and this report cannot speak to it. A staged rollout
is still the right approach — 1 Store, then 2, then a zone — because what is
unproven is amplifiers, speakers, Store networks and sign-in behaviour, none of
which get easier with more Stores.

**Do not read "40 simulated Receivers passed" as "44 Stores are ready."**

## Reproducing

```powershell
$env:ADMIN_USERNAME = "loadtest-admin"
$env:ADMIN_PASSWORD = "<a fresh throwaway value>"
$env:JWT_SECRET     = "<a fresh throwaway value>"
python tools\load_test_receivers.py --stores 40 --pilot-root "$env:TEMP\echocast-load" --report "$env:TEMP\load-40.json"
```

Use a **fresh** `--pilot-root` per run: the pilot root persists its seeded
administrator, so a new random `ADMIN_PASSWORD` against an existing root fails
login with HTTP 401. The tool refuses to run without an explicitly set password
rather than inventing one, which is correct and was not worked around.
