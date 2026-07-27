"""A Receiver computer must earn its own credential, once, with a code that dies.

Today every Receiver for a Store shares one raw token stored in
``stores.receiver_token``. Two computers in the same shop present the same
secret, so the backend cannot tell them apart, and revoking one revokes both.
There is also no way for a Receiver to *obtain* a credential: somebody copies
the Store token out of the UI and pastes it into an environment variable.

The missing piece is a one-time enrolment code: something an administrator can
hand to one computer, that is useless afterwards. This module is that code's
lifecycle - opaque, stored only as a verifier, expiring, and redeemable exactly
once.

Nothing here reaches a network. Every test uses a temporary database and an
injected clock, so no test sleeps for a TTL, and no raw code or credential is
ever printed.
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from db import Base  # noqa: E402
from models import HQUser, ReceiverEnrollmentCode, Store  # noqa: E402
from receiver_enrollment_codes import (  # noqa: E402
    CODE_TTL_SECONDS,
    EnrollmentCodeError,
    EnrollmentCodeExpired,
    EnrollmentCodeInvalid,
    EnrollmentCodeUsed,
    issue_enrollment_code,
    redeem_enrollment_code,
)


PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"
BASE = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'enrol.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        db.add(HQUser(username="pilot-operator", password_hash="not-a-real-hash", role="admin"))
        db.add(Store(
            store_code="UN", store_name="Uttam Nagar Old", city="UN ZONE", region="UN ZONE",
            receiver_token="a" * 32,
        ))
        db.add(Store(
            store_code="ASR", store_name="Uttam Nagar ASR", city="UN ZONE", region="UN ZONE",
            receiver_token="b" * 32, is_active=False,
        ))
        db.commit()
    yield factory
    engine.dispose()


def _ids(factory):
    with factory() as db:
        store = db.query(Store).filter(Store.store_code == "UN").one()
        actor = db.query(HQUser).one()
        return store.id, actor.id


# ---------------------------------------------------------------------------
# The code itself
# ---------------------------------------------------------------------------
def test_a_code_is_issued_and_returned_once(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    assert issued.code
    assert issued.store_id == store_id


def test_the_raw_code_is_never_stored(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)

    with session_factory() as db:
        row = db.query(ReceiverEnrollmentCode).one()
        stored = " ".join(
            str(getattr(row, column.name)) for column in row.__table__.columns
        )
    assert issued.code not in stored, "the raw enrolment code was written to the database"


def test_the_code_is_opaque_and_encodes_nothing_about_its_store(session_factory):
    """Random base64url will contain "UN" or "1" by chance often enough that
    searching for them proves nothing. What is worth asserting is that the code
    is pure random material: a fixed-length URL-safe alphabet whose size does
    not vary with the Store or the administrator who created it."""
    import re

    with session_factory() as db:
        actor = db.query(HQUser).one()
        first_store = db.query(Store).filter(Store.store_code == "UN").one()
        db.add(Store(
            store_code="DM", store_name="Dwarka Mor", city="UN ZONE", region="UN ZONE",
            receiver_token="c" * 32,
        ))
        db.commit()
        second_store = db.query(Store).filter(Store.store_code == "DM").one()

        one = issue_enrollment_code(db, store_id=first_store.id, actor_user_id=actor.id, now=BASE)
        two = issue_enrollment_code(db, store_id=second_store.id, actor_user_id=actor.id, now=BASE)

    for issued in (one, two):
        assert re.fullmatch(r"[A-Za-z0-9_-]+", issued.code), "the code is not URL-safe base64"
        assert "pilot-operator" not in issued.code
    assert len(one.code) == len(two.code), "the code length varies with its Store"
    assert one.code != two.code


def test_codes_are_unique_and_long_enough_to_resist_guessing(session_factory):
    store_id, actor_id = _ids(session_factory)
    codes = set()
    with session_factory() as db:
        for _ in range(200):
            codes.add(issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE).code)
    assert len(codes) == 200
    assert all(len(code) >= 32 for code in codes)


def test_the_issued_object_hides_the_code_when_displayed(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    for rendering in (repr(issued), str(issued)):
        assert issued.code not in rendering


# ---------------------------------------------------------------------------
# Redemption
# ---------------------------------------------------------------------------
def test_a_valid_code_redeems_and_names_its_store(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    with session_factory() as db:
        redeemed = redeem_enrollment_code(db, issued.code, now=BASE)
    assert redeemed.store_id == store_id


def test_a_code_cannot_be_redeemed_twice(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    with session_factory() as db:
        redeem_enrollment_code(db, issued.code, now=BASE)
    with session_factory() as db:
        with pytest.raises(EnrollmentCodeUsed):
            redeem_enrollment_code(db, issued.code, now=BASE)


def test_an_expired_code_is_refused(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    with session_factory() as db:
        with pytest.raises(EnrollmentCodeExpired):
            redeem_enrollment_code(db, issued.code, now=BASE + timedelta(seconds=CODE_TTL_SECONDS + 1))


def test_a_code_is_still_valid_at_the_edge_of_its_window(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    with session_factory() as db:
        edge = BASE + timedelta(seconds=CODE_TTL_SECONDS - 1)
        assert redeem_enrollment_code(db, issued.code, now=edge).store_id == store_id


@pytest.mark.parametrize("bogus", ["", "   ", "not-a-real-code", "x" * 200, None])
def test_an_invented_code_is_refused(session_factory, bogus):
    with session_factory() as db:
        with pytest.raises(EnrollmentCodeInvalid):
            redeem_enrollment_code(db, bogus, now=BASE)


def test_the_ttl_is_short_enough_to_be_worth_little_if_overheard(session_factory):
    """It is handed to one computer during setup, not mailed out."""
    assert 0 < CODE_TTL_SECONDS <= 3600


# ---------------------------------------------------------------------------
# Store state
# ---------------------------------------------------------------------------
def test_an_inactive_store_cannot_have_a_code_issued(session_factory):
    with session_factory() as db:
        inactive = db.query(Store).filter(Store.store_code == "ASR").one()
        actor = db.query(HQUser).one()
        with pytest.raises(EnrollmentCodeError):
            issue_enrollment_code(db, store_id=inactive.id, actor_user_id=actor.id, now=BASE)


def test_an_unknown_store_is_refused(session_factory):
    with session_factory() as db:
        actor = db.query(HQUser).one()
        with pytest.raises(EnrollmentCodeError):
            issue_enrollment_code(db, store_id=999999, actor_user_id=actor.id, now=BASE)


def test_a_code_for_a_store_disabled_after_issue_is_refused(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    with session_factory() as db:
        db.query(Store).filter(Store.id == store_id).one().is_active = False
        db.commit()
    with session_factory() as db:
        with pytest.raises(EnrollmentCodeError):
            redeem_enrollment_code(db, issued.code, now=BASE)


# ---------------------------------------------------------------------------
# Two Receivers for one Store are two separate things
# ---------------------------------------------------------------------------
def test_one_store_can_have_two_outstanding_codes(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        first = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
        second = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    assert first.code != second.code

    with session_factory() as db:
        assert redeem_enrollment_code(db, first.code, now=BASE).store_id == store_id
        assert redeem_enrollment_code(db, second.code, now=BASE).store_id == store_id


def test_redeeming_one_code_does_not_consume_another(session_factory):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        first = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
        second = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    with session_factory() as db:
        redeem_enrollment_code(db, first.code, now=BASE)
    with session_factory() as db:
        assert redeem_enrollment_code(db, second.code, now=BASE).store_id == store_id


# ---------------------------------------------------------------------------
# Concurrency: exactly one winner
# ---------------------------------------------------------------------------
def test_concurrent_redemption_has_exactly_one_winner(session_factory):
    """Two Receiver computers racing with the same code must not both enrol."""
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)

    winners: list[int] = []
    losers: list[str] = []
    barrier = threading.Barrier(8)

    def attempt():
        barrier.wait()
        try:
            with session_factory() as db:
                winners.append(redeem_enrollment_code(db, issued.code, now=BASE).store_id)
        except EnrollmentCodeError as refusal:
            losers.append(type(refusal).__name__)
        except Exception as unexpected:  # surfaced rather than swallowed
            losers.append(f"UNEXPECTED:{type(unexpected).__name__}")

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1, f"{len(winners)} threads redeemed the same code"
    assert len(losers) == 7
    assert not [entry for entry in losers if entry.startswith("UNEXPECTED")], losers


# ---------------------------------------------------------------------------
# Nothing leaks
# ---------------------------------------------------------------------------
def test_a_refusal_never_echoes_the_supplied_code(session_factory):
    with session_factory() as db:
        try:
            redeem_enrollment_code(db, "a-distinctive-invented-code", now=BASE)
        except EnrollmentCodeError as refusal:
            assert "a-distinctive-invented-code" not in str(refusal)
        else:
            pytest.fail("an invented code was accepted")


def test_issuing_and_redeeming_print_nothing(session_factory, capsys):
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issued = issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    with session_factory() as db:
        redeem_enrollment_code(db, issued.code, now=BASE)

    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert issued.code not in stream


def test_expired_and_used_codes_are_reported_distinctly_to_the_operator(session_factory):
    """An administrator debugging a setup needs to know which happened. The
    caller decides what reaches the wire; both are subclasses of one error so a
    handler cannot forget one."""
    assert issubclass(EnrollmentCodeUsed, EnrollmentCodeError)
    assert issubclass(EnrollmentCodeExpired, EnrollmentCodeError)
    assert issubclass(EnrollmentCodeInvalid, EnrollmentCodeError)


# ---------------------------------------------------------------------------
# The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_untouched(session_factory):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    store_id, actor_id = _ids(session_factory)
    with session_factory() as db:
        issue_enrollment_code(db, store_id=store_id, actor_user_id=actor_id, now=BASE)
    assert metadata() == before
    for sidecar in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + sidecar).exists()
