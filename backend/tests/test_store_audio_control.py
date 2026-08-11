"""Per-Store output volume and mute: the runtime state and its refusals.

These are unit tests over the registry rather than HTTP tests, because the
behaviour that matters here is ordering and state, not routing: which command
wins when two acknowledgements race, what an unmute restores, and what is
refused. The HTTP surface is covered in test_store_audio_control_api.py.

WHAT IS DELIBERATELY NOT TESTED HERE

That a shop got louder. Nothing in this process can observe that, and a test
that claimed to would be asserting on a mock of an amplifier.
"""

from __future__ import annotations

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

from store_audio_control import (  # noqa: E402
    DEFAULT_VOLUME_PERCENT,
    InvalidVolumeError,
    StoreAudioControlRegistry,
    StoreNotInSessionError,
    UnknownSessionError,
)


@pytest.fixture()
def registry():
    made = StoreAudioControlRegistry()
    made.start_session(session_id=7, owner_user_id=1, store_ids=[10, 11, 12])
    return made


# ===========================================================================
# Defaults and range
# ===========================================================================
def test_a_new_session_starts_at_full_volume_unmuted(registry):
    for state in (registry.state_for(7, sid) for sid in (10, 11, 12)):
        assert state.requested_volume_percent == DEFAULT_VOLUME_PERCENT == 100
        assert state.requested_muted is False
        # Nothing has been applied yet, and the state says so rather than
        # pretending the Store is already at 100.
        assert state.applied_volume_percent is None
        assert state.last_command_id == 0
        assert state.pending is False


@pytest.mark.parametrize("volume", [0, 1, 50, 99, 100])
def test_the_whole_documented_range_is_accepted(registry, volume):
    state = registry.request(session_id=7, store_id=10, volume_percent=volume)
    assert state.requested_volume_percent == volume


@pytest.mark.parametrize("volume", [-1, 101, 150, 1000])
def test_out_of_range_is_refused_not_clamped(registry, volume):
    """Clamping would report success for a request nobody made."""
    with pytest.raises(InvalidVolumeError):
        registry.request(session_id=7, store_id=10, volume_percent=volume)
    # And the refusal changed nothing.
    assert registry.state_for(7, 10).requested_volume_percent == 100


def test_a_boolean_is_not_a_volume(registry):
    # True is 1 in Python, so without an explicit check this would set 1%.
    with pytest.raises(InvalidVolumeError):
        registry.request(session_id=7, store_id=10, volume_percent=True)


# ===========================================================================
# Mute
# ===========================================================================
def test_mute_preserves_the_chosen_level_and_unmute_restores_it(registry):
    registry.request(session_id=7, store_id=10, volume_percent=65)
    muted = registry.request(session_id=7, store_id=10, muted=True)
    assert muted.requested_muted is True
    # The number survives being muted - this is what makes unmute meaningful.
    assert muted.requested_volume_percent == 65
    assert muted.effective_volume_percent == 0

    unmuted = registry.request(session_id=7, store_id=10, muted=False)
    assert unmuted.requested_volume_percent == 65
    assert unmuted.effective_volume_percent == 65


def test_muting_one_store_leaves_every_other_store_alone(registry):
    registry.request(session_id=7, store_id=10, volume_percent=80)
    registry.request(session_id=7, store_id=11, volume_percent=55)
    registry.request(session_id=7, store_id=12, muted=True)

    assert registry.state_for(7, 10).effective_volume_percent == 80
    assert registry.state_for(7, 11).effective_volume_percent == 55
    assert registry.state_for(7, 12).effective_volume_percent == 0
    # And the muted Store keeps its own level for when it is unmuted.
    assert registry.state_for(7, 12).requested_volume_percent == 100


# ===========================================================================
# Command ids and stale acknowledgements
# ===========================================================================
def test_command_ids_increase_across_the_whole_session(registry):
    first = registry.request(session_id=7, store_id=10, volume_percent=40)
    second = registry.request(session_id=7, store_id=11, volume_percent=50)
    third = registry.request(session_id=7, store_id=10, volume_percent=60)
    assert first.last_command_id < second.last_command_id < third.last_command_id


def test_a_stale_acknowledgement_cannot_walk_the_state_backwards(registry):
    """The drag case: 45 is sent, then 70, and 45's answer arrives last."""
    old = registry.request(session_id=7, store_id=10, volume_percent=45)
    new = registry.request(session_id=7, store_id=10, volume_percent=70)

    applied = registry.acknowledge(
        session_id=7, store_id=10, command_id=new.last_command_id,
        result="applied", applied_volume_percent=70, applied_muted=False,
    )
    assert applied.applied_volume_percent == 70

    late = registry.acknowledge(
        session_id=7, store_id=10, command_id=old.last_command_id,
        result="applied", applied_volume_percent=45, applied_muted=False,
    )
    # Discarded, and the caller is told so, so no dashboard update is sent.
    assert late is None
    assert registry.state_for(7, 10).applied_volume_percent == 70


def test_an_acknowledgement_for_a_command_never_issued_is_ignored(registry):
    registry.request(session_id=7, store_id=10, volume_percent=30)
    forged = registry.acknowledge(
        session_id=7, store_id=10, command_id=9999,
        result="applied", applied_volume_percent=100, applied_muted=False,
    )
    assert forged is None
    assert registry.state_for(7, 10).applied_volume_percent is None


def test_a_repeated_acknowledgement_is_ignored(registry):
    sent = registry.request(session_id=7, store_id=10, volume_percent=30)
    first = registry.acknowledge(session_id=7, store_id=10,
                                 command_id=sent.last_command_id,
                                 result="applied", applied_volume_percent=30,
                                 applied_muted=False)
    assert first is not None
    again = registry.acknowledge(session_id=7, store_id=10,
                                 command_id=sent.last_command_id,
                                 result="applied", applied_volume_percent=30,
                                 applied_muted=False)
    assert again is None


# ===========================================================================
# Requested vs applied honesty
# ===========================================================================
def test_a_sent_command_is_pending_until_the_store_answers(registry):
    state = registry.request(session_id=7, store_id=10, volume_percent=50)
    assert state.pending is True
    assert state.applied_volume_percent is None, "nothing may be claimed applied yet"

    registry.acknowledge(session_id=7, store_id=10,
                         command_id=state.last_command_id, result="applied",
                         applied_volume_percent=50, applied_muted=False)
    assert registry.state_for(7, 10).pending is False


def test_a_failed_acknowledgement_is_recorded_as_failed(registry):
    state = registry.request(session_id=7, store_id=10, volume_percent=50)
    registry.acknowledge(
        session_id=7, store_id=10, command_id=state.last_command_id,
        result="failed", error_code="OUTPUT_CONTROL_FAILED",
        error_message="the output level could not be applied",
    )
    final = registry.state_for(7, 10)
    assert final.last_result == "failed"
    assert final.last_error_code == "OUTPUT_CONTROL_FAILED"
    # The requested value is still what the operator asked for; it is simply
    # not applied. Reverting it would hide that the Store disagreed.
    assert final.requested_volume_percent == 50
    assert final.applied_volume_percent is None


def test_an_unsupported_receiver_is_recorded_as_unsupported_not_failed(registry):
    state = registry.request(session_id=7, store_id=10, volume_percent=50)
    registry.acknowledge(session_id=7, store_id=10,
                         command_id=state.last_command_id, result="unsupported",
                         error_code="OUTPUT_CONTROL_UNSUPPORTED",
                         error_message="this Receiver has no controllable audio output")
    assert registry.state_for(7, 10).last_result == "unsupported"


# ===========================================================================
# Session and Store boundaries
# ===========================================================================
def test_a_store_outside_the_session_is_refused(registry):
    with pytest.raises(StoreNotInSessionError):
        registry.request(session_id=7, store_id=999, volume_percent=50)


def test_a_finished_session_refuses_further_commands(registry):
    registry.end_session(7)
    with pytest.raises(UnknownSessionError):
        registry.request(session_id=7, store_id=10, volume_percent=50)
    with pytest.raises(UnknownSessionError):
        registry.session_owner(7)


def test_an_unknown_session_is_refused_exactly_like_a_finished_one(registry):
    """A guessed session id learns nothing a stale one would not."""
    with pytest.raises(UnknownSessionError):
        registry.request(session_id=424242, store_id=10, volume_percent=50)


def test_concurrent_sessions_do_not_touch_each_other(registry):
    """Alice on 10/11, Bob on 20/21. Alice turns 10 down."""
    registry.start_session(session_id=8, owner_user_id=2, store_ids=[20, 21])
    registry.request(session_id=7, store_id=10, volume_percent=30)

    assert registry.state_for(7, 10).requested_volume_percent == 30
    for store_id in (20, 21):
        assert registry.state_for(8, store_id).requested_volume_percent == 100
        assert registry.state_for(8, store_id).requested_muted is False
    assert registry.session_owner(7) == 1
    assert registry.session_owner(8) == 2


def test_ending_one_session_leaves_the_other_running(registry):
    registry.start_session(session_id=8, owner_user_id=2, store_ids=[20])
    registry.end_session(7)
    assert registry.session_owner(8) == 2
    assert registry.active_session_ids() == (8,)


def test_a_store_broadcast_by_two_sessions_keeps_separate_state(registry):
    """Leases forbid this today; the state model must not rely on that."""
    registry.start_session(session_id=8, owner_user_id=2, store_ids=[10])
    registry.request(session_id=7, store_id=10, volume_percent=20)
    registry.request(session_id=8, store_id=10, volume_percent=90)
    assert registry.state_for(7, 10).requested_volume_percent == 20
    assert registry.state_for(8, 10).requested_volume_percent == 90


def test_ending_a_session_twice_is_harmless(registry):
    registry.end_session(7)
    registry.end_session(7)


def test_describe_lists_every_store_in_a_stable_order(registry):
    rows = registry.describe(7)
    assert [row["store_id"] for row in rows] == [10, 11, 12]
    # The shape the API returns, including both halves of the truth.
    assert set(rows[0]) >= {
        "store_id", "requested_volume_percent", "requested_muted",
        "applied_volume_percent", "applied_muted", "last_command_id",
        "last_acknowledged_command_id", "result", "pending",
    }
