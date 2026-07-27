"""The suite must not be able to open the protected database. Proven, not hoped.

On 2026-07-27 a ``-wal`` and ``-shm`` appeared beside
``backend/speaklink_live.db``. The main file's bytes never changed, so every
size-and-hash assertion in the suite passed while the database's *logical*
contents had moved on - SQLite reads through the WAL. It took a forensic
comparison of two copies to see it at all.

The mechanism is worth stating plainly, because it is not obvious:

* ``backend/db.py`` resolves ``DB_PATH`` from ``SPEAKLINK_DB_PATH`` **at import
  time**, so a module that imports ``db`` before the variable is set binds the
  process-wide engine to the real file.
* ``db`` installs a ``connect`` listener that runs ``PRAGMA journal_mode=WAL``.
  Setting the journal mode **is a write**. So merely opening a connection on
  that engine creates sidecars beside the protected database.

Most test modules guard themselves with ``os.environ.setdefault`` before their
imports, which works only because some earlier module in the same process
already ran one. Under ``-n 2 --dist loadscope`` each worker imports only its
own modules, so a worker handed an unguarded module first has no earlier one -
and a guard that looked collective turns out to have been luck.

These tests are the standing proof that the guarantee holds, in every worker,
including the serial case. They assert about paths and files; none of them opens
the protected database, and that is the point.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = BACKEND_ROOT.parent
PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"
SIDECARS = ("-wal", "-shm")

#: Every test module that reaches the backend. If one of these can bind the
#: default engine to the protected database when imported first, the suite has
#: the 2026-07-27 defect again.
BACKEND_TEST_MODULES = sorted(
    path.name
    for path in (BACKEND_ROOT / "tests").glob("test_*.py")
)


def _load_conftest():
    """Import ``conftest.py`` by path.

    It is not importable by name: pytest loads it as a plugin, not as a module
    on ``sys.path``.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "speaklink_tests_conftest", BACKEND_ROOT / "tests" / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _import_module_alone(module_name: str, *, inherited_db_path: str | None) -> str:
    """Import one test module in a fresh interpreter and report where db points.

    A subprocess because the question is about *import order in a clean
    process*, which cannot be asked of the interpreter already running this
    suite - it imported everything long ago.

    ``db`` is inspected through ``sys.modules`` rather than imported here. An
    unconditional ``import db`` would bind the engine itself and then blame the
    module under test for it - which is exactly the false alarm this line
    replaces. A module that never touches ``db`` reports NOT_IMPORTED, which is
    the honest answer.
    """
    probe = (
        "import os, sys\n"
        "sys.path.insert(0, r'%s')\n"
        "sys.path.insert(0, r'%s')\n"
        "import importlib.util as u\n"
        "spec = u.spec_from_file_location('probe', r'%s')\n"
        "m = u.module_from_spec(spec)\n"
        "try:\n"
        "    spec.loader.exec_module(m)\n"
        "except SystemExit:\n"
        "    pass\n"
        "except Exception as error:\n"
        "    print('IMPORT_ERROR', type(error).__name__)\n"
        "bound = sys.modules.get('db')\n"
        "print('BOUND', bound.DB_PATH if bound is not None else 'NOT_IMPORTED')\n"
        % (str(BACKEND_ROOT), str(REPOSITORY_ROOT),
           str(BACKEND_ROOT / "tests" / module_name))
    )
    environment = {
        key: value for key, value in os.environ.items() if key != "SPEAKLINK_DB_PATH"
    }
    if inherited_db_path is not None:
        environment["SPEAKLINK_DB_PATH"] = inherited_db_path

    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True, text=True, timeout=180, env=environment,
        cwd=str(REPOSITORY_ROOT),
    )
    for line in completed.stdout.splitlines():
        if line.startswith("BOUND "):
            return line[len("BOUND "):].strip()
    return f"NO_BINDING stdout={completed.stdout[-200:]} stderr={completed.stderr[-200:]}"


# ===========================================================================
# 1 & 5. Collection cannot bind the engine to the protected database
# ===========================================================================
def test_the_conftest_guard_is_in_force_right_now():
    """Assert the guarantee rather than trusting the hook ran."""
    resolved = _load_conftest().assert_default_engine_is_disposable()
    assert resolved != PROTECTED_DATABASE.resolve()


def test_collecting_the_whole_suite_does_not_bind_the_protected_database():
    """``--collect-only`` imports every test module and runs no test. If any of
    them can bind the engine to the real file, this is where it shows."""
    environment = {
        key: value for key, value in os.environ.items() if key != "SPEAKLINK_DB_PATH"
    }
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "0", "backend/tests", "-q", "--collect-only"],
        capture_output=True, text=True, timeout=600, env=environment,
        cwd=str(REPOSITORY_ROOT),
    )
    assert completed.returncode == 0, (
        "collection failed - if the conftest guard raised, some module bound the "
        f"protected database:\n{completed.stdout[-1500:]}"
    )


def test_collection_creates_no_sidecar_beside_the_protected_database():
    """The failure mode that hid for a whole session: the main file untouched,
    a WAL beside it holding everything that actually happened."""
    for suffix in SIDECARS:
        assert not Path(str(PROTECTED_DATABASE) + suffix).exists(), (
            f"a {suffix} file exists beside the protected database"
        )


# ===========================================================================
# 3. An inherited protected path is replaced, not obeyed
# ===========================================================================
def test_an_inherited_protected_database_path_is_refused():
    """``setdefault`` would have *inherited* this, which is the whole bug: a
    variable pointing at the protected database is exactly the value that must
    not be kept."""
    bound = _import_module_alone(
        "test_store_catalog.py", inherited_db_path=str(PROTECTED_DATABASE)
    )
    assert Path(bound).resolve() != PROTECTED_DATABASE.resolve(), (
        f"an inherited protected path was obeyed: {bound}"
    )


def test_the_conftest_replaces_an_inherited_protected_path():
    environment = {**os.environ, "SPEAKLINK_DB_PATH": str(PROTECTED_DATABASE)}
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-n", "0",
         "backend/tests/test_protected_database_isolation.py", "-q", "--collect-only"],
        capture_output=True, text=True, timeout=300, env=environment,
        cwd=str(REPOSITORY_ROOT),
    )
    assert completed.returncode == 0, (
        f"the guard did not replace an inherited protected path:\n{completed.stdout[-1000:]}"
    )


# ===========================================================================
# 4. The two modules that actually had the defect
# ===========================================================================
@pytest.mark.parametrize(
    "module_name", ["test_store_catalog.py", "test_store_catalog_reconciliation.py"]
)
def test_the_catalog_modules_are_safe_imported_alone(module_name: str):
    """These two imported ``db``/``models`` with no guard of their own. They were
    safe only while some other module happened to be imported first."""
    bound = _import_module_alone(module_name, inherited_db_path=None)
    assert Path(bound).resolve() != PROTECTED_DATABASE.resolve(), (
        f"{module_name} bound the engine to the protected database: {bound}"
    )


def test_every_backend_test_module_is_safe_imported_first():
    """The general property, checked module by module rather than assumed.

    Slow on purpose - one interpreter per module - and worth it: this is the
    exact question ``--dist loadscope`` asks of a worker every run.
    """
    offenders = []
    for module_name in BACKEND_TEST_MODULES:
        if module_name == Path(__file__).name:
            continue
        bound = _import_module_alone(module_name, inherited_db_path=None)
        if bound.startswith("NO_BINDING") or bound == "NOT_IMPORTED":
            # NOT_IMPORTED is a pass: the module never pulled in ``db`` at all.
            continue
        try:
            if Path(bound).resolve() == PROTECTED_DATABASE.resolve():
                offenders.append(module_name)
        except OSError:
            continue
    assert offenders == [], (
        "these modules bind the default engine to the protected database when "
        f"imported first: {offenders}"
    )


# ===========================================================================
# 2. Workers do not share one throwaway database
# ===========================================================================
def test_each_worker_gets_its_own_temporary_database():
    """Two workers on one SQLite file is a lock-contention flake waiting to be
    blamed on something else."""
    seen = set()
    for worker in ("gw0", "gw1", "gw2"):
        environment = {
            key: value for key, value in os.environ.items() if key != "SPEAKLINK_DB_PATH"
        }
        environment["PYTEST_XDIST_WORKER"] = worker
        probe = (
            "import sys; sys.path.insert(0, r'%s'); sys.path.insert(0, r'%s')\n"
            "import conftest; print('PATH', conftest._DEFAULT_ENGINE_DATABASE)\n"
            % (str(BACKEND_ROOT / "tests"), str(BACKEND_ROOT))
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True,
            timeout=120, env=environment, cwd=str(REPOSITORY_ROOT),
        )
        for line in completed.stdout.splitlines():
            if line.startswith("PATH "):
                seen.add(line[len("PATH "):].strip())
    assert len(seen) == 3, f"workers shared a database file: {seen}"


def test_the_worker_database_is_outside_the_repository():
    chosen = Path(_load_conftest()._DEFAULT_ENGINE_DATABASE)
    assert REPOSITORY_ROOT not in chosen.parents


# ===========================================================================
# 6. The protected file set, unchanged
# ===========================================================================
def test_the_protected_database_matches_its_recorded_baseline():
    """Size and hash together. Size alone would have passed throughout the
    incident, because the incident never changed the size."""
    import hashlib

    if not PROTECTED_DATABASE.exists():
        pytest.skip("no protected database on this machine")

    assert PROTECTED_DATABASE.stat().st_size == 507904
    digest = hashlib.sha256(PROTECTED_DATABASE.read_bytes()).hexdigest().upper()
    assert digest == "8C858B132907DC72180A134D4981C5E8C4BBC03D190D7370B3823DB2BD2EF2AB"


def test_no_sidecar_exists_beside_the_protected_database():
    for suffix in SIDECARS:
        sidecar = Path(str(PROTECTED_DATABASE) + suffix)
        assert not sidecar.exists(), (
            f"{sidecar.name} exists. Something opened the protected database. "
            "Its main file can be byte-identical while this holds every change."
        )
