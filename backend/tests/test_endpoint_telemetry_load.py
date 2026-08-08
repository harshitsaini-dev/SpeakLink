"""Volume telemetry must stay bounded as the estate grows.

One Store dragging its Windows slider emits a notification per step. With forty
Stores doing it during one announcement, the question is not whether the
readings are correct - that is settled elsewhere - but whether the cost stays
proportionate, whether the newest reading always wins, and whether one noisy
Store can affect another.

These are software measurements on the real registry and the real observer.
They say nothing about audio hardware.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
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

from store_audio_control import StoreAudioControlRegistry  # noqa: E402
from tools.windows_endpoint_observer import EndpointObserver  # noqa: E402


class FakeObserverBackend:
    def __init__(self):
        self.registered = {}

    def register(self, endpoint_id, on_change):
        self.registered[endpoint_id] = on_change

    def unregister(self, endpoint_id):
        self.registered.pop(endpoint_id, None)

    def change(self, endpoint_id, volume_percent, muted=False):
        self.registered[endpoint_id](volume_percent, muted)


def endpoint_for(store):
    return "{0.0.0.00000000}.{%08d-1111-2222-3333-444444444444}" % store


def make_session(registry, session_id, store_ids):
    registry.start_session(session_id=session_id, owner_user_id=1,
                           store_ids=store_ids)
    return registry


DRAG_STEPS = 60          # one unhurried gesture from 80 down to 21


@pytest.mark.parametrize("store_count", [5, 10, 20, 40])
def test_a_whole_estate_dragging_at_once_stays_bounded(store_count):
    """Every Store drags its slider simultaneously; each is coalesced alone."""
    registry = StoreAudioControlRegistry()
    stores = list(range(1, store_count + 1))
    make_session(registry, session_id=900, store_ids=stores)

    backend = FakeObserverBackend()
    observers = {}
    for store in stores:
        observer = EndpointObserver(endpoint_for(store), backend=backend)
        observer.start()
        observers[store] = observer

    started = time.perf_counter()
    notifications = 0
    for step in range(80, 80 - DRAG_STEPS, -1):
        for store in stores:
            backend.change(endpoint_for(store), step, muted=False)
            notifications += 1

    # Each Store's reporter takes only the LATEST reading, which is what the
    # single-slot observer gives it - so this is the number of messages the
    # estate would actually put on the wire for the whole gesture.
    taken = 0
    sequence = 0
    for store, observer in observers.items():
        reading = observer.take()
        assert reading is not None, f"store {store} produced nothing"
        taken += 1
        sequence += 1
        registry.observe_endpoint_state(
            session_id=900, store_id=store, state_sequence=sequence,
            volume_percent=reading.volume_percent, muted=reading.muted)
    elapsed = time.perf_counter() - started

    print(f"\nLOAD {store_count:>2} stores: {notifications} notifications -> "
          f"{taken} messages "
          f"({notifications / max(taken, 1):.0f}x coalesced), "
          f"{elapsed * 1000:.1f} ms, max queue depth 1 per store")

    assert taken == store_count, "one message per Store, not one per step"
    assert notifications == store_count * DRAG_STEPS
    # Where the gesture stopped is what every Store must be reporting.
    for store in stores:
        state = registry.state_for(session_id=900, store_id=store)
        assert state.actual_volume_percent == 80 - DRAG_STEPS + 1
    assert elapsed < 5.0, f"{elapsed:.1f}s for {store_count} stores is not bounded"


def test_the_observer_never_holds_more_than_one_reading():
    """The bound is structural: a slot, not a queue that could grow."""
    backend = FakeObserverBackend()
    observer = EndpointObserver(endpoint_for(1), backend=backend)
    observer.start()

    for step in range(100, 0, -1):
        backend.change(endpoint_for(1), step, muted=False)

    first = observer.take()
    assert first.volume_percent == 1, "the latest reading must survive"
    assert observer.take() is None, (
        "a second reading was queued - this is the unbounded growth the "
        "contract forbids")


def test_one_noisy_store_cannot_move_another():
    registry = StoreAudioControlRegistry()
    make_session(registry, session_id=901, store_ids=[1, 2])
    registry.observe_endpoint_state(session_id=901, store_id=1,
                                    state_sequence=1, volume_percent=80,
                                    muted=False)
    registry.observe_endpoint_state(session_id=901, store_id=2,
                                    state_sequence=1, volume_percent=60,
                                    muted=False)

    for sequence in range(2, 200):
        registry.observe_endpoint_state(
            session_id=901, store_id=1, state_sequence=sequence,
            volume_percent=25, muted=False)

    assert registry.state_for(session_id=901, store_id=1).actual_volume_percent == 25
    assert registry.state_for(session_id=901, store_id=2).actual_volume_percent == 60, (
        "store B moved because store A was noisy")


def test_a_burst_never_writes_to_the_database(tmp_path):
    """Runtime state is in memory. A slider tick is not an audit record."""
    registry = StoreAudioControlRegistry()
    make_session(registry, session_id=902, store_ids=[1])

    database = Path(os.environ["SPEAKLINK_DB_PATH"])
    before = database.stat().st_mtime_ns if database.exists() else None

    for sequence in range(1, 500):
        registry.observe_endpoint_state(
            session_id=902, store_id=1, state_sequence=sequence,
            volume_percent=(sequence % 100) or 1, muted=False)

    after = database.stat().st_mtime_ns if database.exists() else None
    assert before == after, "telemetry touched the database"


def test_telemetry_issues_no_outbound_command():
    """The feedback loop that must never exist.

    HQ asked for 80 and the till moved it to 25. If observing that produced a
    command, the Store would jump back to 80 and the person at the till would
    be fighting the software - which is the enforcement system that was
    deliberately removed.
    """
    registry = StoreAudioControlRegistry()
    make_session(registry, session_id=903, store_ids=[1])
    requested = registry.request(session_id=903, store_id=1,
                                 volume_percent=80)
    command_id_after_request = requested.last_command_id

    updated = registry.observe_endpoint_state(
        session_id=903, store_id=1, state_sequence=1,
        volume_percent=25, muted=False)

    assert updated.actual_volume_percent == 25, "the truth is what the shop does"
    assert updated.requested_volume_percent == 80, (
        "a person at the till does not retract the operator's request")
    assert updated.last_command_id == command_id_after_request, (
        "telemetry allocated a command id - something is about to be sent back "
        "to the Store, which is the feedback loop this must never have")
