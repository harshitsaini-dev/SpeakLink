"""A daily window: 10:00 to 22:00, and the awkward cases around it.

These are pure-function tests on purpose. Every question here is about two
times and a clock - no database, no sockets - and each one is a way this
feature could be subtly, expensively wrong in a shop full of customers.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import announcement_schedule as schedule  # noqa: E402


def at(text: str) -> datetime:
    """'2026-08-14 10:00' - a Friday, unless the test says otherwise."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


# ===========================================================================
# The ordinary window
# ===========================================================================

def test_a_shop_hours_window_plays_between_its_two_times():
    inside = at("2026-08-14 12:00")
    assert schedule.is_within("10:00", "22:00", None, inside) is True
    assert schedule.is_within("10:00", "22:00", None, at("2026-08-14 09:59")) is False


def test_the_stop_time_is_exclusive():
    """"Until 22:00" means silent AT 22:00.

    Somebody typed a time they expect the shop to be quiet at, and a jingle
    that starts exactly then is the one the neighbours ring about.
    """
    assert schedule.is_within("10:00", "22:00", None, at("2026-08-14 21:59")) is True
    assert schedule.is_within("10:00", "22:00", None, at("2026-08-14 22:00")) is False


def test_the_start_time_is_inclusive():
    assert schedule.is_within("10:00", "22:00", None, at("2026-08-14 10:00")) is True


def test_no_window_means_always():
    """A campaign with no schedule plays until a person stops it - which is
    how every template behaved before schedules existed."""
    assert schedule.is_within("", "", None, at("2026-08-14 03:00")) is True
    assert schedule.is_within(None, None, None, at("2026-08-14 03:00")) is True


# ===========================================================================
# Midnight
# ===========================================================================

def test_a_window_that_ends_before_it_starts_crosses_midnight():
    """22:00 to 02:00 is four hours of night, not twenty hours of day."""
    assert schedule.is_within("22:00", "02:00", None, at("2026-08-14 23:30")) is True
    assert schedule.is_within("22:00", "02:00", None, at("2026-08-15 01:30")) is True
    assert schedule.is_within("22:00", "02:00", None, at("2026-08-15 12:00")) is False


def test_a_night_window_belongs_to_the_day_it_started():
    """A Friday-only 22:00-02:00 window plays into Saturday morning: that is
    one Friday night, not two half-nights."""
    friday_night = at("2026-08-14 23:00")      # Friday
    saturday_small_hours = at("2026-08-15 01:00")
    assert friday_night.weekday() == 4
    assert schedule.is_within("22:00", "02:00", "4", friday_night) is True
    assert schedule.is_within("22:00", "02:00", "4", saturday_small_hours) is True
    # And a Saturday-only window does NOT play on Saturday morning, because
    # that morning belongs to Friday night.
    assert schedule.is_within("22:00", "02:00", "5", saturday_small_hours) is False


# ===========================================================================
# Days
# ===========================================================================

def test_a_weekday_only_window_is_silent_at_the_weekend():
    weekdays = "0,1,2,3,4"
    assert schedule.is_within("10:00", "22:00", weekdays, at("2026-08-14 12:00")) is True
    assert schedule.is_within("10:00", "22:00", weekdays, at("2026-08-15 12:00")) is False


def test_no_days_means_every_day():
    assert schedule.parse_days("") == schedule.ALL_DAYS
    assert schedule.parse_days(None) == schedule.ALL_DAYS


def test_days_are_stored_sorted_and_deduplicated():
    assert schedule.parse_days("4,0,4,1") == "0,1,4"


def test_a_day_outside_the_week_is_refused():
    with pytest.raises(schedule.ScheduleRefused):
        schedule.parse_days("9")


# ===========================================================================
# What the form is allowed to save
# ===========================================================================

def test_half_a_window_is_refused_rather_than_half_applied():
    """"Start at 10:00" with no stop is a campaign that never stops - which is
    the thing somebody setting a schedule was trying to avoid."""
    with pytest.raises(schedule.ScheduleRefused) as refusal:
        schedule.validate_window("10:00", "")
    assert "both a start and a stop" in str(refusal.value)


def test_both_empty_is_a_template_with_no_schedule():
    assert schedule.validate_window("", "") == ("", "")
    assert schedule.validate_window(None, None) == ("", "")


def test_a_zero_length_window_is_refused():
    with pytest.raises(schedule.ScheduleRefused):
        schedule.validate_window("10:00", "10:00")


def test_a_time_that_is_not_a_time_says_so_in_a_sentence():
    with pytest.raises(schedule.ScheduleRefused) as refusal:
        schedule.parse_clock("10")
    assert "HH:MM" in str(refusal.value)
    with pytest.raises(schedule.ScheduleRefused):
        schedule.parse_clock("25:00")


def test_times_come_back_normalised():
    assert schedule.validate_window("9:5", "22:00") == ("09:05", "22:00")


# ===========================================================================
# Saying it out loud
# ===========================================================================

def test_the_window_can_be_described_for_somebody_reading_a_page():
    assert schedule.describe("10:00", "22:00", None) == "10:00 to 22:00 every day"
    assert "Mon" in schedule.describe("10:00", "22:00", "0")
    assert "through the night" in schedule.describe("22:00", "02:00", None)
    assert schedule.describe("", "", None) == ""
