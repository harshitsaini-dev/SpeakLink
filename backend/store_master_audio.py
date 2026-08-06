"""What every installed Store's Windows mixer is doing, broadcast or not.

WHY THIS EXISTS SEPARATELY FROM ``store_audio_control``

``store_audio_control`` is scoped to a broadcast session: it answers "what has
this operator asked THIS broadcast's Stores to do". That is the right shape for
a live announcement and the wrong shape for the question an operator actually
asks most of the day - *is Rajouri Garden muted right now?* - which has nothing
to do with whether anything is on air.

So this registry is keyed by **Store**, not by session, and it lives for as
long as the process does. A Receiver reports its endpoint whenever it is
connected, and those readings land here whether or not a broadcast exists.

WHAT IS RUNTIME AND WHAT IS PERSISTED

Actual mixer state is **runtime only**. A person dragging a Windows slider
produces a reading a second; writing a row for each would turn a volume knob
into a database load generator for no operational gain, and the value is
worthless thirty seconds later anyway. Nothing here is written to SQLite.

The one thing that IS persisted lives in ``store_audio_pending.py``: a desired
state for an offline Store, which must survive an HQ restart because its whole
purpose is to outlive the disconnection.

TRUTHFULNESS

The hardest rule in this module is that a remembered value must never be
presented as a current one. When a Receiver goes offline its last reading is
kept - it is genuinely useful to know a shop was left at 35% - but it is
marked ``stale`` from that moment, and every caller has to say "last known"
rather than "currently". A confident wrong number is worse than an honest gap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock


#: What HQ knows about a Store's ability to be controlled at all. These mirror
#: the Receiver's own capability report rather than being inferred here: only
#: the Store knows whether it has an endpoint selected, and guessing produced
#: the "Not supported by this Receiver" bug that sent technicians to rebuild
#: software that was already correct.
ENDPOINT_READY = "ready"
ENDPOINT_NEEDS_SELECTION = "needs_output_selection"
ENDPOINT_UNAVAILABLE = "unavailable"
ENDPOINT_UNKNOWN = "unknown"

ENDPOINT_STATUSES = (
    ENDPOINT_READY,
    ENDPOINT_NEEDS_SELECTION,
    ENDPOINT_UNAVAILABLE,
    ENDPOINT_UNKNOWN,
)


class StoreMasterAudioError(RuntimeError):
    """Base class, so one caller cannot handle a subset by accident."""


class InvalidVolumeError(StoreMasterAudioError):
    """A volume outside 0-100.

    Raised rather than clamped. Clamping would silently turn a caller's bug
    into a plausible-looking mixer change, and the Windows endpoint takes this
    number literally.
    """

    def __init__(self, value: object) -> None:
        super().__init__(f"volume must be an integer 0-100, got {value!r}")


def _validate_volume(value: object) -> int:
    # bool is an int in Python and True would quietly become 1%.
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidVolumeError(value)
    if not 0 <= value <= 100:
        raise InvalidVolumeError(value)
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class StoreMasterAudioState:
    """One Store's mixer, as far as HQ can honestly claim to know it."""

    store_id: int
    #: The last reading the Receiver sent. None means HQ has never had one -
    #: which is different from "0%" and must never be rendered as a number.
    volume_percent: int | None = None
    muted: bool | None = None
    #: Monotonic per Receiver connection. Its only job is to let a delayed
    #: reading be discarded instead of dragging the panel backwards.
    state_sequence: int = 0
    updated_at: datetime | None = None
    endpoint_status: str = ENDPOINT_UNKNOWN
    #: Whether the reading above describes the Store NOW, or is a memory of
    #: how it was left. Everything user-facing hangs off this flag.
    online: bool = False
    last_seen_at: datetime | None = None

    @property
    def stale(self) -> bool:
        """True when the numbers are a memory rather than an observation."""
        return not self.online

    @property
    def controllable(self) -> bool:
        """Can HQ change this Store's mixer right now?

        Being online is not enough: a connected Receiver whose output was never
        re-selected has nothing to control, and saying otherwise produces a
        slider that silently does nothing.
        """
        return self.online and self.endpoint_status == ENDPOINT_READY

    def as_dict(self) -> dict:
        return {
            "store_id": self.store_id,
            "volume_percent": self.volume_percent,
            "muted": self.muted,
            "state_sequence": self.state_sequence,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "endpoint_status": self.endpoint_status,
            "online": self.online,
            "stale": self.stale,
            "controllable": self.controllable,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
        }


@dataclass
class _Registry:
    states: dict[int, StoreMasterAudioState] = field(default_factory=dict)


class StoreMasterAudioRegistry:
    """Process-lifetime, Store-keyed, never written to disk.

    Guarded by a plain lock rather than an async one: every mutation is a few
    field assignments, and the readings arrive from the websocket handler and
    are read by HTTP handlers on the same loop plus the occasional test thread.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._data = _Registry()

    # ---- reading ------------------------------------------------------
    def state_for(self, store_id: int) -> StoreMasterAudioState:
        """Never raises for an unknown Store.

        A Store with an installed Receiver that has not yet reported is a
        completely normal state - it must appear in the panel saying it has
        never been heard from, not vanish or blow up a page render.
        """
        with self._lock:
            state = self._data.states.get(store_id)
            if state is None:
                state = StoreMasterAudioState(store_id=store_id)
                self._data.states[store_id] = state
            return state

    def snapshot(self, store_ids) -> dict[int, StoreMasterAudioState]:
        with self._lock:
            return {store_id: self.state_for(store_id) for store_id in store_ids}

    # ---- writing ------------------------------------------------------
    def observe(self, *, store_id: int, state_sequence: int,
                volume_percent: int, muted: bool) -> StoreMasterAudioState | None:
        """Record a reading from the Store. Returns None if it was discarded.

        Discarding rather than applying an out-of-order reading is what stops
        a delayed notification from making the panel jump backwards to a value
        the shop left thirty seconds ago.
        """
        volume_percent = _validate_volume(volume_percent)
        with self._lock:
            state = self.state_for(store_id)
            if state_sequence <= state.state_sequence:
                return None
            state.state_sequence = state_sequence
            state.volume_percent = volume_percent
            state.muted = bool(muted)
            state.updated_at = _now()
            state.last_seen_at = state.updated_at
            return state

    def note_online(self, *, store_id: int, endpoint_status: str = ENDPOINT_UNKNOWN
                    ) -> StoreMasterAudioState:
        """A Receiver connected.

        The sequence counter resets because it is per-connection: a Receiver
        that restarts begins again at 1, and holding the old high-water mark
        would silently reject every reading from the new connection.
        """
        if endpoint_status not in ENDPOINT_STATUSES:
            endpoint_status = ENDPOINT_UNKNOWN
        with self._lock:
            state = self.state_for(store_id)
            state.online = True
            state.endpoint_status = endpoint_status
            state.state_sequence = 0
            state.last_seen_at = _now()
            return state

    def note_capability(self, *, store_id: int, endpoint_status: str
                        ) -> StoreMasterAudioState:
        if endpoint_status not in ENDPOINT_STATUSES:
            endpoint_status = ENDPOINT_UNKNOWN
        with self._lock:
            state = self.state_for(store_id)
            state.endpoint_status = endpoint_status
            return state

    def note_offline(self, *, store_id: int) -> StoreMasterAudioState:
        """A Receiver went away.

        The last reading is deliberately KEPT. Knowing a shop was left muted is
        genuinely useful; what must not happen is that number being shown as
        the current one, which is why `online` flips and `stale` follows it.
        """
        with self._lock:
            state = self.state_for(store_id)
            state.online = False
            return state

    def forget(self, *, store_id: int) -> None:
        """Drop a Store entirely - used when its Receiver is deleted.

        Keeping a reading for a Store with no installed Receiver would put a
        row in the panel that no longer corresponds to any machine.
        """
        with self._lock:
            self._data.states.pop(store_id, None)


#: One registry for the process. Runtime state, like ws_manager's - deliberately
#: not a database, and deliberately not per-request.
registry = StoreMasterAudioRegistry()
