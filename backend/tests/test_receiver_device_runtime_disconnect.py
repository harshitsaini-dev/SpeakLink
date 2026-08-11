"""Withdrawing a Receiver Device's access must close its live connection.

THE LIVE DEFECT THIS COVERS

An operator permanently deleted BP's only Receiver Device. The Receiver Devices
page correctly showed nothing - and fifty seconds later the same Store reported
receiver_ready, audio_receiving and playback_confirmed, because the socket had
authenticated once at connect and was never re-checked. Deleting the Device
changed rows and nothing else, so the connection stayed registered and stayed
in the audio fanout.

These tests drive the manager rather than a real WebSocket because what broke
was registration bookkeeping: which socket is still in `receivers`, still in
the inventory, and therefore still handed audio. A fake socket that records
whether it was closed is enough to prove that, and it keeps the tests free of
network timing.
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
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from datetime import datetime, timezone  # noqa: E402

from receiver_connection_inventory import (  # noqa: E402
    ActiveReceiverConnectionInventory,
    AuthenticatedReceiverConnection,
    ConnectionAuthenticationSource,
)
from ws_manager import WSManager  # noqa: E402


class FakeSocket:
    """Records whether it was closed, and with what."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = False
        self.close_code = None
        self.close_reason = None
        self.sent: list = []

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = True
        self.close_code = code
        self.close_reason = reason

    async def send_bytes(self, payload: bytes) -> None:
        if self.closed:
            raise RuntimeError("socket is closed")
        self.sent.append(payload)


def register_primary(manager, *, store_id: int, device_id: int, socket,
                     connection_id: str):
    """Put a Device into the manager exactly as a real connect would."""
    manager.receivers[store_id] = socket
    manager.receiver_connection_ids[store_id] = connection_id
    manager.receiver_connection_inventory.register(
        AuthenticatedReceiverConnection(
            connection_id=connection_id,
            store_id=store_id,
            device_id=device_id,
            credential_id=device_id,
            authentication_source=(
                ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL),
            authenticated_at=datetime.now(timezone.utc),
        )
    )


@pytest.fixture()
def manager():
    return WSManager(receiver_connection_inventory=ActiveReceiverConnectionInventory())


# ===========================================================================
# The defect itself
# ===========================================================================
def test_withdrawing_a_device_closes_its_live_socket(manager):
    socket = FakeSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")

    closed = asyncio.run(manager.disconnect_device(6))

    assert closed is True
    assert socket.closed is True, "the live socket must actually be closed"
    # And, more importantly than the close itself, it is no longer registered -
    # which is what removes it from the audio fanout.
    assert manager.receivers.get(31) is None
    assert manager.receiver_connection_ids.get(31) is None
    assert manager.receiver_connection_inventory.get("conn-6") is None


def test_a_withdrawn_device_is_no_longer_treated_as_the_current_connection(manager):
    socket = FakeSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")
    assert manager.is_current_receiver_connection(31, socket, "conn-6") is True

    asyncio.run(manager.disconnect_device(6))

    # The live defect in one assertion: this returned True after deletion, so
    # the socket kept being served and its acknowledgements kept being believed.
    assert manager.is_current_receiver_connection(31, socket, "conn-6") is False


def test_the_store_stops_being_online_immediately(manager):
    """Not at the next freshness sweep - immediately."""
    from receiver_contract import ConnectionState, ReceiverSnapshot

    socket = FakeSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")
    manager.receiver_snapshots[31] = ReceiverSnapshot(
        connection=ConnectionState.CONNECTED,
        last_received_at=datetime.now(timezone.utc))

    asyncio.run(manager.disconnect_device(6))

    assert manager.receiver_snapshots[31].connection is ConnectionState.OFFLINE
    assert 31 not in manager.online_store_ids()


def test_the_close_carries_an_explicit_code_and_reason(manager):
    socket = FakeSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")

    asyncio.run(manager.disconnect_device(6))

    assert socket.close_code == 4403
    assert socket.close_reason
    # A close reason reaches logs and the Receiver; it must never carry a
    # credential or anything derived from one.
    for leak in ("token", "credential", "secret", "hash"):
        assert leak not in socket.close_reason.lower()


# ===========================================================================
# Blast radius
# ===========================================================================
def test_another_stores_receiver_is_untouched(manager):
    bp = FakeSocket("bp")
    asr = FakeSocket("asr")
    register_primary(manager, store_id=31, device_id=6, socket=bp,
                     connection_id="conn-6")
    register_primary(manager, store_id=15, device_id=3, socket=asr,
                     connection_id="conn-3")

    asyncio.run(manager.disconnect_device(6))

    assert asr.closed is False
    assert manager.receivers.get(15) is asr
    assert manager.receiver_connection_inventory.get("conn-3") is not None


def test_a_different_device_on_the_SAME_store_is_untouched(manager):
    """The reason this is keyed on Device and not on Store.

    A Store that has already failed over to a second, entirely valid Device
    must not be silenced because an old Device row was deleted.
    """
    current = FakeSocket("current")
    register_primary(manager, store_id=31, device_id=10, socket=current,
                     connection_id="conn-10")

    # Device 6 is the deleted one, and it is no longer the Store's connection.
    closed = asyncio.run(manager.disconnect_device(6))

    assert closed is False, "there was no live socket for that Device"
    assert current.closed is False
    assert manager.receivers.get(31) is current


def test_a_device_with_no_live_socket_is_not_an_error(manager):
    assert asyncio.run(manager.disconnect_device(999)) is False


def test_a_standby_device_can_be_withdrawn_without_touching_the_primary(manager):
    primary = FakeSocket("primary")
    standby = FakeSocket("standby")
    register_primary(manager, store_id=31, device_id=10, socket=primary,
                     connection_id="conn-10")
    manager.standby_receivers[6] = standby
    manager.standby_connection_ids[6] = "conn-6"
    manager.standby_store_ids[6] = 31

    assert asyncio.run(manager.disconnect_device(6)) is True

    assert standby.closed is True
    assert 6 not in manager.standby_receivers
    # The Store keeps playing through the Device that is actually primary.
    assert primary.closed is False
    assert manager.receivers.get(31) is primary


# ===========================================================================
# Audio
# ===========================================================================
def test_a_withdrawn_device_receives_no_further_audio(manager):
    socket = FakeSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")

    asyncio.run(socket.send_bytes(b"chunk-before"))
    asyncio.run(manager.disconnect_device(6))

    # Deregistration is what stops audio: the fanout looks up `receivers`.
    assert manager.receivers.get(31) is None
    assert socket.sent == [b"chunk-before"]
    with pytest.raises(RuntimeError):
        asyncio.run(socket.send_bytes(b"chunk-after"))


def test_disconnecting_twice_is_harmless(manager):
    socket = FakeSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")
    assert asyncio.run(manager.disconnect_device(6)) is True
    assert asyncio.run(manager.disconnect_device(6)) is False


def test_a_socket_that_is_already_gone_still_deregisters(manager):
    """A close that raises must not leave the Device registered."""
    class DeadSocket(FakeSocket):
        async def close(self, code: int = 1000, reason: str = "") -> None:
            raise ConnectionResetError("already gone")

    socket = DeadSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")

    assert asyncio.run(manager.disconnect_device(6)) is True
    assert manager.receivers.get(31) is None


# ===========================================================================
# Legacy connections are a different identity
# ===========================================================================
def test_a_legacy_connection_has_no_device_and_is_never_matched(manager):
    """A legacy Store-token Receiver carries no Device id.

    It must therefore never be closed by a Device-keyed withdrawal, and must
    never be reported as though it were the deleted Device. Whether legacy
    access should be withdrawn at all is a separate, explicit migration.
    """
    legacy = FakeSocket("legacy")
    manager.receivers[31] = legacy
    manager.receiver_connection_ids[31] = "conn-legacy"
    manager.receiver_connection_inventory.register(
        AuthenticatedReceiverConnection(
            connection_id="conn-legacy",
            store_id=31,
            device_id=None,          # the distinguishing fact
            credential_id=None,
            authentication_source=(
                ConnectionAuthenticationSource.LEGACY_STORE_TOKEN),
            authenticated_at=datetime.now(timezone.utc),
        )
    )

    assert manager.find_device_connection(6) is None
    assert asyncio.run(manager.disconnect_device(6)) is False
    assert legacy.closed is False, "a legacy connection is not the deleted Device"
    assert manager.receivers.get(31) is legacy


def test_find_device_connection_reports_the_role_it_found(manager):
    socket = FakeSocket("bp")
    register_primary(manager, store_id=31, device_id=6, socket=socket,
                     connection_id="conn-6")
    role, store_id, found, connection_id = manager.find_device_connection(6)
    assert role == "primary"
    assert store_id == 31
    assert found is socket
    assert connection_id == "conn-6"
