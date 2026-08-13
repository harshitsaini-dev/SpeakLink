"""Why an enrolment was refused - recorded for the operator, hidden from the caller.

THE TENSION THIS RESOLVES

An operator standing at a Store needs to know whether the code was unknown,
expired, already used, or refused because the Store is archived. Those are four
different actions, and one generic sentence needs a phone call to tell apart.

But ``POST /api/receiver-devices/enroll`` is **unauthenticated** - a Receiver has
no credential yet, which is the entire point - and it is therefore the one
endpoint worth guessing at. Telling the caller *which* refusal applied turns it
into an oracle: "expired" means the code existed, "already used" means somebody
enrolled with it, "unknown" means keep going.

So the category goes to the audit log, where a signed-in operator reads it, and
the wire response stays a single generic sentence. The person who needs the
diagnosis is authenticated; the person probing is not.

No value here ever carries a code, a credential or any part of one.
"""

from __future__ import annotations

from enum import Enum


class RefusalCategory(str, Enum):
    UNKNOWN_TOKEN = "UNKNOWN_TOKEN"
    EXPIRED_TOKEN = "EXPIRED_TOKEN"
    ALREADY_USED = "ALREADY_USED"
    STORE_DISABLED = "STORE_DISABLED"
    STORE_ARCHIVED = "STORE_ARCHIVED"
    #: Anything unrecognised. Deliberately its own value rather than being
    #: folded into one of the specific ones - a refusal nobody anticipated must
    #: not be logged as a diagnosis somebody will act on.
    INVALID_STATE = "INVALID_STATE"


REFUSAL_CATEGORIES = tuple(category.value for category in RefusalCategory)

#: Matched against what the enrolment service said, in order. First hit wins, so
#: the more specific phrases come first.
_PATTERNS = (
    ("archiv", RefusalCategory.STORE_ARCHIVED),
    ("disabl", RefusalCategory.STORE_DISABLED),
    ("expire", RefusalCategory.EXPIRED_TOKEN),
    ("already", RefusalCategory.ALREADY_USED),
    ("redeem", RefusalCategory.ALREADY_USED),
    ("used", RefusalCategory.ALREADY_USED),
    ("no such", RefusalCategory.UNKNOWN_TOKEN),
    ("unknown", RefusalCategory.UNKNOWN_TOKEN),
    ("not found", RefusalCategory.UNKNOWN_TOKEN),
    # The phrase the code service ACTUALLY uses, which was missing - so the
    # single most common refusal, a code that does not match anything, was
    # logged as "could not classify". Four hours of a real enrolment were spent
    # reading INVALID_STATE and looking for an exotic fault.
    ("not recognised", RefusalCategory.UNKNOWN_TOKEN),
    ("not recognized", RefusalCategory.UNKNOWN_TOKEN),
    ("no enrolment code was presented", RefusalCategory.UNKNOWN_TOKEN),
    # And the Store guard's own wording, for the same reason.
    ("not available for enrolment", RefusalCategory.STORE_DISABLED),
)


def classify_enrolment_refusal(message: object) -> RefusalCategory:
    """Turn a refusal into a category. Never raises, never returns None.

    A refusal that cannot be classified is ``INVALID_STATE`` rather than a
    guess: recording it as "expired" because that was the nearest match would
    send an operator to do the wrong thing with confidence.
    """
    if not isinstance(message, str):
        return RefusalCategory.INVALID_STATE
    lowered = message.strip().lower()
    if not lowered:
        return RefusalCategory.INVALID_STATE
    for fragment, category in _PATTERNS:
        if fragment in lowered:
            return category
    return RefusalCategory.INVALID_STATE


_DESCRIPTIONS = {
    RefusalCategory.UNKNOWN_TOKEN:
        "That code does not exist. Generate a new enrolment code for this Store "
        "and read it out again - codes are shown once and are easy to mistype.",
    RefusalCategory.EXPIRED_TOKEN:
        "That code was valid but has run out of time. Generate a new one and use "
        "it straight away.",
    RefusalCategory.ALREADY_USED:
        "That code has already enrolled a computer. Each code works exactly once. "
        "Generate a new one for this Store.",
    RefusalCategory.STORE_DISABLED:
        "That Store is disabled, so it cannot take on a new Receiver. Enable the "
        "Store first, then generate a code.",
    RefusalCategory.STORE_ARCHIVED:
        "That Store is archived. Restore it first if it is coming back into use, "
        "then generate a new code.",
    RefusalCategory.INVALID_STATE:
        "The enrolment was refused for a reason SpeakLink could not classify. "
        "Check the Store is active and generate a new code; if it happens again, "
        "the System Logs hold the full sequence.",
}


def describe_for_operator(category: RefusalCategory) -> str:
    """A sentence a beginner can act on. Carries no code and no credential."""
    return _DESCRIPTIONS[category]


def describe_outstanding_codes(db) -> str:
    """How many codes were usable when a presented one matched nothing.

    Written for the case that actually happened: a code was generated, a code
    was typed in forty seconds later, and HQ said it matched nothing. From the
    log alone that is indistinguishable from "somebody is guessing at codes" -
    and the difference decides whether the operator generates another code or
    checks how they are copying it.

    Counts and ages only. No code, no part of a code, no hash.
    """
    import time

    from models import ReceiverEnrollmentCode

    now = time.time()
    rows = (db.query(ReceiverEnrollmentCode)
            .filter(ReceiverEnrollmentCode.redeemed_at_epoch.is_(None))
            .filter(ReceiverEnrollmentCode.expires_at_epoch > now)
            .all())
    if not rows:
        return "outstanding_codes=0"
    newest = max(rows, key=lambda row: row.expires_at_epoch)
    age = max(0, int(now - (newest.expires_at_epoch - CODE_TTL_HINT_SECONDS)))
    return (f"outstanding_codes={len(rows)} "
            f"newest_issued_seconds_ago={age} "
            f"newest_store_id={newest.store_id}")


#: Only used to turn an expiry back into an issue time for the log line above.
#: Kept here rather than imported so this module stays free of the code
#: service, which imports the classifier.
CODE_TTL_HINT_SECONDS = 15 * 60
