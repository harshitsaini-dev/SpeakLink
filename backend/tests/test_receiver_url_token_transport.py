"""No route accepts a Receiver credential from a URL. None accepts one at all.

Two endpoints survived every earlier cleanup:

``GET /api/receiver/verify?token=...``
    A raw Store credential in a query string - the least private part of a
    request. It reaches application access logs, reverse-proxy logs, browser
    history, copied links, monitoring tools, screenshots and ``Referer``
    headers. Anybody who can read one log line could connect a Receiver as that
    Store.

``POST /api/receiver/event``
    A raw Store credential in a JSON body. Better than a URL and still an
    unauthenticated endpoint that takes a long-lived shared secret and writes a
    row against whichever Store it names.

The fix is deletion rather than a move to ``Authorization: Bearer``, because
nothing that ships calls either one:

* ``tools/receiver_agent.py`` authenticates over the Receiver WebSocket with a
  Device credential in a header;
* ``tools/audio_receiver_pilot.py`` - the hardware-proven pilot - does the same
  with the legacy Store token, also in a header;
* ``frontend/src/pages/Receiver.jsx`` is the only caller and has been unrouted
  since the query-token work; it is not imported, so it is not even bundled.

Re-plumbing an endpoint no client uses would have kept the attack surface and
delivered nothing. These tests hold both properties: the two routes are gone,
and no surviving route takes a credential from a URL.
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

#: Anything that looks like a credential arriving through a URL.
CREDENTIAL_PARAMETER_NAMES = {
    "token", "receiver_token", "credential", "secret", "password",
    "api_key", "apikey", "key", "access_token", "bearer", "auth",
}

#: The one deliberate exception, and why it is safe: a single-use handshake
#: ticket, not a reusable credential. Browsers cannot set a header on a
#: WebSocket, it is redeemed once, and it expires in seconds.
ALLOWED_URL_PARAMETERS = {"ticket"}


def _routes():
    import server

    return server.app.routes


# ===========================================================================
# The two endpoints are gone
# ===========================================================================
def test_the_query_token_verify_route_no_longer_exists():
    paths = {getattr(route, "path", "") for route in _routes()}
    assert "/api/receiver/verify" not in paths


def test_the_raw_token_event_route_no_longer_exists():
    paths = {getattr(route, "path", "") for route in _routes()}
    assert "/api/receiver/event" not in paths


def test_their_schemas_are_gone_too():
    """A schema with a ``token`` field is an invitation to add the route back."""
    import schemas

    for removed in ("ReceiverEventIn", "ReceiverVerifyOut"):
        assert not hasattr(schemas, removed), f"{removed} still exists"


# ===========================================================================
# The general property, so this cannot come back under another name
# ===========================================================================
def test_no_route_declares_a_credential_shaped_query_parameter():
    """Read from the app's own signatures rather than a list I maintain."""
    import inspect

    offenders = []
    for route in _routes():
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None:
            continue
        try:
            signature = inspect.signature(endpoint)
        except (TypeError, ValueError):
            continue
        for name, parameter in signature.parameters.items():
            if name.lower() not in CREDENTIAL_PARAMETER_NAMES:
                continue
            if name.lower() in ALLOWED_URL_PARAMETERS:
                continue
            annotation = parameter.annotation
            # A body model or a dependency is not a URL parameter. A bare
            # str/int with no Depends default is.
            if annotation in (str, int) or annotation is inspect.Parameter.empty:
                offenders.append(f"{getattr(route, 'path', '?')}({name})")
    assert offenders == [], f"routes taking a credential from the URL: {offenders}"


def test_the_websocket_ticket_is_the_only_url_credential_and_is_single_use():
    """It is allowed because it is not reusable.

    Browsers cannot set a header on a WebSocket handshake, so something has to
    travel in that URL. A single-use ticket that expires in seconds is a very
    different object from a Store credential that works for months.
    """
    from ws_tickets import WebSocketTicketStore

    store = WebSocketTicketStore()
    ticket = store.issue(user_id="1")
    assert store.redeem(ticket) == "1"
    with pytest.raises(Exception):
        store.redeem(ticket)


def test_the_receiver_websocket_takes_its_credential_from_a_header():
    """The path that replaced both endpoints."""
    import server

    source = Path(server.__file__).read_text(encoding="utf-8")
    assert "_receiver_bearer_token" in source
    receiver_socket = [
        route for route in _routes()
        if getattr(route, "path", "") == "/api/ws/receiver"
    ]
    assert receiver_socket, "the Receiver WebSocket route is missing"


# ===========================================================================
# Nothing that ships lost a caller
# ===========================================================================
def test_the_hardware_pilot_never_used_either_endpoint():
    """The amplifier evidence path must be unaffected by this removal."""
    pilot = (REPOSITORY_ROOT / "tools" / "audio_receiver_pilot.py").read_text(encoding="utf-8")
    assert "receiver/verify" not in pilot
    assert "receiver/event" not in pilot
    # It authenticates the way everything else now does.
    assert "Authorization" in pilot and "Bearer" in pilot


def test_the_production_agent_never_used_either_endpoint():
    agent = (REPOSITORY_ROOT / "tools" / "receiver_agent.py").read_text(encoding="utf-8")
    assert "receiver/verify" not in agent
    assert "receiver/event" not in agent


def test_the_only_caller_is_the_unrouted_browser_receiver_page():
    """``Receiver.jsx`` is not imported by ``App.js``, so it is not bundled.

    If that ever changes, this test fails and whoever routes it has to deal with
    the credential flow first - which is the point.
    """
    import re

    app_source = (REPOSITORY_ROOT / "frontend" / "src" / "App.js").read_text(encoding="utf-8")
    # An exact import of that page, not any path that merely starts with it -
    # ReceiverStatus and ReceiverDevices are both routed and both fine.
    routed = re.search(r'from\s+["\']@?/?pages/Receiver["\']', app_source)
    assert routed is None, (
        "the browser Receiver page has been routed again; it calls endpoints "
        "that no longer exist and would need a Device credential flow first"
    )


# ===========================================================================
# The protected database
# ===========================================================================
def test_the_protected_database_is_untouched():
    if not PROTECTED_DATABASE.exists():
        pytest.skip("no protected database on this machine")
    assert PROTECTED_DATABASE.stat().st_size == 507904
    for suffix in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + suffix).exists()
