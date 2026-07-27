"""Broadcast helpers must talk to the backend they were given, not to loopback.

``_drive_one_broadcast`` received the backend's base URL - which carries the
host - and then threw that host away, rebuilding the WebSocket URL from a
hardcoded ``127.0.0.1`` plus a port number. On a loopback backend the two agreed
and nothing showed. On the LAN pilot, where Uvicorn binds ``192.168.4.134`` and
therefore does **not** listen on loopback, it produced:

    ConnectionRefusedError: [WinError 1225]

after the Receiver had already enrolled, sealed its credential and reached
CONNECTED. The failure looked like a Receiver problem and was a URL problem.

The fix is not to swap one literal for another. It is to stop having a literal:
the WebSocket URL is derived from the HTTP base URL, so the two cannot disagree
about which machine they mean. A helper that takes a base URL and a port and
uses the port to guess the host has one input too many.

Nothing here opens a socket.
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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from tools.receiver_device_staging_smoke import websocket_url_from  # noqa: E402


LAN = "http://192.168.4.134:8000"
LOOPBACK = "http://127.0.0.1:8000"


# ===========================================================================
# The host comes from the caller, every time
# ===========================================================================
def test_a_loopback_backend_produces_a_loopback_socket():
    assert websocket_url_from(LOOPBACK, "/api/ws/receiver") == (
        "ws://127.0.0.1:8000/api/ws/receiver"
    )


def test_a_lan_backend_produces_a_lan_socket():
    """The case that failed. The helper used to answer 127.0.0.1 here."""
    assert websocket_url_from(LAN, "/api/ws/receiver") == (
        "ws://192.168.4.134:8000/api/ws/receiver"
    )


def test_the_http_and_websocket_hosts_always_match():
    for base in (LOOPBACK, LAN, "http://10.4.4.4:9001", "http://localhost:3000"):
        from urllib.parse import urlsplit

        assert urlsplit(websocket_url_from(base, "/api/ws/receiver")).netloc == (
            urlsplit(base).netloc
        )


def test_https_becomes_wss():
    assert websocket_url_from("https://hq.example.internal", "/api/ws/receiver") == (
        "wss://hq.example.internal/api/ws/receiver"
    )


def test_a_non_default_port_is_carried_through():
    assert websocket_url_from("http://192.168.4.134:9000", "/api/ws/receiver") == (
        "ws://192.168.4.134:9000/api/ws/receiver"
    )


def test_a_trailing_slash_on_the_base_does_not_double_up():
    assert websocket_url_from("http://192.168.4.134:8000/", "/api/ws/receiver") == (
        "ws://192.168.4.134:8000/api/ws/receiver"
    )


# ===========================================================================
# Query strings
# ===========================================================================
def test_a_query_string_is_preserved_exactly():
    url = websocket_url_from(LAN, "/api/ws/broadcaster", query={"ticket": "abc123"})
    assert url == "ws://192.168.4.134:8000/api/ws/broadcaster?ticket=abc123"


def test_a_ticket_is_the_only_thing_that_belongs_in_a_query():
    """A single-use ticket may travel in a URL because it is not reusable. A
    Receiver credential may not, and this helper must not become the place that
    changes."""
    url = websocket_url_from(LAN, "/api/ws/broadcaster", query={"ticket": "t-1"})
    for forbidden in ("token=", "credential=", "password=", "secret="):
        assert forbidden not in url


def test_query_values_are_encoded_rather_than_pasted():
    url = websocket_url_from(LAN, "/api/ws/broadcaster", query={"ticket": "a b&c=d"})
    assert " " not in url
    assert url.count("?") == 1
    assert url.count("&") == 0, "an unescaped value split into a second parameter"


# ===========================================================================
# It cannot invent a host
# ===========================================================================
def test_it_refuses_a_base_url_with_no_host():
    for bad in ("", "   ", "/api", "ws://", "not-a-url"):
        with pytest.raises(ValueError):
            websocket_url_from(bad, "/api/ws/receiver")


def test_it_refuses_a_wildcard_bind_address():
    """0.0.0.0 is what a server binds, never what a client connects to.

    Silently turning it into loopback would be a guess, and guessing is what
    produced the original defect.
    """
    for wildcard in ("http://0.0.0.0:8000", "http://[::]:8000"):
        with pytest.raises(ValueError):
            websocket_url_from(wildcard, "/api/ws/receiver")


def test_it_refuses_a_scheme_it_cannot_map():
    for bad in ("ftp://host:8000", "file:///tmp"):
        with pytest.raises(ValueError):
            websocket_url_from(bad, "/api/ws/receiver")


# ===========================================================================
# The callers no longer carry a literal
# ===========================================================================
def test_the_staging_smoke_builds_no_websocket_url_from_a_literal_host():
    """The regression guard. A future edit that reintroduces a hardcoded host
    in a socket URL fails here rather than at 1225 on somebody's LAN."""
    import re

    source = (REPOSITORY_ROOT / "tools" / "receiver_device_staging_smoke.py").read_text(
        encoding="utf-8"
    )
    offenders = re.findall(r'f?"wss?://(?:127\.0\.0\.1|localhost)[^"]*"', source)
    assert offenders == [], f"websocket URLs built from a literal host: {offenders}"


def test_the_smoke_still_binds_loopback_for_its_own_backend():
    """Binding loopback is right for the isolated smoke - it is unreachable from
    anywhere else by construction. What was wrong was assuming it on the client
    side. The two are different decisions and only one of them changed.
    """
    source = (REPOSITORY_ROOT / "tools" / "receiver_device_staging_smoke.py").read_text(
        encoding="utf-8"
    )
    assert '"--host", "127.0.0.1"' in source
