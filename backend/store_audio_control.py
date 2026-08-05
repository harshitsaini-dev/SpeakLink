"""Per-Store output volume and mute for the duration of one broadcast.

WHY THIS IS RUNTIME STATE AND NOT A TABLE

A slider produces dozens of values per drag. Forty Stores being adjusted during
one announcement is thousands of writes describing something that stops being
true the moment the broadcast ends. None of it is configuration and none of it
is history, so none of it belongs in SQLite: this module holds it in memory for
the life of the session and forgets it when the session ends, exactly like the
audio queues alongside it.

What DOES deserve to be durable - a Store's saved announcement level - is
deliberately not implemented here. See the module note at the bottom.

WHAT "APPLIED" MEANS

Two values per Store, never conflated:

    requested_*   what the operator asked for
    applied_*     what the Store said it actually did

They differ whenever a command is in flight, has failed, or reached a Receiver
too old to understand it. The UI renders the difference rather than hiding it,
because "HQ sent 50" and "this shop is at 50" are different claims and only the
second one is about the sound in the room.

MUTE IS NOT VOLUME ZERO

Mute is its own flag. Collapsing it into ``volume_percent = 0`` would destroy
the operator's chosen level, and unmuting would have nothing to restore. A
muted Store also remains a full broadcast target - it keeps its lease, keeps
receiving the stream, and is never marked offline or stopped. It is a Store
that is being sent audio at zero output, which is a different thing from a
Store that is not in the broadcast.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace

MIN_VOLUME_PERCENT = 0
MAX_VOLUME_PERCENT = 100
DEFAULT_VOLUME_PERCENT = 100
DEFAULT_MUTED = False


class StoreAudioControlError(RuntimeError):
    """Base class, so no caller handles one refusal and misses another."""


class UnknownSessionError(StoreAudioControlError):
    """No live control state for that broadcast.

    Raised for a session that has ended as well as one that never existed, and
    deliberately with the same message: a stale command from a finished
    broadcast and a guessed session id deserve the same answer.
    """

    def __init__(self) -> None:
        super().__init__("That broadcast is no longer active.")


class StoreNotInSessionError(StoreAudioControlError):
    """The Store is not a target of this broadcast.

    Its own refusal rather than a generic "not found" so the endpoint can
    answer a request naming somebody else's Store without ever confirming
    whether that Store exists or is busy elsewhere.
    """

    def __init__(self) -> None:
        super().__init__("That Store is not part of this broadcast.")


class InvalidVolumeError(StoreAudioControlError):
    """Out-of-range volume.

    REJECTED, not clamped. Clamping 150 to 100 silently would answer a request
    the caller did not make and report success for it - and if the caller is a
    future client with a boost feature, the operator would be told their Store
    is at 150 when it is at 100. The contract is a whole percent 0-100.
    """

    def __init__(self, value: object) -> None:
        super().__init__(
            f"Volume must be a whole number between {MIN_VOLUME_PERCENT} and "
            f"{MAX_VOLUME_PERCENT}; got {value!r}."
        )


@dataclass(frozen=True, slots=True)
class StoreAudioState:
    """One Store's control state inside one broadcast."""

    store_id: int
    requested_volume_percent: int = DEFAULT_VOLUME_PERCENT
    requested_muted: bool = DEFAULT_MUTED
    applied_volume_percent: int | None = None
    applied_muted: bool | None = None
    #: The newest command sent. 0 means "nothing sent yet", which is why
    #: command ids start at 1 and why an acknowledgement carrying 0 is invalid.
    last_command_id: int = 0
    last_acknowledged_command_id: int = 0
    #: applied | unsupported | failed | None (nothing acknowledged yet)
    last_result: str | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    output_device: str | None = None

    # ---- what the Store's Windows output is ACTUALLY doing ----------------
    #
    # Kept apart from requested_* on purpose. HQ asking for 80% and the person
    # at the till then moving the slider to 25% are both true, and collapsing
    # them would make the Console display a number nobody could act on. The
    # requested value is what this operator asked for; the actual value is
    # what the shop is doing, and the actual value is what the Console shows.
    actual_volume_percent: int | None = None
    actual_muted: bool | None = None
    #: Monotonic per Receiver session. Telemetry older than this is discarded,
    #: so a delayed notification cannot drag the Console backwards.
    actual_state_sequence: int = 0
    actual_state_updated_at: str | None = None

    @property
    def pending(self) -> bool:
        """A command is in flight: sent, not yet answered."""
        return self.last_command_id > self.last_acknowledged_command_id

    @property
    def effective_volume_percent(self) -> int:
        """What the Store should actually be emitting, mute included."""
        return 0 if self.requested_muted else self.requested_volume_percent

    def as_dict(self) -> dict:
        return {
            "store_id": self.store_id,
            "requested_volume_percent": self.requested_volume_percent,
            "requested_muted": self.requested_muted,
            "applied_volume_percent": self.applied_volume_percent,
            "applied_muted": self.applied_muted,
            "last_command_id": self.last_command_id,
            "last_acknowledged_command_id": self.last_acknowledged_command_id,
            "result": self.last_result,
            "error_code": self.last_error_code,
            "error_message": self.last_error_message,
            "output_device": self.output_device,
            "pending": self.pending,
            "actual_volume_percent": self.actual_volume_percent,
            "actual_muted": self.actual_muted,
            "actual_state_sequence": self.actual_state_sequence,
            "actual_state_updated_at": self.actual_state_updated_at,
        }


def _validate_volume(value: object) -> int:
    # bool is an int in Python, and True would otherwise pass as 1%.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidVolumeError(value)
    if not MIN_VOLUME_PERCENT <= value <= MAX_VOLUME_PERCENT:
        raise InvalidVolumeError(value)
    return value


@dataclass
class _SessionControl:
    session_id: int
    owner_user_id: int
    stores: dict[int, StoreAudioState] = field(default_factory=dict)
    #: Monotonic across the WHOLE session, not per Store. A single sequence
    #: makes "which command is newer" answerable without comparing per-Store
    #: counters, and it is what lets a late acknowledgement be recognised as
    #: stale rather than applied on top of a newer value.
    next_command_id: int = 1


class StoreAudioControlRegistry:
    """Live control state for every broadcast currently on air.

    One lock for the whole registry rather than one per session. The critical
    sections are a few dictionary operations with no I/O inside them - sending
    to a Receiver happens outside the lock, in the caller - so contention is
    not a real cost, and a single lock cannot deadlock against itself the way
    a lock-per-session plus a registry lock can.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, _SessionControl] = {}
        self._lock = threading.Lock()

    # ---- lifecycle ---------------------------------------------------------
    def start_session(self, *, session_id: int, owner_user_id: int,
                      store_ids) -> None:
        """Begin control state for a broadcast, at the product defaults.

        Deliberately 100% and unmuted rather than whatever the Store's Windows
        endpoint happened to be sitting at. Adopting the current system volume
        would make EchoCast's default silently depend on whatever a previous
        application left behind, and an announcement that plays at 12% because
        somebody watched a video that morning is not a default anyone chose.
        """
        with self._lock:
            self._sessions[session_id] = _SessionControl(
                session_id=session_id,
                owner_user_id=owner_user_id,
                stores={
                    int(store_id): StoreAudioState(store_id=int(store_id))
                    for store_id in store_ids
                },
            )

    def end_session(self, session_id: int) -> None:
        """Forget a broadcast's control state. Idempotent."""
        with self._lock:
            self._sessions.pop(session_id, None)

    def active_session_ids(self) -> tuple[int, ...]:
        with self._lock:
            return tuple(self._sessions)

    # ---- reads -------------------------------------------------------------
    def session_owner(self, session_id: int) -> int:
        with self._lock:
            control = self._sessions.get(session_id)
            if control is None:
                raise UnknownSessionError()
            return control.owner_user_id

    def describe(self, session_id: int) -> list[dict]:
        with self._lock:
            control = self._sessions.get(session_id)
            if control is None:
                raise UnknownSessionError()
            return [state.as_dict()
                    for _, state in sorted(control.stores.items())]

    def state_for(self, session_id: int, store_id: int) -> StoreAudioState:
        with self._lock:
            control = self._sessions.get(session_id)
            if control is None:
                raise UnknownSessionError()
            state = control.stores.get(int(store_id))
            if state is None:
                raise StoreNotInSessionError()
            return state

    # ---- writes ------------------------------------------------------------
    def request(self, *, session_id: int, store_id: int,
                volume_percent: int | None = None,
                muted: bool | None = None) -> StoreAudioState:
        """Record a new desired state and allocate its command id.

        Either field may be omitted to leave it unchanged, which is what lets
        Mute be a single toggle that preserves the operator's chosen level:
        muting sends ``muted=True`` and says nothing about volume, so unmuting
        restores the number that was there all along rather than a remembered
        copy of it that could drift.
        """
        with self._lock:
            control = self._sessions.get(session_id)
            if control is None:
                raise UnknownSessionError()
            state = control.stores.get(int(store_id))
            if state is None:
                raise StoreNotInSessionError()

            if volume_percent is not None:
                volume_percent = _validate_volume(volume_percent)
            if muted is not None and not isinstance(muted, bool):
                raise StoreAudioControlError("muted must be true or false")

            command_id = control.next_command_id
            control.next_command_id += 1
            updated = replace(
                state,
                requested_volume_percent=(
                    state.requested_volume_percent if volume_percent is None
                    else volume_percent
                ),
                requested_muted=(
                    state.requested_muted if muted is None else muted
                ),
                last_command_id=command_id,
            )
            control.stores[int(store_id)] = updated
            return updated

    def acknowledge(self, *, session_id: int, store_id: int, command_id: int,
                    result: str, applied_volume_percent: int | None = None,
                    applied_muted: bool | None = None,
                    output_device: str | None = None,
                    error_code: str | None = None,
                    error_message: str | None = None) -> StoreAudioState | None:
        """Record what a Store reported. Returns None if the ACK was stale.

        A drag produces 40, 45, 55, 70 in quick succession. Their
        acknowledgements can arrive in any order, and an ACK for 45 landing
        after 70 was applied must not walk the display backwards to 45. The
        rule is simply that an acknowledgement older than the newest one
        already recorded is discarded - not merged, not partially applied.

        Discarding is safe precisely because a command carries whole state
        rather than a delta: the newest command already describes everything
        the Store should be doing, so nothing is lost by ignoring an older
        answer about an older question.
        """
        with self._lock:
            control = self._sessions.get(session_id)
            if control is None:
                return None
            state = control.stores.get(int(store_id))
            if state is None:
                return None
            if command_id <= state.last_acknowledged_command_id:
                return None
            # An acknowledgement for a command this session never issued is
            # not merely late - it is not ours. Ignore it rather than trusting
            # a Store's word about which command it was answering.
            if command_id > state.last_command_id:
                return None

            updated = replace(
                state,
                applied_volume_percent=applied_volume_percent,
                applied_muted=applied_muted,
                last_acknowledged_command_id=command_id,
                last_result=result,
                last_error_code=error_code,
                last_error_message=error_message,
                output_device=output_device or state.output_device,
            )
            control.stores[int(store_id)] = updated
            return updated


    def observe_endpoint_state(self, *, session_id: int, store_id: int,
                               state_sequence: int, volume_percent: int,
                               muted: bool, observed_at: str | None = None):
        """Record what a Store's Windows output is actually doing.

        Telemetry, not a command result: it updates the ACTUAL fields and
        never touches requested_* or any command id. A Store user turning the
        volume down does not retract the operator's request, and must not look
        like one.

        Returns None when the update is stale or not ours, so the caller sends
        no dashboard notification for it.
        """
        from datetime import datetime, timezone

        with self._lock:
            control = self._sessions.get(session_id)
            if control is None:
                # An old session reporting into a broadcast that has ended, or
                # one that was never ours. Ignored rather than applied.
                return None
            state = control.stores.get(int(store_id))
            if state is None:
                return None
            if state_sequence <= state.actual_state_sequence:
                # Out of order. The newest reading already on file is the one
                # that is true; an older one arriving late is not news.
                return None
            volume_percent = _validate_volume(volume_percent)
            if not isinstance(muted, bool):
                raise StoreAudioControlError("muted must be true or false")

            updated = replace(
                state,
                actual_volume_percent=volume_percent,
                actual_muted=muted,
                actual_state_sequence=state_sequence,
                actual_state_updated_at=observed_at
                or datetime.now(timezone.utc).isoformat(),
            )
            control.stores[int(store_id)] = updated
            return updated


#: The one registry the application uses. Imported rather than constructed by
#: callers so a second, invisible copy of the live control state cannot exist.
registry = StoreAudioControlRegistry()


# ---------------------------------------------------------------------------
# NOT IMPLEMENTED HERE, ON PURPOSE: a persistent per-Store announcement default
#
# A saved "this shop announces at 75%" is a genuinely useful setting, and it is
# a different feature from this one. It needs a table, an editor, a permission,
# an audit trail, and an answer to what happens when a saved default and a live
# override disagree at the moment a broadcast starts. Adding it here would have
# meant a schema change in a checkpoint whose whole point is that it needs no
# migration - so live control ships first and defaults follow.
# ---------------------------------------------------------------------------
