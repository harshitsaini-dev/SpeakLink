"""Remove the exact legacy demo catalog from a persistent HQ database.

WHY THIS EXISTS

The persistent HQ was seeded from an original 13-Store demo catalog. The approved
44-Store catalog was later cut over, and the demo rows were *archived* rather than
removed - correct at the time, because archiving is reversible and deletion is
not. The operator has since authorised their removal, so this tool performs it in
a way that can be reviewed, tested, dry-run and repeated.

WHAT MAKES IT SAFE

**Complete fingerprints, never a name match.** A row is legacy only when its id,
code AND name all agree with the recorded fingerprint. "Delete anything called
Mumbai" is how a real Store gets deleted the day somebody opens one in Mumbai.

**Fail closed on anything unrecognised.** An unknown Store, a session whose targets
span both catalogs, an event pointing at neither - any of these aborts before a
single row is written. A purge that "handles" a surprise is a purge that deletes
something nobody classified.

**One transaction.** Child rows before parents, and either all of it happens or
none of it does.

**Idempotent.** A second run finds nothing to do and says NO_CHANGES_REQUIRED
rather than failing or half-working.

WHAT IT NEVER TOUCHES

Canonical Stores, their ids, HQ users, passwords, Receiver Devices or credentials
belonging to canonical Stores, enrolment codes for canonical Stores, the key
container, and system_logs - which are retained in full, because a log line's
identity cannot be proved from its text and losing operational history to tidy a
catalog is a bad trade.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: The exact 13 rows, by COMPLETE immutable fingerprint. id, code and name must
#: all match. Anything else is UNKNOWN and stops the run.
LEGACY_FINGERPRINTS: "frozenset[tuple[int, str, str]]" = frozenset({
    (1, "MUM-001", "Mumbai Andheri Flagship"),
    (2, "MUM-002", "Mumbai Bandra Outlet"),
    (3, "PUN-001", "Pune Koregaon Park"),
    (4, "DEL-001", "Delhi Connaught Place"),
    (5, "DEL-002", "Delhi Saket Mall"),
    (6, "GUR-001", "Gurgaon Cyber Hub"),
    (7, "BLR-001", "Bangalore MG Road"),
    (8, "BLR-002", "Bangalore Whitefield"),
    (9, "HYD-001", "Hyderabad Banjara Hills"),
    (10, "CHN-001", "Chennai T. Nagar"),
    (11, "KOL-001", "Kolkata Park Street"),
    (12, "ONL-001", "Online Store - Web"),
    (13, "ONL-002", "Online Store - App"),
})

NO_CHANGES = "NO_CHANGES_REQUIRED"
PURGED = "SPEAKLINK_LEGACY_CATALOG_PURGED"


class PurgeRefused(Exception):
    """Nothing was written. The message says exactly what was not understood."""


@dataclass
class PurgePlan:
    """Everything that would be deleted, by id. Reviewable before anything runs."""

    store_ids: list = field(default_factory=list)
    session_ids: list = field(default_factory=list)
    target_ids: list = field(default_factory=list)
    event_count: int = 0
    enrollment_code_ids: list = field(default_factory=list)
    device_ids: list = field(default_factory=list)
    credential_ids: list = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.store_ids or self.session_ids or self.target_ids
                    or self.event_count or self.enrollment_code_ids
                    or self.device_ids or self.credential_ids)

    def describe(self) -> str:
        return "\n".join([
            f"  stores            : {len(self.store_ids):>5}  ids={self.store_ids}",
            f"  broadcast_sessions: {len(self.session_ids):>5}  ids={self.session_ids}",
            f"  broadcast_targets : {len(self.target_ids):>5}",
            f"  receiver_events   : {self.event_count:>5}",
            f"  enrollment_codes  : {len(self.enrollment_code_ids):>5}  ids={self.enrollment_code_ids}",
            f"  receiver_devices  : {len(self.device_ids):>5}  ids={self.device_ids}",
            f"  credentials       : {len(self.credential_ids):>5}  ids={self.credential_ids}",
        ])


def canonical_codes() -> "set[str]":
    sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))
    from store_catalog import CANONICAL_STORES

    return {entry.short_name for entry in CANONICAL_STORES}


def _table_exists(connection, name: str) -> bool:
    return bool(connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def build_plan(connection) -> PurgePlan:
    """Classify every row, or refuse. Reads only."""
    connection.row_factory = sqlite3.Row
    approved = canonical_codes()

    rows = list(connection.execute("SELECT id, store_code, store_name FROM stores"))
    legacy_ids, canonical_ids, unknown = set(), set(), []
    for row in rows:
        fingerprint = (row["id"], row["store_code"], row["store_name"])
        if fingerprint in LEGACY_FINGERPRINTS:
            legacy_ids.add(row["id"])
        elif row["store_code"] in approved:
            canonical_ids.add(row["id"])
        else:
            unknown.append(f"id={row['id']} code={row['store_code']} name={row['store_name']}")

    if unknown:
        raise PurgeRefused(
            "these Store rows match neither the approved catalog nor a recorded "
            "legacy fingerprint, so nothing has been deleted:\n  " + "\n  ".join(unknown)
        )

    plan = PurgePlan(store_ids=sorted(legacy_ids))
    if not legacy_ids:
        return plan  # already clean

    missing = approved - {r["store_code"] for r in rows if r["id"] in canonical_ids}
    if missing:
        raise PurgeRefused(
            f"{len(missing)} approved Store code(s) are absent from this database, so "
            f"it is not the catalog this tool was written for: {sorted(missing)}"
        )

    # ---- sessions and targets -------------------------------------------------
    targets = list(connection.execute("SELECT id, session_id, store_id FROM broadcast_targets"))
    by_session = {}
    for t in targets:
        by_session.setdefault(t["session_id"], set()).add(t["store_id"])

    mixed = []
    for session_id, stores in by_session.items():
        if stores & legacy_ids and not stores <= legacy_ids:
            mixed.append(session_id)
    if mixed:
        raise PurgeRefused(
            f"broadcast session(s) {sorted(mixed)} target BOTH legacy and approved "
            "Stores. That is real history mixed with test history and this tool will "
            "not guess which is which. Nothing has been deleted."
        )

    plan.session_ids = sorted(s for s, stores in by_session.items() if stores <= legacy_ids)
    plan.target_ids = sorted(t["id"] for t in targets if t["store_id"] in legacy_ids)

    stray = [t["id"] for t in targets
             if t["store_id"] not in legacy_ids and t["store_id"] not in canonical_ids]
    if stray:
        raise PurgeRefused(
            f"{len(stray)} broadcast target(s) reference a Store in neither set. "
            "Nothing has been deleted."
        )

    # ---- events ---------------------------------------------------------------
    plan.event_count = connection.execute(
        f"SELECT COUNT(*) FROM receiver_events WHERE store_id IN "
        f"({','.join('?' * len(legacy_ids))})", tuple(sorted(legacy_ids))).fetchone()[0]
    orphan_events = connection.execute(
        "SELECT COUNT(*) FROM receiver_events WHERE store_id NOT IN "
        f"({','.join('?' * len(legacy_ids | canonical_ids))})",
        tuple(sorted(legacy_ids | canonical_ids))).fetchone()[0]
    if orphan_events:
        raise PurgeRefused(
            f"{orphan_events} Receiver event(s) reference a Store in neither set. "
            "Nothing has been deleted."
        )

    # ---- devices, credentials, enrolment codes --------------------------------
    if _table_exists(connection, "receiver_devices"):
        plan.device_ids = sorted(
            r["id"] for r in connection.execute("SELECT id, store_id FROM receiver_devices")
            if r["store_id"] in legacy_ids)
        if plan.device_ids and _table_exists(connection, "receiver_credentials"):
            marks = ",".join("?" * len(plan.device_ids))
            plan.credential_ids = sorted(
                r[0] for r in connection.execute(
                    f"SELECT id FROM receiver_credentials WHERE device_id IN ({marks})",
                    tuple(plan.device_ids)))

    if _table_exists(connection, "receiver_enrollment_codes"):
        plan.enrollment_code_ids = sorted(
            r["id"] for r in connection.execute(
                "SELECT id, store_id FROM receiver_enrollment_codes")
            if r["store_id"] in legacy_ids)

    return plan


def apply_plan(connection, plan: PurgePlan) -> None:
    """Child rows before parents, in one transaction, with foreign keys enforced."""
    if plan.empty:
        return
    connection.execute("PRAGMA foreign_keys = ON")
    cursor = connection.cursor()
    try:
        cursor.execute("BEGIN")
        marks = ",".join("?" * len(plan.store_ids))
        stores = tuple(plan.store_ids)

        if plan.credential_ids:
            cursor.execute(
                f"DELETE FROM receiver_credentials WHERE id IN "
                f"({','.join('?' * len(plan.credential_ids))})", tuple(plan.credential_ids))
        if plan.device_ids:
            cursor.execute(
                f"DELETE FROM receiver_devices WHERE id IN "
                f"({','.join('?' * len(plan.device_ids))})", tuple(plan.device_ids))
        if plan.enrollment_code_ids:
            cursor.execute(
                f"DELETE FROM receiver_enrollment_codes WHERE id IN "
                f"({','.join('?' * len(plan.enrollment_code_ids))})",
                tuple(plan.enrollment_code_ids))
        cursor.execute(f"DELETE FROM receiver_events WHERE store_id IN ({marks})", stores)
        cursor.execute(f"DELETE FROM broadcast_targets WHERE store_id IN ({marks})", stores)
        if plan.session_ids:
            cursor.execute(
                f"DELETE FROM broadcast_sessions WHERE id IN "
                f"({','.join('?' * len(plan.session_ids))})", tuple(plan.session_ids))
        cursor.execute(f"DELETE FROM stores WHERE id IN ({marks})", stores)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def verify(connection) -> dict:
    connection.row_factory = sqlite3.Row
    approved = canonical_codes()
    codes = {r["store_code"] for r in connection.execute("SELECT store_code FROM stores")}
    total = connection.execute("SELECT COUNT(*) FROM stores").fetchone()[0]
    zones = connection.execute("SELECT COUNT(DISTINCT region) FROM stores").fetchone()[0]
    return {
        "stores_total": total,
        "zones": zones,
        "codes_match_catalog": codes == approved,
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_violations": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
        "users": [r["username"] for r in connection.execute(
            "SELECT username FROM hq_users ORDER BY id")],
        "devices": [(r["id"], r["store_id"]) for r in connection.execute(
            "SELECT id, store_id FROM receiver_devices ORDER BY id")]
            if _table_exists(connection, "receiver_devices") else [],
    }


def backup(source: Path, destination: Path) -> str:
    """A consistent copy through SQLite's backup API, never a file copy."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(source))
    dst = sqlite3.connect(str(destination))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    check = sqlite3.connect(f"file:{destination.as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        state = check.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        check.close()
    if state != "ok":
        raise PurgeRefused(f"the backup at {destination} failed its own integrity check")
    return hashlib.sha256(destination.read_bytes()).hexdigest().upper()


def default_database() -> Path:
    from tools.persistent_lan_server import ServerProfile

    return ServerProfile.persistent().database


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", help="defaults to the persistent HQ database")
    parser.add_argument("--apply", action="store_true",
                        help="perform the deletion; without this it is a dry run")
    args = parser.parse_args(argv)

    if args.database:
        database = Path(args.database)
    else:
        sys.path.insert(0, str(REPOSITORY_ROOT))
        database = default_database()

    print("=== SpeakLink legacy catalog purge ===")
    print(f"  database : {database}")
    print(f"  mode     : {'APPLY' if args.apply else 'DRY RUN - nothing will be written'}")
    if not database.exists():
        print(f"REFUSED: there is no database at {database}")
        return 2

    read_uri = f"file:{database.as_posix()}?mode=ro"
    connection = sqlite3.connect(read_uri, uri=True)
    try:
        plan = build_plan(connection)
    except PurgeRefused as refusal:
        print(f"\nREFUSED: {refusal}")
        return 2
    finally:
        connection.close()

    if plan.empty:
        print(f"\n{NO_CHANGES}")
        return 0

    print("\nwould delete:")
    print(plan.describe())

    if not args.apply:
        print("\nDRY RUN complete. Re-run with --apply to perform it.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    before = database.parent.parent / "backups" / f"speaklink-before-legacy-purge-{stamp}.db"
    print(f"\n  backup before: {before}")
    print(f"  sha256       : {backup(database, before)}")

    connection = sqlite3.connect(str(database))
    try:
        apply_plan(connection, plan)
        result = verify(connection)
    finally:
        connection.close()

    after = database.parent.parent / "backups" / f"speaklink-after-legacy-purge-{stamp}.db"
    print(f"  backup after : {after}")
    print(f"  sha256       : {backup(database, after)}")

    print("\nverification:")
    for key, value in result.items():
        print(f"  {key:<24} {value}")

    ok = (result["stores_total"] == len(canonical_codes())
          and result["codes_match_catalog"]
          and result["integrity"] == "ok"
          and result["foreign_key_violations"] == 0)
    print(f"\n{PURGED if ok else 'PURGE COMPLETED BUT VERIFICATION FAILED'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
