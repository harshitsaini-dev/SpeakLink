# SpeakLink learning guide

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
  person or LinkGuard hardware, and no amount of software can promote itself
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

## Learning Box 5 — Test the command, not the parser

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
