"""SQLite DB setup for SpeakLink (independent from any existing systems)."""
import os
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "speaklink_live.db"
configured_db_path = os.environ.get("SPEAKLINK_DB_PATH")

DB_PATH = (
    Path(configured_db_path).expanduser().resolve()
    if configured_db_path
    else DEFAULT_DB_PATH
)

DATABASE_URL = f"sqlite:///{DB_PATH.as_posix()}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)

# Enable WAL mode for better concurrency
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
