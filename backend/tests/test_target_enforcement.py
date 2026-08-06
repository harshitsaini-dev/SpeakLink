"""Putting a Store back to its target without fighting the person at the till.

THE HAZARD

"See a mismatch, send a command" is a literal argument with a human holding a
mouse. They drag the slider, EchoCast snaps it back, they drag it again - and
because an endpoint notification arrives within milliseconds, that loop would
run as fast as the socket allows.

So every test here is about a bound: how long EchoCast waits, how long it then
leaves the Store alone, how many times it is willing to try, and the situations
in which it must not act at all.

Time is passed in rather than slept, so these describe the policy exactly
rather than approximately.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
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

import target_enforcement  # noqa: E402


@dataclass
class Target:
    volume_percent: int | None = 70
    muted: bool | None = None


@pytest.fixture()
def policy():
    return target_enforcement.TargetEnforcementPolicy(
        debounce_seconds=20.0, cooldown_seconds=15.0, max_attempts=3,
        post_broadcast_seconds=30.0)


def decide(policy, *, now, target=None, volume=25, muted=False, online=True,
           endpoint_ready=True, owned=False, store_id=1):
    return policy.decide(
        store_id=store_id, target=Target() if target is None else target,
        actual_volume_percent=volume, actual_muted=muted, online=online,
        endpoint_ready=endpoint_ready, owned_by_broadcast=owned, now=now)


# ===========================================================================
# The debounce: let somebody finish adjusting the volume
# ===========================================================================
def test_a_fresh_mismatch_is_not_acted_on_immediately(policy):
    """Somebody may still have their hand on the slider."""
    outcome = decide(policy, now=100.0)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_DEBOUNCE


def test_a_mismatch_left_alone_long_enough_is_enforced(policy):
    decide(policy, now=100.0)                 # drift starts
    outcome = decide(policy, now=121.0)       # 21s later
    assert outcome.should_enforce is True


def test_a_continuing_drag_does_not_postpone_enforcement_for_ever(policy):
    """The clock starts when drift STARTED, not at the latest reading.

    Timing from the most recent reading would let a slow, continuous
    adjustment reset the debounce indefinitely, and nothing would ever be
    enforced.
    """
    policy.note_reading(store_id=1, matches_target=False, now=100.0)
    for moment in (102.0, 105.0, 110.0, 118.0):
        policy.note_reading(store_id=1, matches_target=False, now=moment)
    assert decide(policy, now=121.0).should_enforce is True


def test_a_drag_that_ends_back_at_the_target_is_not_enforced(policy):
    """They changed their mind. There is nothing to correct."""
    policy.note_reading(store_id=1, matches_target=False, now=100.0)
    policy.note_reading(store_id=1, matches_target=True, now=110.0)
    outcome = decide(policy, now=140.0, volume=70)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_MATCHES


def test_dozens_of_readings_produce_at_most_one_command(policy):
    """A drag is many readings and at most one enforcement."""
    for step in range(60):
        policy.note_reading(store_id=1, matches_target=False, now=100.0 + step * 0.1)

    enforced = 0
    for step in range(60):
        now = 130.0 + step * 0.1
        if decide(policy, now=now).should_enforce:
            enforced += 1
            policy.note_enforced(store_id=1, now=now)
    assert enforced == 1, "the cooldown must collapse the rest"


# ===========================================================================
# The cooldown: do not answer your own command
# ===========================================================================
def test_a_store_is_left_alone_after_being_enforced(policy):
    policy.note_enforced(store_id=1, now=100.0)
    outcome = decide(policy, now=105.0)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_COOLDOWN


def test_the_cooldown_expires(policy):
    policy.note_enforced(store_id=1, now=100.0)
    policy.note_reading(store_id=1, matches_target=False, now=116.0)
    assert decide(policy, now=140.0).should_enforce is True


def test_the_readback_of_our_own_command_cannot_trigger_another(policy):
    """The command lands, the endpoint reports it, and that is the end of it."""
    policy.note_enforced(store_id=1, now=100.0)
    policy.note_reading(store_id=1, matches_target=True, now=100.4)
    outcome = decide(policy, now=101.0, volume=70)
    assert outcome.should_enforce is False


# ===========================================================================
# The attempt budget: an argument must end
# ===========================================================================
def test_enforcement_gives_up_after_the_budget_and_says_so(policy):
    """Somebody is disagreeing with HQ, and that is information."""
    now = 100.0
    for _ in range(3):
        policy.note_reading(store_id=1, matches_target=False, now=now)
        assert decide(policy, now=now + 21.0).should_enforce is True
        policy.note_enforced(store_id=1, now=now + 21.0)
        now += 60.0

    policy.note_reading(store_id=1, matches_target=False, now=now)
    outcome = decide(policy, now=now + 21.0)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_SUSPENDED
    assert policy.is_suspended(1) is True


def test_a_store_seen_matching_its_target_is_no_longer_suspended(policy):
    policy.note_enforced(store_id=1, now=100.0)
    policy.note_enforced(store_id=1, now=200.0)
    policy.note_enforced(store_id=1, now=300.0)
    assert policy.is_suspended(1) is True

    policy.note_reading(store_id=1, matches_target=True, now=400.0)
    assert policy.is_suspended(1) is False
    assert policy.attempts(1) == 0


def test_a_new_target_voids_the_previous_disagreement(policy):
    """The operator is asking for something new, not retrying the old one."""
    policy.note_enforced(store_id=1, now=100.0)
    policy.note_enforced(store_id=1, now=200.0)
    policy.note_enforced(store_id=1, now=300.0)
    assert policy.is_suspended(1) is True

    policy.note_target_changed(store_id=1)
    assert policy.is_suspended(1) is False
    policy.note_reading(store_id=1, matches_target=False, now=400.0)
    assert decide(policy, now=421.0).should_enforce is True


def test_repeated_slider_activity_cannot_produce_unbounded_commands(policy):
    """The whole hazard, simulated: somebody fighting EchoCast for an hour."""
    commands = 0
    now = 0.0
    for _ in range(600):                       # ten minutes of activity
        now += 1.0
        policy.note_reading(store_id=1, matches_target=False, now=now)
        if decide(policy, now=now).should_enforce:
            commands += 1
            policy.note_enforced(store_id=1, now=now)
    assert commands <= target_enforcement.MAX_ATTEMPTS
    assert policy.is_suspended(1) is True


# ===========================================================================
# When enforcement must not run at all
# ===========================================================================
def test_a_broadcast_owning_the_store_outranks_everything(policy):
    policy.note_reading(store_id=1, matches_target=False, now=100.0)
    outcome = decide(policy, now=200.0, owned=True)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_BROADCAST


def test_an_offline_store_is_not_enforced(policy):
    policy.note_reading(store_id=1, matches_target=False, now=100.0)
    outcome = decide(policy, now=200.0, online=False)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_OFFLINE


def test_a_store_with_no_controllable_output_is_not_enforced(policy):
    policy.note_reading(store_id=1, matches_target=False, now=100.0)
    outcome = decide(policy, now=200.0, endpoint_ready=False)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_ENDPOINT


def test_a_store_with_no_target_is_not_enforced(policy):
    outcome = policy.decide(
        store_id=1, target=None, actual_volume_percent=25, actual_muted=False,
        online=True, endpoint_ready=True, owned_by_broadcast=False, now=200.0)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_NO_TARGET


def test_enforcement_cannot_race_a_broadcast_restoration(policy):
    """STOP must be SEEN to put a shop back.

    Restoration sets a Store to its pre-broadcast level - which is usually not
    the target - so without this the very next sweep would drag it away and the
    restoration would look as though it never happened.
    """
    policy.note_broadcast_restored(store_id=1, now=100.0)
    policy.note_reading(store_id=1, matches_target=False, now=101.0)

    outcome = decide(policy, now=125.0)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_POST_BROADCAST

    # And after the quiet period it resumes normally.
    policy.note_reading(store_id=1, matches_target=False, now=131.0)
    assert decide(policy, now=160.0).should_enforce is True


def test_a_muted_target_is_enforced_on_mute_alone(policy):
    """Volume may be unset; a mute mismatch is still a mismatch."""
    target = Target(volume_percent=None, muted=True)
    policy.note_reading(store_id=1, matches_target=False, now=100.0)
    outcome = decide(policy, now=121.0, target=target, volume=70, muted=False)
    assert outcome.should_enforce is True


def test_a_volume_only_target_ignores_the_mute(policy):
    target = Target(volume_percent=70, muted=None)
    outcome = decide(policy, now=200.0, target=target, volume=70, muted=True)
    assert outcome.should_enforce is False
    assert outcome.reason == target_enforcement.SKIP_MATCHES


# ===========================================================================
# One Store's argument is its own
# ===========================================================================
def test_one_stores_bookkeeping_never_affects_another(policy):
    policy.note_enforced(store_id=1, now=100.0)
    policy.note_enforced(store_id=1, now=200.0)
    policy.note_enforced(store_id=1, now=300.0)
    assert policy.is_suspended(1) is True
    assert policy.is_suspended(2) is False

    policy.note_reading(store_id=2, matches_target=False, now=100.0)
    assert decide(policy, now=121.0, store_id=2).should_enforce is True


def test_forgetting_a_store_clears_only_that_store(policy):
    policy.note_enforced(store_id=1, now=100.0)
    policy.note_enforced(store_id=2, now=100.0)
    policy.forget(1)
    assert policy.attempts(1) == 0
    assert policy.attempts(2) == 1
