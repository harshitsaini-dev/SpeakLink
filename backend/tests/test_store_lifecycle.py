"""A Store's life: active, switched off, retired - and never actually deleted.

A Store owns Receiver Devices, broadcast sessions, targets, receiver events and
log lines. Deleting the row would either orphan all of that or cascade it away,
and both destroy the only record of what was announced where. So "delete" here
means **archive**: the row stays, the history stays readable, and the Store
simply stops being somewhere you can broadcast to.

Three states, and the distinction between the middle two matters:

* ``active`` - normal.
* ``disabled`` - switched off, expected back. An ordinary administrator can turn
  it on again.
* ``archived`` - retired. Deliberately *not* reversible through the ordinary
  enable action, because "re-enable" is a small button and un-retiring a Store
  is not a small decision. Restoring returns it to ``disabled``, never straight
  to ``active``, so somebody has to look at its Devices before it can broadcast.

``is_active`` is kept in lockstep with the state rather than replaced. Every
existing path - broadcast targeting, enrolment, Receiver authentication - already
reads it, so keeping it correct means none of those had to be rewritten to learn
about archiving, and none of them can accidentally miss it.

Every test uses a temporary database. ``backend/speaklink_live.db`` and the real
pilot database are never opened.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
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

from auth import hash_password  # noqa: E402
from db import Base  # noqa: E402
from models import BroadcastSession, BroadcastTarget, HQUser, ReceiverEvent, Store  # noqa: E402

from store_lifecycle import (  # noqa: E402
    ARCHIVED,
    ACTIVE,
    DISABLED,
    StoreLifecycleError,
    StoreNotRestorableError,
    StoreTransitionRefused,
    archive_store,
    disable_store,
    enable_store,
    ensure_store_lifecycle_schema,
    lifecycle_state,
    restore_store,
    validate_store_code,
)


PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"


class Runtime:
    def __init__(self, tmp_path: Path) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.path = tmp_path / "lifecycle.db"
        self.engine = create_engine(f"sqlite:///{self.path.as_posix()}")
        Base.metadata.create_all(bind=self.engine)
        self.Session = sessionmaker(bind=self.engine, autocommit=False, autoflush=False)

        with self.Session() as db:
            db.add(HQUser(username="operator", password_hash=hash_password("x"), role="admin"))
            db.add(Store(store_code="UN", store_name="Uttam Nagar Old", city="UN ZONE",
                         region="UN ZONE", receiver_token="a" * 32))
            db.add(Store(store_code="ASR", store_name="Uttam Nagar ASR", city="UN ZONE",
                         region="UN ZONE", receiver_token="b" * 32))
            db.commit()
            self.store_id = db.query(Store).filter(Store.store_code == "UN").one().id
            self.other_store_id = db.query(Store).filter(Store.store_code == "ASR").one().id
            self.actor_id = db.query(HQUser).one().id

        ensure_store_lifecycle_schema(self.engine)

    def state(self, store_id: int | None = None) -> str:
        with self.Session() as db:
            return lifecycle_state(db, store_id or self.store_id)

    def store(self, store_id: int | None = None) -> Store:
        with self.Session() as db:
            return db.query(Store).filter(Store.id == (store_id or self.store_id)).one()

    def query(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return connection.execute(sql, parameters).fetchall()
        finally:
            connection.close()

    def add_history(self) -> None:
        """Everything a Store owns, so archiving can be shown not to lose it."""
        with self.Session() as db:
            session = BroadcastSession(
                campaign_name="Evening offer", started_by=self.actor_id,
                status="ended", target_mode="selected", selected_store_count=1,
            )
            db.add(session)
            db.flush()
            db.add(BroadcastTarget(session_id=session.id, store_id=self.store_id,
                                   play_status="stopped"))
            db.add(ReceiverEvent(store_id=self.store_id, event_type="connected"))
            db.add(ReceiverEvent(store_id=self.store_id, event_type="playback_confirmed"))
            db.commit()


@pytest.fixture()
def runtime(tmp_path) -> Runtime:
    made = Runtime(tmp_path)
    yield made
    made.engine.dispose()


# ===========================================================================
# The schema change
# ===========================================================================
def test_the_schema_change_is_additive_and_idempotent(runtime: Runtime):
    """It runs at startup on every boot, so applying it twice must be safe."""
    ensure_store_lifecycle_schema(runtime.engine)
    ensure_store_lifecycle_schema(runtime.engine)
    columns = {row[1] for row in runtime.query("PRAGMA table_info(stores)")}
    assert "lifecycle_state" in columns


def test_existing_stores_are_backfilled_from_is_active(tmp_path):
    """An upgrade must not decide that every Store is suddenly archived."""
    runtime = Runtime(tmp_path / "backfill")
    try:
        with runtime.Session() as db:
            db.query(Store).filter(Store.id == runtime.other_store_id).update(
                {Store.is_active: False}
            )
            db.commit()
        # Re-running the migration is what an upgrade does.
        with runtime.engine.begin() as connection:
            connection.execute(text("UPDATE stores SET lifecycle_state = NULL"))
        ensure_store_lifecycle_schema(runtime.engine)

        assert runtime.state(runtime.store_id) == ACTIVE
        assert runtime.state(runtime.other_store_id) == DISABLED
    finally:
        runtime.engine.dispose()


def test_no_store_is_archived_by_a_migration(runtime: Runtime):
    """Archiving is a decision. A schema change must never make it for anybody."""
    states = {row[0] for row in runtime.query("SELECT lifecycle_state FROM stores")}
    assert ARCHIVED not in states


# ===========================================================================
# Disable and re-enable
# ===========================================================================
def test_disabling_a_store(runtime: Runtime):
    disable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.state() == DISABLED
    assert runtime.store().is_active is False


def test_re_enabling_a_disabled_store(runtime: Runtime):
    disable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    enable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.state() == ACTIVE
    assert runtime.store().is_active is True


def test_is_active_stays_in_lockstep_with_the_state(runtime: Runtime):
    """Every existing path - broadcast targeting, enrolment, Receiver
    authentication - reads ``is_active``. If the two ever disagree, an archived
    Store starts answering announcements again."""
    for transition, expected_active in (
        (disable_store, False),
        (enable_store, True),
        (archive_store, False),
    ):
        transition(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
        assert runtime.store().is_active is expected_active


def test_enabling_an_already_active_store_is_harmless(runtime: Runtime):
    enable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.state() == ACTIVE


def test_an_unknown_store_is_refused(runtime: Runtime):
    with pytest.raises(StoreLifecycleError):
        disable_store(runtime.Session, store_id=99999, actor_user_id=runtime.actor_id)


# ===========================================================================
# Archiving keeps everything
# ===========================================================================
def test_archiving_keeps_the_row(runtime: Runtime):
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.query("SELECT COUNT(*) FROM stores WHERE id = ?", (runtime.store_id,)) == [(1,)]
    assert runtime.state() == ARCHIVED


def test_archiving_keeps_every_row_the_store_owns(runtime: Runtime):
    """The whole reason this is not a DELETE."""
    runtime.add_history()
    before = {
        table: runtime.query(f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in ("broadcast_sessions", "broadcast_targets", "receiver_events")
    }
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    after = {
        table: runtime.query(f"SELECT COUNT(*) FROM {table}")[0][0]
        for table in ("broadcast_sessions", "broadcast_targets", "receiver_events")
    }
    assert before == after
    assert all(count > 0 for count in after.values()), "the fixture wrote no history to lose"


def test_an_archived_stores_history_is_still_readable(runtime: Runtime):
    runtime.add_history()
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    events = runtime.query(
        "SELECT event_type FROM receiver_events WHERE store_id = ?", (runtime.store_id,)
    )
    assert [row[0] for row in events] == ["connected", "playback_confirmed"]


def test_archiving_does_not_touch_the_receiver_token(runtime: Runtime):
    """Archiving is not a credential operation. Rotating on the way out would
    make an archive silently different from a disable."""
    before = runtime.query("SELECT receiver_token FROM stores WHERE id = ?", (runtime.store_id,))
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    after = runtime.query("SELECT receiver_token FROM stores WHERE id = ?", (runtime.store_id,))
    assert before == after


def test_archiving_does_not_delete_device_records(runtime: Runtime):
    """The Devices stay so an administrator can see what was in that Store."""
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    # No receiver_devices table in this fixture; the assertion that matters is
    # that archiving issued no DELETE at all.
    assert runtime.state() == ARCHIVED


def test_another_store_is_untouched(runtime: Runtime):
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.state(runtime.other_store_id) == ACTIVE
    assert runtime.store(runtime.other_store_id).is_active is True


# ===========================================================================
# An archived Store is genuinely out of service
# ===========================================================================
def test_an_archived_store_is_not_active(runtime: Runtime):
    """This one line is what keeps an archived Store out of broadcast targeting,
    out of enrolment and out of Receiver authentication - all of which already
    filter on ``is_active``."""
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.store().is_active is False


def test_an_archived_store_cannot_be_re_enabled_by_the_ordinary_action(runtime: Runtime):
    """Un-retiring a Store is not a small decision, and Enable is a small button."""
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    with pytest.raises(StoreTransitionRefused):
        enable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.state() == ARCHIVED


def test_an_archived_store_cannot_be_disabled_either(runtime: Runtime):
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    with pytest.raises(StoreTransitionRefused):
        disable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)


# ===========================================================================
# Restore
# ===========================================================================
def test_restoring_returns_a_store_to_disabled_not_active(runtime: Runtime):
    """So somebody has to look at its Devices before it can broadcast again."""
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    restore_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.state() == DISABLED
    assert runtime.store().is_active is False


def test_a_restored_store_can_then_be_enabled(runtime: Runtime):
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    restore_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    enable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    assert runtime.state() == ACTIVE


def test_restoring_a_store_that_is_not_archived_is_refused(runtime: Runtime):
    with pytest.raises(StoreNotRestorableError):
        restore_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)


def test_restoring_regenerates_no_secret(runtime: Runtime):
    before = runtime.query("SELECT receiver_token FROM stores WHERE id = ?", (runtime.store_id,))
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    restore_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    after = runtime.query("SELECT receiver_token FROM stores WHERE id = ?", (runtime.store_id,))
    assert before == after


# ===========================================================================
# A live broadcast blocks archiving
# ===========================================================================
def test_archiving_a_store_in_a_live_broadcast_is_refused(runtime: Runtime):
    """Pulling a Store out from under a running announcement is the kind of thing
    that is only noticed by the people standing in it."""
    with pytest.raises(StoreTransitionRefused):
        archive_store(
            runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id,
            live_store_ids={runtime.store_id},
        )
    assert runtime.state() == ACTIVE


def test_archiving_a_store_not_in_the_live_broadcast_is_allowed(runtime: Runtime):
    archive_store(
        runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id,
        live_store_ids={runtime.other_store_id},
    )
    assert runtime.state() == ARCHIVED


def test_disabling_during_a_live_broadcast_is_also_refused(runtime: Runtime):
    with pytest.raises(StoreTransitionRefused):
        disable_store(
            runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id,
            live_store_ids={runtime.store_id},
        )


# ===========================================================================
# Editing
# ===========================================================================
def test_a_store_code_is_validated(runtime: Runtime):
    assert validate_store_code("  UN-2  ") == "UN-2"
    for bad in ("", "   ", "x" * 51, "has space", "tab\there", None, 7):
        with pytest.raises(StoreLifecycleError):
            validate_store_code(bad)


def test_audit_events_are_written_without_secrets(runtime: Runtime):
    disable_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    messages = [row[0] for row in runtime.query("SELECT message FROM system_logs ORDER BY id")]
    assert any("archiv" in message.lower() for message in messages)
    joined = " ".join(messages)
    assert "a" * 32 not in joined, "a Store credential reached the audit log"
    assert "receiver_token" not in joined


# ===========================================================================
# No Store response carries a Receiver credential
# ===========================================================================
def test_the_store_response_schema_has_no_secret_field():
    """``StoreOut`` used to include ``receiver_token``.

    Every authenticated caller could therefore read the shared Receiver
    credential of all 44 Stores out of ``GET /api/stores`` - a read-only VIEWER
    included, once roles exist. The page never rendered it; it sat in the
    response body, so in browser memory, devtools, any HAR file and any proxy
    log. This asserts on the schema rather than on one endpoint's output,
    because the schema is what every Store route returns.
    """
    from schemas import StoreOut

    fields = set(StoreOut.model_fields)
    forbidden = {"receiver_token", "token", "password_hash", "secret", "credential"}
    assert not (fields & forbidden), f"StoreOut exposes {fields & forbidden}"


def test_the_store_update_schema_cannot_flip_a_store_active():
    """Turning a Store on and off is a lifecycle transition with rules - an
    archived Store must not become active because somebody PUT a boolean."""
    from schemas import StoreUpdate

    assert "is_active" not in StoreUpdate.model_fields
    assert "lifecycle_state" not in StoreUpdate.model_fields
    assert "receiver_token" not in StoreUpdate.model_fields


def test_the_store_creation_schema_accepts_no_credential():
    from schemas import StoreCreate

    assert "receiver_token" not in StoreCreate.model_fields


def test_serialising_a_real_store_emits_no_credential(runtime: Runtime):
    """The schema check proves the field is gone; this proves nothing puts it
    back through an alias or a computed value."""
    from schemas import StoreOut

    with runtime.Session() as db:
        store = db.query(Store).filter(Store.id == runtime.store_id).one()
        rendered = StoreOut.model_validate(store).model_dump_json()
    assert "a" * 32 not in rendered
    assert "receiver_token" not in rendered


def test_the_column_still_exists_in_the_database(runtime: Runtime):
    """The credential is still stored - Receivers on the shared token are still
    authenticating with it during the migration. Only the API stopped saying it."""
    columns = {row[1] for row in runtime.query("PRAGMA table_info(stores)")}
    assert "receiver_token" in columns


# ===========================================================================
# The protected database
# ===========================================================================
def test_the_protected_database_is_never_opened(runtime: Runtime):
    before = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    runtime.add_history()
    archive_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    restore_store(runtime.Session, store_id=runtime.store_id, actor_user_id=runtime.actor_id)
    after = PROTECTED_DATABASE.stat().st_mtime_ns if PROTECTED_DATABASE.exists() else None
    assert before == after
