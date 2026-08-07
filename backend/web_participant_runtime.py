"""What a listener's browser is doing right now. Memory only, by design.

WHY NONE OF THIS IS PERSISTED

Connection state, heartbeat times and playback state change every few seconds
per listener. At a hundred listeners that is a database write every few tens of
milliseconds, to record something that is meaningless the moment the process
restarts - a socket that was open before a restart is not open after one. So
``web_rooms`` persists admission lifecycle, and this holds the rest.

WHAT "LISTENING" MEANS, AND WHAT IT DOES NOT

LISTENING means the listener's browser reported that its playback pipeline is
running, from real media events. It does NOT mean the device's volume is above
zero, that headphones are plugged in, or that anybody can hear anything. There
is no way for a web page to know that, so nothing here claims it - and the
existing SPEAKER_VERIFIED vocabulary, which means something specific about a
Store Receiver, is deliberately not reused.

Because the state is reported by the client it is operational telemetry, not
proof. A browser could report LISTENING while muted. The console shows it as
what the listener's browser says, which is the most that can honestly be shown.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PlaybackState",
    "ListenerRuntime",
    "WebParticipantRegistry",
    "HEARTBEAT_INTERVAL_SECONDS",
    "HEARTBEAT_TIMEOUT_SECONDS",
    "REPORTABLE_STATES",
]

#: The client heartbeats on this cadence. Slow enough that a hundred listeners
#: cost nothing, quick enough that a closed laptop shows up within a few
#: seconds rather than a minute.
HEARTBEAT_INTERVAL_SECONDS = 10
#: Missing two heartbeats plus a margin. One missed beat is a hiccup.
HEARTBEAT_TIMEOUT_SECONDS = 25
#: Older than this and the console shows the last-seen time as stale rather
#: than pretending it is current.
HEARTBEAT_STALE_SECONDS = 15


class PlaybackState:
    """Runtime states, from most tentative to actually playing."""

    #: Admitted, but no socket yet - the browser has a session and has not
    #: connected, or has just lost the connection.
    DISCONNECTED = "DISCONNECTED"
    #: Socket open, no media yet.
    CONNECTED = "CONNECTED"
    #: Bootstrapped and waiting for the listener's gesture, which is what an
    #: autoplay refusal looks like from here.
    READY_TO_PLAY = "READY_TO_PLAY"
    BUFFERING = "BUFFERING"
    LISTENING = "LISTENING"
    PAUSED = "PAUSED"


#: The only states a client is allowed to assert. CONNECTED and DISCONNECTED
#: are decided by the server from the socket itself: a client claiming to be
#: disconnected over its own open socket is not information, and a client
#: claiming to be connected adds nothing the socket did not already prove.
REPORTABLE_STATES = frozenset({
    PlaybackState.READY_TO_PLAY,
    PlaybackState.BUFFERING,
    PlaybackState.LISTENING,
    PlaybackState.PAUSED,
})


@dataclass
class ListenerRuntime:
    """One admitted participant's live connection, as far as HQ can see it."""

    participant_id: int
    room_id: int
    session_id: int
    connected: bool = False
    playback_state: str = PlaybackState.DISCONNECTED
    last_seen: float = field(default_factory=time.monotonic)
    connected_at: float | None = None
    #: Identity of the socket currently holding this participant. A late
    #: cleanup from a superseded socket must not evict its replacement, which
    #: is the same rule the Receiver and broadcaster sockets already follow.
    socket: Any = None

    def seconds_since_seen(self, *, now: float | None = None) -> float:
        return (now if now is not None else time.monotonic()) - self.last_seen

    def is_stale(self, *, now: float | None = None) -> bool:
        return self.seconds_since_seen(now=now) > HEARTBEAT_STALE_SECONDS

    def has_timed_out(self, *, now: float | None = None) -> bool:
        return self.seconds_since_seen(now=now) > HEARTBEAT_TIMEOUT_SECONDS

    def public_dict(self, *, now: float | None = None) -> dict[str, Any]:
        """What the broadcaster sees. No socket, no address, no token."""
        return {
            "participant_id": self.participant_id,
            "connected": self.connected,
            "playback_state": self.playback_state,
            "seconds_since_seen": round(self.seconds_since_seen(now=now), 1),
            "stale": self.is_stale(now=now),
        }


class WebParticipantRegistry:
    """Every live listener across every Broadcast, keyed by participant.

    Keyed by participant id rather than by room: a participant belongs to
    exactly one room, and a flat map means a Kick does not have to know which
    room it is looking in to find the socket it must close.
    """

    def __init__(self) -> None:
        self._runtimes: dict[int, ListenerRuntime] = {}

    # -- lifecycle --------------------------------------------------------
    def attach(self, *, participant_id: int, room_id: int, session_id: int,
               socket: Any) -> ListenerRuntime:
        """Bind one socket to one participant, replacing any previous socket."""
        runtime = ListenerRuntime(
            participant_id=participant_id, room_id=room_id,
            session_id=session_id, connected=True,
            playback_state=PlaybackState.CONNECTED,
            connected_at=time.monotonic(), socket=socket)
        self._runtimes[participant_id] = runtime
        return runtime

    def detach(self, *, participant_id: int, socket: Any = None) -> bool:
        """Release a participant's runtime, but only if THIS socket still holds it.

        Without the identity check a slow cleanup from a replaced socket would
        evict the connection that replaced it, and the listener would go silent
        for no reason a log would explain.
        """
        runtime = self._runtimes.get(participant_id)
        if runtime is None:
            return False
        if socket is not None and runtime.socket is not socket:
            return False
        del self._runtimes[participant_id]
        return True

    def get(self, participant_id: int) -> ListenerRuntime | None:
        return self._runtimes.get(participant_id)

    def socket_for(self, participant_id: int) -> Any:
        runtime = self._runtimes.get(participant_id)
        return runtime.socket if runtime is not None else None

    # -- telemetry --------------------------------------------------------
    def heartbeat(self, *, participant_id: int,
                  playback_state: str | None = None) -> bool:
        """Record a heartbeat, and optionally the browser's playback state.

        An unrecognised state is ignored rather than stored: the console shows
        this to an operator, and a value nothing produced would be a state
        nobody could interpret.
        """
        runtime = self._runtimes.get(participant_id)
        if runtime is None:
            return False
        runtime.last_seen = time.monotonic()
        if playback_state in REPORTABLE_STATES:
            runtime.playback_state = playback_state
        return True

    def sweep_timed_out(self, *, now: float | None = None) -> list[int]:
        """Participants whose browser has stopped reporting. Returns their ids."""
        moment = now if now is not None else time.monotonic()
        lapsed = [runtime.participant_id for runtime in self._runtimes.values()
                  if runtime.has_timed_out(now=moment)]
        for participant_id in lapsed:
            runtime = self._runtimes.get(participant_id)
            if runtime is not None:
                runtime.connected = False
                runtime.playback_state = PlaybackState.DISCONNECTED
        return lapsed

    # -- reporting --------------------------------------------------------
    def for_room(self, room_id: int) -> list[ListenerRuntime]:
        return [runtime for runtime in self._runtimes.values()
                if runtime.room_id == room_id]

    def counts_for_room(self, room_id: int) -> dict[str, int]:
        """Connected and Listening, deliberately kept apart.

        Approved-but-not-connected is not connected, and connected is not
        listening. Collapsing them would let a console report an audience that
        is not hearing anything.
        """
        runtimes = self.for_room(room_id)
        now = time.monotonic()
        return {
            "connected": sum(1 for r in runtimes if r.connected),
            "listening": sum(1 for r in runtimes
                             if r.playback_state == PlaybackState.LISTENING),
            "buffering": sum(1 for r in runtimes
                             if r.playback_state == PlaybackState.BUFFERING),
            "paused": sum(1 for r in runtimes
                          if r.playback_state == PlaybackState.PAUSED),
            "stale": sum(1 for r in runtimes if r.is_stale(now=now)),
        }

    def drop_room(self, room_id: int) -> list[Any]:
        """Forget every listener of one room. Returns the sockets to close."""
        doomed = [runtime for runtime in self._runtimes.values()
                  if runtime.room_id == room_id]
        for runtime in doomed:
            self._runtimes.pop(runtime.participant_id, None)
        return [runtime.socket for runtime in doomed if runtime.socket is not None]

    def __len__(self) -> int:
        return len(self._runtimes)
