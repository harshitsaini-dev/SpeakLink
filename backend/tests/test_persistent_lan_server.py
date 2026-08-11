r"""A HQ server that remembers, instead of one that starts again every morning.

THE P0 THIS EXISTS TO END

``Start-SpeakLinkLanPilot.ps1`` line 92 builds its data root from the clock::

    $pilotRoot = ...\lan-pilot\$(Get-Date -Format 'yyyyMMdd-HHmmss')

and line 104 mints a fresh random administrator to go with it. So every start is
a new, empty database. Measured on this machine: eight pilot roots, each with a
different ``lan-pilot-xxxxxx`` user, and the Store's enrolled Device sitting in
one of the older ones.

That single design decision produced both reported failures:

* the Store showed OFFLINE after a restart, because the Device it enrolled as
  does not exist in the database the running backend now uses;
* ``owneradmin`` could not sign in, because it lives in
  ``backend/speaklink_live.db`` and the running backend was looking at a
  throwaway pilot file.

Neither is a Receiver bug and neither is an authentication bug. The server
forgot.

The persistent profile resolves ONE fixed root, reuses the same database and the
same keys for ever, and refuses to be confused with a throwaway pilot.

Nothing here opens a socket, starts a server, or touches the protected database.
"""

from __future__ import annotations

import json
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

from tools.persistent_lan_server import (  # noqa: E402
    PERSISTENT_MARKER,
    PersistentServerError,
    ServerProfile,
    describe_profile,
    is_throwaway_pilot_database,
    resolve_persistent_root,
    server_mode,
)


# ===========================================================================
# One fixed root, for ever
# ===========================================================================
def test_the_root_is_fixed_not_timestamped(monkeypatch):
    """The whole bug in one assertion."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    first = resolve_persistent_root()
    second = resolve_persistent_root()
    assert first == second
    assert first == Path(r"C:\Users\someone\AppData\Local\SpeakLink\persistent-lan-server")


def test_the_root_carries_no_date(monkeypatch):
    """A path built from the clock is a path that changes every morning."""
    import re

    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert not re.search(r"\d{8}-\d{6}", str(resolve_persistent_root()))


def test_the_layout_is_stable(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    profile = ServerProfile.persistent()
    assert profile.database == resolve_persistent_root() / "data" / "speaklink.db"
    assert profile.key_container == resolve_persistent_root() / "keys" / "receiver-hmac-keys.bin"
    for folder in ("data", "config", "keys", "logs", "backups", "runtime",
                   "migration-reports"):
        assert (resolve_persistent_root() / folder) == profile.folder(folder)


def test_the_profile_is_labelled_so_a_ui_can_say_which_server_this_is(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert ServerProfile.persistent().mode == PERSISTENT_MARKER
    assert "PERSISTENT" in PERSISTENT_MARKER


# ===========================================================================
# Telling the two servers apart
# ===========================================================================
def test_a_pilot_database_is_recognised_as_throwaway(tmp_path):
    """The safety catch. Pointing the persistent server at a pilot file would
    quietly adopt a database that was designed to be discarded."""
    pilot = tmp_path / "lan-pilot" / "20260729-181918" / "lan-pilot.db"
    pilot.parent.mkdir(parents=True)
    pilot.write_bytes(b"")
    assert is_throwaway_pilot_database(pilot)


def test_a_persistent_database_is_not_mistaken_for_a_pilot(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert not is_throwaway_pilot_database(ServerProfile.persistent().database)


@pytest.mark.parametrize("name", [
    "lan-pilot.db",
    "staging.db",
    "manual-ui.db",
])
def test_every_known_throwaway_name_is_recognised(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"")
    assert is_throwaway_pilot_database(path)


def test_the_protected_database_is_not_a_throwaway():
    """It is the opposite - the one file that must never be treated as
    disposable."""
    assert not is_throwaway_pilot_database(BACKEND_ROOT / "speaklink_live.db")


def test_server_mode_names_what_is_actually_running(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    persistent = ServerProfile.persistent().database
    assert server_mode(persistent) == PERSISTENT_MARKER
    assert server_mode(tmp_path / "lan-pilot.db") != PERSISTENT_MARKER


# ===========================================================================
# It refuses to guess
# ===========================================================================
def test_it_refuses_a_throwaway_database_as_the_persistent_source(tmp_path):
    pilot = tmp_path / "lan-pilot.db"
    pilot.write_bytes(b"")
    with pytest.raises(PersistentServerError) as refusal:
        ServerProfile.persistent().require_not_throwaway(pilot)
    assert "throwaway" in str(refusal.value).lower()


def test_the_refusal_explains_what_to_do_instead(tmp_path):
    pilot = tmp_path / "lan-pilot.db"
    pilot.write_bytes(b"")
    with pytest.raises(PersistentServerError) as refusal:
        ServerProfile.persistent().require_not_throwaway(pilot)
    assert "Initialize" in str(refusal.value)


# ===========================================================================
# The description a technician reads, carrying nothing secret
# ===========================================================================
def test_the_description_names_no_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    text = describe_profile(ServerProfile.persistent())
    for forbidden in ("JWT_SECRET", "password", "speaklink_rcv_v1", "Bearer"):
        assert forbidden not in text


def test_the_description_names_the_mode_and_the_database(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    text = describe_profile(ServerProfile.persistent())
    assert PERSISTENT_MARKER in text
    assert "speaklink.db" in text


# ===========================================================================
# The scripts an operator runs
# ===========================================================================
SCRIPTS = REPOSITORY_ROOT / "scripts"


@pytest.mark.parametrize("name", [
    "Initialize-SpeakLinkPersistentLanServer.ps1",
    "Start-SpeakLinkPersistentLanServer.ps1",
    "Stop-SpeakLinkPersistentLanServer.ps1",
    "Test-SpeakLinkPersistentLanServer.ps1",
])
def test_the_operator_scripts_exist(name):
    assert (SCRIPTS / name).exists()


def test_initialize_defaults_to_planonly():
    """A first run must change nothing. An operator finding out what a tool
    would do should never be the same action as letting it do it."""
    source = (SCRIPTS / "Initialize-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    assert "$Apply" in source
    assert "PlanOnly" in source


def test_initialize_backs_up_before_it_applies():
    source = (SCRIPTS / "Initialize-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    assert "backup" in source.lower()
    assert "integrity_check" in source


def test_initialize_never_deletes_a_source_database():
    source = (SCRIPTS / "Initialize-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    import re

    for match in re.finditer(r"Remove-Item[^\n]*", source):
        line = match.group(0)
        assert ".db" not in line, f"a database removal survives: {line.strip()}"


def test_start_never_builds_a_dated_path():
    """The line that caused all of this, and its absence here."""
    source = (SCRIPTS / "Start-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    assert "yyyyMMdd-HHmmss" not in source


def test_start_mints_no_random_administrator():
    """The pilot creates lan-pilot-xxxxxx on every start. The persistent server
    uses the accounts already in its database."""
    source = (SCRIPTS / "Start-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    assert "lan-pilot-" not in source
    assert "Get-Random" not in source


def test_start_uses_exactly_one_uvicorn_worker():
    """Connection state is process-local, so a second worker loses Receivers.

    Asserted on the argument list rather than on the string '--workers 1',
    because the script passes arguments as a PowerShell array - the property is
    'one worker', not 'these characters appear next to each other'.
    """
    import re

    source = (SCRIPTS / "Start-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    assert re.search(r"'--workers'\s*,\s*'1'", source) or "--workers 1" in source
    assert not re.search(r"'--workers'\s*,\s*'([02-9]|\d\d+)'", source)


def test_start_binds_a_private_address_only():
    source = (SCRIPTS / "Start-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    assert "0.0.0.0" not in source


def test_start_refuses_a_second_instance():
    source = (SCRIPTS / "Start-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    assert "already" in source.lower()


def test_no_script_carries_a_password_or_secret():
    """Scans for a secret being *used*, not for one being *looked for*.

    The first version failed on Test-SpeakLinkPersistentLanServer.ps1, whose
    whole job includes a regex that detects 'speaklink_rcv_v1' in a lock file. A
    scan that cannot tell "this handles a secret" from "this hunts for one" is
    loudest about the file doing the most to protect you.
    """
    import re

    for path in SCRIPTS.glob("*SpeakLinkPersistentLanServer.ps1"):
        source = path.read_text(encoding="utf-8")
        assert "ConvertTo-SecureString -AsPlainText" not in source, path.name
        # A literal credential assigned or passed, rather than named inside a
        # -notmatch / -match guard.
        for line in source.splitlines():
            if "-match" in line or "-notmatch" in line or line.strip().startswith("#"):
                continue
            assert not re.search(r"speaklink_rcv_v1\.[A-Za-z0-9]", line), f"{path.name}: {line.strip()}"
            assert "-Password " not in line, f"{path.name}: {line.strip()}"


def test_the_jwt_secret_is_never_printed():
    """It is generated and reused, so it must exist - but it must not reach the
    console, where it would sit in a scrollback buffer for the rest of the day.
    """
    source = (SCRIPTS / "Start-SpeakLinkPersistentLanServer.ps1").read_text(encoding="utf-8")
    for line in source.splitlines():
        if "Write-Output" in line:
            assert "$jwtSecret" not in line, line.strip()
            assert "$generated" not in line, line.strip()
