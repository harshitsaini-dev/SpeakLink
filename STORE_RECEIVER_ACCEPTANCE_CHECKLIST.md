# Store Receiver — manual acceptance checklist

Eight tests. Each one proves something **no automated test in this repository
proves**, because each one needs a person, a real speaker, or an action that
disrupts a running machine.

Do them in order. One action at a time. Write down what you actually see and
hear — do not infer it from the HQ screen.

> **Nothing in this file has been performed by the automated build.** Every box
> below is currently unproven.

---

## Before you start

On the Store PC, after installation:

```powershell
& "$env:LOCALAPPDATA\EchoCast-AI\receiver-app\EchoCastReceiver.exe" diagnose
```

Write down the `installed ver`, `source commit` and `audio device` lines. If
anything later goes wrong, this is the "before" picture.

---

## TEST 1 — PowerShell independence

**What it proves:** the Receiver is a separate background process, not something
living inside your PowerShell window.

1. Install the Receiver (see the kit's `README-FIRST.txt`).
2. Confirm on HQ that the Store is **ONLINE**.
3. Close **every** PowerShell window on the Store PC.
4. Wait one minute.
5. Look at HQ. The Store must still be **ONLINE**.
6. Run a **five-second** broadcast from HQ.
7. **Listen.** Write down what you heard, in your own words.

- [ ] Store stayed ONLINE with no PowerShell open
- [ ] Sound was heard — exact words: _______________________
- [ ] No black console window appeared at any point

---

## TEST 2 — sign out and sign in

**What it proves:** it comes back by itself when a member of staff signs in.

1. Sign out of Windows on the Store PC.
2. Sign back in as the same user.
3. **Open nothing. Start nothing.**
4. Watch HQ. Note how long until the Store shows **ONLINE**: ______ seconds.
5. Run a five-second broadcast. Listen.

- [ ] Store came ONLINE without anybody starting anything
- [ ] Sound was heard
- [ ] Time to ONLINE: ______

---

## TEST 3 — reboot

**Only do this when you are ready for the Store PC to be off for a few minutes.**

1. Restart the Store PC.
2. At the login screen, **stop and look at HQ**. Note whether the Store is
   ONLINE. It is expected to be **OFFLINE** — the Receiver has not started,
   because nobody has signed in.
3. Sign in normally.
4. Open nothing.
5. Note how long until HQ shows **ONLINE**: ______ seconds.
6. Run a five-second broadcast. Listen.

- [ ] At the login screen the Store was OFFLINE (expected)
- [ ] After sign-in the Store came ONLINE by itself
- [ ] Sound was heard
- [ ] Time from sign-in to ONLINE: ______

> If step 2 showed ONLINE, tell HQ — that would mean something is running that
> this design does not expect.

---

## TEST 4 — HQ backend outage

**Ask HQ before doing this. It affects every Store.**

1. Note the Receiver is running (`diagnose`, or HQ shows ONLINE).
2. HQ stops the backend.
3. Wait two minutes. On the Store PC check the Receiver is **still running**:
   ```powershell
   Get-Process EchoCastReceiverBackground -ErrorAction SilentlyContinue
   ```
   It must still be there. It is retrying, not dead.
4. HQ starts the backend again.
5. Note how long until HQ shows the Store ONLINE again: ______ seconds.

- [ ] Receiver process stayed alive during the outage
- [ ] Store reconnected by itself
- [ ] Reconnect time: ______

---

## TEST 5 — network outage

1. Unplug the Store PC's network cable (or turn off its Wi-Fi).
2. Wait two minutes. Confirm the Receiver process is still running.
3. Reconnect the network.
4. Note how long until HQ shows ONLINE: ______ seconds.

- [ ] Receiver survived the disconnection
- [ ] Reconnected by itself
- [ ] Reconnect time: ______

---

## TEST 6 — Receiver crash

**What it proves:** Windows brings it back without anybody noticing.

1. End the Receiver deliberately:
   ```powershell
   Stop-Process -Name EchoCastReceiverBackground -Force
   ```
2. Confirm HQ shows the Store go **OFFLINE**.
3. **Wait up to six minutes** and do nothing.
4. Note when HQ shows **ONLINE** again: ______ minutes.

- [ ] It came back with no human action
- [ ] Time to recover: ______

> Recovery here is the task's repetition schedule, which is set to five minutes
> by default. It is not instant, and it is not meant to be.

---

## TEST 7 — lock and unlock

**This one has an honest unknown in it. Report what you find.**

1. Start a broadcast from HQ that lasts about 30 seconds.
2. While it is playing, press `Win+L` to lock the screen. **Keep listening.**
3. Write down: did the audio keep playing while locked? YES / NO
4. Unlock.
5. Check HQ still shows the Store ONLINE.
6. Run another five-second broadcast. Listen.

- [ ] Audio while locked: YES / NO  ← whatever it is, write it down
- [ ] Still ONLINE after unlocking
- [ ] Sound heard after unlocking

> Nobody has measured this yet. A locked screen keeps the user signed in, so the
> audio session should survive — but "should" is not evidence, which is why you
> are being asked.

---

## TEST 8 — missing audio device

**What it proves:** it says DEVICE_ERROR instead of pretending to play.

1. Unplug the earphones/speakers, **or** disable the output device in Windows
   Sound settings.
2. Run a five-second broadcast from HQ.
3. Look at the Store's status on HQ. It must show **DEVICE_ERROR**, not
   PLAYBACK_CONFIRMED.
4. Plug the device back in / re-enable it.
5. Run another five-second broadcast. Listen.

- [ ] HQ showed DEVICE_ERROR, not a fake PLAYBACK_CONFIRMED
- [ ] After reconnecting, sound was heard again
- [ ] No reinstall and no re-enrolment was needed

> If Windows renumbered the device while it was unplugged, the Receiver refuses
> to open a *different* speaker rather than guessing. If that happens, run
> `list-audio-devices` again and then
> `Repair-EchoCastStoreReceiver.ps1 -PackagePath .\Receiver -AudioOutputDevice "index:N@Name"`.

---

## What none of these prove

Even with all eight green:

- **Audio before anybody logs in.** Not supported, not implemented, and not
  possible in this design — the Windows sound device belongs to a signed-in
  user's session.
- **`SPEAKER_VERIFIED`.** That is an EchoGuard hardware result. What you are
  recording above is a human hearing something, which is more than the software
  can claim and less than acoustic verification.
- **Anything about the other 43 Stores.**
