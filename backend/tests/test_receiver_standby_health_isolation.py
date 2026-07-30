"""A standby Device's health is its own, and must never be spent as the Store's.

``test_receiver_standby_runtime.py`` already proves a standby stays out of the
audio fanout. This file is about the other half, which was recorded as a deferred
P1: **acknowledgements**. ``WSManager`` kept exactly one ``ReceiverSnapshot`` per
Store, and the receiver WebSocket endpoint drove it with whatever arrived on any
socket for that Store - primary or standby. Four separate consequences, each one
its own failure in a shop:

1. **A standby could reject the primary's acknowledgements.** The contract
   requires ``sequence`` to strictly increase per snapshot. A standby that had
   been up for hours sits at a high sequence; the moment its ack lands in the
   Store snapshot, the primary's next ack is *below* ``last_sequence`` and is
   refused with ``NonMonotonicSequenceError``. The primary is working perfectly
   and the server stops believing it.

2. **A standby's heartbeat could keep a dead primary looking online.** Freshness
   is decided by ``last_received_at`` on the Store snapshot. A standby
   heartbeating every few seconds refreshes it, so a primary whose machine has
   been switched off never goes STALE or OFFLINE. This is the dangerous one: HQ
   shows a green Store and nothing comes out of the speakers.

3. **A standby's PLAYBACK_CONFIRMED became the Store's.** Playback confirmation
   is the only evidence that an announcement was actually heard. A standby
   receives no audio at all, so a confirmation from it is not weak evidence -
   it is evidence of something that cannot have happened.

4. **A promoted standby inherited its own standby-era status.** Whatever a
   Device proved while it was a standby was not proved while carrying the
   Store's audio. Promotion has to start from honest ignorance.

The fix routes a standby's acknowledgements to a per-Device snapshot and leaves
the Store's aggregate snapshot as the primary's alone. The Store aggregate is
deliberately kept - the dashboard, the freshness sweep and the session logic are
all built on "the Store's Receiver", and this is a health-attribution defect, not
a reason to redesign that.

Nothing here opens a real socket or a database.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
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

from receiver_connection_inventory import ConnectionAuthenticationSource  # noqa: E402
from receiver_contract import (  # noqa: E402
    ConnectionState,
    NonMonotonicSequenceError,
    PlaybackState,
    ReadinessState,
)
from ws_manager import WSManager  # noqa: E402


NOW = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)
STORE = 41
PRIMARY_DEVICE = 7
STANDBY_DEVICE = 8
SESSION = 900


class FakeWebSocket:
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
             at=NOW, demote_superseded_device=False):
    return manager.connect_receiver(
        STORE,
        websocket,
        connection_id,
        at,
        authentication_source=(
            ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
            if device_id is not None
            else ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
        ),
        device_id=device_id,
        credential_id=device_id,
        is_primary=is_primary,
        demote_superseded_device=demote_superseded_device,
    )


def _ack(kind: str, sequence: int, *, at=NOW, session_id=None, **extra) -> dict:
    payload = {
        "type": kind,
        "protocol_version": "1.0",
        "message_id": str(uuid.uuid4()),
        "occurred_at": at.isoformat(),
        "sequence": sequence,
    }
    if session_id is not None:
        payload["session_id"] = session_id
    payload.update(extra)
    return payload


def _ready(sequence: int, *, at=NOW) -> dict:
    return _ack(
        "receiver_ready",
        sequence,
        at=at,
        software_checks_passed=True,
        output_device_checks_passed=True,
    )


@pytest.fixture()
def manager() -> WSManager:
    return WSManager()


@pytest.fixture()
def both(manager: WSManager):
    """A primary and a standby, both connected, both ready in their own right."""
    primary, standby = FakeWebSocket("primary"), FakeWebSocket("standby")

    async def scenario():
        await _connect(manager, primary, connection_id="c-primary", device_id=PRIMARY_DEVICE)
        await _connect(manager, standby, connection_id="c-standby",
                       device_id=STANDBY_DEVICE, is_primary=False)

    asyncio.run(scenario())
    return manager, primary, standby


# ===========================================================================
# 1. A standby cannot reject the primary's acknowledgements
# ===========================================================================
def test_a_standby_ack_does_not_advance_the_stores_sequence(both):
    """The mechanism behind the rejection, isolated.

    ``last_sequence`` on the Store snapshot belongs to the primary. If a
    standby's sequence lands in it, the primary's next ack is arithmetically
    doomed however healthy the machine is.
    """
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ready(5), NOW, device_id=PRIMARY_DEVICE)
    manager.apply_receiver_payload(
        STORE, _ack("heartbeat", 9_000), NOW, device_id=STANDBY_DEVICE
    )

    store_snapshot = manager.get_receiver_snapshot(STORE)
    assert store_snapshot.last_sequence == 5, (
        f"the Store's sequence moved to {store_snapshot.last_sequence} because a "
        "standby acknowledged something"
    )


def test_a_long_running_standby_cannot_lock_the_primary_out(both):
    """The failure as it would actually present: the primary is refused."""
    manager, _, _ = both

    # A standby that has been up for hours sits at a high sequence.
    manager.apply_receiver_payload(
        STORE, _ack("heartbeat", 40_000), NOW, device_id=STANDBY_DEVICE
    )

    # The primary, freshly restarted, starts at 1. This must be accepted.
    manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=PRIMARY_DEVICE)

    snapshot = manager.get_receiver_snapshot(STORE)
    assert snapshot.readiness is ReadinessState.READY, (
        "the primary reported READY and the server refused to record it"
    )


def test_the_standby_keeps_its_own_sequence_line(both):
    """A standby is not exempt from the contract - it has its own line of it.

    Without this the fix would be "ignore standby acks", which would lose the
    duplicate and replay protection that makes the contract worth having.
    """
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ack("heartbeat", 10), NOW, device_id=STANDBY_DEVICE)
    with pytest.raises(NonMonotonicSequenceError):
        manager.apply_receiver_payload(
            STORE, _ack("heartbeat", 10), NOW, device_id=STANDBY_DEVICE
        )

    standby_snapshot = manager.get_standby_snapshot(STANDBY_DEVICE)
    assert standby_snapshot.last_sequence == 10


# ===========================================================================
# 2. A standby's heartbeat cannot keep a dead primary looking online
# ===========================================================================
def test_a_standby_heartbeat_does_not_refresh_the_stores_last_seen(both):
    """The dangerous one. A green Store and silent speakers."""
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=PRIMARY_DEVICE)
    before = manager.get_receiver_snapshot(STORE).last_received_at

    much_later = NOW + timedelta(minutes=10)
    manager.apply_receiver_payload(
        STORE, _ack("heartbeat", 2, at=much_later), much_later, device_id=STANDBY_DEVICE
    )

    after = manager.get_receiver_snapshot(STORE).last_received_at
    assert after == before, (
        "a standby's heartbeat refreshed the Store's freshness clock, so a primary "
        "that has been switched off will never be reported OFFLINE"
    )


def test_a_dead_primary_still_goes_offline_while_the_standby_heartbeats(both):
    """The consequence, end to end through the freshness sweep."""
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=PRIMARY_DEVICE)

    # The primary's machine is switched off here. The standby keeps heartbeating.
    later = NOW + timedelta(minutes=10)
    manager.apply_receiver_payload(
        STORE, _ack("heartbeat", 2, at=later), later, device_id=STANDBY_DEVICE
    )

    store_state = manager.evaluate_receiver_freshness(STORE, later)
    assert store_state.connection is ConnectionState.OFFLINE, (
        f"the Store is reported {store_state.connection.value} while the only "
        "computer carrying its audio has been silent for ten minutes"
    )


def test_the_standbys_own_freshness_is_still_tracked_separately(both):
    """A standby that dies must be reportable as dead, on its own account.

    Otherwise the fix trades one blind spot for another: an operator would have
    no way to know the spare machine had stopped answering.
    """
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ack("heartbeat", 1), NOW, device_id=STANDBY_DEVICE)
    later = NOW + timedelta(minutes=10)

    standby_state = manager.evaluate_standby_freshness(STANDBY_DEVICE, later)
    assert standby_state.connection is ConnectionState.OFFLINE


# ===========================================================================
# 3. Playback confirmation is attributed to the Device that could have played it
# ===========================================================================
def test_a_standby_cannot_confirm_playback_for_the_store(both):
    """A standby receives no audio. A confirmation from it is evidence of
    something that cannot have happened.

    It is refused outright rather than merely not recorded, and that falls out of
    routing rather than being a rule of its own: the session is prepared on the
    *Store's* snapshot, so a standby has no active session for the confirmation to
    match and ``WrongSessionError`` fires. ``WrongSessionError`` is a
    ``ReceiverContractError``, which the WebSocket endpoint already answers with
    ``ack_rejected`` - so the standby is told no and stays connected.
    """
    from receiver_contract import WrongSessionError

    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=PRIMARY_DEVICE)
    manager.prepare_receiver_session(STORE, SESSION)

    with pytest.raises(WrongSessionError):
        manager.apply_receiver_payload(
            STORE, _ack("playback_confirmed", 50, session_id=SESSION), NOW,
            device_id=STANDBY_DEVICE,
        )

    store_snapshot = manager.get_receiver_snapshot(STORE)
    assert store_snapshot.playback is not PlaybackState.PLAYBACK_CONFIRMED, (
        "the Store is recorded as having played an announcement, on the word of a "
        "Device that was never sent any audio"
    )
    assert manager.get_standby_snapshot(STANDBY_DEVICE).playback is not (
        PlaybackState.PLAYBACK_CONFIRMED
    ), "the standby recorded a playback confirmation for audio it never received"


def test_the_primarys_confirmation_is_recorded_for_the_store(both):
    """The other half: the real one must still count."""
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=PRIMARY_DEVICE)
    manager.prepare_receiver_session(STORE, SESSION)
    manager.apply_receiver_payload(
        STORE, _ack("audio_receiving", 2, session_id=SESSION), NOW, device_id=PRIMARY_DEVICE
    )
    manager.apply_receiver_payload(
        STORE, _ack("playback_confirmed", 3, session_id=SESSION), NOW,
        device_id=PRIMARY_DEVICE,
    )

    assert manager.get_receiver_snapshot(STORE).playback is PlaybackState.PLAYBACK_CONFIRMED


def test_a_legacy_receiver_without_a_device_still_drives_the_store(both):
    """A Receiver on the shared Store token has no Device identity at all.

    ``device_id=None`` must keep meaning "this is the Store's Receiver", or the
    fix takes 44 working Stores off the air.
    """
    manager = WSManager()
    legacy = FakeWebSocket("legacy")
    asyncio.run(_connect(manager, legacy, connection_id="c-legacy"))

    manager.apply_receiver_payload(STORE, _ready(1), NOW)
    assert manager.get_receiver_snapshot(STORE).readiness is ReadinessState.READY


# ===========================================================================
# 4. A standby never receives live broadcast audio
# ===========================================================================
def test_a_standby_receives_no_audio_even_while_acknowledging(both):
    """Covered structurally in test_receiver_standby_runtime; asserted here again
    against a standby that is fully alive and talking, which is the state in which
    somebody would be tempted to "just add it to the fanout"."""
    manager, primary, standby = both

    async def scenario():
        manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=PRIMARY_DEVICE)
        manager.apply_receiver_payload(
            STORE, _ack("heartbeat", 2), NOW, device_id=STANDBY_DEVICE
        )
        manager.start_live_session(SESSION, {STORE})
        for index in range(6):
            await manager.fanout_audio(bytes([index]) * 64)
        await asyncio.sleep(0.2)
        await manager.stop_audio_fanout()

    asyncio.run(scenario())

    assert standby.sent_bytes == [], f"the standby received {len(standby.sent_bytes)} chunks"
    assert primary.sent_bytes, "the primary received nothing at all"


def test_the_standby_snapshot_is_not_reachable_through_the_store_key(both):
    """Two dictionaries, two keyspaces. A standby's device id must never be
    usable as a Store id, or a Store numbered 8 would collide with Device 8."""
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ack("heartbeat", 1), NOW, device_id=STANDBY_DEVICE)

    assert manager.get_receiver_snapshot(STANDBY_DEVICE) is None
    assert manager.get_standby_snapshot(STORE) is None
    assert manager.get_standby_snapshot(STANDBY_DEVICE) is not None


# ===========================================================================
# 5. Promotion starts from honest ignorance
# ===========================================================================
def test_a_promoted_standby_does_not_inherit_its_standby_era_readiness(both):
    """What a Device proved as a standby, it proved without carrying the audio."""
    manager, _, standby = both

    manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=STANDBY_DEVICE)
    assert manager.get_standby_snapshot(STANDBY_DEVICE).readiness is ReadinessState.READY

    promoted = FakeWebSocket("promoted")
    later = NOW + timedelta(seconds=30)
    asyncio.run(_connect(manager, promoted, connection_id="c-promoted",
                         device_id=STANDBY_DEVICE, at=later,
                         demote_superseded_device=True))

    store_snapshot = manager.get_receiver_snapshot(STORE)
    assert store_snapshot.requires_ready is True, (
        "the promoted Device carried its standby-era readiness into the Store"
    )
    assert store_snapshot.readiness is not ReadinessState.READY


def test_promotion_discards_the_devices_standby_snapshot(both):
    """It is no longer a standby. Leaving the row behind would show an operator a
    standby that does not exist, and would let a stale sequence rejoin later."""
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ack("heartbeat", 77), NOW, device_id=STANDBY_DEVICE)

    promoted = FakeWebSocket("promoted")
    later = NOW + timedelta(seconds=30)
    asyncio.run(_connect(manager, promoted, connection_id="c-promoted",
                         device_id=STANDBY_DEVICE, at=later,
                         demote_superseded_device=True))

    assert manager.get_standby_snapshot(STANDBY_DEVICE) is None
    assert STANDBY_DEVICE not in manager.standby_receivers


def test_a_promoted_device_can_prove_readiness_from_a_low_sequence(both):
    """The sequence line is per snapshot, so a promotion must not carry the old
    one across. A Device that heartbeat to 77 as a standby and then restarts as
    the primary at 1 has to be believed."""
    manager, _, _ = both

    manager.apply_receiver_payload(STORE, _ack("heartbeat", 77), NOW, device_id=STANDBY_DEVICE)

    promoted = FakeWebSocket("promoted")
    later = NOW + timedelta(seconds=30)
    asyncio.run(_connect(manager, promoted, connection_id="c-promoted",
                         device_id=STANDBY_DEVICE, at=later,
                         demote_superseded_device=True))

    manager.apply_receiver_payload(STORE, _ready(1, at=later), later, device_id=STANDBY_DEVICE)
    assert manager.get_receiver_snapshot(STORE).readiness is ReadinessState.READY


def test_a_disconnecting_standby_leaves_no_snapshot_behind(both):
    """A spare machine unplugged and taken away must not stay in memory as a
    standby forever."""
    manager, _, standby = both

    manager.apply_receiver_payload(STORE, _ack("heartbeat", 1), NOW, device_id=STANDBY_DEVICE)
    manager.disconnect_receiver(STORE, standby, "c-standby", device_id=STANDBY_DEVICE)

    assert manager.get_standby_snapshot(STANDBY_DEVICE) is None


def test_the_store_snapshot_survives_a_standby_disconnecting(both):
    """The primary is unaffected by anything the standby does, including leaving."""
    manager, _, standby = both

    manager.apply_receiver_payload(STORE, _ready(1), NOW, device_id=PRIMARY_DEVICE)
    manager.disconnect_receiver(STORE, standby, "c-standby", device_id=STANDBY_DEVICE)

    snapshot = manager.get_receiver_snapshot(STORE)
    assert snapshot.readiness is ReadinessState.READY
    assert snapshot.connection is ConnectionState.CONNECTED
