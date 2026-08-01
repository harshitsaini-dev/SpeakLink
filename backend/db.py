"""Database engine setup for EchoCast Live.

Development uses local SQLite by default - no configuration step, no
internet connection required. Production requires an explicit
``DATABASE_URL`` (PostgreSQL/Supabase) and refuses to start without one.
See ``db_config.py`` for the exact rule and ``docs/SUPABASE_PRODUCTION_
CUTOVER.md`` for how a Windows HQ machine is given that URL without it ever
reaching Git, a log line, or the frontend.
"""
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from db_config import DatabaseConfig, load_database_config

_config: DatabaseConfig = load_database_config()

DATABASE_URL = _config.url
DB_DIALECT = _config.dialect
#: Only meaningful when DB_DIALECT == "sqlite"; None in production/Postgres.
DB_PATH = _config.sqlite_path

_engine_kwargs = {"pool_pre_ping": True}
if DB_DIALECT == "sqlite":
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)

if DB_DIALECT == "sqlite":
    # WAL mode and foreign-key enforcement are SQLite connection-level
    # settings with no PostgreSQL equivalent - PostgreSQL already enforces
    # foreign keys unconditionally and has its own concurrency model, so
    # this listener is never registered for a PostgreSQL engine.
    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
