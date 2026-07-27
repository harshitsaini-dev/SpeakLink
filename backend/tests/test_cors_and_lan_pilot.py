"""CORS names exact origins, and the LAN pilot binds one checked address.

Two things meet here, and both are about a decision that used to be implicit.

**CORS** defaulted to ``"*"`` while sending credentials. Browsers refuse that
exact combination, so it was not doing what it looked like it was doing - but a
deployment that set one real origin somewhere and inherited the default
elsewhere would have been genuinely open, and a credentialed API that answers
every origin fails silently. There is no error, no log line, and no symptom
until somebody points a page at it.

**The LAN pilot address** is the same shape of problem one layer down. Loopback
is unreachable from another machine by construction; a LAN address is not. An
address that "falls back" when the expected one is missing puts an HTTP API
carrying credentials somewhere nobody chose. So it is checked, and a public,
loopback, link-local, multicast or non-literal address is refused outright.

Nothing here starts a server or binds a port.
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
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-key")

PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"
HQ_ADDRESS = "192.168.4.134"


# ===========================================================================
# CORS
# ===========================================================================
def _origins(configured: str | None, monkeypatch):
    from server import allowed_cors_origins

    if configured is None:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
    else:
        monkeypatch.setenv("CORS_ORIGINS", configured)
    return allowed_cors_origins()


def test_a_wildcard_is_refused_rather_than_honoured(monkeypatch):
    """The old default. Refusing beats quietly accepting something this API,
    which sends credentials, must never allow."""
    with pytest.raises(RuntimeError):
        _origins("*", monkeypatch)


def test_a_wildcard_hidden_among_real_origins_is_still_refused(monkeypatch):
    with pytest.raises(RuntimeError):
        _origins("http://192.168.4.134:3000,*", monkeypatch)


def test_the_default_is_loopback_not_everything(monkeypatch):
    """What a developer on this machine actually needs, and nothing wider."""
    assert _origins(None, monkeypatch) == [
        "http://localhost:3000", "http://127.0.0.1:3000"
    ]
    assert _origins("   ", monkeypatch) == [
        "http://localhost:3000", "http://127.0.0.1:3000"
    ]


def test_exact_origins_are_used_verbatim(monkeypatch):
    configured = f"http://{HQ_ADDRESS}:3000,http://localhost:3000"
    assert _origins(configured, monkeypatch) == [
        f"http://{HQ_ADDRESS}:3000", "http://localhost:3000"
    ]


def test_whitespace_and_empty_entries_do_not_become_origins(monkeypatch):
    """A trailing comma must not turn into an empty allowed origin."""
    assert _origins(f" http://{HQ_ADDRESS}:3000 , ,", monkeypatch) == [
        f"http://{HQ_ADDRESS}:3000"
    ]


def test_the_application_is_configured_with_exact_origins():
    """Read the middleware the app actually installed, not the helper."""
    import server
    from starlette.middleware.cors import CORSMiddleware

    cors = [m for m in server.app.user_middleware if m.cls is CORSMiddleware]
    assert cors, "the CORS middleware is not installed"
    configured = cors[0].kwargs.get("allow_origins", [])
    assert "*" not in configured
    assert configured, "no origin is allowed at all"


# ===========================================================================
# The LAN pilot address
# ===========================================================================
def _require(address: str):
    from tools.lan_pilot import require_private_ipv4

    return require_private_ipv4(address)


def test_the_fixed_private_address_is_accepted():
    assert _require(HQ_ADDRESS) == HQ_ADDRESS


@pytest.mark.parametrize("address", ["10.0.0.5", "172.16.3.9", "192.168.1.20"])
def test_other_rfc1918_addresses_are_accepted(address: str):
    """The guard is about address class, not about one magic string."""
    assert _require(address) == address


@pytest.mark.parametrize(
    "address,reason",
    [
        ("8.8.8.8", "public"),
        ("1.1.1.1", "public"),
        ("127.0.0.1", "loopback"),
        ("169.254.10.1", "link-local"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("hq.example.internal", "not a literal address"),
        ("192.168.4.134:8000", "not a bare address"),
        ("", "empty"),
        ("::1", "IPv6 loopback"),
    ],
)
def test_everything_else_is_refused(address: str, reason: str):
    from tools.lan_pilot import LanPilotError

    with pytest.raises(LanPilotError):
        _require(address)


def test_a_hostname_that_would_resolve_is_still_refused():
    """A name can resolve anywhere, and can be made to resolve somewhere else
    tomorrow. Only a literal private address is checkable at the moment it is
    used."""
    from tools.lan_pilot import LanPilotError

    for hostname in ("localhost", "example.com", "hq"):
        with pytest.raises(LanPilotError):
            _require(hostname)


# ===========================================================================
# The pilot root
# ===========================================================================
def test_the_pilot_refuses_a_root_inside_the_repository(tmp_path):
    from tools.lan_pilot import LanPilotError, reject_protected_paths

    with pytest.raises(LanPilotError):
        reject_protected_paths(REPOSITORY_ROOT / "pilot")
    with pytest.raises(LanPilotError):
        reject_protected_paths(BACKEND_ROOT)
    # Somewhere genuinely outside is fine.
    reject_protected_paths(tmp_path / "lan-pilot")


def test_the_pilot_refuses_the_protected_database_directory():
    from tools.lan_pilot import LanPilotError, reject_protected_paths

    with pytest.raises(LanPilotError):
        reject_protected_paths(PROTECTED_DATABASE.parent)


def test_the_cors_origins_the_pilot_asks_for_are_exact():
    from tools.lan_pilot import cors_origins

    origins = cors_origins(HQ_ADDRESS)
    assert origins == [
        f"http://{HQ_ADDRESS}:3000",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    assert "*" not in origins


def test_the_pilot_database_always_lives_under_the_pilot_root(tmp_path):
    """The real property, rather than sniffing the source for a filename.

    ``lan_pilot`` does name ``speaklink_live.db`` - that is the constant it
    refuses paths against. An earlier version of this test flagged the guard as
    if it were a use, which is the wrong question. What matters is where the
    database it creates actually goes.
    """
    from tools.lan_pilot import prepare

    root = tmp_path / "lan-pilot"
    manifest = prepare(
        root, hq_address=HQ_ADDRESS,
        admin_username="lan-pilot-test",
        admin_password="a-long-enough-temporary-password",
    )
    database = Path(manifest["database_path"]).resolve()
    assert root.resolve() in database.parents
    assert database != PROTECTED_DATABASE.resolve()
    assert Path(manifest["key_container"]).resolve().is_relative_to(root.resolve())


def test_the_pilot_manifest_carries_no_secret(tmp_path):
    """It is written to disk and read by the Stop and Test scripts."""
    from tools.lan_pilot import prepare

    password = "another-long-temporary-password"
    manifest = prepare(
        tmp_path / "lan-pilot", hq_address=HQ_ADDRESS,
        admin_username="lan-pilot-test", admin_password=password,
    )
    rendered = (tmp_path / "lan-pilot" / "manifest.json").read_text(encoding="utf-8")

    # Secret VALUES, not the words. The manifest legitimately records the path
    # of the key container - "...\receiver-hmac-keys.bin" - which is where the
    # keys live, not what they are. An earlier version of this test flagged that
    # filename and would have pushed the fix in the wrong direction.
    assert password not in rendered
    assert "password" not in {key.lower() for key in manifest}
    assert not any(
        isinstance(value, str) and len(value) > 40 and value.isalnum()
        for value in manifest.values()
    ), "a long opaque value that looks like key material is in the manifest"

    # The Store token exists in the database; it must not be copied out here.
    import sqlite3

    connection = sqlite3.connect(f"file:{manifest['database_path']}?mode=ro", uri=True)
    try:
        token = connection.execute("SELECT receiver_token FROM stores").fetchone()[0]
    finally:
        connection.close()
    assert token not in rendered, "the Store credential reached the manifest"
    assert manifest["admin_username"] == "lan-pilot-test"


def test_preparing_refuses_a_short_password(tmp_path):
    from tools.lan_pilot import LanPilotError, prepare

    with pytest.raises(LanPilotError):
        prepare(tmp_path / "lan-pilot", hq_address=HQ_ADDRESS,
                admin_username="x", admin_password="short")


def test_preparing_refuses_an_address_other_than_the_configured_one(tmp_path):
    """Binding "some private address" is not the same as binding the one the
    operator assigned and firewalled."""
    from tools.lan_pilot import LanPilotError, prepare

    with pytest.raises(LanPilotError):
        prepare(tmp_path / "lan-pilot", hq_address="10.0.0.5",
                admin_username="x", admin_password="a-long-enough-password")


def test_the_protected_database_is_untouched():
    if not PROTECTED_DATABASE.exists():
        pytest.skip("no protected database on this machine")
    assert PROTECTED_DATABASE.stat().st_size == 507904
    for suffix in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + suffix).exists()
