"""Deterministic regression tests for the Receiver replacement handover race.

``WSManager.connect_receiver`` closes the Store's previous socket while holding
``_receiver_lock``. ``await old.close(...)`` yields the event loop, and the old
socket's own cleanup path (``disconnect_receiver``) is synchronous and does not
take that lock. If the old connection is still installed as the Store's current
connection at that yield point, the old handler's ``finally`` block observes
live state, reports itself as the current connection, and the server then writes
Store health for a connection this replacement already owns.

That breaks the documented invariant that a rejected or superseded connection
produces no Store health write. These tests force the interleaving explicitly
instead of relying on scheduler timing, so they fail reliably when the ordering
regresses.

They use no SQLite database, no network socket, no Uvicorn and no credentials.

``ws_manager`` is imported lazily inside each test rather than at module
import time. Other focused suites assert that ``ws_manager`` is absent from
``sys.modules``, and pytest-xdist ``--dist loadscope`` can schedule this module
onto the same worker, so a module-level import here would leak into those
purity checks.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from receiver_connection_inventory import (
    ActiveReceiverConnectionInventory,
    AuthenticatedReceiverConnection,
    ConnectionAuthenticationSource,
    ConnectionInventoryCapacityError,
)
from receiver_contract import ConnectionState


AUTHENTICATED_AT = datetime(2026, 1, 5, 9, 30, tzinfo=timezone.utc)
STORE_ID = 11


class FakeReceiverWebSocket:
    """A minimal stand-in; it never opens a real socket."""

    def __init__(self) -> None:
        self.accepted = False
        self.close_calls: list[int] = []

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.close_calls.append(code)


class ReentrantCloseWebSocket(FakeReceiverWebSocket):
    """Runs the old connection's cleanup at the exact ``close()`` yield point.

    This is what the real handler does: it wakes from the close, and its
    ``finally`` block calls ``disconnect_receiver`` for its own identity.
    """

    def __init__(self, manager, connection_id: str) -> None:
        super().__init__()
        self._manager = manager
        self._connection_id = connection_id
        self.cleanup_claimed_current: bool | None = None

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        await super().close(code, reason)
        self.cleanup_claimed_current = self._manager.disconnect_receiver(
            STORE_ID,
            self,
            self._connection_id,
        )


def _manager(max_connections: int = 256):
    # Imported here, not at module scope; see the module docstring.
    from ws_manager import WSManager

    return WSManager(
        receiver_connection_inventory=ActiveReceiverConnectionInventory(
            max_connections=max_connections
        )
    )


async def _connect(manager, ws, connection_id: str):
    return await manager.connect_receiver(
        STORE_ID,
        ws,
        connection_id,
        AUTHENTICATED_AT,
        authentication_source=ConnectionAuthenticationSource.LEGACY_STORE_TOKEN,
    )


def test_superseded_socket_cleanup_cannot_claim_to_be_the_current_connection():
    """The replaced socket must never win ``disconnect_receiver``.

    A True result here is what makes the server write ``status='offline'`` for
    a Store whose connection has already been handed to a replacement.
    """

    async def scenario():
        manager = _manager()
        old_ws = ReentrantCloseWebSocket(manager, "conn-old")
        await _connect(manager, old_ws, "conn-old")

        new_ws = FakeReceiverWebSocket()
        await _connect(manager, new_ws, "conn-new")

        assert old_ws.close_calls == [4001]
        assert old_ws.cleanup_claimed_current is False
        # The replacement owns the Store after the handover.
        assert manager.get_receiver_connection_id(STORE_ID) == "conn-new"
        assert manager.receivers[STORE_ID] is new_ws

    asyncio.run(scenario())


def test_superseded_socket_cleanup_does_not_remove_the_replacement_from_inventory():
    async def scenario():
        manager = _manager()
        old_ws = ReentrantCloseWebSocket(manager, "conn-old")
        await _connect(manager, old_ws, "conn-old")

        new_ws = FakeReceiverWebSocket()
        await _connect(manager, new_ws, "conn-new")

        snapshot = manager.receiver_connection_inventory.snapshot()
        assert [record.connection_id for record in snapshot.records] == ["conn-new"]
        assert snapshot.total_active_count == 1
        assert manager.get_receiver_snapshot(STORE_ID).connection is ConnectionState.CONNECTED

    asyncio.run(scenario())


def test_capacity_rejected_replacement_leaves_no_current_connection():
    """A capacity failure must detach the old connection and install nothing.

    The old socket's cleanup must still not claim to be current, so the server
    performs no Store health write for either socket.
    """

    async def scenario():
        manager = _manager(max_connections=1)
        old_ws = ReentrantCloseWebSocket(manager, "conn-old")
        await _connect(manager, old_ws, "conn-old")

        # Fill the single inventory slot with an unrelated Store so the
        # replacement registration cannot succeed.
        manager.receiver_connection_inventory.remove("conn-old")
        manager.receiver_connection_inventory.register(
            AuthenticatedReceiverConnection(
                connection_id="conn-other-store",
                store_id=99,
                device_id=None,
                credential_id=None,
                authentication_source=ConnectionAuthenticationSource.LEGACY_STORE_TOKEN,
                authenticated_at=AUTHENTICATED_AT,
            )
        )

        new_ws = FakeReceiverWebSocket()
        with pytest.raises(ConnectionInventoryCapacityError):
            await _connect(manager, new_ws, "conn-new")

        assert old_ws.cleanup_claimed_current is False
        assert manager.get_receiver_connection_id(STORE_ID) is None
        assert STORE_ID not in manager.receivers

    asyncio.run(scenario())


def test_normal_disconnect_still_reports_the_current_connection():
    """The fix must not stop a genuine current socket from cleaning up."""

    async def scenario():
        manager = _manager()
        ws = FakeReceiverWebSocket()
        await _connect(manager, ws, "conn-only")

        assert manager.disconnect_receiver(STORE_ID, ws, "conn-only") is True
        assert manager.get_receiver_connection_id(STORE_ID) is None
        assert manager.receiver_connection_inventory.snapshot().total_active_count == 0

    asyncio.run(scenario())
