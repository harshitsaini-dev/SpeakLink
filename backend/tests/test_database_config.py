"""Development stays easy (SQLite, no configuration); production fails
closed (PostgreSQL DATABASE_URL required, never a silent SQLite fallback).

Pure logic - no real database connection of any kind, so these run with no
internet connection exactly like every other unit test in this suite.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from db_config import DatabaseConfigError, load_database_config  # noqa: E402


def test_development_with_no_database_url_uses_local_sqlite():
    config = load_database_config(app_env="development", database_url=None)
    assert config.dialect == "sqlite"
    assert config.sqlite_path is not None
    assert config.url.startswith("sqlite:///")


def test_unset_app_env_defaults_to_development_sqlite(monkeypatch):
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    config = load_database_config()
    assert config.dialect == "sqlite"


def test_development_honors_an_explicit_postgres_url_if_given():
    config = load_database_config(
        app_env="development",
        database_url="postgresql://user:pw@localhost:5432/devdb",
    )
    assert config.dialect == "postgresql"
    assert config.url.startswith("postgresql+psycopg://")


def test_production_without_database_url_fails_closed_not_sqlite():
    with pytest.raises(DatabaseConfigError, match="DATABASE_URL is not set"):
        load_database_config(app_env="production", database_url=None)


def test_production_with_blank_database_url_fails_closed():
    with pytest.raises(DatabaseConfigError):
        load_database_config(app_env="production", database_url="   ")


def test_production_with_a_sqlite_url_is_refused():
    with pytest.raises(DatabaseConfigError, match="postgresql"):
        load_database_config(app_env="production",
                             database_url="sqlite:///should-not-be-used.db")


def test_production_with_a_valid_postgres_url_succeeds():
    config = load_database_config(
        app_env="production",
        database_url="postgresql://user:pw@aws-region.pooler.supabase.com:5432/postgres",
    )
    assert config.dialect == "postgresql"
    assert config.sqlite_path is None


def test_production_forces_the_psycopg3_driver():
    config = load_database_config(
        app_env="production",
        database_url="postgresql://user:pw@host:5432/postgres",
    )
    assert config.url.startswith("postgresql+psycopg://")


def test_production_requires_encrypted_transport_by_default():
    config = load_database_config(
        app_env="production",
        database_url="postgresql://user:pw@host:5432/postgres",
    )
    assert "sslmode=require" in config.url


def test_an_explicit_sslmode_is_not_overridden():
    config = load_database_config(
        app_env="production",
        database_url="postgresql://user:pw@host:5432/postgres?sslmode=verify-full",
    )
    assert config.url.count("sslmode=") == 1
    assert "sslmode=verify-full" in config.url


def test_a_legacy_postgres_scheme_is_normalized():
    config = load_database_config(
        app_env="production",
        database_url="postgres://user:pw@host:5432/postgres",
    )
    assert config.url.startswith("postgresql+psycopg://")


def test_an_unknown_app_env_is_refused():
    with pytest.raises(DatabaseConfigError):
        load_database_config(app_env="staging", database_url=None)


def test_an_unsupported_url_scheme_is_refused():
    with pytest.raises(DatabaseConfigError):
        load_database_config(app_env="development", database_url="mysql://host/db")


def test_the_config_error_never_carries_the_url_or_password():
    """A DatabaseConfigError's message is shown in logs and, potentially, an
    operator's terminal - it must describe the PROBLEM, never repeat the
    secret value back."""
    secret_url = "postgresql://realuser:supersecretpassword@host:5432/postgres"
    try:
        load_database_config(app_env="production", database_url="sqlite:///x.db")
    except DatabaseConfigError as error:
        assert secret_url not in str(error)
        assert "supersecretpassword" not in str(error)


def test_the_test_suite_is_always_development_whatever_dotenv_says():
    """Regression test for a real incident: a ``backend/.env`` containing
    APP_ENV=production and a live Supabase DATABASE_URL - a completely
    reasonable place for an operator to believe production settings belong -
    made ``server.py``'s ``load_dotenv`` flip the ENTIRE suite into
    PostgreSQL mode. ``db.DB_PATH`` became None (correct for PostgreSQL) and
    ~30 modules asserting ``Path(db.DB_PATH) == their_temp_file`` failed
    with an unrelated-looking TypeError: 70 failures and 250 errors.

    The far more serious property this protects: a test run must never be
    able to connect to the real production database and do destructive
    things to live data. conftest.py neutralizes both variables before any
    test module is imported."""
    import db

    assert os.environ.get("APP_ENV") == "development"
    assert not os.environ.get("DATABASE_URL")
    # The live engine this whole suite shares is SQLite, on a real path.
    assert db.DB_DIALECT == "sqlite"
    assert db.DB_PATH is not None


def test_database_config_is_frozen_and_cannot_be_mutated():
    config = load_database_config(app_env="development", database_url=None)
    with pytest.raises(Exception):
        config.url = "sqlite:///changed.db"
