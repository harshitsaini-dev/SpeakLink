"""Bind the default engine to a throwaway database before anything imports it.

``backend/db.py`` resolves ``DB_PATH`` from ``SPEAKLINK_DB_PATH`` **at import
time**, and installs a ``connect`` listener that runs ``PRAGMA
journal_mode=WAL``. Two consequences follow, and together they are sharp:

1. A test module that imports ``db``, ``models`` or ``seed`` without setting
   ``SPEAKLINK_DB_PATH`` first binds the process-wide engine to the real
   ``backend/speaklink_live.db``.
2. The *first connection* on that engine then writes - setting the journal mode
   is a write - which creates ``-wal`` and ``-shm`` beside the protected
   database, whether or not any test intended to touch it.

Most test modules guard themselves with an ``os.environ.setdefault`` before
their imports. That works only because some earlier module in the same process
already ran one. Under ``-n 2 --dist loadscope`` each worker imports only the
modules assigned to it, so a worker that receives an unguarded module *first*
has no such earlier module - and the guard that looked collective turns out to
have been luck. Adding an unrelated test file is enough to change which worker
gets what, which is exactly how this was found.

``conftest.py`` is imported by pytest before any test module in its directory,
in every worker. That makes it the only place the guarantee can actually live.

The assertion at the bottom is the part that matters most: it turns "we think
nothing points at the protected database" into something the suite proves on
every run, in every worker, before a single test executes.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"

#: One throwaway file per worker. ``PYTEST_XDIST_WORKER`` is set by xdist and
#: absent in a serial run, so workers cannot collide on one file and a serial
#: run still gets its own.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "serial")
_DEFAULT_ENGINE_DATABASE = (
    Path(tempfile.gettempdir()) / f"speaklink-tests-default-engine-{_WORKER}.db"
)

# Before pytest imports a single test module. Not ``setdefault`` on a value we
# do not control: if something upstream pointed this at the protected database,
# inheriting it would be the whole bug.
_configured = os.environ.get("SPEAKLINK_DB_PATH")
if not _configured or Path(_configured).resolve() == PROTECTED_DATABASE.resolve():
    os.environ["SPEAKLINK_DB_PATH"] = str(_DEFAULT_ENGINE_DATABASE)

# The test suite is ALWAYS a development environment, whatever the machine it
# runs on happens to be configured for.
#
# ``server.py`` calls ``load_dotenv(backend/.env)`` at import. The moment an
# operator puts real production settings in that file - APP_ENV=production and
# a Supabase DATABASE_URL, which is a completely reasonable thing to believe
# belongs there - every test that imports ``server`` resolves its engine to
# PostgreSQL instead of its own throwaway SQLite file. ``db.DB_PATH`` is then
# None (correct for PostgreSQL), and the ~30 test modules that assert
# ``Path(db.DB_PATH) == their_temp_file`` fail with an unrelated-looking
# TypeError. That is not hypothetical: it happened here, and cost 70 failures
# and 250 errors across files that have nothing to do with PostgreSQL.
#
# Worse than the noise is the risk it removes: a test run that silently
# connected to the real production database would be doing destructive things
# to live data. Clearing these two here means a local .env can never point the
# suite at production, no matter what it contains.
#
# Production configuration reaches the real HQ through
# ``keys/database-url.txt`` and ``tools/hq_runtime.py``, never through this
# path - see docs/SUPABASE_PRODUCTION_CUTOVER.md.
# Both are set to a value rather than deleted, and that detail is the whole
# mechanism: ``load_dotenv`` defaults to ``override=False``, so it only fills
# in names that are ABSENT from os.environ. Deleting DATABASE_URL here would
# simply let .env put it back a moment later, when server.py is imported.
# An empty string is present (so dotenv leaves it alone) and is treated as
# "not configured" by db_config.load_database_config's development branch.
os.environ["DATABASE_URL"] = ""
os.environ["APP_ENV"] = "development"


def pytest_configure(config):  # noqa: D401 - pytest hook
    """Refuse to run at all if the default engine points at the protected file.

    Failing here is deliberate. A test suite that quietly opens the production
    database has already done the damage by the time an assertion notices, and
    the damage is invisible: the main file's bytes can be unchanged while a
    ``-wal`` beside it holds committed frames. That is exactly what happened on
    2026-07-27, and it took a forensic comparison of two copies to see it.
    """
    assert_default_engine_is_disposable()


def assert_default_engine_is_disposable() -> Path:
    """Import ``db`` and prove where its engine points. Returns the path.

    Exposed as a function, not just a hook, so a test can assert the guarantee
    rather than trusting that the hook ran.
    """
    import db

    resolved = Path(db.DB_PATH).resolve()
    if resolved == PROTECTED_DATABASE.resolve():
        raise RuntimeError(
            "the default SQLAlchemy engine resolved to the protected database "
            f"({resolved}). Refusing to run. Some module imported backend.db "
            "before SPEAKLINK_DB_PATH was set."
        )
    return resolved


def pytest_sessionfinish(session, exitstatus):  # noqa: D401 - pytest hook
    """Remove this worker's throwaway database and its sidecars.

    Best effort and deliberately narrow: only the file this module chose, and
    only when nothing else claimed it. A cleanup that guessed at paths would be
    a worse hazard than the files it removed.
    """
    if os.environ.get("SPEAKLINK_DB_PATH") != str(_DEFAULT_ENGINE_DATABASE):
        return
    try:
        import db

        db.engine.dispose()
    except Exception:
        pass
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(_DEFAULT_ENGINE_DATABASE) + suffix)
        try:
            if candidate.exists() and candidate.resolve() != PROTECTED_DATABASE.resolve():
                candidate.unlink()
        except OSError:
            pass
