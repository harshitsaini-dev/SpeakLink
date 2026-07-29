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
```

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
