"""A rebuilt table must not be able to switch enrolment off.

WHAT HAPPENED

Enrolment on a live estate failed for a day. Every attempt said "that enrolment
code could not be used", every code was freshly generated, and every code was
still unredeemed afterwards - because the code WAS claimed, the Device creation
then failed, and the claim was released. The refusal underneath was:

    receiver credential Phase 1 indexes are inconsistent

``receiver_credentials`` had been rebuilt to change a CHECK constraint, and
SQLite drops a table's indexes when the table is rebuilt. Two of them were
never recreated. From that moment HQ refused to enrol anything and gave no
usable reason: the message named no index, and the wire response - correctly -
said only that the code could not be used.

WHAT THESE TESTS HOLD IN PLACE

  * a database missing those indexes is REPAIRED, not refused: an index is
    derivable, so recreating it cannot lose anything;
  * enrolment works again immediately afterwards, which is the property that
    actually matters;
  * a missing COLUMN or CONSTRAINT is still refused, because those cannot be
    reconstructed from what is there;
  * the refusal, when it happens, NAMES what is missing.
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

PASSWORD = "a-long-enough-temporary-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("SPEAKLINK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("SPEAKLINK_KEY_PROTECTOR", "fake")
    monkeypatch.setenv("SPEAKLINK_KEY_CONTAINER",
                       str(tmp_path / "keys" / "receiver-hmac-keys.bin"))
    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "receiver_index_repair")]:
        sys.modules.pop(module, None)
    from fastapi.testclient import TestClient
    import server as server_module
    from migrations import run_receiver_credential_phase_one
    run_receiver_credential_phase_one(server_module.engine)
    # The live estate is on hash_only, which is the state that lets HQ verify
    # what it issues. Without setting it, every enrolment here is refused by
    # the migration gate and the test would prove nothing about indexes.
    with server_module.engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE receiver_credential_migration_state SET state = 'hash_only'")
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def drop_index(engine, name: str) -> None:
    """Exactly what a table rebuild does to an index, without the rebuild."""
    with engine.begin() as connection:
        connection.exec_driver_sql(f"DROP INDEX IF EXISTS {name}")


def enrol(client, headers, store_id: int, device_name="Till 1"):
    made = client.post("/api/receiver-devices/enrollment-codes", headers=headers,
                       json={"store_id": store_id})
    assert made.status_code in (200, 201), made.text
    code = made.json()["code"]
    return client.post("/api/receiver-devices/enroll", json={
        "code": code, "device_name": device_name,
        "hostname": "till-1", "software_version": "1.7"})


def make_store(client, headers, code="AAA"):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": "DELHI", "region": "NORTH"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


# ===========================================================================
# The repair
# ===========================================================================

def test_a_rebuilt_table_loses_its_indexes_and_they_come_back(client):
    """The exact failure, reproduced and then repaired."""
    from receiver_index_repair import missing_indexes, repair_receiver_indexes
    engine = client.server_module.engine

    assert missing_indexes(engine) == []
    drop_index(engine, "ix_receiver_credentials_auth_lookup")
    drop_index(engine, "ix_receiver_credentials_device_status")
    assert missing_indexes(engine) == [
        "ix_receiver_credentials_auth_lookup",
        "ix_receiver_credentials_device_status",
    ]

    assert set(repair_receiver_indexes(engine)) == {
        "ix_receiver_credentials_auth_lookup",
        "ix_receiver_credentials_device_status",
    }
    assert missing_indexes(engine) == []


def test_enrolment_works_after_the_repair(client):
    """The property that matters. The index check is upstream of everything -
    a Store cannot be enrolled, so nothing else about it can be tested."""
    from receiver_index_repair import repair_receiver_indexes
    engine = client.server_module.engine
    headers = sign_in(client)
    store_id = make_store(client, headers)

    drop_index(engine, "ix_receiver_credentials_auth_lookup")
    refused = enrol(client, headers, store_id)
    assert refused.status_code == 400, "a missing index should stop enrolment"

    repair_receiver_indexes(engine)
    enrolled = enrol(client, headers, store_id, device_name="Till 2")
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json()["credential"], "no credential was issued"


def test_repairing_twice_changes_nothing(client):
    from receiver_index_repair import repair_receiver_indexes
    engine = client.server_module.engine
    assert repair_receiver_indexes(engine) == []
    assert repair_receiver_indexes(engine) == []


def test_a_database_with_no_receiver_tables_is_not_repaired_into_one(tmp_path):
    """An index on a table that does not exist is not a missing index - it is
    an unmigrated database, which is a different problem with a different fix."""
    from sqlalchemy import create_engine
    from receiver_index_repair import missing_indexes, repair_receiver_indexes

    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
    assert missing_indexes(engine) == []
    assert repair_receiver_indexes(engine) == []


# ===========================================================================
# What is still refused, and how it reads
# ===========================================================================

def test_a_missing_column_is_still_refused(client):
    """Columns and constraints cannot be reconstructed from what is left, so
    the strictness that protects them is unchanged."""
    from receiver_device_service import MigrationNotReadyError, _validate_phase_one
    engine = client.server_module.engine

    with engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE receiver_credentials "
                                   "RENAME COLUMN token_hash TO token_hash_old")

    with engine.connect() as connection:
        with pytest.raises(MigrationNotReadyError) as refusal:
            _validate_phase_one(connection)
    assert "schema is inconsistent" in str(refusal.value)


def test_the_refusal_names_the_missing_index(client):
    """"indexes are inconsistent" sent a day of investigation everywhere except
    the database, because it never said which index."""
    from receiver_device_service import MigrationNotReadyError, _validate_phase_one
    engine = client.server_module.engine
    drop_index(engine, "ix_receiver_credentials_auth_lookup")

    with engine.connect() as connection:
        with pytest.raises(MigrationNotReadyError) as refusal:
            _validate_phase_one(connection)
    assert "ix_receiver_credentials_auth_lookup" in str(refusal.value)


def test_startup_repairs_a_database_that_lost_them(client):
    """A Store PC gets a working HQ from a restart, without anybody knowing
    that indexes exist."""
    from receiver_index_repair import missing_indexes
    engine = client.server_module.engine
    drop_index(engine, "ix_receiver_credentials_device_status")

    # Exactly what the startup hook calls.
    from receiver_index_repair import repair_receiver_indexes
    repair_receiver_indexes(engine)
    assert missing_indexes(engine) == []
