"""Why an enrolment failed - recorded for the operator, hidden from the caller.

THE TENSION, AND HOW IT IS RESOLVED

An operator standing at a Store needs to know whether the code was unknown,
expired, already used, or refused because the Store is archived. Those need
four different actions, and "That enrolment code cannot be used" needs a phone
call to work out which.

But the enrolment endpoint is **unauthenticated** - a Receiver has no credential
yet, which is the whole point - and it is the one endpoint worth guessing at.
Telling the caller *which* refusal applied turns it into an oracle: "expired"
means the code existed, "already used" means somebody enrolled with it,
"unknown" means keep guessing.

So the category is written to the audit log, where an authenticated operator can
read it, and the wire response stays a single generic sentence. The person who
needs the diagnosis is signed in; the person probing is not.

Nothing here opens a socket or touches the protected database.
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

from enrolment_refusal import (  # noqa: E402
    REFUSAL_CATEGORIES,
    RefusalCategory,
    classify_enrolment_refusal,
    describe_for_operator,
)


A_REAL_LOOKING_CODE = "ECHO-4H7K-9QW2"


# ===========================================================================
# The categories that exist
# ===========================================================================
def test_every_category_the_brief_names_exists():
    for name in ("UNKNOWN_TOKEN", "EXPIRED_TOKEN", "ALREADY_USED",
                 "STORE_DISABLED", "STORE_ARCHIVED", "INVALID_STATE"):
        assert name in REFUSAL_CATEGORIES


def test_there_is_a_fallback_for_anything_unclassified():
    """A refusal nobody anticipated must still be recorded as *something*,
    rather than logged as one of the specific ones by accident."""
    assert RefusalCategory.INVALID_STATE.value in REFUSAL_CATEGORIES


# ===========================================================================
# Classification
# ===========================================================================
@pytest.mark.parametrize("message,expected", [
    ("no such enrolment code", RefusalCategory.UNKNOWN_TOKEN),
    ("that code has expired", RefusalCategory.EXPIRED_TOKEN),
    ("this code was already redeemed", RefusalCategory.ALREADY_USED),
    ("the Store is disabled", RefusalCategory.STORE_DISABLED),
    ("that Store is archived", RefusalCategory.STORE_ARCHIVED),
])
def test_a_refusal_is_classified_from_what_the_service_said(message, expected):
    assert classify_enrolment_refusal(message) is expected


def test_an_unrecognised_refusal_is_invalid_state_not_a_guess():
    assert classify_enrolment_refusal("something nobody wrote down") is \
        RefusalCategory.INVALID_STATE


def test_classification_is_case_insensitive():
    assert classify_enrolment_refusal("THAT CODE HAS EXPIRED") is \
        RefusalCategory.EXPIRED_TOKEN


def test_classification_never_returns_none():
    for message in ("", None, 42, "   "):
        assert classify_enrolment_refusal(message) is RefusalCategory.INVALID_STATE


# ===========================================================================
# Nothing secret is ever carried
# ===========================================================================
def test_a_category_never_contains_the_code():
    category = classify_enrolment_refusal(
        f"no such enrolment code {A_REAL_LOOKING_CODE}")
    assert A_REAL_LOOKING_CODE not in category.value


def test_the_operator_description_never_contains_a_code():
    for category in RefusalCategory:
        text = describe_for_operator(category)
        assert A_REAL_LOOKING_CODE not in text
        assert "ECHO-" not in text


def test_every_category_has_a_description_a_beginner_can_act_on():
    for category in RefusalCategory:
        text = describe_for_operator(category)
        assert len(text) > 20, f"{category} has no usable description"
        # Each one has to say what to DO, not just what went wrong.
        assert any(word in text.lower() for word in
                   ("issue", "generate", "enable", "restore", "check", "new"))


# ===========================================================================
# What reaches the wire, and what reaches the log
# ===========================================================================
SERVER_SOURCE = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")


def test_the_endpoint_logs_the_category():
    """The operator's half of the bargain."""
    assert "classify_enrolment_refusal" in SERVER_SOURCE
    assert "enrollment_code_rejected category=" in SERVER_SOURCE


def _refusal_handler_lines():
    """The `except EnrollmentRefused` block that raises ENROLMENT_REFUSED.

    Located by walking the AST rather than by slicing text between two phrases.
    The first version sliced from the first occurrence of the phrase to the next
    occurrence of another - which landed on a different handler entirely and
    failed against code that was correct.
    """
    import ast

    tree = ast.parse(SERVER_SOURCE)
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        name = getattr(node.type, "id", None) or getattr(node.type, "attr", None)
        if name != "EnrollmentRefused":
            continue
        block = ast.get_source_segment(SERVER_SOURCE, node) or ""
        if "ENROLMENT_REFUSED" in block:
            return block
    raise AssertionError("no EnrollmentRefused handler raising ENROLMENT_REFUSED")


def test_the_wire_response_is_still_one_generic_sentence():
    """The attacker's half.

    The endpoint is unauthenticated and rate-limited, and it is the one worth
    guessing at. Telling the caller which refusal applied would turn it into an
    oracle: 'expired' means the code existed, 'already used' means somebody
    enrolled with it, 'unknown' means keep going.
    """
    import re

    block = _refusal_handler_lines()
    raised = re.search(r"detail=([A-Za-z_]+)", block)
    assert raised, "the refusal no longer raises with a named detail"
    assert raised.group(1) == "ENROLMENT_REFUSED", (
        "the category must not reach the unauthenticated caller")


def test_no_category_name_reaches_the_http_detail():
    block = _refusal_handler_lines()
    detail = block.split("detail=", 1)[1] if "detail=" in block else ""
    for name in REFUSAL_CATEGORIES:
        assert name not in detail, f"{name} would be returned to the caller"


def test_the_raw_code_is_never_written_to_the_log():
    """The log line names a category and nothing else about the attempt."""
    block = _refusal_handler_lines()
    assert "payload.code" not in block
    assert "code=payload" not in block
