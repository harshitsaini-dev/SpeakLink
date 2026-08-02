"""Two Devices connected to one Store, and only one of them hearing the audio.

``WSManager`` tracked Receivers by ``store_id`` alone, so a second computer in
the same Store did not become a standby - it *replaced* the first. That was
correct while a Store meant one Receiver. It stops being correct the moment a
Store can hold a primary and a standby.

What has to be true, and what each of these tests is actually protecting:

* **Both can be connected at once.** Otherwise a standby is not a standby; it is
  a way to knock the primary off the air by plugging in a spare.
* **Only the primary is in the fanout.** ``fanout_audio`` sends to whatever sits
  in ``receivers[store_id]``, so a standby must never be put there. A standby
  that received chunks "just in case" is an echo in the shop.
* **A socket is only ever replaced by the same Device.** A different computer
  connecting must not close somebody else's socket.
* **A slow standby cannot touch the primary.** They are separate sockets with
  separate queues; the standby is not in the fanout at all, so it has nothing to
  be slow *in*.
* **A legacy Store-token Receiver keeps working exactly as before**, and is never
  given a Device identity it does not have.

Nothing here opens a real socket or a database. A fake WebSocket records what it
was sent, which is the only question worth asking.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import datetime, timezone
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

from receiver_connection_inventory import ConnectionAuthenticationSource  # noqa: E402
from ws_manager import WSManager  # noqa: E402


NOW = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
#: The one session these tests drive. fanout and teardown are per session
#: now, so the id has to be named rather than implied.
SESSION_UNDER_TEST = 1
STORE = 41
PRIMARY_DEVICE = 7
STANDBY_DEVICE = 8


class FakeWebSocket:
    """Records what it was sent. Close is recorded, never real."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.accepted = False
        self.sent_bytes: list[bytes] = []
        self.sent_text: list[str] = []
        self.close_calls: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def send_bytes(self, data: bytes) -> None:
        if self.close_calls:
            raise RuntimeError("this socket is closed")
        self.sent_bytes.append(data)

    async def send_text(self, data: str) -> None:
        self.sent_text.append(data)

    async def send_json(self, data) -> None:
        self.sent_text.append(str(data))

    async def close(self, code: int = 1000) -> None:
        self.close_calls.append(code)


def _connect(manager, websocket, *, connection_id, device_id=None, is_primary=True,
             store_id=STORE, source=None, demote_superseded_device=False):
    return manager.connect_receiver(
        store_id,
        websocket,
        connection_id,
        NOW,
        authentication_source=source or (
            ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
            if device_id is not None
            else ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
        ),
        device_id=device_id,
        credential_id=device_id,
        is_primary=is_primary,
        demote_superseded_device=demote_superseded_device,
    )


@pytest.fixture()
def manager() -> WSManager:
    return WSManager()


# ===========================================================================
# Both connected, one in the fanout
# ===========================================================================
def test_a_primary_and_a_standby_are_both_connected(manager: WSManager):
    primary, standby = FakeWebSocket("primary"), FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)

    asyncio.run(scenario())

    assert primary.accepted and standby.accepted
    assert standby.close_calls == [], "connecting a standby knocked it straight off again"
    assert primary.close_calls == [], "a standby knocked the primary off the air"


def test_only_the_primary_socket_is_in_the_fanout(manager: WSManager):
    """``fanout_audio`` sends to whatever is in ``receivers[store_id]``. This is
    the single fact that keeps a standby silent."""
    primary, standby = FakeWebSocket("primary"), FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)

    asyncio.run(scenario())

    assert manager.receivers[STORE] is primary
    assert standby not in manager.receivers.values()


def test_the_standby_receives_no_audio_chunks(manager: WSManager):
    primary, standby = FakeWebSocket("primary"), FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)
        await manager.start_live_session(SESSION_UNDER_TEST, {STORE}, owner_user_id=1)
        for index in range(5):
            await manager.fanout_audio(SESSION_UNDER_TEST, bytes([index]) * 64)
        await asyncio.sleep(0.2)
        await manager.stop_live_session(SESSION_UNDER_TEST)

    asyncio.run(scenario())

    assert standby.sent_bytes == [], f"the standby received {len(standby.sent_bytes)} chunks"
    assert primary.sent_bytes, "the primary received nothing at all"


def test_both_devices_are_visible_separately_in_the_inventory(manager: WSManager):
    """Two Devices in one Store must be two records. One record would make the
    dashboard show a Store that has half of what it has."""
    primary, standby = FakeWebSocket("primary"), FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)

    asyncio.run(scenario())

    snapshot = manager.receiver_connection_inventory.snapshot(captured_at=NOW)
    assert {record.device_id for record in snapshot.records} == {PRIMARY_DEVICE, STANDBY_DEVICE}
    assert snapshot.total_active_count == 2
    assert [count.connection_count for count in snapshot.store_counts] == [2], (
        "the Store reports one connection when two computers are attached to it"
    )


# ===========================================================================
# Replacement belongs to one Device only
# ===========================================================================
def test_the_same_standby_device_reconnecting_replaces_only_its_own_socket(manager: WSManager):
    primary = FakeWebSocket("primary")
    first, second = FakeWebSocket("standby-1"), FakeWebSocket("standby-2")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, first, connection_id="c-s1",
                       device_id=STANDBY_DEVICE, is_primary=False)
        await _connect(manager, second, connection_id="c-s2",
                       device_id=STANDBY_DEVICE, is_primary=False)

    asyncio.run(scenario())

    assert first.close_calls, "the standby's stale socket was left open"
    assert second.close_calls == []
    assert primary.close_calls == [], "replacing a standby closed the primary"


def test_a_standby_disconnecting_leaves_the_primary_alone(manager: WSManager):
    primary, standby = FakeWebSocket("primary"), FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)
        manager.disconnect_receiver(STORE, standby, "c-standby", device_id=STANDBY_DEVICE)

    asyncio.run(scenario())

    assert manager.receivers[STORE] is primary
    assert manager.get_receiver_connection_id(STORE) == "c-primary"


def test_a_promoted_device_takes_over_without_closing_the_old_primary(manager: WSManager):
    """After a promotion the demoted computer stays connected and reports health.
    It simply stops being in the fanout. Closing it would look like a fault."""
    old_primary, new_primary = FakeWebSocket("old"), FakeWebSocket("new")

    async def scenario():
        await _connect(manager, old_primary, connection_id="c-old", device_id=PRIMARY_DEVICE)
        await _connect(manager, new_primary, connection_id="c-new", device_id=STANDBY_DEVICE,
                       demote_superseded_device=True)

    asyncio.run(scenario())

    assert manager.receivers[STORE] is new_primary
    assert old_primary.close_calls == [], "the demoted Device was disconnected instead of demoted"


def test_a_different_device_replaces_rather_than_demotes_unless_asked(manager: WSManager):
    """Demotion is never inferred from the device ids alone.

    Under dual_verify a legacy Store token authenticates through the *backfilled*
    Device, so an ordinary reconnect with an enrolled credential also presents two
    different device ids while plainly meaning "replace me". Inferring a promotion
    from that left the old socket open forever - the cutover rehearsal hung on
    ``wait_closed()`` until this defaulted back to replacement.
    """
    old_primary, new_primary = FakeWebSocket("old"), FakeWebSocket("new")

    async def scenario():
        await _connect(manager, old_primary, connection_id="c-old", device_id=PRIMARY_DEVICE)
        await _connect(manager, new_primary, connection_id="c-new", device_id=STANDBY_DEVICE)

    asyncio.run(scenario())

    assert old_primary.close_calls == [4001], "the superseded socket was left open"
    assert manager.receivers[STORE] is new_primary


def test_the_demoted_device_must_prove_readiness_again(manager: WSManager):
    """READY and PLAYBACK_CONFIRMED were proved on the old Device's sound card.
    The incoming primary has to earn them on its own."""
    from receiver_contract import ConnectionState

    old_primary, new_primary = FakeWebSocket("old"), FakeWebSocket("new")

    async def scenario():
        await _connect(manager, old_primary, connection_id="c-old", device_id=PRIMARY_DEVICE)
        await _connect(manager, new_primary, connection_id="c-new", device_id=STANDBY_DEVICE,
                       demote_superseded_device=True)

    asyncio.run(scenario())

    snapshot = manager.get_receiver_snapshot(STORE)
    assert snapshot.connection is ConnectionState.CONNECTED
    assert snapshot.requires_ready is True, "the new primary inherited the old one's readiness"


def test_a_legacy_receiver_is_still_replaced_by_a_legacy_reconnect(manager: WSManager):
    """The existing one-connection-per-Store behaviour, unchanged, for a Receiver
    on the shared Store token."""
    first, second = FakeWebSocket("legacy-1"), FakeWebSocket("legacy-2")

    async def scenario():
        await _connect(manager, first, connection_id="c-l1")
        await _connect(manager, second, connection_id="c-l2")

    asyncio.run(scenario())

    assert first.close_calls == [4001]
    assert manager.receivers[STORE] is second


def test_a_legacy_connection_is_never_given_a_device_identity(manager: WSManager):
    legacy = FakeWebSocket("legacy")

    async def scenario():
        return await _connect(manager, legacy, connection_id="c-legacy")

    record = asyncio.run(scenario())
    assert record.device_id is None
    assert record.authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN


# ===========================================================================
# A slow standby is not in the fanout, so it cannot be slow in it
# ===========================================================================
def test_a_standby_that_never_reads_cannot_delay_the_primary(manager: WSManager):
    class StuckWebSocket(FakeWebSocket):
        async def send_bytes(self, data: bytes) -> None:
            await asyncio.sleep(30)  # would stall anything that awaited it

    primary, stuck = FakeWebSocket("primary"), StuckWebSocket("stuck-standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, stuck, connection_id="c-stuck",
                       device_id=STANDBY_DEVICE, is_primary=False)
        await manager.start_live_session(SESSION_UNDER_TEST, {STORE}, owner_user_id=1)
        for index in range(8):
            await manager.fanout_audio(SESSION_UNDER_TEST, bytes([index]) * 32)
        await asyncio.sleep(0.3)
        await manager.stop_live_session(SESSION_UNDER_TEST)

    asyncio.run(asyncio.wait_for(scenario(), timeout=15))
    assert primary.sent_bytes, "the primary was starved by a standby it never sends to"
    assert stuck.sent_bytes == []


# ===========================================================================
# Standby health is still tracked
# ===========================================================================
def test_a_standby_is_registered_so_its_heartbeat_has_somewhere_to_go(manager: WSManager):
    standby = FakeWebSocket("standby")

    async def scenario():
        return await _connect(manager, standby, connection_id="c-standby",
                              device_id=STANDBY_DEVICE, is_primary=False)

    record = asyncio.run(scenario())
    assert record.device_id == STANDBY_DEVICE
    assert manager.receiver_connection_inventory.get("c-standby") is not None
    assert manager.is_current_receiver_connection(
        STORE, standby, "c-standby", device_id=STANDBY_DEVICE
    )


def test_a_store_with_only_a_standby_has_no_audio_target(manager: WSManager):
    """No primary means no audio, not "send it to whoever turned up"."""
    standby = FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)
        await manager.start_live_session(SESSION_UNDER_TEST, {STORE}, owner_user_id=1)
        await manager.fanout_audio(SESSION_UNDER_TEST, b"x" * 32)
        await asyncio.sleep(0.15)
        await manager.stop_live_session(SESSION_UNDER_TEST)

    asyncio.run(scenario())
    assert standby.sent_bytes == []
    assert STORE not in manager.receivers


def test_disconnecting_a_standby_removes_only_its_inventory_record(manager: WSManager):
    primary, standby = FakeWebSocket("primary"), FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)
        manager.disconnect_receiver(STORE, standby, "c-standby", device_id=STANDBY_DEVICE)

    asyncio.run(scenario())

    assert manager.receiver_connection_inventory.get("c-standby") is None
    assert manager.receiver_connection_inventory.get("c-primary") is not None
