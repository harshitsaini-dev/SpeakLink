# EchoCast learning guide

Short, concrete lessons taken from real defects in this repository. Each one is
here because it cost time, and because the same shape will happen again.

---

## Learning Box 1 — "Confirmed" must say *what* was confirmed

**What happened.** A Store showed `PLAYBACK_CONFIRMED` on the HQ dashboard and
made no sound at all. Windows was fine — the same earphones played the Windows
test tone. The Receiver was fine too. Both were telling the truth about
different things.

`PLAYBACK_CONFIRMED` was emitted when FFmpeg proved it had decoded audio. In the
Receiver's default *null* sink, decoded audio is then thrown away: no Windows
device is ever opened. So the message meant **"the audio decoded"**, and
everybody read it as **"the audio played"**.

**The general shape.** A status name that describes the *last step your code
performed* gets read as *the outcome the user cares about*. `sent`, `queued`,
`processed`, `confirmed`, `synced` — every one of these has a version that is
technically accurate and practically a lie.

**What to do about it.**

- Name the step, not the hope. `pcm_decoded` cannot be misread; `confirmed`
  can.
- If one status covers several modes, print the mode next to it. The fix here
  was not a new state machine — it was a start-up banner saying, in words, that
  audio was being discarded and what the status meant *in this mode*.
- Ask: "if this succeeds and the real-world thing did not happen, would anything
  look different?" If the answer is no, the status is not evidence.
- Keep the acoustic claim separate. `SPEAKER_VERIFIED` in this system requires a
  person or EchoGuard hardware, and no amount of software can promote itself
  into it.

---

## Learning Box 2 — A safe default is not a safe *silent* default

The null sink is the right default. It means a test run cannot suddenly blast
audio out of a build machine at 2am, and it is what every automated smoke uses.

The mistake was that choosing it said nothing. A Store desktop got the
test-harness default and looked identical to a working Store.

**Rule of thumb.** A default that is right for developers and wrong for users
must announce itself on the machines where it is wrong. Loudly, in the console,
in the words a non-programmer uses — "no sound will be played on this computer".

---

## Learning Box 3 — Fail-closed is only half of it; be *reachable*

The device resolver was written carefully. It refuses a partial name, refuses an
ambiguous one, and never silently opens the Windows default. All correct: one
physical speaker appears under MME, DirectSound, WASAPI and WDM-KS, so a bare
name genuinely cannot say which to open.

But the only way to see the list of valid selectors was
`python tools/windows_audio_devices.py` — and a Store desktop has no Python.
That is the whole point of shipping a packaged executable.

So the operator did the only thing available: guessed the name Windows showed
them. It was ambiguous, it was refused, and because `SinkConfigurationError`
inherits `AudioReceiverError` rather than `AgentError`, the refusal was never
caught. They got a Python traceback naming files that do not exist on their
machine, and the Store went OFFLINE.

**Three lessons, in order of how often they bite:**

1. **If you fail closed, hand back the valid options.** The refusal now lists
   the exact selectors that would have worked. A refusal that only says "no" is
   a dead end.
2. **Ship the discovery tool with the thing it discovers for.** A helper that
   exists only in the source tree does not exist for the people running the
   binary.
3. **Check your exception hierarchy at every catch site.** `except (AgentError,
   CredentialStoreError)` looked exhaustive. `AudioReceiverError` was a sibling,
   not a child, so a whole category of ordinary configuration mistakes came out
   as crashes.

---

## Learning Box 4 — Validate the thing you can validate first

The Receiver used to unseal its credential, connect, get promoted to PRIMARY,
and *then* resolve the audio device. A bad device selector therefore surfaced
after the Store already looked online.

Configuration that needs no secrets and no network should be checked before
anything that does. It is cheap, it fails in a second, and it fails while the
operator is still watching the console rather than looking at a dashboard.

---

## Learning Box 5 — "Hidden" usually hides the wrong thing

Task Scheduler has a **Hidden** setting. It hides the *task* in Task
Scheduler's own list. It does nothing whatsoever about whether the program you
scheduled puts a window on the screen.

Windows decides that from the **Subsystem** field in the executable's PE
header — `3` is a console application and gets a console; `2` is a windowed one
and does not. Nothing at launch time can change it.

So the way to have no console is to build a second, windowed executable. Which
then creates the real design problem: a windowed process has no stdout, so
`list-audio-devices` prints nothing and `enrol` cannot prompt. The answer was
to ship both from one PyInstaller analysis — console for the commands a person
runs and reads, windowed for the one Windows starts at logon.

**The general lesson.** When a setting is named after the outcome you want,
check what it actually controls before relying on it. And when a platform
constrains you (one binary, one console mode), the fix is usually *two
artefacts*, not a cleverer flag.

You can test this without a person watching a screen: read the PE header. That
check is now part of package verification.

---

## Learning Box 6 — Ask what the platform can physically do, before designing

The obvious answer to "make it start on its own" is a Windows service. It is
what services are for. Here it would have been silently, expensively wrong.

A service runs in **session 0**. Session 0 has no audio endpoint. The Receiver
would have started at boot, authenticated, decoded audio, written PCM into
nothing, and reported success — a more convincing silence than the bug that
started all this.

The tempting middle ground — a service that supervises a user-session agent —
was also rejected, and rejecting it is the more useful lesson. It sounds
rigorous. It adds a process, a failure domain and an IPC channel. And it *still*
cannot play a sound before somebody logs in, because the audio endpoint does not
exist until then. It would have bought complexity and no capability.

**The lesson.** Before choosing an architecture, write down the physical
constraint in one sentence — "the sound device belongs to a signed-in user's
session" — and check each candidate design against it. A design that cannot beat
the constraint is not a better design, however sophisticated it looks.

And then say the limitation out loud in the documentation: **announcements need
the Store user signed in.** A limitation that is written down is a requirement
someone can plan around. One that is buried is a 3am support call.

---

## Learning Box 7 — Check the premise before you build on it

A request arrived to "remove the inconsistent 16-character password minimum" and
to "add an OWNER role". Both sounded like straightforward work. Neither premise
survived five minutes of `git grep`.

**There was no 16-character rule.** Not anywhere, in any tracked file. The real
number was 12, in four places. Building to the stated brief would have meant
hunting for something that did not exist, then quietly "fixing" the wrong number.

**The OWNER role already existed**, called `SUPER_ADMIN`, with precisely the
protections being asked for. Adding a *second* top-level role beside it was the
genuinely dangerous option: two accounts-of-last-resort means two separate "this
one may not be removed" rules, and the failure mode is that only one of them
ends up being applied — which is exactly the permanent lockout the rule exists
to prevent. Renaming was smaller, safer, and gave the same vocabulary.

**The habit.** When a request describes the current state of the code, verify
that description before designing against it. Two greps cost nothing. Building
on a wrong premise costs the whole change, and the wrongness usually only
surfaces after review, when it looks like your mistake rather than a
misunderstanding.

Say the correction out loud, plainly, and then get on with the work.

---

## Learning Box 8 — Renaming a value is not the same as renaming a variable

A find-and-replace turned `Role.SUPER_ADMIN` into `Role.OWNER` across seventeen
files. It also rewrote this:

```python
LEGACY_ROLE_ALIASES = {"SUPER_ADMIN": Role.OWNER}   # before
LEGACY_ROLE_ALIASES = {"OWNER": Role.OWNER}         # after — a no-op
```

That is the one place in the entire change where the *old string literal* was
the point. The replacement left code that reads perfectly, passes a casual eye,
and silently removes backward compatibility: every account row still saying
`SUPER_ADMIN` would have parsed to `None`, which means **no permissions at all**
— every administrator locked out at the moment of upgrade.

**The lesson.** Before a bulk rename, ask which occurrences are *references* and
which are *data*. Compatibility shims, migration tables, serialized payloads and
test fixtures hold the old name deliberately. A rename tool cannot tell the
difference; you can, and the test that catches it is one asserting the old value
still resolves.

The same sweep also put a UTF-8 BOM on sixteen files via
`Set-Content -Encoding utf8`. That one was caught by a guard written after it
happened the first time — which is the whole argument for writing such guards.

---

## Learning Box 9 — Refusing must mean refusing

The owner-bootstrap command refuses when the account already exists. The
tempting shortcut is to make a second run "helpful" by resetting the password
instead.

Do not. A create command that quietly becomes a reset command is an account
takeover with a friendly name: whoever runs the installer next owns the account
that everything else is protected by.

There is a test for it, and the test is not "it raises an error" — it is that
the **stored hash is unchanged** and the **original password still verifies**
after the refusal. Asserting only on the exception would pass even if the row
had been rewritten first.

---

## Learning Box 10 — A process with no console gives its children a new one

The background Receiver was built GUI-subsystem specifically so no window would
appear. It worked. Then a black window appeared the moment a broadcast started.

The rule nobody had written down: on Windows, a process with **no** console that
starts a **console-subsystem** child gets that child a brand-new console — and a
new console is a visible window. Making the parent windowless does not make its
children windowless; if anything it guarantees the opposite.

The fix is `CREATE_NO_WINDOW` on every spawn. The measurement that proved it,
using `pythonw.exe` as a stand-in parent because it is GUI-subsystem too:

```
parent_has_console            : False
child, no creation flags      : has_console=True,  console_hwnd=721134
child, with CREATE_NO_WINDOW  : has_console=False, console_hwnd=0
```

Three lessons:

1. **Hiding the parent is not hiding the tree.** Any claim about "no window"
   has to name every process that will exist, not just the one you launched.
2. **Not `shell=True`.** Running through `cmd.exe` to hide a console swaps one
   console for another and adds a shell that parses your command line.
3. **Put the flags in one helper and route every spawn through it.** There were
   three spawn sites; the one that was missed was the only one that runs during
   a broadcast. A test now walks the AST and fails on any spawn that skips it.

And the honest limit: this environment has no interactive desktop, so window
enumeration returned "no visible window" for the broken *and* the fixed variant.
It proves nothing. The console-allocation measurement above is real evidence;
the window claim needs a person watching a Store screen.

---

## Learning Box 11 — Rebaseline deliberately, and keep the old value

A checksum guard on the live database failed. The file had changed: four
additive columns from a migration, no row deleted, no password hash touched.
Size identical, hash different — which is exactly why the guard checks both.

The tempting move is to paste the new hash over the old one and move on. That
turns a guard into a rubber stamp: it will "pass" for ever, because it is
re-derived from whatever the file happens to be whenever it complains.

What was done instead:

1. **Prove the content first, not the hash.** `integrity_check`, row counts in
   every table, the administrator's row and password-hash fingerprint, and that
   no plaintext password column exists. A hash tells you *that* something
   changed; only the content tells you *whether it mattered*.
2. **Take a real backup before accepting anything** — SQLite's backup API, not a
   file copy. Copying a WAL-mode database can capture the main file without the
   committed pages still sitting in the `-wal`, giving you a backup that is
   silently older than the thing it came from.
3. **Get an explicit decision.** Rebaselining is the operator's call, not the
   engineer's. Ask, with the evidence attached.
4. **Keep the old value next to the new one**, with a note on what moved it, and
   a test asserting the two differ. If somebody ever "fixes" a failure by
   overwriting the current hash with itself, that test notices.

The habit generalises well past checksums: any expected-value fixture — golden
files, snapshot tests, approved screenshots — is only worth something if
updating it is a decision somebody made rather than a reflex.

---

## Learning Box 12 — Prove the file is free before deleting anything beside it

The `-wal` and `-shm` sidecars had to go. Deleting a `-wal` that still holds
committed pages destroys data, so "it looks empty" is not good enough.

Two checks, both cheap:

```powershell
# 1. nothing references it
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'echocast_live' }

# 2. the decisive one - an exclusive open fails if anything holds the file
[System.IO.File]::Open($path, 'Open', 'ReadWrite', 'None')
```

The second is worth more than the first: process-list matching depends on
command lines that may not mention the file at all, while an exclusive open asks
the operating system directly. Plus the WAL was 0 bytes, so there was nothing to
lose either way — and the main file's hash was unchanged afterwards, which is
the proof that the removal took nothing with it.

---

## Learning Box 13 — A path built from the clock is a fact you forget

One line caused two bugs that looked completely unrelated:

```powershell
$pilotRoot = ...\lan-pilot\$(Get-Date -Format 'yyyyMMdd-HHmmss')
```

Every start of the server made a new, empty database. So:

* the Store went **OFFLINE** — its Device was in a database the server no longer
  used;
* **`owneradmin` could not sign in** — that account was in a different database.

Two tickets, two suspects (the Receiver, the auth code), one cause: *the server
forgot*. Neither suspect had a bug.

**The tell.** When two unrelated features break together after a restart,
suspect shared state that the restart discarded, before suspecting either
feature.

**The rule.** A throwaway environment and a daily one must be different tools
with different names, not the same tool run differently. Ours now refuses to
adopt a throwaway database at all, because "point it at the pilot file for now"
is exactly how the temporary thing becomes permanent.

And the smaller habit that made it visible: the pilot root changed **three
times** during a single day's investigation. Measuring the same thing twice, an
hour apart, is often faster than reasoning about it.

---

## Learning Box 14 — Repair must not be able to create what it repairs

A repair tool that can build a database is a repair tool that will one day build
one *over the real one* — and the operator will find out when every Store has
vanished.

So `Repair-EchoCastPersistentLanServer.ps1` splits its world in two:

* **rebuildable** — folder layout, a stale lock left by a dead process;
* **never touched** — database, keys, users, Stores, Devices, history, backups.

If the database is missing it stops and points at the backups. A missing
database is a *restore* decision, made by a person who knows which backup, not a
side effect of running a tool called "repair".

The same reasoning gave `Start` its refusal to create an empty fallback
database. Being helpful there is what turns "some data is missing" into "the
Store has disappeared".

---

## Learning Box 15 — Rebaseline by appending, never by overwriting

The protected-database hash has now moved twice, both times for a good reason
and both times with the operator's explicit approval.

What makes that safe is not the approval. It is that the file keeps the whole
chain:

```
8C858B13…  original
EEF1EA79…  + four additive user-lifecycle columns
8A7E3413…  + session_version, + the owneradmin row
9F155E1D…  exposed ADMIN password replaced, session_version 1 → 2
```

The fourth entry is the one worth studying, because it is the first that was not
a schema change. The size stayed at 507904 bytes through all four. When the
fourth one was checked, the size assertion **passed** and only the hash failed —
a password change rewrites one row in place. A guard that had checked size alone
would have reported the database as untouched while a credential inside it moved.

with what moved each one, and two tests: the chain contains no repeated entry,
and the current value is the newest rather than an older one pasted back.

A baseline overwritten in place looks identical to one that was never
challenged. Appending keeps the argument, so the next person can see that it
moved, when, and why — and can disagree.

The verification that mattered most was not the hash at all: `admin`'s
password-hash *fingerprint* was unchanged, which proved the existing account was
preserved rather than quietly rewritten. Check the property you actually care
about, not the checksum that noticed something.

---

## Learning Box 16 — Test the command, not the parser

Two of my own tests in this change passed while the code was broken.

- One asserted the exit code of a failed run. Both "bad audio device" and
  "no credential" exit `REFUSED`, so it passed even when the ordering was still
  wrong — the very thing it was written to pin.
- One asserted that `list-audio-devices` *parsed*. Running it crashed
  immediately, because `main()` computed a credential path for every command
  before dispatching and this command has none. It was the single command an
  operator runs before enrolment.

**Rule.** Assert on the specific message or state you care about, not on a
generic outcome several paths share. And invoke the entry point — parsing an
argument list proves the parser works, nothing more.

---

## Learning Box 17 — A build artifact can be verified and still do nothing

`EchoCastHQRuntime.exe` was built, its PE subsystem was read directly from the
file, it was confirmed `WINDOWS_GUI`, and it was committed. All of that was
correct and still is.

**The executable did nothing.** The module defined a supervisor class and never
called it — no `main`, no `__main__` block. Run it and it imports, defines, and
exits 0.

Exit 0 is what Task Scheduler records as *"the task ran successfully"*. The end
state would have been a green task history, no window, no error, and no HQ.

Why it got that far: **shape is easy to check and behaviour is not.** "Is this
file a GUI-subsystem PE?" is one header read. "Does this program do its job?"
needs somebody to run it and ask it a question. So the easy check got done, and
it looked like verification.

**Rule.** Verifying an artifact's *shape* is not verifying its *behaviour*.
Before a build is called done, run it and make it answer something — an exit
code, a status file, a health response. This is Learning Box 16's rule one level
up: don't test the parser, test the command; and don't measure the file, run it.

Everything else worth having in this phase came from doing that. Running
`--check` against the real profile found three more defects in minutes that
1864 passing tests had not:

- a refusal that no documented procedure could satisfy
- `sys.executable` being the supervisor itself once frozen
- `Path(__file__).parents[1]` being the bundle, not the repository

---

## Learning Box 18 — A test can pass without the fix it was written for

Twice in this project now.

Writing the guard for "has HQ ever actually started", I searched a *window of
text* in the script for the new helper's name. The helper was there. The two
checks that were supposed to call it were still calling the old one, a few lines
below the window. Green test, unfixed code.

I only caught it because the edit that was supposed to rewire those call sites
had failed loudly a moment earlier — and I nearly read the passing test as
proof that it hadn't mattered.

**Rule.** Assert on the **call site**, not on the presence of a name somewhere
in the file. "The function exists" and "the function is used" are different
claims, and only the second one is the fix.

**And the cheap way to prove it:** revert only the production file, run the test,
watch it fail, put the file back. Thirty seconds, and it converts "I believe
this test works" into evidence. If a test cannot be made to fail, it is not
holding anything.

---

## Learning Box 19 — Fix a defect in every language it lives in

The runtime demanded a key file that nothing creates before the first start, and
refused a correctly initialized HQ with an instruction no procedure in this
repository could carry out. I fixed it in Python, wrote it up, and moved on.

Minutes later the installer dry run failed for the identical reason. **The same
rule had been written twice — once in `hq_runtime.py`, once in
`Install-EchoCastHQAutoStart.ps1`** — and fixing one copy left the other.

**Rule.** When you fix a rule, grep for the rule, not for the function. A
duplicated *policy* is far more dangerous than duplicated code, because the two
copies drift silently and each one looks correct on its own.

The repair here was not "fix both copies" — it was to give each half of the rule
one home. The installer checks what a filesystem can answer (is there a database,
is there a keys folder). Whether a *missing* key container is normal or an
emergency depends on how many Devices are enrolled, so the runtime decides that,
because only the runtime can count them.

---

## A pass list can be a lie by omission

The credential remediation had 22 required proofs. My comparison script printed
`[PASS]` twenty-two times and I nearly reported it as complete.

Two of those proofs were *"Receiver Device rows unchanged"* and *"enrollment
records unchanged"*. The script looked those tables up, failed to find them,
caught the error, and printed **nothing at all** for them. Not a failure, not a
skip — nothing. The output looked like full coverage because the eye counts
`[PASS]` lines and cannot count absences.

The tables genuinely do not exist in either database: they are created by
`backend/migrations.py`, which has never run against those files. So the two
proofs *do* hold — there is no Device row and no Receiver credential in either
database to change. But that is a completely different statement from "I compared
the rows and they matched", and only one of them is true.

**Rule.** A check that cannot run must say so out loud. `SKIPPED - table absent`
is information; silence is a false negative wearing the costume of a pass. If your
loop has a `try/except: continue`, ask what the operator sees when it fires.

The same shape appeared twice more in the same session:

* A Store kit built from a Receiver package **three days old**, because I picked
  "newest" by sorting on **name** — and `ff04aea` sorts above `3c3d945`. The build
  script printed `package commit ff04aea` and `kit commit 3c3d945` on adjacent
  lines and still declared success. Nothing was hidden; nothing was compared
  either. **Sort by time, never by a name that contains a hash.**
* A test suite that passed a CRLF bug because the fixture used `\n` and the real
  file used `\r\n`. A fixture that does not resemble the file it stands in for
  cannot catch anything about that file.

## Hash the file, not just its size

The protected database has a recorded baseline: size **and** SHA-256. The comment
above it had always claimed the hash was the part that mattered. Nobody had seen it
proved.

Then the exposed ADMIN password was changed. The file stayed at **507904 bytes** —
exactly what it had been through all three previous baselines. On the failing run:

```
assert PROTECTED_DATABASE.stat().st_size == PROTECTED_BASELINE_SIZE   <- PASSED
assert digest == PROTECTED_BASELINE_SHA256                            <- FAILED
```

A password change rewrites one row in place. A size guard would have reported the
database as untouched while a credential inside it moved.

**Rule.** Size detects truncation and little else. If you care whether a file
changed, hash it.

There is a second half. The baseline moving is *expected* here, so it gets
rebaselined — and a baseline that is rebaselined every time it fails is not a
baseline. What makes it survivable is that the old value is appended to
`BASELINE_HISTORY` with the reason, two tests refuse a value pasted over history
instead of appended to it, and a *third* test now asserts the specific fact rather
than the hash:

```python
assert row[0] >= 2, "this database predates the exposed-password remediation"
```

`session_version` only counts up. The hash guard would notice a restore from a
pre-remediation backup, but only as "the hash moved" — which reads like any other
schema change and invites another rebaseline. Guard the *fact*, not just the bytes.

## Run the runbook before you need the runbook

`docs/ROLLBACK_PLAN.md` told an operator to run:

```powershell
python tools\compare_databases.py --left <current> --right <backup>
```

The tool takes **positional** paths. It read `--left` as a filename, printed
`UNREADABLE - file does not exist`, and then printed a confident-looking
recommendation about the two real files underneath — so the command half-worked,
which is worse than failing.

It was found by using it during a real incident. That is the worst possible moment
to discover that a documented command is wrong, and the only reason it was found at
all is that the incident forced someone to actually type it.

Worse: the tool's `SUGGESTION` ranks candidates by *operational history*, so with
equal row counts it recommended keeping the **pre-change** backup — which would
have restored the exposed password and undone the remediation. The tool ends with
*"This tool does not choose. You do."* That line is not decoration; it is the
safety mechanism, and the runbook now explains when to overrule the suggestion.

**Rule.** A runbook command that has never been executed is a guess. Execute it,
and read what it recommends as well as whether it runs.

## The dangerous bug is the one that looks healthy

A standby Receiver's acknowledgements were being written into its Store's health
snapshot. The audit recorded one consequence: interleaved sequence numbers
rejecting each other's messages. Writing tests found four, and the second was far
worse than the recorded one:

**A standby's heartbeat kept a switched-off primary reading as online.** Freshness
is decided by `last_received_at` on the Store snapshot; the standby refreshed it
every few seconds. HQ showed a green Store. Nothing came out of the speakers.

An obviously broken Store gets investigated. A green one does not. That is what
makes this class of defect expensive, and it is why the manual acceptance step for
it is *"switch the primary off at the wall and watch HQ go OFFLINE"* rather than
*"check both Devices appear"*.

Two of the four consequences also lived in the **database**, not just in memory:
the endpoint set `status='online'`, refreshed `last_seen`, and filed a `connected`
event for a standby connection. An in-memory-only fix would have looked complete
and left the half an operator actually reads.

**Rule.** When you fix an attribution bug, follow the value all the way to
whatever a human eventually looks at. Memory, database, dashboard, log. The fix is
not done at the first layer that looks right.

---

## A rule with three owners and no implementation

An earlier box in this guide says: *when you fix a rule, grep for the rule, not
for the function*, because a duplicated policy drifts and each copy looks correct
on its own.

This is the inverse, and it is worse.

Three places said the backend creates the Receiver HMAC key container on first
start: `tools/hq_runtime.py`, `scripts/Test-EchoCastHQAutoStart.ps1`, and the
tests written to match them. `backend/server.py` did not, and said so plainly in
its own docstring — *"The container is never created here"*.

Nobody was lying. Each statement was quoting the others. The runtime's comment
justified removing a refusal; the PowerShell check's comment justified treating an
absence as normal; the tests encoded both. Every reviewer who read any one of them
found a confident, well-reasoned claim with no reason to doubt it.

It survived a fourteen-area security audit, a full release-candidate gate, and a
`git grep` for `create_key_container` that found the function defined and tested
and never called from a production path. It was caught by the **first real
installed start**, by a check that had been written to pass before that start and
failed the moment there was something to check.

**Rule.** A comment that says *another component does X* is a claim about code you
are not looking at. Name the module that does it, so the next reader can check
instead of believe. Better: write a test that asserts the caller exists.
`test_the_server_module_calls_the_bootstrap_before_building_the_authenticator`
walks the AST and fails if the call is missing or in the wrong place — that is the
shape of assertion that would have caught this on the day it was written.

**Corollary.** "Grep found the function, so it is implemented" is not the same as
"grep found a caller on the path that runs in production". Search for the call
site, and check which path it is on.

## The convenient answer is the dangerous answer

The bootstrap may only create a key when zero Devices are enrolled. So the whole
design rests on one number, and the number has a failure mode: when the count
cannot be established, the tempting answer is zero, and zero is the answer that
mints a key over credentials that are still in use — 44 Stores re-enrolling.

So "I could not count them" must never become "there are none". That much was
already written down.

What I got wrong was the opposite direction. I treated a **missing database file**
as "could not establish" and refused. That refused 66 tests and would have refused
a genuine first-ever start, because the backend is imported before it creates its
own schema.

The two are not the same claim:

* **No file** — nothing can be enrolled in a file that does not exist. Zero, with
  certainty. Not a guess.
* **A file that will not open** — corrupt, locked, permission denied. Unknown, and
  unknown must fail closed.

**Rule.** When you write a fail-closed rule, enumerate the failure modes and ask
of each one: *is this actually unknown, or is it a definite answer I have not
distinguished?* Collapsing "certainly none" into "cannot tell" is as much a defect
as collapsing "cannot tell" into "none" — it is just a safer-looking one, so it
survives review and fails in production instead.

## A default path is a live path

The bootstrap is gated on environment variables. The first version gated on
`ECHOCAST_DB_PATH` alone.

`conftest.py` always sets `ECHOCAST_DB_PATH`. The container path falls back to
`SERVICE_CONTAINER_PATH` — `C:\ProgramData\EchoCast-AI\keys\` — which is the
machine's real service custody location. Every test database has zero Devices.

So running the test suite would have minted a **real key container in the
machine's service custody path**, which a later service-account HQ would have
found, opened and treated as the one it was always meant to have. Silent, and
indistinguishable from correct.

It was caught by asking "what does this do when the variable is unset?" before
running the suite, rather than after.

**Rule.** A fallback constant that points at a real system location is not a
default — it is a live target. Any code path that can write to it must require an
explicit configuration to get there, and a test should assert that the constant is
unreachable without one. The test here strips the docstring and walks the AST,
because the docstring *explains* the constant and a text scan flags its own
explanation. That is the fifth time prose has tripped a text scan in this
repository.
