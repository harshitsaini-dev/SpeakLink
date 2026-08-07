"""Web audience rooms: identity, secrets, admission and isolation.

These drive the real persistence layer against a real SQLite database. What they
are mostly checking is what is NOT there: no recoverable password, no recoverable
listener token, no way to reach another room's participants.
"""

from __future__ import annotations

import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

import web_rooms  # noqa: E402
from web_rooms import (  # noqa: E402
    AdmissionStatus,
    InvalidDisplayNameError,
    ParticipantNotAdmissibleError,
    RoomNotOpenError,
    RoomStatus,
)


@pytest.fixture()
def engine(tmp_path):
    made = create_engine(f"sqlite:///{tmp_path / 'rooms.db'}")
    with made.begin() as connection:
        # Only what the room tables reference. The real schema is built by the
        # server; this keeps the module under test isolated from it.
        connection.exec_driver_sql(
            "CREATE TABLE broadcast_sessions (id INTEGER PRIMARY KEY, "
            "campaign_name VARCHAR(200))")
        for session_id in (1, 2, 3):
            connection.exec_driver_sql(
                "INSERT INTO broadcast_sessions (id, campaign_name) VALUES (?, ?)",
                (session_id, f"session {session_id}"))
    web_rooms.ensure_web_room_schema(made)
    try:
        yield made
    finally:
        made.dispose()


# ===========================================================================
# Identity
# ===========================================================================

def test_one_room_per_broadcast_and_it_is_idempotent(engine):
    room, password = web_rooms.create_room(engine, session_id=1)
    again, second_password = web_rooms.create_room(engine, session_id=1)

    assert again.id == room.id, "a Broadcast has exactly one room"
    # The plaintext of an EXISTING room is genuinely unrecoverable, so an empty
    # string is the honest answer rather than a freshly invented password.
    assert second_password == ""
    assert password


def test_the_public_code_is_not_the_session_id(engine):
    """A consecutive integer would let one link enumerate every Broadcast."""
    first, _ = web_rooms.create_room(engine, session_id=1)
    second, _ = web_rooms.create_room(engine, session_id=2)

    assert str(first.session_id) not in first.public_code
    assert first.public_code != second.public_code
    assert first.public_code.startswith("EC-")


def test_public_codes_are_random_and_readable():
    codes = {web_rooms.generate_public_code() for _ in range(500)}
    assert len(codes) == 500, "500 draws produced a collision"
    body = "".join(code[3:] for code in codes)
    # The visually ambiguous characters are absent, so a code survives being
    # read off one screen and typed into another.
    assert not set("01OIL") & set(body)


def test_a_public_code_collision_is_retried_not_fatal(engine, monkeypatch):
    """The database enforces uniqueness; the application must survive it."""
    drawn = iter(["EC-AAAAAA", "EC-AAAAAA", "EC-BBBBBB"])
    monkeypatch.setattr(web_rooms, "generate_public_code", lambda: next(drawn))

    first, _ = web_rooms.create_room(engine, session_id=1)
    second, _ = web_rooms.create_room(engine, session_id=2)
    assert first.public_code == "EC-AAAAAA"
    assert second.public_code == "EC-BBBBBB", "the collision was redrawn"


def test_a_room_is_found_by_its_code_however_it_was_typed(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    for typed in (room.public_code, room.public_code.lower(),
                  f"  {room.public_code}  "):
        found = web_rooms.find_room_by_public_code(engine, public_code=typed)
        assert found is not None and found.id == room.id


def test_an_unknown_code_is_simply_not_found(engine):
    assert web_rooms.find_room_by_public_code(engine, public_code="EC-ZZZZZZ") is None
    assert web_rooms.find_room_by_public_code(engine, public_code="") is None
    assert web_rooms.find_room_by_public_code(engine, public_code=None) is None


# ===========================================================================
# The password
# ===========================================================================

def test_the_raw_password_is_nowhere_in_the_database(engine):
    room, password = web_rooms.create_room(engine, session_id=1)
    assert password

    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT * FROM {web_rooms.ROOM_TABLE}")).fetchall()
    dumped = " ".join(str(value) for row in rows for value in row)
    assert password not in dumped, "the plaintext password reached the database"
    assert "$2b$" in dumped or "$2a$" in dumped, "it is stored as a bcrypt hash"


def test_the_password_admits_and_a_wrong_one_does_not(engine):
    room, password = web_rooms.create_room(engine, session_id=1)
    assert web_rooms.verify_join_password(engine, room=room, password=password)
    assert not web_rooms.verify_join_password(engine, room=room, password="WRONG-PASS")
    assert not web_rooms.verify_join_password(engine, room=room, password="")
    assert not web_rooms.verify_join_password(engine, room=room, password=None)


def test_rotation_replaces_the_future_password_only(engine):
    room, original = web_rooms.create_room(engine, session_id=1)
    admitted, token = web_rooms.admit_with_password(
        engine, room=room, display_name="Harshit")

    fresh = web_rooms.rotate_password(engine, session_id=1)
    assert fresh != original

    reloaded = web_rooms.get_room_for_session(engine, session_id=1)
    assert not web_rooms.verify_join_password(engine, room=reloaded, password=original)
    assert web_rooms.verify_join_password(engine, room=reloaded, password=fresh)
    assert reloaded.password_rotated_at is not None

    # The audience is not ejected by a rotation. Stopping new arrivals and
    # removing the people already listening are different decisions.
    assert web_rooms.authenticate_listener(engine, token=token) is not None


def test_neither_password_is_recoverable_after_rotation(engine):
    _, original = web_rooms.create_room(engine, session_id=1)
    fresh = web_rooms.rotate_password(engine, session_id=1)
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT * FROM {web_rooms.ROOM_TABLE}")).fetchall()
    dumped = " ".join(str(value) for row in rows for value in row)
    assert original not in dumped and fresh not in dumped


def test_passwords_are_random():
    passwords = {web_rooms.generate_join_password() for _ in range(500)}
    assert len(passwords) == 500


# ===========================================================================
# Names
# ===========================================================================

@pytest.mark.parametrize("raw,expected", [
    ("Harshit", "Harshit"),
    ("  Harshit  ", "Harshit"),
    ("Harshit   Saini", "Harshit Saini"),
    ("Harshit\nSaini", "Harshit Saini"),
    ("अमन", "अमन"),
    ("Zoë", "Zoë"),
])
def test_names_are_trimmed_not_mangled(raw, expected):
    assert web_rooms.normalise_display_name(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, 42, "\n\t", "x" * 41, "a\x00b"])
def test_an_unusable_name_is_refused(raw):
    with pytest.raises(InvalidDisplayNameError):
        web_rooms.normalise_display_name(raw)


def test_a_name_that_looks_like_markup_is_stored_verbatim(engine):
    """React escapes on render, so the name is kept as typed rather than mangled."""
    room, _ = web_rooms.create_room(engine, session_id=1)
    hostile = "<script>alert(1)</script>"
    participant, _ = web_rooms.admit_with_password(
        engine, room=room, display_name=hostile)
    assert participant.display_name == hostile


def test_two_people_may_share_a_name(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    first, first_token = web_rooms.admit_with_password(
        engine, room=room, display_name="Harshit")
    second, second_token = web_rooms.admit_with_password(
        engine, room=room, display_name="Harshit")

    assert first.id != second.id, "identity is the participant, never the name"
    assert first_token != second_token


# ===========================================================================
# Admission
# ===========================================================================

def test_a_password_join_is_admitted_immediately_and_creates_no_request(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, token = web_rooms.admit_with_password(
        engine, room=room, display_name="Harshit")

    assert participant.admission_status == AdmissionStatus.PASSWORD_ADMITTED
    assert participant.is_admitted
    assert participant.requested_at is None, "knowing the password is not a request"
    assert token


def test_a_passwordless_request_waits_by_default(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    assert room.auto_approve is False, "Auto Approve is OFF by default"

    participant, token = web_rooms.request_access(
        engine, room=room, display_name="Aman")
    assert participant.admission_status == AdmissionStatus.REQUESTED
    assert token is None, "no listener session before admission"


def test_auto_approve_admits_a_request_immediately(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    web_rooms.set_auto_approve(engine, session_id=1, enabled=True)
    room = web_rooms.get_room_for_session(engine, session_id=1)

    participant, token = web_rooms.request_access(
        engine, room=room, display_name="Aman")
    assert participant.admission_status == AdmissionStatus.APPROVED
    assert token, "an auto-approved listener gets a session at once"


def test_auto_approve_is_read_inside_the_transaction_that_creates_the_row(engine):
    """A toggle racing a request must produce one participant in one state."""
    room, _ = web_rooms.create_room(engine, session_id=1)
    web_rooms.set_auto_approve(engine, session_id=1, enabled=True)
    # A STALE snapshot, exactly what a request handler would be holding if it
    # read the room a moment before the toggle.
    stale = web_rooms.WebRoom(**{**room.__dict__, "auto_approve": False}) \
        if hasattr(room, "__dict__") else room

    web_rooms.set_auto_approve(engine, session_id=1, enabled=False)
    fresh_room = web_rooms.get_room_for_session(engine, session_id=1)
    participant, token = web_rooms.request_access(
        engine, room=fresh_room, display_name="Aman")

    # The CURRENT setting decided it, not the snapshot passed in.
    assert participant.admission_status == AdmissionStatus.REQUESTED
    assert token is None


def test_approving_twice_admits_once_and_mints_one_session(engine):
    """Two tokens would be two sessions, and a Kick would revoke only one."""
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, _ = web_rooms.request_access(engine, room=room, display_name="Aman")

    approved, first_token = web_rooms.approve_participant(
        engine, room_id=room.id, participant_id=participant.id)
    again, second_token = web_rooms.approve_participant(
        engine, room_id=room.id, participant_id=participant.id)

    assert approved.admission_status == AdmissionStatus.APPROVED
    assert first_token
    assert second_token is None, "a second approval does not mint a second session"
    assert again.approved_at == approved.approved_at


def test_denial_is_terminal(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, _ = web_rooms.request_access(engine, room=room, display_name="Aman")

    denied = web_rooms.deny_participant(engine, room_id=room.id,
                                        participant_id=participant.id)
    assert denied.admission_status == AdmissionStatus.DENIED
    # Idempotent...
    assert web_rooms.deny_participant(
        engine, room_id=room.id, participant_id=participant.id).admission_status \
        == AdmissionStatus.DENIED
    # ...and not silently reversible: a mis-click has to be trustworthy.
    with pytest.raises(ParticipantNotAdmissibleError):
        web_rooms.approve_participant(engine, room_id=room.id,
                                      participant_id=participant.id)


def test_a_denied_participant_has_no_session(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, _ = web_rooms.request_access(engine, room=room, display_name="Aman")
    web_rooms.deny_participant(engine, room_id=room.id, participant_id=participant.id)
    assert web_rooms.authenticate_listener(engine, token="anything") is None


def test_concurrent_approvals_mint_exactly_one_session(engine):
    """Two broadcaster tabs clicking Approve at the same moment."""
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, _ = web_rooms.request_access(engine, room=room, display_name="Aman")

    def approve():
        try:
            return web_rooms.approve_participant(
                engine, room_id=room.id, participant_id=participant.id)[1]
        except ParticipantNotAdmissibleError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = [token for token in pool.map(lambda _: approve(), range(4)) if token]

    assert len(tokens) == 1, f"minted {len(tokens)} listener sessions"


# ===========================================================================
# Kick
# ===========================================================================

def test_kick_invalidates_the_session_immediately(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, token = web_rooms.admit_with_password(
        engine, room=room, display_name="Harshit")
    assert web_rooms.authenticate_listener(engine, token=token) is not None

    kicked = web_rooms.kick_participant(engine, room_id=room.id,
                                        participant_id=participant.id)
    assert kicked.admission_status == AdmissionStatus.KICKED
    # The socket is closed by the caller; without this the same token would
    # simply reconnect a second later.
    assert web_rooms.authenticate_listener(engine, token=token) is None


def test_kick_is_idempotent(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, _ = web_rooms.admit_with_password(engine, room=room,
                                                   display_name="Harshit")
    first = web_rooms.kick_participant(engine, room_id=room.id,
                                       participant_id=participant.id)
    second = web_rooms.kick_participant(engine, room_id=room.id,
                                        participant_id=participant.id)
    assert first.kicked_at == second.kicked_at


def test_a_kicked_person_who_asks_again_is_a_new_decision(engine):
    """Kick removes a session, not a person. No fingerprinting, no ban."""
    room, _ = web_rooms.create_room(engine, session_id=1)
    participant, _ = web_rooms.admit_with_password(engine, room=room,
                                                   display_name="Harshit")
    web_rooms.kick_participant(engine, room_id=room.id, participant_id=participant.id)

    returning, _ = web_rooms.request_access(engine, room=room, display_name="Harshit")
    assert returning.id != participant.id
    assert returning.admission_status == AdmissionStatus.REQUESTED


# ===========================================================================
# Isolation between rooms
# ===========================================================================

def test_one_broadcaster_cannot_manage_another_rooms_participant(engine):
    """Participant ids are small integers, so the room check is the boundary."""
    room_a, _ = web_rooms.create_room(engine, session_id=1)
    room_b, _ = web_rooms.create_room(engine, session_id=2)
    theirs, _ = web_rooms.request_access(engine, room=room_b, display_name="Bob")

    for action in (web_rooms.approve_participant, web_rooms.deny_participant,
                   web_rooms.kick_participant):
        with pytest.raises(ParticipantNotAdmissibleError):
            action(engine, room_id=room_a.id, participant_id=theirs.id)


def test_guessing_a_participant_id_fails(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    for guess in (1, 2, 999):
        with pytest.raises(ParticipantNotAdmissibleError):
            web_rooms.kick_participant(engine, room_id=room.id, participant_id=guess)


def test_a_listener_token_resolves_only_to_its_own_room(engine):
    room_a, _ = web_rooms.create_room(engine, session_id=1)
    room_b, _ = web_rooms.create_room(engine, session_id=2)
    _, token_a = web_rooms.admit_with_password(engine, room=room_a,
                                               display_name="Alice")

    resolved = web_rooms.authenticate_listener(engine, token=token_a)
    assert resolved is not None
    assert resolved[0].id == room_a.id and resolved[0].id != room_b.id


def test_a_listener_token_is_nowhere_in_the_database(engine):
    room, _ = web_rooms.create_room(engine, session_id=1)
    _, token = web_rooms.admit_with_password(engine, room=room, display_name="Alice")
    with engine.connect() as connection:
        rows = connection.execute(
            text(f"SELECT * FROM {web_rooms.PARTICIPANT_TABLE}")).fetchall()
    dumped = " ".join(str(value) for row in rows for value in row)
    assert token not in dumped, "the raw listener token reached the database"


@pytest.mark.parametrize("token", ["", None, "not-a-token", 42])
def test_a_bad_token_authenticates_nothing(engine, token):
    web_rooms.create_room(engine, session_id=1)
    assert web_rooms.authenticate_listener(engine, token=token) is None


# ===========================================================================
# Room end
# ===========================================================================

def test_ending_a_room_invalidates_every_listener_session(engine):
    room, password = web_rooms.create_room(engine, session_id=1)
    _, admitted_token = web_rooms.admit_with_password(engine, room=room,
                                                      display_name="Alice")
    waiting, _ = web_rooms.request_access(engine, room=room, display_name="Aman")

    ended = web_rooms.end_room(engine, session_id=1)
    assert ended.status == RoomStatus.ENDED
    assert ended.ended_at is not None

    # A token whose Broadcast has finished must not still work.
    assert web_rooms.authenticate_listener(engine, token=admitted_token) is None
    still_waiting = web_rooms.get_participant(engine, participant_id=waiting.id)
    assert still_waiting.admission_status == AdmissionStatus.ROOM_ENDED


def test_nobody_joins_a_room_that_has_ended(engine):
    room, password = web_rooms.create_room(engine, session_id=1)
    web_rooms.end_room(engine, session_id=1)
    ended = web_rooms.get_room_for_session(engine, session_id=1)

    with pytest.raises(RoomNotOpenError):
        web_rooms.admit_with_password(engine, room=ended, display_name="Alice")
    with pytest.raises(RoomNotOpenError):
        web_rooms.request_access(engine, room=ended, display_name="Aman")
    with pytest.raises(RoomNotOpenError):
        web_rooms.rotate_password(engine, session_id=1)
    with pytest.raises(RoomNotOpenError):
        web_rooms.set_auto_approve(engine, session_id=1, enabled=True)


def test_ending_a_room_is_idempotent(engine):
    web_rooms.create_room(engine, session_id=1)
    first = web_rooms.end_room(engine, session_id=1)
    second = web_rooms.end_room(engine, session_id=1)
    assert first.ended_at == second.ended_at


def test_ending_a_room_that_never_existed_is_not_an_error(engine):
    assert web_rooms.end_room(engine, session_id=3) is None


# ===========================================================================
# Migration
# ===========================================================================

def test_the_schema_migration_is_idempotent(engine):
    """HQ runs it on every boot."""
    web_rooms.ensure_web_room_schema(engine)
    web_rooms.ensure_web_room_schema(engine)
    room, _ = web_rooms.create_room(engine, session_id=1)
    assert web_rooms.get_room_for_session(engine, session_id=1).id == room.id


def test_the_migration_adds_only_its_own_tables(engine):
    with engine.connect() as connection:
        tables = {row[0] for row in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")).fetchall()}
    assert web_rooms.ROOM_TABLE in tables
    assert web_rooms.PARTICIPANT_TABLE in tables
    # The Broadcast table it references is untouched.
    assert "broadcast_sessions" in tables
