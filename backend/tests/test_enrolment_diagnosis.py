"""Telling an operator WHY an enrolment failed.

Both halves of this were wrong at once on a real Store, and together they cost
an afternoon: the wizard said "check the code" for failures that had nothing to
do with the code, and HQ's own log said "could not classify" for the commonest
cause there is.

The code that failed was 28 characters. A SpeakLink code is 32. Nothing in the
system said so - HQ cannot tell an incomplete code from an unknown one, because
to HQ both are a hash that matches nothing, and the wizard did not look at the
string it was about to send.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend",
                  REPOSITORY_ROOT / "tools"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

import pytest  # noqa: E402

from enrolment_refusal import RefusalCategory, classify_enrolment_refusal  # noqa: E402
from store_setup_core import CODE_LENGTH, describe_code_shape  # noqa: E402


# ===========================================================================
# HQ's log has to name the cause
# ===========================================================================

@pytest.mark.parametrize(("message", "expected"), [
    # The exact sentences receiver_enrollment_codes.py raises. Parametrised
    # from the real wording rather than paraphrased, because the bug was that
    # the classifier knew paraphrases and not the wording.
    ("that enrolment code is not recognised", RefusalCategory.UNKNOWN_TOKEN),
    ("that enrolment code has already been used", RefusalCategory.ALREADY_USED),
    ("that enrolment code has expired", RefusalCategory.EXPIRED_TOKEN),
    ("no enrolment code was presented", RefusalCategory.UNKNOWN_TOKEN),
    ("that Store is not available for enrolment", RefusalCategory.STORE_DISABLED),
])
def test_every_refusal_the_service_can_raise_is_classified(message, expected):
    assert classify_enrolment_refusal(message) is expected


def test_the_service_and_the_classifier_do_not_drift_apart():
    """The check that would have caught this before a Store did.

    Every refusal message in the code service is read out of the source and
    put through the classifier. A new message that nobody teaches the
    classifier fails here rather than in a shop.
    """
    import re

    source = (REPOSITORY_ROOT / "backend" / "receiver_enrollment_codes.py").read_text(
        encoding="utf-8")
    messages = re.findall(r'raise EnrollmentCode\w+\("([^"]+)"', source)
    assert messages, "no refusal messages found - has the service moved?"

    unclassified = [message for message in messages
                    if classify_enrolment_refusal(message)
                    is RefusalCategory.INVALID_STATE]
    assert unclassified == [], (
        "these refusals would be logged as unclassifiable: " + str(unclassified))


def test_something_genuinely_unrecognised_is_still_not_guessed_at():
    """INVALID_STATE has to keep meaning what it says. Recording an unknown
    fault as "expired" because that was the nearest match would send an
    operator to do the wrong thing with confidence."""
    assert classify_enrolment_refusal("the disk caught fire")         is RefusalCategory.INVALID_STATE


# ===========================================================================
# The wizard has to look at the string before spending it
# ===========================================================================

def test_the_code_that_actually_failed_is_named_as_incomplete():
    """28 characters, from the real attempt."""
    complaint = describe_code_shape("nps9ZFbAdBYMlwRZiFKaHr5mHZ6n")
    assert complaint is not None
    assert "28 characters" in complaint and str(CODE_LENGTH) in complaint
    assert "incomplete" in complaint


def test_a_well_formed_code_is_left_alone():
    """HQ decides what is VALID. This check only speaks up when the string
    cannot possibly be a code - refusing one HQ would have accepted is worse
    than a wasted round trip."""
    assert describe_code_shape("A" * CODE_LENGTH) is None
    assert describe_code_shape("  " + "b" * CODE_LENGTH + "  ") is None


def test_something_pasted_with_the_code_is_called_out_as_too_long():
    complaint = describe_code_shape("x" * (CODE_LENGTH + 5))
    assert "too long" in complaint


def test_characters_a_code_never_contains_are_named():
    complaint = describe_code_shape("A" * (CODE_LENGTH - 1) + "!")
    assert "never has" in complaint and "!" in complaint


def test_an_empty_box_asks_for_the_code_rather_than_complaining():
    assert describe_code_shape("") == "Enter the one-time code from HQ."
