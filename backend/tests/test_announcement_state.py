"""The announcement state machine, tested without a database or a Store.

The rule these exist to hold in place is one sentence: a broadcast ending
resumes only what the broadcast itself paused. Everything else in this file is
that sentence approached from a different direction.

Getting it wrong is not a subtle bug. It is a shop that starts talking on its
own, an hour after somebody deliberately silenced it, with nobody able to
explain why.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from announcements import (  # noqa: E402
    AnnouncementRefused,
    DEFAULT_VOLUME,
    MAX_VOLUME,
    STATE_DUCKED,
    STATE_PAUSED,
    STATE_PLAYING,
    STATE_STOPPED,
    duck,
    item_targets_exactly_one,
    next_state_for_pause,
    next_state_for_play,
    unduck,
    validate_upload,
    validate_volume,
)


# ===========================================================================
# Ducking: the broadcast arrives
# ===========================================================================

def test_a_playing_announcement_steps_aside_for_a_broadcast():
    state, remembered = duck(STATE_PLAYING)
    assert state == STATE_DUCKED
    assert remembered == STATE_PLAYING


def test_a_paused_announcement_is_not_touched_by_a_broadcast():
    """Somebody paused this Store on purpose. A broadcast passing through must
    not quietly convert that into "will resume shortly"."""
    state, remembered = duck(STATE_PAUSED)
    assert state == STATE_PAUSED
    assert remembered is None


def test_a_stopped_store_is_not_touched_by_a_broadcast():
    state, remembered = duck(STATE_STOPPED)
    assert state == STATE_STOPPED
    assert remembered is None


def test_ducking_twice_does_not_lose_where_it_came_from():
    """A second broadcast starting - or the same one reported twice - must not
    overwrite the memory with DUCKED, which would make the announcement
    un-resumable."""
    state, remembered = duck(STATE_DUCKED)
    assert state == STATE_DUCKED
    assert remembered is None, "the original state must not be overwritten"


# ===========================================================================
# Unducking: the broadcast ends
# ===========================================================================

def test_the_announcement_comes_back_when_the_broadcast_ends():
    state, _ = unduck(STATE_DUCKED, STATE_PLAYING)
    assert state == STATE_PLAYING


def test_a_store_paused_during_the_broadcast_stays_silent():
    """THE failure this whole design is shaped around.

    An operator pauses a Store while a broadcast is running. If pause and duck
    were one state, the broadcast ending would start the announcement anyway.
    """
    ducked, remembered = duck(STATE_PLAYING)
    assert ducked == STATE_DUCKED

    # The operator presses Pause while the broadcast is still running.
    paused = next_state_for_pause(ducked)
    assert paused == STATE_PAUSED

    # The broadcast ends.
    after, _ = unduck(paused, remembered)
    assert after == STATE_PAUSED, (
        "a Store an operator silenced must not start talking when an "
        "unrelated broadcast finishes")


def test_a_store_that_was_never_ducked_is_not_started_by_a_broadcast_ending():
    for resting in (STATE_STOPPED, STATE_PAUSED):
        state, _ = unduck(resting, None)
        assert state == resting


def test_unducking_without_a_memory_falls_back_to_playing():
    """A DUCKED row can only have been created by ducking, which always records
    where it came from - but a database restored from an older version might
    not have. PLAYING is the honest fallback: DUCKED means "was playing"."""
    state, _ = unduck(STATE_DUCKED, None)
    assert state == STATE_PLAYING


# ===========================================================================
# Play and Pause, pressed by a person
# ===========================================================================

def test_pressing_play_starts_a_stopped_store():
    assert next_state_for_play(STATE_STOPPED) == STATE_PLAYING


def test_pressing_play_during_a_broadcast_is_refused_in_words():
    """Not ignored, and not obeyed. Obeying would talk over the broadcast;
    ignoring would leave the operator pressing a button that does nothing."""
    with pytest.raises(AnnouncementRefused) as refusal:
        next_state_for_play(STATE_DUCKED)
    message = str(refusal.value)
    assert "broadcast" in message.lower()
    assert "resume" in message.lower(), "it must say what happens next"


def test_pausing_a_stopped_store_changes_nothing():
    assert next_state_for_pause(STATE_STOPPED) == STATE_STOPPED


# ===========================================================================
# Validation
# ===========================================================================

def test_volume_outside_the_range_is_refused_with_both_bounds():
    with pytest.raises(AnnouncementRefused) as refusal:
        validate_volume(140)
    assert "140" in str(refusal.value)
    assert validate_volume(MAX_VOLUME) == MAX_VOLUME
    assert validate_volume(0) == 0
    assert validate_volume(str(DEFAULT_VOLUME)) == DEFAULT_VOLUME


def test_volume_that_is_not_a_number_is_refused():
    with pytest.raises(AnnouncementRefused):
        validate_volume("loud")


def test_a_template_line_names_a_store_or_a_zone_but_not_both():
    item_targets_exactly_one(4, None)
    item_targets_exactly_one(None, "NORTH")
    with pytest.raises(AnnouncementRefused):
        item_targets_exactly_one(4, "NORTH")
    with pytest.raises(AnnouncementRefused):
        item_targets_exactly_one(None, None)


def test_an_oversized_recording_is_refused_before_anything_is_written():
    from announcements import MAX_AUDIO_BYTES

    with pytest.raises(AnnouncementRefused) as refusal:
        validate_upload(b"x" * (MAX_AUDIO_BYTES + 1), "audio/mpeg", "long.mp3")
    assert "MB" in str(refusal.value)


def test_a_format_the_receiver_cannot_play_is_refused_by_name():
    with pytest.raises(AnnouncementRefused) as refusal:
        validate_upload(b"data", "application/pdf", "offer.pdf")
    assert "offer.pdf" in str(refusal.value)
    assert "mp3" in str(refusal.value)


def test_an_empty_file_is_refused():
    with pytest.raises(AnnouncementRefused):
        validate_upload(b"", "audio/mpeg", "silence.mp3")


def test_an_accepted_upload_returns_the_extension_to_store_under():
    assert validate_upload(b"ID3data", "audio/mpeg", "diwali.mp3") == ".mp3"
    assert validate_upload(b"RIFFdata", "audio/wav; charset=binary",
                           "diwali.wav") == ".wav"


def test_the_uploaded_filename_never_decides_where_the_bytes_land():
    """The one part of an upload a stranger chooses must decide nothing."""
    from announcements import new_storage_name

    first = new_storage_name(".mp3")
    second = new_storage_name(".mp3")
    assert first != second
    assert first.endswith(".mp3")
    assert "/" not in first and "\\" not in first and ".." not in first
