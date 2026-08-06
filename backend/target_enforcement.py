"""Bringing a Store back to its configured TARGET, without starting a fight.

THE PRODUCT DECISION THIS IMPLEMENTS

While a Store is online and no broadcast owns it, the TARGET is authoritative.
If somebody at the till turns the shop down to 25% when HQ has configured 70%,
SpeakLink puts it back.

WHY THAT IS DANGEROUS, AND WHAT MAKES IT SAFE

The obvious implementation - "see a mismatch, send a command" - produces a
literal fight with a human being holding a mouse. They drag the slider, HQ
snaps it back, they drag it again, and the Windows volume oscillates while two
parties argue at machine speed. The endpoint notification arrives within
milliseconds, so that loop would run as fast as the socket allows.

Four bounds prevent it, and every one of them is deliberate:

**Debounce.** Nothing is enforced until a Store has been left alone for
``DEBOUNCE_SECONDS``. Somebody adjusting the volume gets to finish adjusting
it. This also collapses a drag - dozens of readings - into at most one command.

**Cooldown.** After SpeakLink itself sends an enforcement command, that Store is
left alone for ``COOLDOWN_SECONDS`` whatever it reports. This is what stops a
command and its own readback from triggering the next one.

**An attempt budget.** A Store may be enforced at most ``MAX_ATTEMPTS`` times
before SpeakLink gives up and says so. A shop where somebody is determined to
have it quieter, or where a third application keeps moving the mixer, must end
in an honest ``ENFORCEMENT_SUSPENDED`` rather than an endless argument. The
budget resets when the Store is seen matching its target, or when an operator
sets a new one.

**Ownership and restoration.** Enforcement never runs while a broadcast owns
the Store, and never within ``POST_BROADCAST_SECONDS`` of a restoration -
otherwise STOP would appear to put a shop back to 25% and something would
immediately drag it to 70% in the same breath.

WHAT THIS MODULE IS NOT

It is not a scheduler and it holds no timers. It is a pure decision function
plus a small amount of per-Store bookkeeping, so the policy can be tested by
calling it rather than by waiting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from threading import RLock

#: How long a Store must be quiet before SpeakLink acts on a mismatch. Long
#: enough that a person adjusting the volume is finished; short enough that a
#: shop is not left wrong for a noticeable part of a trading day.
DEBOUNCE_SECONDS = 20.0

#: How long a Store is left alone after SpeakLink sends an enforcement command.
#: Covers the command, the mixer change, the endpoint notification and the
#: readback, so none of those can look like fresh drift.
COOLDOWN_SECONDS = 15.0

#: Attempts before SpeakLink stops and says it has stopped. Three is enough to
#: ride out a transient - an application that momentarily grabbed the mixer -
#: and few enough that a genuine disagreement surfaces as a fact an operator
#: can act on rather than as traffic nobody sees.
MAX_ATTEMPTS = 3

#: Quiet period after a broadcast restores a Store's original state. STOP must
#: be seen to restore 25% muted; enforcement arriving in the same moment would
#: make the restoration look like it never happened.
POST_BROADCAST_SECONDS = 30.0

#: Reasons enforcement did not run. Returned rather than logged so callers -
#: including tests - can assert on WHY, not merely that nothing happened.
ENFORCE = "enforce"
SKIP_NO_TARGET = "no target configured"
SKIP_MATCHES = "already at target"
SKIP_OFFLINE = "receiver offline"
SKIP_ENDPOINT = "no controllable output"
SKIP_BROADCAST = "a broadcast owns this Store"
SKIP_DEBOUNCE = "recently changed at the Store"
SKIP_COOLDOWN = "recently enforced"
SKIP_POST_BROADCAST = "restoring after a broadcast"
SKIP_SUSPENDED = "enforcement suspended after repeated attempts"


@dataclass
class _StoreBookkeeping:
    #: Monotonic time of the last reading that did NOT match the target.
    last_drift_at: float | None = None
    #: Monotonic time SpeakLink last sent an enforcement command.
    last_enforced_at: float | None = None
    #: Monotonic time a broadcast last restored this Store.
    restored_at: float | None = None
    attempts: int = 0
    suspended: bool = False


@dataclass
class EnforcementDecision:
    """Whether to act, and why. ``reason`` is always populated."""

    should_enforce: bool
    reason: str

    def __bool__(self) -> bool:                       # pragma: no cover - clarity
        return self.should_enforce


class TargetEnforcementPolicy:
    """Per-Store bookkeeping plus the decision. Runtime only, never persisted."""

    def __init__(self, *, debounce_seconds: float = DEBOUNCE_SECONDS,
                 cooldown_seconds: float = COOLDOWN_SECONDS,
                 max_attempts: int = MAX_ATTEMPTS,
                 post_broadcast_seconds: float = POST_BROADCAST_SECONDS) -> None:
        self.debounce_seconds = debounce_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_attempts = max_attempts
        self.post_broadcast_seconds = post_broadcast_seconds
        self._lock = RLock()
        self._stores: dict[int, _StoreBookkeeping] = {}

    # ---- bookkeeping --------------------------------------------------
    def _entry(self, store_id: int) -> _StoreBookkeeping:
        with self._lock:
            entry = self._stores.get(store_id)
            if entry is None:
                entry = _StoreBookkeeping()
                self._stores[store_id] = entry
            return entry

    def note_reading(self, *, store_id: int, matches_target: bool, now: float) -> None:
        """Record what a Store just reported.

        A reading that MATCHES resets everything: the shop is where it should
        be, so whatever argument was happening is over.
        """
        entry = self._entry(store_id)
        with self._lock:
            if matches_target:
                entry.last_drift_at = None
                entry.attempts = 0
                entry.suspended = False
            elif entry.last_drift_at is None:
                # The moment drift STARTED, not the latest reading. Otherwise a
                # slow continuous drag would reset the debounce for ever and
                # nothing would be enforced at all.
                entry.last_drift_at = now

    def note_enforced(self, *, store_id: int, now: float) -> None:
        entry = self._entry(store_id)
        with self._lock:
            entry.last_enforced_at = now
            entry.last_drift_at = None
            entry.attempts += 1
            if entry.attempts >= self.max_attempts:
                # Said out loud rather than tried for ever. Somebody is
                # disagreeing with HQ, and that is information.
                entry.suspended = True

    def note_target_changed(self, *, store_id: int) -> None:
        """An operator set a new target, so any previous argument is void."""
        entry = self._entry(store_id)
        with self._lock:
            entry.attempts = 0
            entry.suspended = False
            entry.last_drift_at = None

    def note_broadcast_restored(self, *, store_id: int, now: float) -> None:
        entry = self._entry(store_id)
        with self._lock:
            entry.restored_at = now
            entry.last_drift_at = None

    def is_suspended(self, store_id: int) -> bool:
        return self._entry(store_id).suspended

    def attempts(self, store_id: int) -> int:
        return self._entry(store_id).attempts

    def forget(self, store_id: int) -> None:
        with self._lock:
            self._stores.pop(store_id, None)

    # ---- the decision -------------------------------------------------
    def decide(self, *, store_id: int, target, actual_volume_percent,
               actual_muted, online: bool, endpoint_ready: bool,
               owned_by_broadcast: bool, now: float) -> EnforcementDecision:
        """Should SpeakLink put this Store back to its target right now?

        Ordered so the most important refusals win. Ownership outranks
        everything: a broadcast is the single writer while it runs, and no
        amount of drift justifies a second one.
        """
        if target is None:
            return EnforcementDecision(False, SKIP_NO_TARGET)
        if owned_by_broadcast:
            return EnforcementDecision(False, SKIP_BROADCAST)
        if not online:
            return EnforcementDecision(False, SKIP_OFFLINE)
        if not endpoint_ready:
            return EnforcementDecision(False, SKIP_ENDPOINT)

        matches = (
            (target.volume_percent is None
             or target.volume_percent == actual_volume_percent)
            and (target.muted is None
                 or bool(target.muted) == bool(actual_muted))
        )
        if matches:
            return EnforcementDecision(False, SKIP_MATCHES)

        entry = self._entry(store_id)
        if entry.suspended:
            return EnforcementDecision(False, SKIP_SUSPENDED)
        if (entry.restored_at is not None
                and now - entry.restored_at < self.post_broadcast_seconds):
            return EnforcementDecision(False, SKIP_POST_BROADCAST)
        if (entry.last_enforced_at is not None
                and now - entry.last_enforced_at < self.cooldown_seconds):
            return EnforcementDecision(False, SKIP_COOLDOWN)
        if entry.last_drift_at is None:
            # Never seen this Store drift. Start the clock now rather than
            # acting on the very first reading after a restart.
            with self._lock:
                entry.last_drift_at = now
            return EnforcementDecision(False, SKIP_DEBOUNCE)
        if now - entry.last_drift_at < self.debounce_seconds:
            return EnforcementDecision(False, SKIP_DEBOUNCE)

        return EnforcementDecision(True, ENFORCE)


def _tuned(name: str, default: float) -> float:
    """Read a policy bound from the environment.

    Exists so the load harness can exercise the mechanism in a run that lasts
    seconds rather than minutes. The DEFAULTS are the product behaviour; an
    override only ever shortens a wait, and there is deliberately no way to
    switch a bound off.
    """
    import os

    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


#: One policy for the process, like the other runtime registries.
policy = TargetEnforcementPolicy(
    debounce_seconds=_tuned("SPEAKLINK_TARGET_DEBOUNCE_SECONDS", DEBOUNCE_SECONDS),
    cooldown_seconds=_tuned("SPEAKLINK_TARGET_COOLDOWN_SECONDS", COOLDOWN_SECONDS),
    max_attempts=int(_tuned("SPEAKLINK_TARGET_MAX_ATTEMPTS", MAX_ATTEMPTS)),
    post_broadcast_seconds=_tuned("SPEAKLINK_TARGET_POST_BROADCAST_SECONDS",
                                  POST_BROADCAST_SECONDS),
)
