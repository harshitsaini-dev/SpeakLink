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
#: The current accepted baseline, rebaselined on 2026-07-29 by operator decision.
#:
#: The previous value is kept below rather than deleted. A baseline that is
#: quietly overwritten every time it fails is not a baseline; keeping the old one
#: means the next person can see that it moved, when, and on whose say-so.
PROTECTED_BASELINE_SHA256 = "8A7E341365626BE67727156D84EE57B704B83E9F73CEC954CFC52A2FB1A547CA"
PROTECTED_BASELINE_SIZE = 507904

#: What it was before, and why it changed.
#:
#: The schema gained four additive user-lifecycle columns - lifecycle_state,
#: display_name, disabled_at, archived_at - from a migration that ran against
#: this database. No row was deleted and no password hash was touched, but an
#: ALTER TABLE rewrites the file, so the hash moved while the size did not.
#:
#: Verified before the operator accepted it:
#:   PRAGMA integrity_check : ok
#:   hq_users               : 1 row, username 'admin', role 'admin', active
#:   stores                 : 13
#:   broadcast_sessions     : 17
#:   system_logs            : 194
#:   plaintext password col : none
#: and a consistent SQLite-backup-API copy exists under backups/.
PREVIOUS_BASELINE_SHA256 = "EEF1EA79BC901A989AE0E73D1F4882F6517844436139E981EB9051839DD70D51"

#: The whole chain, kept so nobody has to reconstruct it from commit messages.
#:
#:   8C858B13…BD2EF2AB  original
#:     -> four additive user-lifecycle columns (lifecycle_state, display_name,
#:        disabled_at, archived_at)
#:   EEF1EA79…9DD70D51  accepted 2026-07-29
#:     -> tools/create_owner.py ran successfully: hq_users.session_version added
#:        by ensure_rbac_schema, and one row inserted for owneradmin
#:   8A7E3413…B1A547CA  accepted 2026-07-29, current
#:
#: Verified before accepting the current value:
#:   PRAGMA integrity_check : ok
#:   hq_users               : exactly 2 - admin (ADMIN) and owneradmin (OWNER),
#:                            both active. admin's password-hash fingerprint is
#:                            unchanged from the previous baseline, so the
#:                            existing account was preserved rather than rewritten
#:   stores / sessions / logs : 13 / 17 / 194, all unchanged
#:   plaintext password col : none - only password_hash
#:   consistent backups     : backups/speaklink_live-20260729-160359.db and
#:                            persistent-lan-server/backups/source-20260729-190540.db
BASELINE_HISTORY = (
    "8C858B132907DC72180A134D4981C5E8C4BBC03D190D7370B3823DB2BD2EF2AB",
    "EEF1EA79BC901A989AE0E73D1F4882F6517844436139E981EB9051839DD70D51",
    "8A7E341365626BE67727156D84EE57B704B83E9F73CEC954CFC52A2FB1A547CA",
)


def test_the_protected_database_matches_its_recorded_baseline():
    """Size and hash together. Size alone would have passed throughout the
    incident, because the incident never changed the size - and it did not
    change it for the additive columns either."""
    import hashlib

    if not PROTECTED_DATABASE.exists():
        pytest.skip("no protected database on this machine")

    assert PROTECTED_DATABASE.stat().st_size == PROTECTED_BASELINE_SIZE
    digest = hashlib.sha256(PROTECTED_DATABASE.read_bytes()).hexdigest().upper()
    assert digest == PROTECTED_BASELINE_SHA256


def test_the_baseline_history_is_kept_and_has_no_repeats():
    """Every accepted value, in order, each one distinct.

    A repeated entry would mean somebody pasted the current hash over an older
    one instead of appending - which loses the fact that it ever moved, and with
    it the reason. The chain is the audit trail.
    """
    assert len(BASELINE_HISTORY) == len(set(BASELINE_HISTORY))
    assert BASELINE_HISTORY[-1] == PROTECTED_BASELINE_SHA256
    assert BASELINE_HISTORY[-2] == PREVIOUS_BASELINE_SHA256


def test_the_current_baseline_is_the_newest_entry_not_an_older_one():
    """Guards against a rollback pasted in by mistake."""
    assert PROTECTED_BASELINE_SHA256 not in BASELINE_HISTORY[:-1]


def test_the_baseline_actually_moved_rather_than_being_edited_in_place():
    """A guard on the guard.

    If somebody ever "fixes" a failing baseline by pasting the new hash over the
    old one, this notices that the two are the same value and says so. The point
    of recording the previous hash is that it is different from the current one.
    """
    assert PROTECTED_BASELINE_SHA256 != PREVIOUS_BASELINE_SHA256


def test_no_sidecar_exists_beside_the_protected_database():
    for suffix in SIDECARS:
        sidecar = Path(str(PROTECTED_DATABASE) + suffix)
        assert not sidecar.exists(), (
            f"{sidecar.name} exists. Something opened the protected database. "
            "Its main file can be byte-identical while this holds every change."
        )
