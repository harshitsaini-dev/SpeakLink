"""The repo-native launcher's view of who is actually serving the port."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _launcher():
    """Load tools/speaklink_server.py by path.

    It is a script rather than a package module - it has to run before
    anything is installed - so it cannot simply be imported by name.
    """
    if "speaklink_server_launcher" in sys.modules:
        return sys.modules["speaklink_server_launcher"]
    path = REPOSITORY_ROOT / "tools" / "speaklink_server.py"
    spec = importlib.util.spec_from_file_location("speaklink_server_launcher", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["speaklink_server_launcher"] = module
    spec.loader.exec_module(module)
    return module


# ===========================================================================
# The port-owner check must not cry wolf
#
# It compared pids directly, and the launcher starts a parent which starts
# uvicorn - so the process holding the port is a CHILD of the one it tracks.
# Every healthy server was therefore reported as "NOT the process this
# repository started", with an instruction to kill it. That advice stops a
# working HQ, and it was followed.
#
# A warning that is wrong every time is worse than no warning: the one time it
# is right, nobody believes it.
# ===========================================================================

def test_the_process_holding_the_port_may_be_our_child(monkeypatch):
    launcher = _launcher()
    parents = {17332: 16860, 16860: 4}

    monkeypatch.setattr(launcher, "port_owner", lambda port: 17332)
    monkeypatch.setattr(launcher, "running_pid", lambda: 16860)
    monkeypatch.setattr(launcher, "_parent_of", lambda pid: parents.get(pid))

    assert launcher._is_descendant_of(17332, 16860)
    config = launcher.Config({"APP_PORT": "8000"})
    assert launcher.foreign_port_holder(config) is None, (
        "a healthy server was reported as foreign")


def test_a_grandchild_is_still_ours(monkeypatch):
    """Windows adds a shell between the launcher and uvicorn often enough that
    one generation is not a safe assumption."""
    launcher = _launcher()
    parents = {900: 800, 800: 700, 700: 4}
    monkeypatch.setattr(launcher, "_parent_of", lambda pid: parents.get(pid))
    assert launcher._is_descendant_of(900, 700)


def test_an_unrelated_process_is_still_reported(monkeypatch):
    """The warning has to keep working - a genuinely stale server answering
    with old code is the failure it exists for."""
    launcher = _launcher()
    monkeypatch.setattr(launcher, "port_owner", lambda port: 5555)
    monkeypatch.setattr(launcher, "running_pid", lambda: 16860)
    monkeypatch.setattr(launcher, "_parent_of", lambda pid: 4)

    config = launcher.Config({"APP_PORT": "8000"})
    assert launcher.foreign_port_holder(config) == 5555


def test_a_parent_chain_that_loops_does_not_hang(monkeypatch):
    """A pid table can be inconsistent while processes are exiting, and an
    unbounded walk there is a launcher that hangs instead of answering."""
    launcher = _launcher()
    monkeypatch.setattr(launcher, "_parent_of", lambda pid: 7 if pid == 9 else 9)
    assert launcher._is_descendant_of(9, 12345) is False
