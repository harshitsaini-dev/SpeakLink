# HQ Runtime — design

`EchoCastHQRuntime.exe` is one windowed Windows process that starts the EchoCast
backend and the production frontend, watches both, and restarts a crashed one
with bounded backoff. It exists so an HQ machine can be signed into and then
left alone, with no console window for anybody to close.

Source: [`tools/hq_runtime.py`](../tools/hq_runtime.py) ·
Build: [`hq_runtime.spec`](../hq_runtime.spec) ·
Tests: [`backend/tests/test_hq_runtime.py`](../backend/tests/test_hq_runtime.py),
[`backend/tests/test_hq_runtime_entry.py`](../backend/tests/test_hq_runtime_entry.py)

---

## The two properties that carry the weight

### 1. It refuses rather than improvises

Every failure mode here has a tempting, helpful-looking fallback — create the
database, use `backend/echocast_live.db`, pick up a pilot file, start the
development server. Every one of them turns *"some data is missing"* into
*"every Store has vanished"*, which is precisely the failure the whole
persistent-server effort exists to end.

So the runtime refuses all of them and names the command to run instead. A test
walks the module's AST to prove no repository-database path survives in code —
scanned through the parser, because a text scan that skips lines starting with
`#` does not skip a docstring, and the paragraph explaining why the fallback
must not exist reads exactly like the fallback.

### 2. READY means healthy, not spawned

A child process that exists is not a backend that answers. Both children are
asked over HTTP before anything claims to be working:

| State | What is actually true |
|---|---|
| `STARTING` | nothing started yet |
| `BACKEND_STARTING` | the backend process exists |
| `BACKEND_HEALTHY` | the backend **answered** on `/docs` |
| `FRONTEND_STARTING` | the frontend process exists |
| `READY` | **both** answered over HTTP |
| `DEGRADED` | one of them would not become healthy within the bound |
| `STOPPING` / `STOPPED` | shutting down / stopped cleanly |
| `CONFIG_ERROR` | the persistent profile is unusable; nothing was started |

"The process started" is the same shape of claim that once let a silent Receiver
look like a working one. It is not allowed to mean READY here.

---

## Why exactly one Uvicorn worker

**Not a performance choice.** WebSocket connection state in this backend is
process-local — a dictionary in memory, not in SQLite and not in a shared
broker. With two workers, a Receiver that connected to worker A is invisible to
a broadcast handled by worker B. Half the Stores would go silent, intermittently,
depending on which process answered the HTTP request.

The runtime therefore starts `uvicorn --workers 1`, never `--reload`, never
`--debug`. There are tests that hold it to all three.

Scaling past one worker is a real architectural change: connection state has to
move out of the process (Redis pub/sub or equivalent) first. It is not a flag.

## Why the frontend is `python -m http.server`

The production build is static files served to a handful of browsers on a
private LAN. `http.server` adds no dependency to a machine that already has the
Python the backend needs. `react-scripts start` is a watcher with a compiler
attached, and it is not what an unattended system should depend on at seven in
the morning. A test forbids `react-scripts`, `yarn start` and `npm start` in the
frontend command.

## Ordering: backend first, then frontend

Deliberate. A frontend served before the backend answers is a login page that
cannot log anybody in — which a Store manager reads as "HQ is broken" rather
than "HQ is still starting". The frontend is not started until the backend is
`BACKEND_HEALTHY`.

---

## Bounded restart, and giving up

`BackoffPolicy` grows the delay (2s, 4s, 8s … capped at 60s) with jitter, so
several children never retry in lockstep. After `DEFAULT_MAX_ATTEMPTS` (6 —
roughly a minute of patience, enough for a network stack that is not up yet at
logon) the runtime reports `DEGRADED` and **exits non-zero**.

Giving up is the design, not a limitation. A permanently broken backend
respawned for ever fills the disk with logs and buries the one line that says
why. Recovery from `DEGRADED` is the Scheduled Task's periodic trigger, which
starts the runtime again later — see [HQ_AUTO_START.md](HQ_AUTO_START.md).

Exit codes, because Task Scheduler records them:

| Code | Meaning |
|---|---|
| 0 | ran and stopped cleanly |
| 2 | `CONFIG_ERROR` — refused to start |
| 3 | `DEGRADED` — gave up |
| 4 | another runtime already holds the lock |

A supervisor that gave up must never hand back zero. A green task history and a
dead HQ is the worst combination available.

## Single instance

`runtime_lock()` is an advisory byte-range lock scoped to the persistent root,
reused from `receiver_agent.InstanceLock`. Two supervisors would fight over one
SQLite file and one port, and the second would look like a crash loop.
Staleness is handled by Windows releasing the lock when the holder dies, however
it dies — a PID file has to answer "is process 4812 still the runtime, or a text
editor that got the same number after a reboot?" and eventually answers wrong.

## The status file

`%LOCALAPPDATA%\EchoCast-AI\hq-runtime-status.json` — machine-level, **not**
inside the persistent root, and that placement is the point: the status file
matters most when there is no persistent root to put it in.

A GUI-subsystem process has nowhere to print. Without this file, a refusal
produces a Scheduled Task that "ran successfully" and an HQ that is not there.
It is replaced whole through a temporary file and a rename (a status file is a
current fact; the log is the history), and every `detail` goes through the same
redactor the Receiver logs use.

---

## Persistent-profile refusal rules

| Situation | Behaviour | Why |
|---|---|---|
| No persistent root | **Refuse**, name `Initialize-EchoCastPersistentLanServer.ps1` | A runtime that can create a server will one day create one over the real one |
| Database missing | **Refuse**, name the backups folder | An empty database that looks healthy is worse than an obvious absence |
| Database is a throwaway pilot file | **Refuse** | Rebuilt from scratch on every start — the original P0 |
| Key container missing, **0 Devices enrolled** | **Allow** | First start; the backend mints it |
| Key container missing, **n Devices enrolled** | **Refuse**, state *n* | Minting a new one silently breaks every enrolled Store while all of them still look enrolled — 44 re-enrolments |
| Database unreadable while counting Devices | **Refuse** | "I could not count them" must never become "there are none" |
| Signing secret missing | **Create it** | Costs everybody one sign-in, not 44 re-enrolments. `Start-EchoCastPersistentLanServer.ps1` already mints it here |
| Signing secret present | **Reuse, never replace** | Replacing it signs every user out on every restart |
| No production frontend | **Refuse**, name `yarn build` | This runtime does not start a development server |
| `server.py` not found | **Refuse**, name `backend_root` in config | Nothing to start |

The asymmetry between the two secrets is the whole rule: **a new signing secret
costs a sign-in; a new HMAC container costs 44 Stores a re-enrolment.**

That table is not theoretical. The first packaged build refused the real,
correctly initialized HQ profile because nothing in this repository creates the
key container before the first start — a refusal no documented procedure could
satisfy. It was found by running the executable, not by the unit suite.

---

## Packaging notes

- `console=False` and `disable_windowed_traceback=True`. An unattended HQ desk is
  exactly where a modal error box sits unclosed for a week with the runtime dead
  behind it. Failures belong in the rotating log.
- The spec **excludes** FastAPI, SQLAlchemy and uvicorn. The backend is a child
  process run by the machine's own Python, so the supervisor does not host it.
- `sys.executable` inside a frozen build is `EchoCastHQRuntime.exe` itself — so
  the backend command would have relaunched the supervisor with `-m uvicorn`.
  `resolve_python_executable()` handles this and refuses up front if no
  interpreter is found, rather than letting a child fail an hour later.
- `Path(__file__).parents[1]` inside a frozen build is the unpacked bundle, not
  the repository. A packaged runtime looks for `frontend\` and `backend\` beside
  the executable; a checkout uses the repository. A test holds the installer and
  the runtime to the same answer, because they disagreed and a package that
  installed cleanly could not start.
- Children are started with `CREATE_NO_WINDOW` (reused from
  `audio_receiver_pilot.hidden_child_process_options`). A windowed parent
  starting a console child gets a **brand-new console** — the FFmpeg defect one
  layer up.
- Secrets travel in the child **environment**, never on a command line. A
  command line is visible in the process list to every user on the machine.

## Stopping

`stop_children` verifies each PID's command line before signalling anything.
Windows reuses process numbers, and a recorded PID alone is not proof of
identity.

---

## What this design does not give you

- **HQ does not run before somebody signs in.** See
  [HQ_AUTO_START.md](HQ_AUTO_START.md).
- **Plain HTTP on a private LAN.** No HTTPS, no WSS. Tokens travel in clear
  text. This is acceptable for a private-LAN pilot and unacceptable for anything
  reachable from outside it.
- **One machine.** No failover, no clustering, no load balancing.
