"""Rebuild the Receiver credential indexes when a table rebuild has lost them.

WHY THIS MODULE EXISTS

Enrolment on the live HQ failed for a day with one generic sentence - "that
enrolment code could not be used" - while the codes were fine. Every attempt
reached the database, claimed a code, failed, and gave the code back, which is
why every code stayed unredeemed and why generating a new one never helped.

The actual refusal was ``receiver credential Phase 1 indexes are inconsistent``.
Two indexes on ``receiver_credentials`` were missing:

    ix_receiver_credentials_auth_lookup
    ix_receiver_credentials_device_status

SQLite drops a table's indexes when the table is rebuilt, and
``receiver_credentials`` was rebuilt to change a CHECK constraint. The rebuild
recreated the table and its constraints and did not recreate these two - so
from that moment HQ refused to enrol any Device, and said nothing about why.

WHY REPAIR RATHER THAN REFUSE

An index carries no data. It is derivable from the table in every case, so
recreating one cannot lose anything, cannot change a query's answer, and cannot
mask a genuine schema problem - the columns and constraints are still checked
as strictly as before. Refusing to serve because a derivable artefact is absent
is a policy that costs a working estate and buys nothing.

Everything else stays as it was: a missing COLUMN, a missing CONSTRAINT or a
missing migration ledger entry still stop enrolment, because those cannot be
reconstructed without knowing what they were supposed to contain.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger("speaklink.receiver.schema")

#: The canonical definitions, copied from the Phase 1 migration that creates
#: them. Kept here as SQL rather than imported so a repair cannot depend on the
#: migration module having been run in this process.
REQUIRED_INDEXES: dict[str, str] = {
    "ix_receiver_devices_store_status":
        "CREATE INDEX ix_receiver_devices_store_status "
        "ON receiver_devices(store_id, status)",
    "ix_receiver_credentials_public_id":
        "CREATE UNIQUE INDEX ix_receiver_credentials_public_id "
        "ON receiver_credentials(public_id)",
    "ix_receiver_credentials_auth_lookup":
        "CREATE INDEX ix_receiver_credentials_auth_lookup "
        "ON receiver_credentials(hash_key_version, token_hash)",
    "ix_receiver_credentials_device_status":
        "CREATE INDEX ix_receiver_credentials_device_status "
        "ON receiver_credentials(device_id, status)",
    "ix_receiver_credentials_expires_at":
        "CREATE INDEX ix_receiver_credentials_expires_at "
        "ON receiver_credentials(expires_at)",
    "ix_receiver_credential_events_device_time":
        "CREATE INDEX ix_receiver_credential_events_device_time "
        "ON receiver_credential_events(device_id, occurred_at)",
    "ix_receiver_credential_events_credential_time":
        "CREATE INDEX ix_receiver_credential_events_credential_time "
        "ON receiver_credential_events(credential_id, occurred_at)",
    "ix_receiver_credential_events_store_time":
        "CREATE INDEX ix_receiver_credential_events_store_time "
        "ON receiver_credential_events(store_id, occurred_at)",
}


def missing_indexes(engine: Engine) -> list[str]:
    """Which required indexes this database does not have."""
    if engine.dialect.name != "sqlite":
        # Postgres carries these in its own schema module, which is applied as
        # a unit. Repairing them here would be two sources of truth.
        return []
    with engine.connect() as connection:
        present = {
            row[0] for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        tables = {
            row[0] for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    # An index on a table that does not exist yet is not "missing" - it is a
    # database that has not been migrated, which is a different problem with a
    # different answer.
    wanted = {name: sql for name, sql in REQUIRED_INDEXES.items()
              if _table_of(sql) in tables}
    return sorted(set(wanted) - present)


def _table_of(create_sql: str) -> str:
    return create_sql.split(" ON ", 1)[1].split("(", 1)[0].strip()


def repair_receiver_indexes(engine: Engine) -> list[str]:
    """Recreate any missing required index. Returns the names it created.

    Safe to run on every boot: each statement is skipped when the index is
    already there, and creating one is idempotent in effect even if it were
    not.
    """
    absent = missing_indexes(engine)
    if not absent:
        return []

    created: list[str] = []
    with engine.begin() as connection:
        for name in absent:
            try:
                connection.exec_driver_sql(REQUIRED_INDEXES[name])
                created.append(name)
            except Exception as failure:  # noqa: BLE001
                # Reported, not raised. A database that cannot take an index is
                # a database with a bigger problem, and the schema validators
                # downstream will say so in their own words.
                logger.warning("Could not recreate index %s: %s", name, failure)
    if created:
        logger.warning(
            "Recreated %d Receiver credential index(es) that a table rebuild "
            "had dropped: %s. Enrolment refuses to run without them.",
            len(created), ", ".join(created))
    return created
