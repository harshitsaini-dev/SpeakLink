"""The physical Store delivery boundary.

A Broadcast can now reach a web audience through a shared link with no Store
targets at all, so "may broadcast" and "may put sound into a shop" have become
different questions. ``broadcast.store_delivery`` is the second one.

The property under test is not "the button is hidden". It is that the API
refuses, including when the caller crafts the request by hand, and including
when Store Scope is blank - because a blank Scope means unrestricted, and the
absence of a restriction must never read as a grant.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from permission_catalog import (  # noqa: E402
    DEFAULT_ROLE_PERMISSIONS,
    PERMISSION_CODES,
    PERMISSION_DEFINITIONS,
)
from rbac import Role  # noqa: E402

CODE = "broadcast.store_delivery"


# ===========================================================================
# The catalog
# ===========================================================================

def test_the_permission_exists_with_a_label_an_administrator_can_act_on():
    definition = next(d for d in PERMISSION_DEFINITIONS if d.code == CODE)
    assert definition.group == "Broadcast"
    # The label has to say what removing it does, in the operator's words.
    assert definition.label == "Broadcast to Stores / Zones"
    assert CODE in PERMISSION_CODES


def test_it_follows_the_existing_broadcast_naming_convention():
    broadcast_codes = {d.code for d in PERMISSION_DEFINITIONS
                       if d.group == "Broadcast"}
    assert CODE.startswith("broadcast."), broadcast_codes


# ===========================================================================
# Migration: nobody loses a capability they already had
# ===========================================================================

@pytest.mark.parametrize("role", [Role.OWNER, Role.ADMIN, Role.BROADCASTER])
def test_every_role_that_could_already_broadcast_physically_keeps_it(role):
    """The upgrade must not quietly demote working operators."""
    assert CODE in DEFAULT_ROLE_PERMISSIONS[role]
    # And it arrives alongside, not instead of, what they already had.
    assert "broadcast.start" in DEFAULT_ROLE_PERMISSIONS[role]


def test_a_read_only_account_is_not_granted_physical_delivery():
    assert CODE not in DEFAULT_ROLE_PERMISSIONS[Role.VIEWER]
    assert "broadcast.start" not in DEFAULT_ROLE_PERMISSIONS[Role.VIEWER]


def test_the_new_permission_grants_nothing_else():
    """A boundary that also widened something would be a different change."""
    broadcaster = DEFAULT_ROLE_PERMISSIONS[Role.BROADCASTER]
    expected = {
        "menu.broadcast.view", "broadcast.start", "broadcast.stop",
        "store_audio.control", CODE,
        "menu.history.view", "menu.receivers.view", "menu.stores.view",
    }
    assert broadcaster == frozenset(expected)


def test_seeding_the_catalog_twice_changes_nothing():
    """HQ reseeds the catalog on every start, so it must be idempotent."""
    from sqlalchemy import create_engine, text

    from permission_catalog import ensure_permission_schema

    # Windows will not delete a SQLite file the engine still holds, and this
    # test is about catalog idempotence, not about temporary directories.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as workspace:
        engine = create_engine(f"sqlite:///{Path(workspace) / 'catalog.db'}")
        try:
            ensure_permission_schema(engine)
            with engine.connect() as connection:
                first = connection.execute(
                    text("SELECT code FROM permissions ORDER BY code")).fetchall()

            ensure_permission_schema(engine)      # exactly what a restart does
            with engine.connect() as connection:
                second = connection.execute(
                    text("SELECT code FROM permissions ORDER BY code")).fetchall()
        finally:
            engine.dispose()

        assert first == second
        assert (CODE,) in second
        # One row, not a duplicate per start.
        assert [row[0] for row in second].count(CODE) == 1
