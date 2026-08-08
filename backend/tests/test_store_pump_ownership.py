"""A Store that loses its Receiver must get audio again when it comes back.

THE DEFECT

``LiveBroadcast._started_stores`` was add-only, and a Store's pump task returns
permanently the first time a send raises - which ``_audio_sender`` does on any
failed delivery. So one dropped socket ended that Store's audio for the rest of
the Broadcast: the Store stayed in the target set, its queue kept accepting and
drop-oldest-ing chunks with nothing consuming them, and HQ went on telling the
reconnecting Receiver to play.

Nothing had noticed because every existing fanout test starts a Store once and
never kills its sender.

THE INVARIANT

At most one live pump per (session, store, connection), and a dead pump marks
the Store as needing a new one rather than as finished.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from audio_streaming import AudioFanout  # noqa: E402
from broadcast_runtime import BroadcastRuntime  # noqa: E402

CHUNK = b"\x1a\x45\xdf\xa3" + b"x" * 200


class Receiver:
    """One Store's socket, which can be broken and later replaced."""

    def __init__(self):
        self.delivered: list[bytes] = []
        self.broken = False
        self.connection_id = "conn-1"

    def sender(self, store_id):
        async def send(chunk: bytes) -> None:
            if self.broken:
                # Exactly what ws_manager._audio_sender does on a failed send.
                raise ConnectionError(f"receiver send failed for store {store_id}")
            self.delivered.append(chunk)
        return send


async def settle(times: int = 6):
    for _ in range(times):
        await asyncio.sleep(0)


# ===========================================================================
# The reported defect, at the runtime level
# ===========================================================================

def test_a_store_gets_a_new_pump_after_its_receiver_reconnects():
    """FAILS before the fix: the Store never receives audio again."""
    receiver = Receiver()

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=receiver.sender)
        await runtime.start(session_id=1, owner_user_id=1, target_store_ids=[10])

        await runtime.fanout(1, CHUNK, connected_store_ids=[10])
        await settle()
        assert receiver.delivered, "the Store never received the first chunk"
        before = len(receiver.delivered)

        # The socket dies mid-Broadcast. The next send raises and the pump ends.
        receiver.broken = True
        await runtime.fanout(1, CHUNK, connected_store_ids=[10])
        await settle()

        # The Receiver reconnects: a new socket, and audio must flow again.
        receiver.broken = False
        receiver.connection_id = "conn-2"
        for _ in range(3):
            await runtime.fanout(1, CHUNK, connected_store_ids=[10])
            await settle()

        assert len(receiver.delivered) > before, (
            "the Store received nothing after its Receiver reconnected - its "
            "pump died and was never replaced")
        await runtime.end(1)

    asyncio.run(scenario())


def test_a_reconnecting_store_starts_from_an_empty_queue():
    """No inherited backlog: what it lost while disconnected stays lost."""
    receiver = Receiver()

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=receiver.sender)
        await runtime.start(session_id=2, owner_user_id=1, target_store_ids=[10])
        await runtime.fanout(2, CHUNK, connected_store_ids=[10])
        await settle()

        receiver.broken = True
        # A whole announcement's worth of audio while it is away.
        for _ in range(30):
            await runtime.fanout(2, CHUNK, connected_store_ids=[10])
        await settle()

        receiver.broken = False
        receiver.delivered.clear()
        await runtime.fanout(2, b"\x1a\x45\xdf\xa3" + b"NEW" + b"y" * 100,
                             connected_store_ids=[10])
        await settle()

        assert len(receiver.delivered) <= 2, (
            f"{len(receiver.delivered)} chunks arrived - the reconnecting "
            "Store inherited a stale queue")
        assert b"NEW" in receiver.delivered[-1]
        await runtime.end(2)

    asyncio.run(scenario())


def test_exactly_one_pump_exists_however_many_chunks_arrive():
    receiver = Receiver()

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=receiver.sender)
        await runtime.start(session_id=3, owner_user_id=1, target_store_ids=[10])
        for _ in range(25):
            await runtime.fanout(3, CHUNK, connected_store_ids=[10])
        await settle()

        live = runtime._sessions[3]
        alive = [t for t in live.fanout._tasks.values() if not t.done()]
        assert len(alive) == 1, f"{len(alive)} pumps for one Store"
        await runtime.end(3)

    asyncio.run(scenario())


def test_a_dying_old_pump_cannot_remove_the_pump_that_replaced_it():
    """The identity guard. Cleanup must name the pump it is cleaning up."""
    receiver = Receiver()

    async def scenario():
        fanout = AudioFanout()
        await fanout.start_store(10, receiver.sender(10))
        first = fanout._tasks[10]

        # A replacement arrives while the first is still unwinding.
        await fanout.start_store(10, receiver.sender(10))
        second = fanout._tasks[10]
        assert second is not first

        # The old task finishing must not take the new one's slot with it.
        await settle()
        assert fanout._tasks.get(10) is second, (
            "a late cleanup from the old pump removed its replacement")
        assert not second.done()
        await fanout.stop_all()

    asyncio.run(scenario())


def test_one_stores_dead_receiver_does_not_stop_another_store():
    good = Receiver()
    bad = Receiver()
    bad.broken = True

    def factory(store_id):
        return good.sender(store_id) if store_id == 10 else bad.sender(store_id)

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=factory)
        await runtime.start(session_id=4, owner_user_id=1,
                            target_store_ids=[10, 11])
        for _ in range(5):
            await runtime.fanout(4, CHUNK, connected_store_ids=[10, 11])
            await settle()

        assert len(good.delivered) >= 3, (
            "the healthy Store stopped because another Store's socket died")
        assert bad.delivered == []
        await runtime.end(4)

    asyncio.run(scenario())


def test_one_broadcast_cannot_disturb_another():
    a = Receiver()
    b = Receiver()

    async def scenario():
        runtime_a = BroadcastRuntime(sender_factory=a.sender)
        runtime_b = BroadcastRuntime(sender_factory=b.sender)
        await runtime_a.start(session_id=5, owner_user_id=1, target_store_ids=[10])
        await runtime_b.start(session_id=6, owner_user_id=2, target_store_ids=[11])

        await runtime_a.fanout(5, CHUNK, connected_store_ids=[10])
        await settle()
        a.broken = True
        await runtime_a.fanout(5, CHUNK, connected_store_ids=[10])
        await settle()

        before = len(b.delivered)
        for _ in range(3):
            await runtime_b.fanout(6, CHUNK, connected_store_ids=[11])
            await settle()
        assert len(b.delivered) > before

        await runtime_a.end(5)
        await runtime_b.end(6)

    asyncio.run(scenario())


def test_a_store_that_never_reconnects_costs_nothing_unbounded():
    """A permanently dead Store must not grow anything without bound."""
    receiver = Receiver()
    receiver.broken = True

    async def scenario():
        runtime = BroadcastRuntime(sender_factory=receiver.sender)
        await runtime.start(session_id=7, owner_user_id=1, target_store_ids=[10])
        for _ in range(200):
            await runtime.fanout(7, CHUNK, connected_store_ids=[10])
        await settle()

        live = runtime._sessions[7]
        metrics = live.fanout.all_metrics()[10]
        assert metrics["depth"] <= live.fanout.capacity, (
            "the queue grew past its bound while nothing was consuming it")
        alive = [t for t in live.fanout._tasks.values() if not t.done()]
        assert len(alive) <= 1, "restart attempts are piling up pumps"
        await runtime.end(7)

    asyncio.run(scenario())
