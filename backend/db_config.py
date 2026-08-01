"""One authoritative place that decides which database EchoCast talks to.

Two environments, two very different failure philosophies:

DEVELOPMENT (``APP_ENV`` unset or ``development``)
    Easy by default. No ``DATABASE_URL``? Use the local SQLite file this
    codebase has always used - no internet connection, no external service,
    no configuration step required to run a test or start a dev server.

PRODUCTION (``APP_ENV=production``)
    Fail closed. ``DATABASE_URL`` is REQUIRED. A production process that
    cannot find it must refuse to start, never silently fall back to a local
    SQLite file - that file would look like it works, accumulate real data
    nobody intended to put there, and diverge from the actual production
    database with no error ever raised. This is the one behavior in this
    module that is not allowed to have an escape hatch.

Nothing here knows a Supabase hostname, project id, username or password -
those live in ``DATABASE_URL`` itself, supplied by the environment (see
``tools/hq_runtime.py`` and ``docs/SUPABASE_PRODUCTION_CUTOVER.md`` for how
it reaches that environment on the Windows HQ machine without ever being
committed to Git).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SQLITE_PATH = Path(__file__).resolve().parent / "echocast_live.db"


class DatabaseConfigError(RuntimeError):
    """Startup must stop. Never carries the URL or password."""


@dataclass(frozen=True)
class DatabaseConfig:
    url: str
    #: 'sqlite' or 'postgresql' - never anything callers have to parse the
    #: URL themselves to learn.
    dialect: str
    #: Only meaningful for sqlite; None for postgresql.
    sqlite_path: Path | None


def _sqlite_config() -> DatabaseConfig:
    configured = os.environ.get("ECHOCAST_DB_PATH")
    path = (
        Path(configured).expanduser().resolve()
        if configured
        else DEFAULT_SQLITE_PATH
    )
    return DatabaseConfig(
        url=f"sqlite:///{path.as_posix()}", dialect="sqlite", sqlite_path=path,
    )


def _dialect_of(url: str) -> str:
    # "postgresql://", "postgresql+psycopg://", "postgres://" (some providers'
    # dashboards still hand out the legacy scheme) all mean the same engine.
    scheme = url.split("://", 1)[0].split("+", 1)[0]
    if scheme in ("postgres", "postgresql"):
        return "postgresql"
    if scheme == "sqlite":
        return "sqlite"
    raise DatabaseConfigError(
        f"Unsupported DATABASE_URL scheme {scheme!r}. Only sqlite and "
        "postgresql are supported."
    )


def _normalized_postgres_url(url: str) -> str:
    """Force the psycopg 3 driver explicitly rather than trusting whatever
    SQLAlchemy would otherwise guess, and require encrypted transport - a
    Supabase Session Pooler URL already includes ?sslmode=require, but a
    hand-typed one might not, and a production database credential must
    never travel unencrypted even on a LAN."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    if "sslmode=" not in url:
        separator = "&" if "?" in url else "?"
        url = f"{url}{separator}sslmode=require"
    return url


def load_database_config(*, app_env: str | None = None,
                         database_url: str | None = None) -> DatabaseConfig:
    """The one function every entry point (db.py, the migration tool, tests)
    calls instead of reading ``os.environ`` directly - so "what decides the
    database" has exactly one answer, not one per caller.

    Parameters default to reading the real environment; tests pass them
    explicitly so this is provable without ``monkeypatch``-ing globals.
    """
    env = (app_env if app_env is not None else os.environ.get("APP_ENV", "development")).strip().lower()
    url = database_url if database_url is not None else os.environ.get("DATABASE_URL")

    if env not in ("development", "production", "test"):
        raise DatabaseConfigError(
            f"Unknown APP_ENV={env!r}. Expected 'development', 'production' or 'test'."
        )

    if env == "production":
        if not url or not url.strip():
            # The one deliberately unconditional refusal in this module: a
            # production process with no DATABASE_URL must never guess, and
            # must never fall back to a file on the local disk that looks
            # like it works.
            raise DatabaseConfigError(
                "APP_ENV=production but DATABASE_URL is not set. Production "
                "never falls back to a local SQLite database - configure "
                "DATABASE_URL (see docs/SUPABASE_PRODUCTION_CUTOVER.md) "
                "before starting."
            )
        dialect = _dialect_of(url)
        if dialect != "postgresql":
            raise DatabaseConfigError(
                f"APP_ENV=production requires a postgresql:// DATABASE_URL, "
                f"got a {dialect} URL."
            )
        return DatabaseConfig(
            url=_normalized_postgres_url(url), dialect="postgresql", sqlite_path=None,
        )

    # development or test: an explicit DATABASE_URL is honored (so a
    # developer CAN point local work at a real Postgres/Supabase instance to
    # test that path), but its absence is never an error - the local SQLite
    # file is the default, exactly as it always has been.
    if url and url.strip():
        dialect = _dialect_of(url)
        if dialect == "postgresql":
            return DatabaseConfig(
                url=_normalized_postgres_url(url), dialect="postgresql", sqlite_path=None,
            )
        return _sqlite_config()
    return _sqlite_config()


__all__ = ["DatabaseConfig", "DatabaseConfigError", "load_database_config"]
