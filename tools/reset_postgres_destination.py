"""Empty the SpeakLink tables in a PostgreSQL destination, and nothing else.

WHY THIS EXISTS, AND WHY IT IS THIS NARROW

``migrate_sqlite_to_postgres.py`` copies into an EMPTY destination. It has no
delta mode, and ``--force`` only INSERTs - it never replaces - so running it
against a destination that already holds an older snapshot produces primary
key collisions, not a refresh. The production Supabase project holds exactly
such an older snapshot.

The obvious fix is to type ``DROP SCHEMA public CASCADE`` at a production
database. This project has already learned what that class of accident costs:
an earlier test fixture lost its ``search_path`` and created nineteen tables
in the production ``public`` schema while reporting success. Hand-typed
destructive SQL against production is the same failure wearing a different
hat.

So this tool exists instead, and it is deliberately NOT a general database
utility:

* it knows the SpeakLink table inventory by name and will touch nothing else;
* it refuses outright if the destination contains a public table it does not
  recognise - an unexpected table means the operator's mental model and the
  database disagree, and that is a reason to stop, not to guess;
* it never issues DROP. It DELETEs rows and leaves every table, the ``public``
  schema itself, and every Supabase-managed schema exactly where they were;
* dry-run is the DEFAULT. Doing nothing is what an unadorned invocation does;
* a real reset requires the typed confirmation ``RESET`` and an explicit
  ``--i-understand-this-deletes-rows``;
* it prints table names and row counts, and never a URL, host, user or
  password.

WHAT IT DOES NOT DO

It does not decide whether the destination is the right database. It reports
the project fingerprint and requires the caller to pass the one they expect
via ``--expect-fingerprint``; a mismatch is a hard stop. Deciding is the
operator's job, and making the tool guess would remove the one check that
cannot be automated away.

Usage:

    python tools/reset_postgres_destination.py --expect-fingerprint <fp>
    python tools/reset_postgres_destination.py --expect-fingerprint <fp> \
        --confirm RESET --i-understand-this-deletes-rows

``DATABASE_URL`` is read from the environment only, never from a CLI argument,
so a password cannot land in a process listing or a shell history file.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))


#: Every table SpeakLink owns, in the order the migration tool populates them
#: (FK-safe parents first). Deleting runs in the REVERSE of this order, so a
#: child row never outlives the parent it points at.
#:
#: Imported rather than restated: one inventory, not two that can drift.
def _speaklink_tables() -> list[str]:
    from migrate_sqlite_to_postgres import TABLE_ORDER  # noqa: E402
    return list(TABLE_ORDER)


#: Schemas this tool must never look at, let alone write to. Supabase owns
#: these; damaging one breaks the project itself, not just SpeakLink's data.
PROTECTED_SCHEMAS = frozenset({
    "auth", "storage", "realtime", "vault", "extensions", "graphql",
    "supabase_migrations", "pg_catalog", "information_schema", "pgsodium",
    "pgbouncer", "cron", "net",
})

CONFIRMATION_WORD = "RESET"


class ResetRefused(RuntimeError):
    """Raised for every refusal. Never carries a URL or a password."""


def fingerprint_of(database_url: str) -> str:
    """The non-secret project fingerprint: sha256 of the pooler username.

    Matches the convention recorded in docs/SUPABASE_PRODUCTION_CUTOVER.md -
    the FULL username (``postgres.<project-ref>``), not the bare project-ref.
    Hashing the ref alone yields a different value and reads as a mismatch on
    a project that is in fact correct.
    """
    username = urlsplit(database_url).username or ""
    return hashlib.sha256(username.encode()).hexdigest()[:16]


def _engine():
    """Build the engine through the same loader production uses."""
    from db_config import load_database_config
    from sqlalchemy import create_engine

    url = os.environ.get("DATABASE_URL")
    if not url or not url.strip():
        raise ResetRefused(
            "DATABASE_URL is not set. This tool reads it from the environment "
            "only, so a password never reaches a command line."
        )
    config = load_database_config(app_env="production", database_url=url)
    return create_engine(config.url, future=True), url


#: The schema this tool operates on. Named explicitly and NOT resolved through
#: ``search_path``, because a lost or reset search_path is precisely how an
#: earlier test fixture created nineteen tables in the production ``public``
#: schema while reporting success. Every statement below is schema-qualified.
#:
#: It is a parameter only so the test suite can point it at its own generated
#: schema. It is deliberately NOT a command-line flag: an operator has no
#: legitimate reason to reset a schema other than public, and offering the
#: choice would turn a safety property into a footgun.
DEFAULT_SCHEMA = "public"


def inspect_destination(engine, *, schema: str = DEFAULT_SCHEMA
                        ) -> tuple[dict[str, int], list[str], list[str]]:
    """Report what is in the destination schema, without changing anything.

    Returns ``(counts, missing, unexpected)``:

    * ``counts``   - row count per SpeakLink table that exists
    * ``missing``  - SpeakLink tables not present (fine: a fresh project)
    * ``unexpected`` - tables this tool does not recognise (NOT fine)
    """
    from sqlalchemy import inspect, text

    known = _speaklink_tables()
    present = set(inspect(engine).get_table_names(schema=schema))

    counts: dict[str, int] = {}
    missing: list[str] = []
    with engine.connect() as connection:
        for table in known:
            if table in present:
                counts[table] = connection.execute(
                    text(f'SELECT count(*) FROM "{schema}"."{table}"')).scalar_one()
            else:
                missing.append(table)

    unexpected = sorted(present - set(known))
    return counts, missing, unexpected


def managed_schema_snapshot(engine) -> dict[str, int]:
    """Table counts for the Supabase-managed schemas, for a before/after proof.

    This tool never touches them. Recording the numbers either side of the
    reset is how that stops being a claim and becomes evidence.
    """
    from sqlalchemy import text

    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT table_schema, count(*) FROM information_schema.tables "
            "WHERE table_schema = ANY(:schemas) GROUP BY table_schema "
            "ORDER BY table_schema"
        ), {"schemas": sorted(PROTECTED_SCHEMAS)}).all()
    return {schema: count for schema, count in rows}


def reset(engine, *, counts: dict[str, int], schema: str = DEFAULT_SCHEMA
          ) -> dict[str, int]:
    """DELETE every row from the SpeakLink tables, children first.

    One transaction: if any statement fails, nothing is deleted. DELETE and
    not TRUNCATE, and certainly not DROP - the tables, their constraints and
    their indexes all survive, so the destination stays ready for the
    migration tool without re-running any DDL.
    """
    from sqlalchemy import text

    deleted: dict[str, int] = {}
    with engine.begin() as connection:
        # Reverse of the FK-safe creation order, so no child row is orphaned
        # even for the instant between two statements.
        for table in reversed(_speaklink_tables()):
            if table not in counts:
                continue
            result = connection.execute(text(f'DELETE FROM "{schema}"."{table}"'))
            deleted[table] = result.rowcount if result.rowcount is not None else counts[table]
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-fingerprint", required=True,
                        help="The project fingerprint you intend to reset. A "
                             "mismatch stops the tool.")
    parser.add_argument("--confirm", default="",
                        help=f"Type {CONFIRMATION_WORD} to perform a real reset.")
    parser.add_argument("--i-understand-this-deletes-rows", action="store_true",
                        help="Required alongside --confirm for a real reset.")
    args = parser.parse_args(argv)

    try:
        engine, raw_url = _engine()
    except ResetRefused as refusal:
        print(f"REFUSED: {refusal}")
        return 2

    actual = fingerprint_of(raw_url)
    print("=== SpeakLink PostgreSQL destination reset ===")
    print(f"  destination fingerprint : {actual}")
    print(f"  expected                : {args.expect_fingerprint}")
    if actual != args.expect_fingerprint:
        print("REFUSED: this is not the project you named. Nothing was read or changed.")
        engine.dispose()
        return 2
    print("  fingerprint             : MATCH")

    counts, missing, unexpected = inspect_destination(engine)
    managed_before = managed_schema_snapshot(engine)

    print()
    print("  SpeakLink tables carrying rows:")
    carrying = {t: n for t, n in counts.items() if n}
    if carrying:
        for table, number in carrying.items():
            print(f"    {table:<34} {number:>8} row(s)")
    else:
        print("    (none - the destination is already empty)")
    print(f"  SpeakLink tables present but empty : {sum(1 for n in counts.values() if not n)}")
    print(f"  SpeakLink tables not present yet   : {len(missing)}"
          f"{' ' + str(missing) if missing else ''}")

    print()
    print("  Supabase-managed schemas (never touched by this tool):")
    for schema, number in managed_before.items():
        print(f"    {schema:<34} {number:>8} table(s)")

    if unexpected:
        print()
        print("  UNEXPECTED public tables:")
        for table in unexpected:
            print(f"    {table}")
        print("REFUSED: this destination holds public tables SpeakLink does not own. "
              "That means the database is not what this tool assumes. Nothing was "
              "changed. Inspect it before running any reset.")
        engine.dispose()
        return 2

    real = (args.confirm == CONFIRMATION_WORD
            and args.i_understand_this_deletes_rows)
    if not real:
        print()
        print("DRY RUN - nothing was changed.")
        if args.confirm and args.confirm != CONFIRMATION_WORD:
            print(f"(--confirm must be exactly {CONFIRMATION_WORD})")
        print(f"To perform the reset: --confirm {CONFIRMATION_WORD} "
              "--i-understand-this-deletes-rows")
        engine.dispose()
        return 0

    print()
    print(f"  performing reset of {len(carrying)} table(s) carrying rows...")
    deleted = reset(engine, counts=counts)

    after, _, _ = inspect_destination(engine)
    managed_after = managed_schema_snapshot(engine)
    remaining = {t: n for t, n in after.items() if n}

    print()
    for table, number in deleted.items():
        if number:
            print(f"    cleared {table:<32} {number:>8} row(s)")
    print(f"  SpeakLink rows remaining : {sum(after.values())}")
    print(f"  managed schemas unchanged : {managed_before == managed_after}")
    engine.dispose()

    if remaining:
        print(f"FAILED: rows remain in {sorted(remaining)}")
        return 1
    if managed_before != managed_after:
        print("FAILED: a Supabase-managed schema changed. Investigate immediately.")
        return 1

    print()
    print("SPEAKLINK_DESTINATION_RESET_COMPLETE")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
