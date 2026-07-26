"""Strictly read-only Store catalog reconciliation report.

Compares an explicitly supplied, operator-isolated SQLite snapshot against the
canonical catalog in ``store_catalog``. The report explains what differs and
what a human should review. It never inserts, updates, deletes, migrates,
seeds, archives or repairs anything, and it never emits executable SQL.

Safety boundary:

- The protected application database is refused before any connection is
  opened, by resolved path and by same-file identity.
- The snapshot is opened through a SQLite ``mode=ro`` URI with
  ``PRAGMA query_only = ON``.
- Columns are always listed explicitly. ``SELECT *`` is never used and the
  Store credential column is never selected, so no credential material can
  reach a report, a log or a terminal.
- A snapshot with an adjacent WAL or SHM file fails closed rather than being
  read, merged or repaired.

Being present in the catalog proves only that HQ knows the Store. It is not
evidence of CONNECTED, READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED or
SPEAKER_VERIFIED.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import Enum
import json
import os
from pathlib import Path
import sqlite3
import sys

from migrations import PROTECTED_DATABASE_PATH
from store_catalog import CANONICAL_STORES, CANONICAL_ZONES


EXIT_EXACT_MATCH = 0
EXIT_FAILURE = 1
EXIT_DIFFERENCES = 2

# Columns the report reads. The Store credential column is deliberately absent.
_REQUIRED_STORE_COLUMNS = (
    "id",
    "store_code",
    "store_name",
    "city",
    "region",
    "is_online_store",
    "is_active",
)

# Exact fingerprints of the 13-entry demo seed removed in commit e8b75dd,
# recovered read-only from ``git show af168aa:backend/seed.py``.
# Order: store_code, store_name, city, region, is_online_store.
LEGACY_DEMO_FINGERPRINTS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("MUM-001", "Mumbai Andheri Flagship", "Mumbai", "West", False),
    ("MUM-002", "Mumbai Bandra Outlet", "Mumbai", "West", False),
    ("PUN-001", "Pune Koregaon Park", "Pune", "West", False),
    ("DEL-001", "Delhi Connaught Place", "Delhi", "North", False),
    ("DEL-002", "Delhi Saket Mall", "Delhi", "North", False),
    ("GUR-001", "Gurgaon Cyber Hub", "Gurgaon", "North", False),
    ("BLR-001", "Bangalore MG Road", "Bangalore", "South", False),
    ("BLR-002", "Bangalore Whitefield", "Bangalore", "South", False),
    ("HYD-001", "Hyderabad Banjara Hills", "Hyderabad", "South", False),
    ("CHN-001", "Chennai T. Nagar", "Chennai", "South", False),
    ("KOL-001", "Kolkata Park Street", "Kolkata", "East", False),
    ("ONL-001", "Online Store - Web", "Online", "Online", True),
    ("ONL-002", "Online Store - App", "Online", "Online", True),
)
_LEGACY_DEMO_INDEX = frozenset(LEGACY_DEMO_FINGERPRINTS)


class ReconciliationError(RuntimeError):
    """Base class for controlled, secret-free reconciliation failures."""


class ProtectedDatabaseError(ReconciliationError):
    """Raised before connecting when the protected database was supplied."""


class DatabaseInputError(ReconciliationError):
    """Raised for a missing, non-file or non-SQLite input path."""


class DatabaseSchemaError(ReconciliationError):
    """Raised when the snapshot has no usable ``stores`` table."""


class UnsafeSnapshotError(ReconciliationError):
    """Raised when an adjacent WAL/SHM file makes the snapshot inconsistent."""


class StoreClassification(str, Enum):
    EXACT_CANONICAL_MATCH = "EXACT_CANONICAL_MATCH"
    CANONICAL_FIELD_MISMATCH = "CANONICAL_FIELD_MISMATCH"
    KNOWN_LEGACY_DEMO_EXACT_MATCH = "KNOWN_LEGACY_DEMO_EXACT_MATCH"
    CUSTOM_OR_UNKNOWN_NON_CANONICAL = "CUSTOM_OR_UNKNOWN_NON_CANONICAL"
    AMBIGUOUS_IDENTITY_CONFLICT = "AMBIGUOUS_IDENTITY_CONFLICT"


class Recommendation(str, Enum):
    NO_ACTION = "NO_ACTION"
    ADD_MISSING_CANONICAL_STORE_LATER = "ADD_MISSING_CANONICAL_STORE_LATER"
    REVIEW_FIELD_CORRECTION = "REVIEW_FIELD_CORRECTION"
    REVIEW_IDENTITY_CONFLICT = "REVIEW_IDENTITY_CONFLICT"
    REVIEW_ARCHIVAL = "REVIEW_ARCHIVAL"
    REVIEW_TARGETED_DELETION = "REVIEW_TARGETED_DELETION"
    BLOCKED_BY_DEPENDENCIES = "BLOCKED_BY_DEPENDENCIES"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


@dataclass(frozen=True, slots=True)
class DependencyCounts:
    """Reference counts per proven Store relationship.

    ``None`` means the table is absent from this snapshot, which is different
    from a proven zero.
    """

    broadcast_targets: int
    receiver_events: int
    receiver_devices: int | None
    receiver_credential_events: int | None
    receiver_credentials_via_devices: int | None

    @property
    def has_any(self) -> bool:
        return any(
            value for value in self._present_values()
        )

    def _present_values(self) -> tuple[int, ...]:
        return tuple(
            value
            for value in (
                self.broadcast_targets,
                self.receiver_events,
                self.receiver_devices,
                self.receiver_credential_events,
                self.receiver_credentials_via_devices,
            )
            if value is not None
        )

    @property
    def total_present(self) -> int:
        return sum(self._present_values())

    def as_dict(self) -> dict[str, int | None]:
        return {
            "broadcast_targets": self.broadcast_targets,
            "receiver_credential_events": self.receiver_credential_events,
            "receiver_credentials_via_devices": self.receiver_credentials_via_devices,
            "receiver_devices": self.receiver_devices,
            "receiver_events": self.receiver_events,
        }


@dataclass(frozen=True, slots=True)
class ReportedStore:
    store_id: int
    store_code: str
    store_name: str
    city: str
    region: str
    is_online_store: bool
    is_active: bool
    classification: StoreClassification
    issues: tuple[str, ...]
    dependencies: DependencyCounts
    recommendation: Recommendation

    def as_dict(self) -> dict[str, object]:
        return {
            "city": self.city,
            "classification": self.classification.value,
            "dependencies": self.dependencies.as_dict(),
            "is_active": self.is_active,
            "is_online_store": self.is_online_store,
            "issues": list(self.issues),
            "recommendation": self.recommendation.value,
            "store_code": self.store_code,
            "store_id": self.store_id,
            "store_name": self.store_name,
            "zone": self.region,
        }


@dataclass(frozen=True, slots=True)
class MissingCanonicalStore:
    catalog_position: int
    zone: str
    short_name: str
    full_name: str
    expected_city: str
    expected_region: str
    recommendation: Recommendation

    def as_dict(self) -> dict[str, object]:
        return {
            "catalog_position": self.catalog_position,
            "expected_city": self.expected_city,
            "expected_region": self.expected_region,
            "full_name": self.full_name,
            "recommendation": self.recommendation.value,
            "short_name": self.short_name,
            "zone": self.zone,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    database_path: str
    database_store_count: int
    canonical_zone_count: int
    canonical_store_count: int
    stores: tuple[ReportedStore, ...]
    missing_canonical: tuple[MissingCanonicalStore, ...]
    duplicate_codes: tuple[str, ...]
    duplicate_full_names: tuple[str, ...]

    def _of(self, *classifications: StoreClassification) -> tuple[ReportedStore, ...]:
        return tuple(
            store for store in self.stores if store.classification in classifications
        )

    @property
    def exact_matches(self) -> tuple[ReportedStore, ...]:
        return self._of(StoreClassification.EXACT_CANONICAL_MATCH)

    @property
    def field_mismatches(self) -> tuple[ReportedStore, ...]:
        return self._of(StoreClassification.CANONICAL_FIELD_MISMATCH)

    @property
    def identity_conflicts(self) -> tuple[ReportedStore, ...]:
        return self._of(StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT)

    @property
    def non_canonical(self) -> tuple[ReportedStore, ...]:
        return self._of(
            StoreClassification.KNOWN_LEGACY_DEMO_EXACT_MATCH,
            StoreClassification.CUSTOM_OR_UNKNOWN_NON_CANONICAL,
        )

    @property
    def known_legacy_demo(self) -> tuple[ReportedStore, ...]:
        return self._of(StoreClassification.KNOWN_LEGACY_DEMO_EXACT_MATCH)

    @property
    def custom_or_unknown(self) -> tuple[ReportedStore, ...]:
        return self._of(StoreClassification.CUSTOM_OR_UNKNOWN_NON_CANONICAL)

    @property
    def stores_with_dependencies(self) -> tuple[ReportedStore, ...]:
        return tuple(store for store in self.stores if store.dependencies.has_any)

    @property
    def overall_result(self) -> str:
        clean = (
            not self.missing_canonical
            and not self.field_mismatches
            and not self.identity_conflicts
            and not self.non_canonical
            and not self.duplicate_codes
            and not self.duplicate_full_names
            and self.database_store_count == self.canonical_store_count
        )
        return "EXACT_CANONICAL_MATCH" if clean else "DIFFERENCES_FOUND"

    @property
    def exit_code(self) -> int:
        return (
            EXIT_EXACT_MATCH
            if self.overall_result == "EXACT_CANONICAL_MATCH"
            else EXIT_DIFFERENCES
        )

    def summary(self) -> dict[str, object]:
        return {
            "ambiguous_identity_conflict_count": len(self.identity_conflicts),
            "canonical_store_count": self.canonical_store_count,
            "canonical_zone_count": self.canonical_zone_count,
            "custom_or_unknown_count": len(self.custom_or_unknown),
            "database_store_count": self.database_store_count,
            "duplicate_code_count": len(self.duplicate_codes),
            "duplicate_full_name_count": len(self.duplicate_full_names),
            "exact_match_count": len(self.exact_matches),
            "field_mismatch_count": len(self.field_mismatches),
            "known_legacy_demo_count": len(self.known_legacy_demo),
            "missing_canonical_count": len(self.missing_canonical),
            "overall_result": self.overall_result,
            "stores_with_dependencies_count": len(self.stores_with_dependencies),
        }


# ---------------------------------------------------------------------------
# Safety boundary
# ---------------------------------------------------------------------------
def _resolve(candidate: str | os.PathLike[str]) -> Path:
    return Path(candidate).expanduser().resolve()


def _reject_protected_database(resolved: Path) -> None:
    """Refuse the protected database before any connection is attempted."""
    protected = Path(PROTECTED_DATABASE_PATH).expanduser().resolve()
    if resolved == protected:
        raise ProtectedDatabaseError(
            "the protected SpeakLink database was refused; supply an isolated snapshot"
        )
    try:
        if resolved.exists() and protected.exists() and os.path.samefile(resolved, protected):
            raise ProtectedDatabaseError(
                "the supplied path is the protected SpeakLink database; "
                "it was refused before opening"
            )
    except ProtectedDatabaseError:
        raise
    except OSError:
        # samefile is unavailable or failed. Fall back to a conservative
        # comparison of resolved identity only, which already ran above.
        pass


def _validate_input_path(resolved: Path) -> None:
    if resolved.is_dir():
        raise DatabaseInputError("the supplied path is a directory, not a database file")
    if not resolved.is_file():
        raise DatabaseInputError("the supplied database path does not exist")
    for suffix in ("-wal", "-shm"):
        if Path(str(resolved) + suffix).exists():
            raise UnsafeSnapshotError(
                f"a {suffix.lstrip('-').upper()} file sits beside the snapshot, so it may be "
                "inconsistent; supply a quiesced copy instead. This report never merges "
                "or repairs write-ahead data."
            )


def _open_read_only(resolved: Path) -> sqlite3.Connection:
    uri = f"file:{resolved.as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.execute("PRAGMA query_only = ON")
        connection.execute("PRAGMA journal_mode")  # read-only probe
    except sqlite3.Error as error:
        raise DatabaseInputError(
            "the supplied file could not be opened as a read-only SQLite database"
        ) from error
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _validate_store_schema(connection: sqlite3.Connection) -> None:
    try:
        if not _table_exists(connection, "stores"):
            raise DatabaseSchemaError("the snapshot has no 'stores' table")
        columns = {
            row[1] for row in connection.execute('PRAGMA table_info("stores")')
        }
    except sqlite3.DatabaseError as error:
        raise DatabaseInputError(
            "the supplied file is not a readable SQLite database"
        ) from error
    missing = set(_REQUIRED_STORE_COLUMNS) - columns
    if missing:
        raise DatabaseSchemaError(
            "the 'stores' table is missing required columns: "
            + ", ".join(sorted(missing))
        )


# ---------------------------------------------------------------------------
# Dependency counting
# ---------------------------------------------------------------------------
def _count(connection: sqlite3.Connection, sql: str, store_id: int) -> int:
    return int(connection.execute(sql, (store_id,)).fetchone()[0])


def _dependency_counts(
    connection: sqlite3.Connection,
    store_id: int,
    available: dict[str, bool],
) -> DependencyCounts:
    return DependencyCounts(
        broadcast_targets=(
            _count(
                connection,
                "SELECT COUNT(*) FROM broadcast_targets WHERE store_id = ?",
                store_id,
            )
            if available["broadcast_targets"]
            else 0
        ),
        receiver_events=(
            _count(
                connection,
                "SELECT COUNT(*) FROM receiver_events WHERE store_id = ?",
                store_id,
            )
            if available["receiver_events"]
            else 0
        ),
        receiver_devices=(
            _count(
                connection,
                "SELECT COUNT(*) FROM receiver_devices WHERE store_id = ?",
                store_id,
            )
            if available["receiver_devices"]
            else None
        ),
        receiver_credential_events=(
            _count(
                connection,
                "SELECT COUNT(*) FROM receiver_credential_events WHERE store_id = ?",
                store_id,
            )
            if available["receiver_credential_events"]
            else None
        ),
        receiver_credentials_via_devices=(
            _count(
                connection,
                "SELECT COUNT(*) FROM receiver_credentials c "
                "JOIN receiver_devices d ON d.id = c.device_id WHERE d.store_id = ?",
                store_id,
            )
            if available["receiver_credentials"] and available["receiver_devices"]
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def _recommend(
    classification: StoreClassification, dependencies: DependencyCounts
) -> Recommendation:
    if classification is StoreClassification.EXACT_CANONICAL_MATCH:
        return Recommendation.NO_ACTION
    if classification is StoreClassification.CANONICAL_FIELD_MISMATCH:
        return Recommendation.REVIEW_FIELD_CORRECTION
    if classification is StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT:
        return Recommendation.REVIEW_IDENTITY_CONFLICT
    if classification is StoreClassification.KNOWN_LEGACY_DEMO_EXACT_MATCH:
        # History still points at this Store, so archival is the safe path and
        # deletion is not proposed.
        return (
            Recommendation.REVIEW_ARCHIVAL
            if dependencies.has_any
            else Recommendation.REVIEW_TARGETED_DELETION
        )
    return (
        Recommendation.BLOCKED_BY_DEPENDENCIES
        if dependencies.has_any
        else Recommendation.HUMAN_REVIEW_REQUIRED
    )


def _classify(
    row: sqlite3.Row,
    canonical_by_code: dict,
    canonical_by_full_name: dict,
    duplicate_codes: set[str],
    duplicate_full_names: set[str],
) -> tuple[StoreClassification, tuple[str, ...]]:
    code = row["store_code"]
    full_name = row["store_name"]
    issues: list[str] = []

    entry = canonical_by_code.get(code)
    if entry is not None:
        if entry.full_name == full_name:
            if row["region"] != entry.zone:
                issues.append("wrong_zone")
            if row["city"] != entry.zone:
                issues.append("wrong_city")
            classification = (
                StoreClassification.CANONICAL_FIELD_MISMATCH
                if issues
                else StoreClassification.EXACT_CANONICAL_MATCH
            )
        else:
            issues.append("code_matches_canonical_but_full_name_differs")
            classification = StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT
    elif full_name in canonical_by_full_name:
        issues.append("full_name_matches_canonical_but_code_differs")
        classification = StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT
    else:
        fingerprint = (
            code,
            full_name,
            row["city"],
            row["region"],
            bool(row["is_online_store"]),
        )
        classification = (
            StoreClassification.KNOWN_LEGACY_DEMO_EXACT_MATCH
            if fingerprint in _LEGACY_DEMO_INDEX
            else StoreClassification.CUSTOM_OR_UNKNOWN_NON_CANONICAL
        )

    if code in duplicate_codes:
        issues.append("duplicate_store_code")
        classification = StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT
    if full_name in duplicate_full_names:
        issues.append("duplicate_store_full_name")
        classification = StoreClassification.AMBIGUOUS_IDENTITY_CONFLICT

    return classification, tuple(issues)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def reconcile(database_path: str | os.PathLike[str]) -> ReconciliationReport:
    """Produce a read-only reconciliation report for an isolated snapshot."""
    resolved = _resolve(database_path)
    _reject_protected_database(resolved)
    _validate_input_path(resolved)

    connection = _open_read_only(resolved)
    try:
        connection.row_factory = sqlite3.Row
        _validate_store_schema(connection)

        rows = connection.execute(
            "SELECT id, store_code, store_name, city, region, is_online_store, is_active "
            "FROM stores ORDER BY id"
        ).fetchall()

        available = {
            table: _table_exists(connection, table)
            for table in (
                "broadcast_targets",
                "receiver_events",
                "receiver_devices",
                "receiver_credential_events",
                "receiver_credentials",
            )
        }

        canonical_by_code = {entry.short_name: entry for entry in CANONICAL_STORES}
        canonical_by_full_name = {entry.full_name: entry for entry in CANONICAL_STORES}

        code_counts: dict[str, int] = {}
        name_counts: dict[str, int] = {}
        for row in rows:
            code_counts[row["store_code"]] = code_counts.get(row["store_code"], 0) + 1
            name_counts[row["store_name"]] = name_counts.get(row["store_name"], 0) + 1
        duplicate_codes = {code for code, count in code_counts.items() if count > 1}
        duplicate_full_names = {
            name for name, count in name_counts.items() if count > 1
        }

        reported: list[ReportedStore] = []
        for row in rows:
            classification, issues = _classify(
                row,
                canonical_by_code,
                canonical_by_full_name,
                duplicate_codes,
                duplicate_full_names,
            )
            dependencies = _dependency_counts(connection, row["id"], available)
            reported.append(
                ReportedStore(
                    store_id=row["id"],
                    store_code=row["store_code"],
                    store_name=row["store_name"],
                    city=row["city"],
                    region=row["region"],
                    is_online_store=bool(row["is_online_store"]),
                    is_active=bool(row["is_active"]),
                    classification=classification,
                    issues=issues,
                    dependencies=dependencies,
                    recommendation=_recommend(classification, dependencies),
                )
            )
    finally:
        connection.close()

    present_codes = {row["store_code"] for row in rows}
    missing = tuple(
        MissingCanonicalStore(
            catalog_position=position,
            zone=entry.zone,
            short_name=entry.short_name,
            full_name=entry.full_name,
            # seed.py stores the Zone display name in both fields because the
            # approved source supplies no separate city data.
            expected_city=entry.zone,
            expected_region=entry.zone,
            recommendation=Recommendation.ADD_MISSING_CANONICAL_STORE_LATER,
        )
        for position, entry in enumerate(CANONICAL_STORES, start=1)
        if entry.short_name not in present_codes
    )

    return ReconciliationReport(
        database_path=str(resolved),
        database_store_count=len(rows),
        canonical_zone_count=len(CANONICAL_ZONES),
        canonical_store_count=len(CANONICAL_STORES),
        stores=tuple(reported),
        missing_canonical=missing,
        duplicate_codes=tuple(sorted(duplicate_codes)),
        duplicate_full_names=tuple(sorted(duplicate_full_names)),
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def render_json(report: ReconciliationReport) -> str:
    payload = {
        "duplicate_full_names": list(report.duplicate_full_names),
        "duplicate_store_codes": list(report.duplicate_codes),
        "missing_canonical": [item.as_dict() for item in report.missing_canonical],
        "stores": [store.as_dict() for store in report.stores],
        "summary": report.summary(),
    }
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)


def _dependency_text(dependencies: DependencyCounts) -> str:
    parts = []
    for name, value in sorted(dependencies.as_dict().items()):
        parts.append(f"{name}={'n/a' if value is None else value}")
    return " ".join(parts)


def render_text(report: ReconciliationReport) -> str:
    lines: list[str] = []
    summary = report.summary()
    lines.append("SpeakLink Store Catalog Reconciliation Report (read-only)")
    lines.append(f"Snapshot: {report.database_path}")
    lines.append("")
    lines.append("== Summary ==")
    for key in sorted(summary):
        lines.append(f"  {key}: {summary[key]}")
    lines.append("")

    lines.append(f"== Exact canonical matches ({len(report.exact_matches)}) ==")
    for store in report.exact_matches:
        lines.append(
            f"  id={store.store_id} code={store.store_code} "
            f"name={store.store_name} zone={store.region} city={store.city} "
            f"[{store.classification.value}]"
        )
    lines.append("")

    lines.append(f"== Missing canonical Stores ({len(report.missing_canonical)}) ==")
    for item in report.missing_canonical:
        lines.append(
            f"  #{item.catalog_position} zone={item.zone} code={item.short_name} "
            f"name={item.full_name} expected_city={item.expected_city} "
            f"expected_zone={item.expected_region} -> {item.recommendation.value}"
        )
    lines.append("")

    conflicts = report.field_mismatches + report.identity_conflicts
    lines.append(f"== Field and identity conflicts ({len(conflicts)}) ==")
    for store in conflicts:
        lines.append(
            f"  id={store.store_id} code={store.store_code} name={store.store_name} "
            f"zone={store.region} city={store.city} [{store.classification.value}] "
            f"issues={','.join(store.issues) or 'none'} -> {store.recommendation.value}"
        )
        lines.append(f"      dependencies: {_dependency_text(store.dependencies)}")
    lines.append("")

    lines.append(f"== Non-canonical Stores ({len(report.non_canonical)}) ==")
    for store in report.non_canonical:
        lines.append(
            f"  id={store.store_id} code={store.store_code} name={store.store_name} "
            f"zone={store.region} city={store.city} [{store.classification.value}] "
            f"-> {store.recommendation.value}"
        )
        lines.append(f"      dependencies: {_dependency_text(store.dependencies)}")
    lines.append("")

    if report.duplicate_codes:
        lines.append(f"Duplicate Store codes: {', '.join(report.duplicate_codes)}")
    if report.duplicate_full_names:
        lines.append(f"Duplicate full names: {', '.join(report.duplicate_full_names)}")
    lines.append("")
    lines.append(f"Overall result: {report.overall_result}")
    lines.append(
        "This report changed nothing. Recommendations are for human review only "
        "and are never executed here."
    )
    lines.append(
        "Catalog presence proves only that HQ knows the Store. It is not evidence "
        "of CONNECTED, READY, AUDIO_RECEIVING, PLAYBACK_CONFIRMED or SPEAKER_VERIFIED."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="store_catalog_reconciliation",
        description=(
            "Read-only comparison of an isolated SQLite snapshot against the "
            "canonical SpeakLink Store catalog. Changes nothing."
        ),
    )
    parser.add_argument(
        "--database",
        required=True,
        help="Path to an operator-supplied isolated snapshot. The protected "
        "application database is refused.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        report = reconcile(arguments.database)
    except ReconciliationError as error:
        print(f"Reconciliation refused: {error}", file=sys.stderr)
        return EXIT_FAILURE

    rendered = (
        render_json(report) if arguments.format == "json" else render_text(report)
    )
    print(rendered)
    return report.exit_code


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
