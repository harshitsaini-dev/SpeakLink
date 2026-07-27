"""Tests for the strictly read-only Store catalog reconciliation report.

Every test uses a pytest temporary file-backed SQLite database. None of them
open, copy, migrate, seed or modify ``backend/echocast_live.db``, start
FastAPI/Uvicorn, or touch the network.

The canonical expectations are always read from ``store_catalog`` rather than
duplicated as literals here, so this suite cannot drift from the approved
catalog the way a hand-copied list can.

That was true of every *test* here and still not enough. Importing ``models``
pulls in ``db``, which binds a process-wide engine to whatever
``ECHOCAST_DB_PATH`` said at import time and installs a ``connect`` listener
running ``PRAGMA journal_mode=WAL`` - itself a write. With the variable unset,
that engine points at the protected database. ``conftest.py`` now guarantees
the variable; this file sets it too, so it is safe when imported or run alone.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile

_PROTECTED = Path(__file__).resolve().parents[1] / "echocast_live.db"
_configured = os.environ.get("ECHOCAST_DB_PATH")
if not _configured or Path(_configured).resolve() == _PROTECTED.resolve():
    os.environ["ECHOCAST_DB_PATH"] = str(
        Path(tempfile.gettempdir())
        / f"echocast-reconciliation-{os.environ.get('PYTEST_XDIST_WORKER', 'serial')}.db"
    )

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402

import models  # noqa: F401,E402  - registers the tables on Base.metadata
from db import Base  # noqa: E402
from migrations import PROTECTED_DATABASE_PATH, run_receiver_credential_phase_one
from store_catalog import CANONICAL_STORES, CANONICAL_ZONES
from store_catalog_reconciliation import (
    EXIT_DIFFERENCES,
    EXIT_EXACT_MATCH,
    EXIT_FAILURE,
    LEGACY_DEMO_FINGERPRINTS,
    DatabaseInputError,
    DatabaseSchemaError,
    ProtectedDatabaseError,
    Recommendation,
    StoreClassification,
    UnsafeSnapshotError,
    main,
    reconcile,
    render_json,
    render_text,
)


REAL_DATABASE = Path(__file__).resolve().parents[1] / "echocast_live.db"

# A deliberately non-secret placeholder. The report must never select or
# serialize receiver_token, so this value doubles as a leak canary.
TOKEN_CANARY = "canary0token0value0must0never0appear"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _file_fingerprint(path: Path):
    stat = path.stat()
    return (
        hashlib.sha256(path.read_bytes()).hexdigest(),
        stat.st_size,
        stat.st_mtime_ns,
    )


def _real_database_metadata():
    if not REAL_DATABASE.exists():
        return None
    stat = REAL_DATABASE.stat()
    return stat.st_size, stat.st_mtime_ns


def _canonical_row(entry, store_id: int):
    """A database row that exactly matches a canonical catalog entry."""
    return {
        "id": store_id,
        "store_code": entry.short_name,
        "store_name": entry.full_name,
        "city": entry.zone,
        "region": entry.zone,
        "is_online_store": 0,
        "is_active": 1,
    }


def _make_database(
    tmp_path: Path,
    rows: list[dict],
    *,
    name: str = "snapshot.db",
    with_phase_one: bool = False,
) -> Path:
    database_path = tmp_path / name
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(bind=engine)
    if with_phase_one:
        run_receiver_credential_phase_one(engine)
    engine.dispose()

    connection = sqlite3.connect(database_path)
    try:
        for index, row in enumerate(rows, start=1):
            connection.execute(
                "INSERT INTO stores ("
                "id, store_code, store_name, city, region, is_online_store, "
                "receiver_token, is_active, status, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'offline', "
                "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
                (
                    row["id"],
                    row["store_code"],
                    row["store_name"],
                    row["city"],
                    row["region"],
                    row["is_online_store"],
                    f"{TOKEN_CANARY}{index:03d}",
                    row["is_active"],
                ),
            )
        connection.commit()
    finally:
        connection.close()
    # Ensure no stray WAL/SHM remains beside the snapshot.
    for suffix in ("-wal", "-shm"):
        stray = Path(str(database_path) + suffix)
        if stray.exists():
            stray.unlink()
    return database_path


def _canonical_database(tmp_path: Path, **kwargs) -> Path:
    rows = [
        _canonical_row(entry, index)
        for index, entry in enumerate(CANONICAL_STORES, start=1)
    ]
    return _make_database(tmp_path, rows, **kwargs)


def _demo_rows() -> list[dict]:
    return [
        {
            "id": index,
            "store_code": code,
            "store_name": name,
            "city": city,
            "region": region,
            "is_online_store": 1 if is_online else 0,
            "is_active": 1,
        }
        for index, (code, name, city, region, is_online) in enumerate(
            LEGACY_DEMO_FINGERPRINTS, start=1
        )
    ]


def _by_code(report):
    return {store.store_code: store for store in report.stores}


# ---------------------------------------------------------------------------
# 1-4. Protected-path refusal, before any connection
# ---------------------------------------------------------------------------
def test_exact_protected_path_is_refused_before_connecting():
    before = _real_database_metadata()
    with pytest.raises(ProtectedDatabaseError):
        reconcile(PROTECTED_DATABASE_PATH)
    assert _real_database_metadata() == before


def test_relative_and_dotdot_protected_paths_are_refused():
    before = _real_database_metadata()
    dotdot = PROTECTED_DATABASE_PATH.parent / ".." / "backend" / "echocast_live.db"
    with pytest.raises(ProtectedDatabaseError):
        reconcile(dotdot)
    with pytest.raises(ProtectedDatabaseError):
        reconcile(str(PROTECTED_DATABASE_PATH))
    assert _real_database_metadata() == before


def test_relative_cwd_protected_path_is_refused(monkeypatch):
    before = _real_database_metadata()
    monkeypatch.chdir(PROTECTED_DATABASE_PATH.parent)
    with pytest.raises(ProtectedDatabaseError):
        reconcile("echocast_live.db")
    assert _real_database_metadata() == before


def test_same_file_reference_to_the_protected_path_is_refused(tmp_path, monkeypatch):
    """Proven with an isolated stand-in; the real database is never linked."""
    stand_in = _canonical_database(tmp_path, name="protected-stand-in.db")
    monkeypatch.setattr(
        "store_catalog_reconciliation.PROTECTED_DATABASE_PATH", stand_in
    )
    alias = tmp_path / "alias.db"
    try:
        os.link(stand_in, alias)
    except (OSError, NotImplementedError, AttributeError):
        pytest.skip("hard links are unavailable on this filesystem")

    with pytest.raises(ProtectedDatabaseError):
        reconcile(alias)


# ---------------------------------------------------------------------------
# 5-10. Controlled input, schema and snapshot-safety errors
# ---------------------------------------------------------------------------
def test_missing_path_gives_controlled_error(tmp_path):
    with pytest.raises(DatabaseInputError):
        reconcile(tmp_path / "absent.db")


def test_directory_path_gives_controlled_error(tmp_path):
    with pytest.raises(DatabaseInputError):
        reconcile(tmp_path)


def test_non_sqlite_file_gives_controlled_error(tmp_path):
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is definitely not a SQLite database file")
    with pytest.raises(DatabaseInputError):
        reconcile(corrupt)


def test_missing_stores_table_gives_controlled_schema_error(tmp_path):
    empty = tmp_path / "no-stores.db"
    connection = sqlite3.connect(empty)
    try:
        connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DatabaseSchemaError):
        reconcile(empty)


def test_unsupported_stores_schema_gives_controlled_schema_error(tmp_path):
    partial = tmp_path / "partial.db"
    connection = sqlite3.connect(partial)
    try:
        connection.execute("CREATE TABLE stores (id INTEGER PRIMARY KEY, nickname TEXT)")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(DatabaseSchemaError):
        reconcile(partial)


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_adjacent_wal_or_shm_fails_closed(tmp_path, suffix):
    database_path = _canonical_database(tmp_path)
    Path(str(database_path) + suffix).write_bytes(b"")
    with pytest.raises(UnsafeSnapshotError):
        reconcile(database_path)


# ---------------------------------------------------------------------------
# 11-13. Canonical comparison driven by store_catalog, not test literals
# ---------------------------------------------------------------------------
def test_empty_store_table_reports_every_canonical_store_missing(tmp_path):
    database_path = _make_database(tmp_path, [])
    report = reconcile(database_path)

    assert report.database_store_count == 0
    assert report.canonical_zone_count == len(CANONICAL_ZONES)
    assert report.canonical_store_count == len(CANONICAL_STORES)
    assert len(report.missing_canonical) == len(CANONICAL_STORES)
    assert [item.short_name for item in report.missing_canonical] == [
        entry.short_name for entry in CANONICAL_STORES
    ]
    assert all(
        item.recommendation is Recommendation.ADD_MISSING_CANONICAL_STORE_LATER
        for item in report.missing_canonical
    )
    assert report.exit_code == EXIT_DIFFERENCES


def test_exact_canonical_database_reports_only_exact_matches(tmp_path):
    database_path = _canonical_database(tmp_path)
    report = reconcile(database_path)

    assert report.database_store_count == len(CANONICAL_STORES)
    assert len(report.exact_matches) == len(CANONICAL_STORES)
    assert report.missing_canonical == ()
    assert report.field_mismatches == ()
    assert report.identity_conflicts == ()
    assert report.non_canonical == ()
    assert report.duplicate_codes == ()
    assert report.duplicate_full_names == ()
    assert all(
        store.classification is StoreClassification.EXACT_CANONICAL_MATCH
        and store.recommendation is Recommendation.NO_ACTION
        for store in report.stores
    )
    assert report.exit_code == EXIT_EXACT_MATCH


def test_canonical_counts_come_from_store_catalog_not_test_literals(tmp_path):
    report = reconcile(_make_database(tmp_path, []))
    assert report.canonical_store_count == len(CANONICAL_STORES)
    assert report.canonical_zone_count == len(CANONICAL_ZONES)
    # A literal drift guard: the module must not hard-code its own copy.
    assert report.canonical_store_count == 44
    assert report.canonical_zone_count == 9


# ---------------------------------------------------------------------------
# 14-16. Legacy demo fingerprints versus custom rows
# ---------------------------------------------------------------------------
def test_historical_demo_rows_are_classified_as_known_legacy_demo(tmp_path):
    database_path = _make_database(tmp_path, _demo_rows())
    report = reconcile(database_path)

    assert len(LEGACY_DEMO_FINGERPRINTS) == 13
    assert len(report.non_canonical) == 13
    assert all(
        store.classification is StoreClassification.KNOWN_LEGACY_DEMO_EXACT_MATCH
        for store in report.non_canonical
    )
    assert len(report.missing_canonical) == len(CANONICAL_STORES)
    assert report.exit_code == EXIT_DIFFERENCES


@pytest.mark.parametrize(
    "field, replacement",
    [
        ("store_name", "Mumbai Andheri Flagship Renamed"),
        ("city", "Navi Mumbai"),
        ("region", "Western"),
        ("is_online_store", 1),
    ],
)
def test_one_field_variation_is_not_an_exact_legacy_demo_match(tmp_path, field, replacement):
    rows = _demo_rows()[:1]
    rows[0][field] = replacement
    report = reconcile(_make_database(tmp_path, rows))

    store = report.stores[0]
    assert store.classification is StoreClassification.CUSTOM_OR_UNKNOWN_NON_CANONICAL
    assert store.classification is not StoreClassification.KNOWN_LEGACY_DEMO_EXACT_MATCH


def test_custom_store_stays_custom_or_unknown(tmp_path):
    rows = [
        {
            "id": 1,
            "store_code": "OPS-WAREHOUSE-1",
            "store_name": "Operations Warehouse",
            "city": "Delhi",
            "region": "Internal",
            "is_online_store": 0,
            "is_active": 1,
        }
    ]
    report = reconcile(_make_database(tmp_path, rows))
    store = report.stores[0]
    assert store.classification is StoreClassification.CUSTOM_OR_UNKNOWN_NON_CANONICAL
    assert store.recommendation is Recommendation.HUMAN_REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# 17-22. Identity conflicts, field mismatches and duplicates
# ---------------------------------------------------------------------------
def test_same_code_with_different_full_name_is_an_identity_conflict(tmp_path):
    entry = CANONICAL_STORES[0]
    rows = [_canonical_row(entry, 1)]
    rows[0]["store_name"] = "Completely Different Store Name"
    report = reconcile(_make_database(tmp_path, rows))

    store = report.stores[0]
    assert store.classification is StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT
    assert "code_matches_canonical_but_full_name_differs" in store.issues
    assert store.recommendation is Recommendation.REVIEW_IDENTITY_CONFLICT


def test_same_full_name_with_different_code_is_an_identity_conflict(tmp_path):
    entry = CANONICAL_STORES[0]
    rows = [_canonical_row(entry, 1)]
    rows[0]["store_code"] = "NOT-CANONICAL-CODE"
    report = reconcile(_make_database(tmp_path, rows))

    store = report.stores[0]
    assert store.classification is StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT
    assert "full_name_matches_canonical_but_code_differs" in store.issues


def test_wrong_zone_is_a_field_mismatch(tmp_path):
    entry = CANONICAL_STORES[0]
    rows = [_canonical_row(entry, 1)]
    rows[0]["region"] = "WRONG ZONE"
    report = reconcile(_make_database(tmp_path, rows))

    store = report.stores[0]
    assert store.classification is StoreClassification.CANONICAL_FIELD_MISMATCH
    assert "wrong_zone" in store.issues
    assert store.recommendation is Recommendation.REVIEW_FIELD_CORRECTION
    assert len(report.field_mismatches) == 1


def test_wrong_city_is_a_field_mismatch(tmp_path):
    entry = CANONICAL_STORES[0]
    rows = [_canonical_row(entry, 1)]
    rows[0]["city"] = "Some Other City"
    report = reconcile(_make_database(tmp_path, rows))

    store = report.stores[0]
    assert store.classification is StoreClassification.CANONICAL_FIELD_MISMATCH
    assert "wrong_city" in store.issues


def test_duplicate_full_name_is_reported(tmp_path):
    """``stores.store_name`` has no unique constraint, so this is reachable."""
    second = CANONICAL_STORES[1]
    rows = [
        _canonical_row(second, 1),
        {
            "id": 2,
            "store_code": "OTHER-CODE",
            "store_name": second.full_name,
            "city": second.zone,
            "region": second.zone,
            "is_online_store": 0,
            "is_active": 1,
        },
    ]
    report = reconcile(_make_database(tmp_path, rows))

    assert second.full_name in report.duplicate_full_names
    assert all(
        "duplicate_store_full_name" in store.issues
        and store.classification is StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT
        for store in report.stores
    )


def test_duplicate_store_code_is_reported_when_the_snapshot_allows_it(tmp_path):
    """The live schema has UNIQUE(store_code), so duplicates cannot occur there.

    A snapshot from an older or hand-built schema still can, and the report
    must detect it rather than silently pick one row, so this uses a relaxed
    schema on purpose.
    """
    first = CANONICAL_STORES[0]
    database_path = tmp_path / "relaxed-schema.db"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "CREATE TABLE stores ("
            "id INTEGER PRIMARY KEY, store_code TEXT NOT NULL, "
            "store_name TEXT NOT NULL, city TEXT NOT NULL, region TEXT NOT NULL, "
            "is_online_store INTEGER NOT NULL, receiver_token TEXT NOT NULL, "
            "is_active INTEGER NOT NULL)"
        )
        for store_id in (1, 2):
            connection.execute(
                "INSERT INTO stores (id, store_code, store_name, city, region, "
                "is_online_store, receiver_token, is_active) VALUES (?, ?, ?, ?, ?, 0, ?, 1)",
                (
                    store_id,
                    first.short_name,
                    first.full_name,
                    first.zone,
                    first.zone,
                    f"{TOKEN_CANARY}{store_id:03d}",
                ),
            )
        connection.commit()
    finally:
        connection.close()

    report = reconcile(database_path)

    assert first.short_name in report.duplicate_codes
    assert all(
        "duplicate_store_code" in store.issues
        and store.classification is StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT
        for store in report.stores
    )


# ---------------------------------------------------------------------------
# 23. Dependency counts across every proven Store foreign key
# ---------------------------------------------------------------------------
def test_dependency_counts_cover_every_proven_store_foreign_key(tmp_path):
    rows = _demo_rows()[:1]
    database_path = _make_database(tmp_path, rows, with_phase_one=True)

    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO hq_users (id, username, password_hash, role, is_active, created_at) "
            "VALUES (1, 'operator', 'not-a-real-hash', 'admin', 1, '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO broadcast_sessions (id, campaign_name, started_by, status, "
            "target_mode, selected_store_count, online_store_count, offline_store_count, "
            "created_at) VALUES (1, 'Campaign', 1, 'ended', 'all', 1, 0, 1, "
            "'2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO broadcast_targets (id, session_id, store_id, play_status) "
            "VALUES (1, 1, 1, 'stopped')"
        )
        connection.execute(
            "INSERT INTO receiver_events (id, store_id, event_type, event_time) "
            "VALUES (1, 1, 'connected', '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO receiver_events (id, store_id, event_type, event_time) "
            "VALUES (2, 1, 'disconnected', '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO receiver_devices (public_id, store_id, display_name, status, "
            "enrolled_at, created_at, updated_at) VALUES "
            "('11111111-1111-4111-8111-111111111111', 1, 'Legacy Receiver 1', 'active', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        device_id = connection.execute(
            "SELECT id FROM receiver_devices WHERE store_id = 1"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO receiver_credentials (public_id, device_id, credential_version, "
            "token_format, token_hash, hash_key_version, status, expiry_policy, issued_at, "
            "created_at) VALUES ('22222222-2222-4222-8222-222222222222', ?, 1, "
            "'legacy_uuid_hex', 'hmac-sha256$v1$" + ("a" * 64) + "', 1, 'active', "
            "'non_expiring', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (device_id,),
        )
        connection.execute(
            "INSERT INTO receiver_credential_events (public_id, event_type, outcome, "
            "store_id, event_at) VALUES ('33333333-3333-4333-8333-333333333333', "
            "'device_enrolled', 'success', 1, '2026-01-01T00:00:00+00:00')"
        )
        connection.commit()
    finally:
        connection.close()

    report = reconcile(database_path)
    dependencies = report.stores[0].dependencies

    assert dependencies.broadcast_targets == 1
    assert dependencies.receiver_events == 2
    assert dependencies.receiver_devices == 1
    assert dependencies.receiver_credential_events == 1
    assert dependencies.receiver_credentials_via_devices == 1
    assert dependencies.has_any is True
    # A legacy demo row that history still references must not be proposed for
    # deletion.
    assert report.stores[0].recommendation is Recommendation.REVIEW_ARCHIVAL


def test_absent_phase_one_tables_are_reported_as_unavailable_not_zero(tmp_path):
    report = reconcile(_make_database(tmp_path, _demo_rows()[:1]))
    dependencies = report.stores[0].dependencies
    assert dependencies.broadcast_targets == 0
    assert dependencies.receiver_events == 0
    assert dependencies.receiver_devices is None
    assert dependencies.receiver_credential_events is None
    assert dependencies.receiver_credentials_via_devices is None
    assert dependencies.has_any is False
    assert report.stores[0].recommendation is Recommendation.REVIEW_TARGETED_DELETION


# ---------------------------------------------------------------------------
# 24-27. The supplied snapshot must be byte-for-byte unchanged
# ---------------------------------------------------------------------------
def test_report_leaves_the_supplied_snapshot_byte_for_byte_unchanged(tmp_path):
    database_path = _canonical_database(tmp_path, with_phase_one=True)
    before = _file_fingerprint(database_path)
    real_before = _real_database_metadata()

    reconcile(database_path)
    render_text(reconcile(database_path))
    render_json(reconcile(database_path))

    assert _file_fingerprint(database_path) == before
    assert _real_database_metadata() == real_before
    assert not Path(str(database_path) + "-wal").exists()
    assert not Path(str(database_path) + "-shm").exists()


def test_the_connection_itself_rejects_writes(tmp_path):
    """Prove the read-only boundary directly, not just by comparing hashes."""
    from store_catalog_reconciliation import _open_read_only

    database_path = _canonical_database(tmp_path)
    connection = _open_read_only(database_path)
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        for statement in (
            "UPDATE stores SET city = 'tampered' WHERE id = 1",
            "DELETE FROM stores WHERE id = 1",
            "CREATE TABLE tampered (id INTEGER)",
        ):
            with pytest.raises(sqlite3.OperationalError):
                connection.execute(statement)
    finally:
        connection.close()

    report = reconcile(database_path)
    assert report.exit_code == EXIT_EXACT_MATCH


def test_store_and_dependent_rows_are_unchanged_after_a_report(tmp_path):
    database_path = _make_database(tmp_path, _demo_rows(), with_phase_one=True)
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(
            "INSERT INTO receiver_events (id, store_id, event_type, event_time) "
            "VALUES (1, 1, 'connected', '2026-01-01 00:00:00')"
        )
        connection.commit()
        stores_before = connection.execute(
            "SELECT id, store_code, store_name, city, region, is_active FROM stores "
            "ORDER BY id"
        ).fetchall()
        events_before = connection.execute(
            "SELECT id, store_id, event_type FROM receiver_events ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    reconcile(database_path)

    connection = sqlite3.connect(database_path)
    try:
        assert (
            connection.execute(
                "SELECT id, store_code, store_name, city, region, is_active FROM stores "
                "ORDER BY id"
            ).fetchall()
            == stores_before
        )
        assert (
            connection.execute(
                "SELECT id, store_id, event_type FROM receiver_events ORDER BY id"
            ).fetchall()
            == events_before
        )
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# 28-31. Output safety, determinism and stable ordering
# ---------------------------------------------------------------------------
def test_text_and_json_output_contain_no_token_or_credential_material(tmp_path):
    database_path = _make_database(tmp_path, _demo_rows(), with_phase_one=True)
    report = reconcile(database_path)

    text_output = render_text(report)
    json_output = render_json(report)

    for rendered in (text_output, json_output):
        assert TOKEN_CANARY not in rendered
        lowered = rendered.lower()
        for marker in (
            "receiver_token",
            "token_hash",
            "hmac",
            "authorization",
            "bearer",
            "password",
            "jwt",
            "secret",
        ):
            assert marker not in lowered


def test_json_output_is_deterministic_and_machine_readable(tmp_path):
    database_path = _canonical_database(tmp_path)
    first = render_json(reconcile(database_path))
    second = render_json(reconcile(database_path))
    assert first == second

    payload = json.loads(first)
    assert payload["summary"]["canonical_store_count"] == len(CANONICAL_STORES)
    assert payload["summary"]["exact_match_count"] == len(CANONICAL_STORES)
    assert payload["summary"]["overall_result"] == "EXACT_CANONICAL_MATCH"


def test_missing_canonical_ordering_follows_the_approved_catalog(tmp_path):
    report = reconcile(_make_database(tmp_path, []))
    assert [item.catalog_position for item in report.missing_canonical] == list(
        range(1, len(CANONICAL_STORES) + 1)
    )
    assert [item.zone for item in report.missing_canonical] == [
        entry.zone for entry in CANONICAL_STORES
    ]
    seen_zones: list[str] = []
    for item in report.missing_canonical:
        if item.zone not in seen_zones:
            seen_zones.append(item.zone)
    assert tuple(seen_zones) == tuple(CANONICAL_ZONES)


def test_reported_stores_are_ordered_by_database_id(tmp_path):
    rows = [_canonical_row(entry, index) for index, entry in enumerate(CANONICAL_STORES[:5], start=1)]
    rows.reverse()
    report = reconcile(_make_database(tmp_path, rows))
    assert [store.store_id for store in report.stores] == [1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# 32-35. The report must stay outside the runtime
# ---------------------------------------------------------------------------
def test_report_does_not_import_or_execute_runtime_startup(tmp_path):
    """Proven in a clean interpreter, so the module's own import graph is tested.

    A plain ``sys.modules`` assertion cannot work here: pytest-xdist shares a
    worker process with suites that legitimately import FastAPI, so it would
    measure the worker rather than this module.
    """
    import subprocess
    import sys

    database_path = _canonical_database(tmp_path)
    backend_dir = Path(__file__).resolve().parents[1]
    script = (
        "import sys\n"
        "import store_catalog_reconciliation as module\n"
        f"report = module.reconcile({str(database_path)!r})\n"
        "assert report.exit_code == 0\n"
        "runtime = ('server', 'fastapi', 'uvicorn', 'starlette', 'ws_manager')\n"
        "print('LEAKED=' + ','.join(n for n in runtime if n in sys.modules))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(backend_dir),
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "LEAKED="


def test_report_never_calls_seed_or_migrations(tmp_path, monkeypatch):
    import migrations
    import seed

    def _forbidden(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the reconciliation report must not mutate a database")

    monkeypatch.setattr(seed, "seed_stores", _forbidden)
    monkeypatch.setattr(seed, "seed_admin", _forbidden)
    monkeypatch.setattr(migrations, "run_receiver_credential_phase_one", _forbidden)

    report = reconcile(_canonical_database(tmp_path))
    assert report.exit_code == EXIT_EXACT_MATCH


# ---------------------------------------------------------------------------
# 36. Documented exit codes through the CLI entry point
# ---------------------------------------------------------------------------
def test_cli_exit_code_zero_for_an_exact_canonical_database(tmp_path, capsys):
    database_path = _canonical_database(tmp_path)
    assert main(["--database", str(database_path)]) == EXIT_EXACT_MATCH
    assert "EXACT_CANONICAL_MATCH" in capsys.readouterr().out


def test_cli_exit_code_two_when_differences_are_found(tmp_path, capsys):
    database_path = _make_database(tmp_path, _demo_rows())
    assert main(["--database", str(database_path)]) == EXIT_DIFFERENCES
    assert "DIFFERENCES_FOUND" in capsys.readouterr().out


def test_cli_exit_code_one_for_the_protected_path(capsys):
    before = _real_database_metadata()
    assert main(["--database", str(PROTECTED_DATABASE_PATH)]) == EXIT_FAILURE
    assert _real_database_metadata() == before
    assert "refused" in capsys.readouterr().err.lower()


def test_cli_json_format_is_valid_json(tmp_path, capsys):
    database_path = _canonical_database(tmp_path)
    assert main(["--database", str(database_path), "--format", "json"]) == EXIT_EXACT_MATCH
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["database_store_count"] == len(CANONICAL_STORES)
