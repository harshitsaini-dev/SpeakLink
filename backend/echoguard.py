"""The contract EchoGuard must satisfy, and a no-op that satisfies nothing.

There is no EchoGuard **pause/resume** implementation in this repository - no
code, executable, configuration or IPC surface that can be asked to stop
playing. Nothing here pretends otherwise: the protocol below describes what an
adapter must do, and the only implementation provided announces that it did
nothing.

One piece does already exist, and it is not this one. ``receiver_contract.py``
defines ``TrustedSpeakerVerifiedEvent`` with ``source: Literal["echoguard"]``,
parsed by its own adapter and deliberately excluded from the ordinary
acknowledgement union - so a Receiver claiming ``speaker_verified`` is rejected,
and only a trusted acoustic verifier on a separate path can assert it. That is
the *acoustic verification* contract. This module is the *pause and resume*
contract, which is a different problem and has no counterpart yet.

Why write the contract before the integration exists: the broadcast path needs
to know *now* what it will have to do around a pause - whether it waits, how
long, and what happens when the answer never comes. Deciding that later, under
time pressure, is how "we sent the command" quietly becomes "it was paused".

The rule that shapes everything here: **a command sent is not a pause
performed.** Every method returns what actually happened, and
:class:`NullEchoGuard` always reports ``UNAVAILABLE`` rather than ``PAUSED``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class GuardOutcome(Enum):
    """What actually happened, never what was attempted."""

    PAUSED = "paused"
    RESUMED = "resumed"
    ALREADY_IN_STATE = "already_in_state"
    TIMED_OUT = "timed_out"
    REFUSED = "refused"
    UNAVAILABLE = "unavailable"


#: Outcomes after which live audio would overlap EchoGuard's own output.
#: A broadcast may still proceed - an emergency announcement must not be blocked
#: by a silent monitoring service - but the session must be marked at risk.
OVERLAP_RISK_OUTCOMES = frozenset(
    {GuardOutcome.TIMED_OUT, GuardOutcome.REFUSED, GuardOutcome.UNAVAILABLE}
)


@dataclass(frozen=True)
class GuardResult:
    """One pause or resume attempt.

    ``detail`` is a short bounded string for an operator. It must never carry a
    credential, a path, or free text supplied by a remote service.
    """

    outcome: GuardOutcome
    detail: str = ""

    @property
    def overlap_risk(self) -> bool:
        return self.outcome in OVERLAP_RISK_OUTCOMES

    @property
    def succeeded(self) -> bool:
        return self.outcome in {GuardOutcome.PAUSED, GuardOutcome.RESUMED, GuardOutcome.ALREADY_IN_STATE}


@runtime_checkable
class EchoGuardAdapter(Protocol):
    """What a real EchoGuard integration must provide.

    Required semantics, all of which a real adapter has to honour:

    * **Idempotent.** Pausing an already-paused EchoGuard returns
      ``ALREADY_IN_STATE``, not an error, because a retry after a dropped
      response must be safe.
    * **Acknowledged, not fired.** ``pause`` must wait for EchoGuard to confirm,
      bounded by ``timeout_seconds``, and return ``TIMED_OUT`` when it does not.
    * **Never silently successful.** There is no return value meaning "command
      sent".
    * **Recoverable.** ``resume`` must be safe to call after a crash or restart,
      including when this process never issued the matching pause.
    """

    name: str

    def pause(self, *, session_id: int, timeout_seconds: float) -> GuardResult: ...

    def resume(self, *, session_id: int, timeout_seconds: float) -> GuardResult: ...

    def status(self) -> GuardResult: ...


class NullEchoGuard:
    """The only implementation that exists, and it does nothing.

    It is not a stub standing in for a working integration. It reports
    ``UNAVAILABLE`` so that any code path treating "EchoGuard was paused" as
    true will fail its own assertions rather than quietly proceed.
    """

    name = "null"

    def pause(self, *, session_id: int, timeout_seconds: float) -> GuardResult:
        return GuardResult(GuardOutcome.UNAVAILABLE, "no EchoGuard integration is configured")

    def resume(self, *, session_id: int, timeout_seconds: float) -> GuardResult:
        return GuardResult(GuardOutcome.UNAVAILABLE, "no EchoGuard integration is configured")

    def status(self) -> GuardResult:
        return GuardResult(GuardOutcome.UNAVAILABLE, "no EchoGuard integration is configured")


def describe_overlap_risk(result: GuardResult) -> str:
    """One sentence an operator can act on, for the broadcast record."""
    if not result.overlap_risk:
        return ""
    return (
        "EchoGuard was not confirmed paused, so this announcement may have "
        "overlapped its output."
    )


__all__ = [
    "OVERLAP_RISK_OUTCOMES",
    "EchoGuardAdapter",
    "GuardOutcome",
    "GuardResult",
    "NullEchoGuard",
    "describe_overlap_risk",
]
