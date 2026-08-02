"""Several broadcasts at once, each owning only its own Stores and audio.

WHAT WAS SINGLETON

WSManager held one ``active_broadcaster_ws``, one ``live_session_id``, one
``live_target_store_ids`` and one ``AudioFanout``. Every one of those is a
statement that EchoCast has exactly one broadcast, and together they made the
concurrent case not merely unsupported but actively dangerous: a second
session would overwrite the first's target set, ``_end_session`` sent STOP to
whatever the singleton currently listed, and a broadcaster disconnect
auto-stopped whichever session happened to be in the field.

THE PROPERTY THIS FILE EXISTS FOR

Audio from one session must never reach another session's Stores. Not "should
not" - the queues are keyed per session so there is no shared structure for it
to leak through, and the tests below push distinguishable bytes and assert on
what each Store actually received.

WHAT IS NOT HERE

Emergency Stop, the ownership-visibility API and the admin UI are later
checkpoints. Store exclusivity is NOT re-implemented here: the database lease
from the previous checkpoint remains the only authority, and this runtime
deliberately has no second in-memory exclusivity list to disagree with it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from broadcast_runtime import BroadcastRuntime  # noqa: E402

BP, KG, RG, VP, JHA6 = 101, 102, 103, 104, 105
USER_A, USER_B, USER_C = 11, 22, 33


class Delivered:
    """Records what each Store actually received, per session."""

    def __init__(self) -> None:
        self.by_store: dict[int, list[bytes]] = {}
        self.gate: dict[int, asyncio.Event] = {}

    def factory(self, store_id: int):
        async def send(chunk: bytes) -> None:
            held = self.gate.get(store_id)
            if held is not None:
                # A deliberately slow Store: blocks until released.
                await held.wait()
            self.by_store.setdefault(store_id, []).append(chunk)
        return send

    def stall(self, store_id: int) -> None:
        self.gate[store_id] = asyncio.Event()

    def release(self, store_id: int) -> None:
        gate = self.gate.pop(store_id, None)
        if gate is not None:
            gate.set()


def new_runtime():
    """A recorder and a runtime, built together.

    Created INSIDE the scenario coroutine on purpose: AudioFanout starts
    asyncio tasks, and a runtime built outside the loop that later runs it
    would attach its pumps to a loop that is already closed.
    """
    delivered = Delivered()
    return delivered, BroadcastRuntime(sender_factory=delivered.factory)


def run(scenario):
    """Drive one async scenario.

    pytest-asyncio is not a dependency of this project and there were no async
    tests before this file. Adding a plugin to the test stack to test one
    module is a bigger change than calling asyncio.run, so this does the
    latter - each scenario gets its own fresh event loop, which also
    guarantees no task leaks between tests.
    """
    return asyncio.run(scenario())


async def settle(times: int = 8) -> None:
    """Let the per-Store pump tasks run. They are ordinary asyncio tasks, so
    yielding is what makes their work observable."""
    for _ in range(times):
        await asyncio.sleep(0)


# ===========================================================================
# Several sessions at once
# ===========================================================================
def test_two_sessions_with_disjoint_stores_are_both_live():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP, KG})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG, VP})

        assert runtime.is_live(1)
        assert runtime.is_live(2)
        assert set(runtime.active_session_ids()) == {1, 2}

    run(scenario)


def test_three_sessions_can_be_live_together():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP, KG})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG, VP})
        await runtime.start(session_id=3, owner_user_id=USER_C, target_store_ids={JHA6})

        assert set(runtime.active_session_ids()) == {1, 2, 3}
        assert runtime.live_store_ids() == frozenset({BP, KG, RG, VP, JHA6})

    run(scenario)


def test_each_session_owns_a_distinct_fanout():
    """Not an implementation detail: a shared fanout is the structure through
    which audio could cross, and AudioFanout.start_store stops any existing
    pump for the same Store id - so sharing one would let session B silently
    cancel session A's Store."""
    async def scenario():
        _delivered, runtime = new_runtime()
        a = await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        b = await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})

        assert a.fanout is not b.fanout

    run(scenario)


# ===========================================================================
# Audio isolation - the property this exists for
# ===========================================================================
def test_audio_reaches_only_its_own_sessions_stores():
    async def scenario():
        delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP, KG})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG, VP})
        connected = {BP, KG, RG, VP}

        await runtime.fanout(1, b"AUDIO-A", connected_store_ids=connected)
        await runtime.fanout(2, b"AUDIO-B", connected_store_ids=connected)
        await settle()

        assert delivered.by_store.get(BP) == [b"AUDIO-A"]
        assert delivered.by_store.get(KG) == [b"AUDIO-A"]
        assert delivered.by_store.get(RG) == [b"AUDIO-B"]
        assert delivered.by_store.get(VP) == [b"AUDIO-B"]

    run(scenario)


def test_no_chunk_from_one_session_appears_in_another():
    async def scenario():
        delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP, KG})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG, VP})
        connected = {BP, KG, RG, VP}

        for _ in range(20):
            await runtime.fanout(1, b"AUDIO-A", connected_store_ids=connected)
            await runtime.fanout(2, b"AUDIO-B", connected_store_ids=connected)
        await settle(40)

        for store_id in (BP, KG):
            assert b"AUDIO-B" not in delivered.by_store.get(store_id, [])
        for store_id in (RG, VP):
            assert b"AUDIO-A" not in delivered.by_store.get(store_id, [])

    run(scenario)


def test_a_session_cannot_send_to_a_store_it_does_not_target():
    """Even if the Store is connected and another session owns it."""
    async def scenario():
        delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})

        await runtime.fanout(1, b"AUDIO-A", connected_store_ids={BP, RG})
        await settle()

        assert delivered.by_store.get(RG) is None

    run(scenario)


def test_a_slow_store_in_one_session_does_not_delay_another():
    """The isolation that matters when a Receiver is on a bad link."""
    async def scenario():
        delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP, KG})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        delivered.stall(BP)
        connected = {BP, KG, RG}

        await runtime.fanout(1, b"AUDIO-A", connected_store_ids=connected)
        await runtime.fanout(2, b"AUDIO-B", connected_store_ids=connected)
        await settle()

        # BP is stuck by construction; the others must have been served anyway.
        assert delivered.by_store.get(BP) is None
        assert delivered.by_store.get(KG) == [b"AUDIO-A"]
        assert delivered.by_store.get(RG) == [b"AUDIO-B"]

        delivered.release(BP)
        await settle()
        assert delivered.by_store.get(BP) == [b"AUDIO-A"]

    run(scenario)


def test_queues_stay_bounded_under_a_stalled_store():
    """A bounded queue drops; it never grows without limit. Unbounded here
    would mean one unreachable Receiver consuming HQ memory until it fell
    over, taking every other broadcast with it."""
    async def scenario():
        delivered, runtime = new_runtime()
        live = await runtime.start(session_id=1, owner_user_id=USER_A,
                                   target_store_ids={BP})
        delivered.stall(BP)

        capacity = live.fanout.capacity
        for _ in range(capacity * 4):
            await runtime.fanout(1, b"AUDIO-A", connected_store_ids={BP})
        await settle()

        metrics = live.fanout.metrics(BP)
        assert metrics is not None
        assert metrics["depth"] <= capacity
        assert metrics["dropped"] > 0, "nothing was dropped, so the queue grew"
        delivered.release(BP)

    run(scenario)


# ===========================================================================
# Broadcaster sockets
# ===========================================================================
class FakeSocket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed_with = None

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


def test_two_sessions_can_each_hold_their_own_broadcaster_socket():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        socket_a, socket_b = FakeSocket("a"), FakeSocket("b")

        assert await runtime.attach_broadcaster(1, socket_a, owner_user_id=USER_A)
        assert await runtime.attach_broadcaster(2, socket_b, owner_user_id=USER_B)
        assert runtime.get(1).broadcaster_ws is socket_a
        assert runtime.get(2).broadcaster_ws is socket_b

    run(scenario)


def test_a_second_socket_for_the_same_session_is_refused():
    """Explicit policy: first socket keeps the session. Replacing it would let
    anyone holding a ticket evict the operator who is mid-announcement."""
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        first, second = FakeSocket("first"), FakeSocket("second")

        assert await runtime.attach_broadcaster(1, first, owner_user_id=USER_A)
        assert not await runtime.attach_broadcaster(1, second, owner_user_id=USER_A)
        assert runtime.get(1).broadcaster_ws is first

    run(scenario)


def test_a_non_owner_cannot_attach_to_someones_session():
    """The URL-editing attack. The session id is a small integer, so guessing
    one is trivial; ownership is what stops it, not obscurity."""
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        intruder = FakeSocket("intruder")

        assert not await runtime.attach_broadcaster(1, intruder, owner_user_id=USER_B)
        assert runtime.get(1).broadcaster_ws is None

    run(scenario)


def test_attaching_to_a_session_that_is_not_live_is_refused():
    async def scenario():
        _delivered, runtime = new_runtime()
        socket = FakeSocket("orphan")
        assert not await runtime.attach_broadcaster(999, socket, owner_user_id=USER_A)

    run(scenario)


def test_detaching_frees_the_slot_for_a_reconnect():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        first, second = FakeSocket("first"), FakeSocket("second")
        await runtime.attach_broadcaster(1, first, owner_user_id=USER_A)

        await runtime.detach_broadcaster(1, first)
        assert runtime.get(1).broadcaster_ws is None
        assert await runtime.attach_broadcaster(1, second, owner_user_id=USER_A)

    run(scenario)


def test_detaching_a_stale_socket_does_not_evict_its_replacement():
    """A late cleanup from a superseded socket must not close the live one -
    the same rule the Receiver connection code already follows."""
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        first, second = FakeSocket("first"), FakeSocket("second")
        await runtime.attach_broadcaster(1, first, owner_user_id=USER_A)
        await runtime.detach_broadcaster(1, first)
        await runtime.attach_broadcaster(1, second, owner_user_id=USER_A)

        await runtime.detach_broadcaster(1, first)      # the stale one, arriving late
        assert runtime.get(1).broadcaster_ws is second

    run(scenario)


# ===========================================================================
# Ending one session leaves the others alone
# ===========================================================================
def test_ending_one_session_leaves_the_other_live():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP, KG})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG, VP})

        await runtime.end(1)

        assert not runtime.is_live(1)
        assert runtime.is_live(2)
        assert runtime.live_store_ids() == frozenset({RG, VP})

    run(scenario)


def test_ending_one_session_does_not_disturb_the_others_audio():
    async def scenario():
        delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        connected = {BP, RG}

        await runtime.end(1)
        await runtime.fanout(2, b"AUDIO-B", connected_store_ids=connected)
        await settle()

        assert delivered.by_store.get(RG) == [b"AUDIO-B"]

    run(scenario)


def test_ending_a_session_reaps_only_its_own_queue_tasks():
    async def scenario():
        _delivered, runtime = new_runtime()
        a = await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        b = await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        await runtime.fanout(1, b"AUDIO-A", connected_store_ids={BP})
        await runtime.fanout(2, b"AUDIO-B", connected_store_ids={RG})
        await settle()

        await runtime.end(1)
        await settle()

        assert a.fanout.active_store_ids() == ()
        assert b.fanout.active_store_ids() == (RG,)

    run(scenario)


def test_ending_a_session_closes_only_its_own_broadcaster_socket():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        socket_a, socket_b = FakeSocket("a"), FakeSocket("b")
        await runtime.attach_broadcaster(1, socket_a, owner_user_id=USER_A)
        await runtime.attach_broadcaster(2, socket_b, owner_user_id=USER_B)

        await runtime.end(1)

        assert socket_a.closed_with is not None
        assert socket_b.closed_with is None, "session B's operator was cut off"

    run(scenario)


def test_ending_an_unknown_session_is_safe():
    async def scenario():
        _delivered, runtime = new_runtime()
        assert await runtime.end(999) is None

    run(scenario)


def test_ending_twice_is_safe():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        assert await runtime.end(1) is not None
        assert await runtime.end(1) is None

    run(scenario)


# ===========================================================================
# Disconnect isolation
# ===========================================================================
def test_the_socket_lookup_finds_only_its_own_session():
    """How a disconnect handler learns WHICH session just lost its operator.
    Getting this wrong is how a disconnect on A stops B."""
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        socket_a, socket_b = FakeSocket("a"), FakeSocket("b")
        await runtime.attach_broadcaster(1, socket_a, owner_user_id=USER_A)
        await runtime.attach_broadcaster(2, socket_b, owner_user_id=USER_B)

        assert runtime.session_id_for_socket(socket_a) == 1
        assert runtime.session_id_for_socket(socket_b) == 2
        assert runtime.session_id_for_socket(FakeSocket("stranger")) is None

    run(scenario)


def test_one_broadcasters_disconnect_leaves_the_other_session_live():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        socket_a, socket_b = FakeSocket("a"), FakeSocket("b")
        await runtime.attach_broadcaster(1, socket_a, owner_user_id=USER_A)
        await runtime.attach_broadcaster(2, socket_b, owner_user_id=USER_B)

        # Exactly what the disconnect handler does: identify, then end that one.
        session_id = runtime.session_id_for_socket(socket_a)
        await runtime.detach_broadcaster(session_id, socket_a)
        await runtime.end(session_id)

        assert not runtime.is_live(1)
        assert runtime.is_live(2)
        assert runtime.get(2).broadcaster_ws is socket_b

    run(scenario)


# ===========================================================================
# Store ownership questions the runtime must answer
# ===========================================================================
def test_the_runtime_can_name_the_session_targeting_a_store():
    """Used when a Receiver reconnects mid-broadcast: it must be told the
    session it is rejoining, not 'the' session."""
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP, KG})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})

        assert runtime.session_id_for_store(KG) == 1
        assert runtime.session_id_for_store(RG) == 2
        assert runtime.session_id_for_store(JHA6) is None

    run(scenario)


def test_the_runtime_keeps_no_second_store_exclusivity_list():
    """Exclusivity belongs to the database lease. If this runtime also
    refused, the two could disagree - and the in-memory one would be the one
    that is wrong after a restart."""
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        # No refusal here by design; the lease is what prevents this reaching the
        # runtime at all.
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={BP})
        assert runtime.is_live(1) and runtime.is_live(2)

    run(scenario)


def test_metrics_are_reported_per_session():
    async def scenario():
        _delivered, runtime = new_runtime()
        await runtime.start(session_id=1, owner_user_id=USER_A, target_store_ids={BP})
        await runtime.start(session_id=2, owner_user_id=USER_B, target_store_ids={RG})
        await runtime.fanout(1, b"AUDIO-A", connected_store_ids={BP})
        await runtime.fanout(2, b"AUDIO-B", connected_store_ids={RG})
        await settle()

        metrics = runtime.metrics()
        assert set(metrics) == {1, 2}
        assert BP in metrics[1]
        assert RG in metrics[2]
        assert BP not in metrics[2]

    run(scenario)
