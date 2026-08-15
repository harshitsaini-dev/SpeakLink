"""When a recorded announcement should be playing, and when it should not.

WHY THIS IS A SEPARATE FILE OF PURE FUNCTIONS

A daily window is the one part of this feature that is easy to get subtly,
expensively wrong: 22:00-02:00 crosses midnight, "until 22:00" has to mean the
jingle is silent AT 22:00, and a shop that was switched off overnight must not
come back to a promotion that expired last week. Every one of those is a
question about two times and a clock, with no database and no network in it -
so they are answered here, where a test can ask them directly.

The scheduler that USES this lives in the server and does exactly two things:
it starts a template when the window opens, and it stops it when the window
closes. It deliberately does not "correct" anything in between.

WHAT THE SCHEDULE DOES NOT DO

It does not overrule a person. If somebody pauses a shop at eleven in the
morning, the window does not un-pause it at noon: a pause that a machine
undoes thirty seconds later is not a pause. The window opens the campaign and
closes it; between those two moments the operator is in charge.

TIME IS THE HQ MACHINE'S LOCAL TIME

Deliberately, and it is written on the form. A shop opens at ten by the clock
on the wall, and every Store in this estate keeps the same wall clock as HQ.
Storing UTC and converting would be more correct in a general product and
would mean the person typing "10:00" has to think about which ten o'clock they
mean - which is exactly the thinking this feature exists to remove.
"""

from __future__ import annotations

from datetime import datetime, time

#: Monday is 0, matching datetime.weekday(), so nothing has to be renumbered
#: on the way in or out.
ALL_DAYS = "0,1,2,3,4,5,6"


class ScheduleRefused(Exception):
    """A schedule that cannot mean anything, said in a sentence."""


def parse_clock(value: str | None) -> time | None:
    """`"10:00"` as a time. Empty means "no schedule", which is not an error.

    Accepts H:MM and HH:MM and nothing else. A field that quietly accepted
    "10" would leave somebody guessing whether that was ten o'clock or ten
    minutes past midnight.
    """
    text = (value or "").strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        raise ScheduleRefused(
            f"'{value}' is not a time. Write it as HH:MM, like 10:00 or 22:30.")
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except ValueError:
        raise ScheduleRefused(
            f"'{value}' is not a time. Write it as HH:MM, like 10:00 or 22:30.")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleRefused(
            f"'{value}' is not a time on a 24-hour clock.")
    return time(hour=hour, minute=minute)


def parse_days(value: str | None) -> str:
    """Which weekdays this window applies to, as a stored string.

    Empty means every day, because that is what somebody typing only "10:00 to
    22:00" means. Monday is 0.
    """
    text = (value or "").strip()
    if not text:
        return ALL_DAYS
    days = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            day = int(part)
        except ValueError:
            raise ScheduleRefused("Days are numbers, Monday 0 to Sunday 6.")
        if not 0 <= day <= 6:
            raise ScheduleRefused("Days are numbers, Monday 0 to Sunday 6.")
        if day not in days:
            days.append(day)
    if not days:
        return ALL_DAYS
    return ",".join(str(day) for day in sorted(days))


def validate_window(start: str | None, end: str | None) -> tuple[str, str]:
    """The pair, checked together, and returned as stored strings.

    Half a window is refused rather than half-applied. "Start at 10:00" with
    no end is a campaign that never stops, which is precisely the thing the
    person was trying to avoid by setting a schedule at all.
    """
    first = parse_clock(start)
    last = parse_clock(end)
    if first is None and last is None:
        return "", ""
    if first is None or last is None:
        raise ScheduleRefused(
            "A daily schedule needs both a start and a stop time. Leave both "
            "empty for a campaign that plays until somebody stops it.")
    if first == last:
        raise ScheduleRefused(
            "The start and the stop are the same time, so the window is "
            "either a moment or a whole day - say which by moving one of them.")
    return first.strftime("%H:%M"), last.strftime("%H:%M")


def is_within(start: str | None, end: str | None, days: str | None,
              now: datetime) -> bool:
    """Is this moment inside the daily window?

    A window that ends before it starts crosses midnight - 22:00 to 02:00 is
    four hours of night, not twenty hours of day. The end is EXCLUSIVE: a
    campaign that runs "until 22:00" is silent at 22:00, which is what the
    person who typed it meant and what the shop's neighbours expect.

    On a window that crosses midnight, the DAY is the day it started. A
    Friday-only 22:00-02:00 window plays into Saturday morning, because that
    is one Friday night rather than two half-nights.
    """
    first = parse_clock(start)
    last = parse_clock(end)
    if first is None or last is None:
        return True                      # no schedule: the window is always
    allowed = {int(day) for day in parse_days(days).split(",")}
    current = now.time()

    if first < last:
        return now.weekday() in allowed and first <= current < last
    # Crosses midnight.
    if current >= first:
        return now.weekday() in allowed
    if current < last:
        # Still inside the night that began yesterday.
        return (now.weekday() - 1) % 7 in allowed
    return False


def describe(start: str | None, end: str | None, days: str | None) -> str:
    """The window in words, for a page that has to explain why nothing is
    playing at nine in the morning."""
    first = parse_clock(start)
    last = parse_clock(end)
    if first is None or last is None:
        return ""
    stored_days = parse_days(days)
    when = ("every day" if stored_days == ALL_DAYS
            else "on " + ", ".join(
                ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][int(day)]
                for day in stored_days.split(",")))
    crossing = " (through the night)" if first > last else ""
    return (f"{first.strftime('%H:%M')} to {last.strftime('%H:%M')} "
            f"{when}{crossing}")
