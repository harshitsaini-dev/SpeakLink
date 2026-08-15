"""Database engine setup for SpeakLink.

Development uses local SQLite by default - no configuration step, no
internet connection required. Production requires an explicit
``DATABASE_URL`` (PostgreSQL/Supabase) and refuses to start without one.
See ``db_config.py`` for the exact rule and ``docs/SUPABASE_PRODUCTION_
CUTOVER.md`` for how a Windows HQ machine is given that URL without it ever
reaching Git, a log line, or the frontend.
"""
from sqlalchemy import create_engine

from sqlite_settings import apply_sqlite_pragmas
from sqlalchemy.orm import declarative_base, sessionmaker

from db_config import DatabaseConfig, load_database_config

_config: DatabaseConfig = load_database_config()

DATABASE_URL = _config.url
DB_DIALECT = _config.dialect
#: Only meaningful when DB_DIALECT == "sqlite"; None in production/Postgres.
DB_PATH = _config.sqlite_path

_engine_kwargs = {"pool_pre_ping": True}
if DB_DIALECT == "sqlite":
    # The application serves requests from several threads against one file.
    _engine_kwargs["connect_args"] = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, **_engine_kwargs)

# Every SQLite connection in this product is configured the same way, from one
# place - see sqlite_settings.py for why that matters.
apply_sqlite_pragmas(engine)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
