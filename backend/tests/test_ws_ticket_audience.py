"""The WebSocket handshake ticket must carry an audience and a permission.

THE DEFECT THIS FILE EXISTS FOR

``POST /api/auth/ws-ticket`` was authenticated-only for every role, and
``WebSocketTicketStore.issue`` stored nothing but ``(user_id, expiry)`` - no
audience, no purpose. ``/api/ws/broadcaster`` then redeemed the ticket and
**discarded the returned user id**, with no ``require(...)``, no role lookup and
no re-read of the account.

So a VIEWER - an account whose entire definition is read-only, and which is
refused by every broadcast HTTP route - could:

* mint a ticket over the ordinary authenticated API,
* connect to the mic uplink, and
* push arbitrary WebM/Opus audio to the loudspeakers of every targeted Store,

or simply occupy the single broadcaster slot and deny it to the operator who is
allowed to use it.

The same ticket was equally valid on ``/api/ws/hq`` and ``/api/ws/broadcaster``,
so even a correctly-permissioned dashboard ticket was a mic uplink ticket.

WHAT THE FIX HAS TO DO

An audience on the ticket is not enough on its own: a permission checked only at
mint time is a permission checked once, and an account can be demoted or disabled
in the seconds before the handshake. So the permission is required to mint AND
re-checked against a freshly loaded account at redemption.
"""

from __future__ import annotations

import importlib
import os
import secrets
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

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

from ws_tickets import (  # noqa: E402
    AUDIENCE_BROADCASTER,
    AUDIENCE_HQ,
    TicketRejected,
    WebSocketTicketStore,
)


# ===========================================================================
# The store: an audience is part of the ticket, not advice
# ===========================================================================
def test_a_ticket_minted_for_the_dashboard_is_refused_on_the_uplink():
    """The core separation. Before this, one ticket opened both sockets."""
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="7", audience=AUDIENCE_HQ)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_BROADCASTER)


def test_a_ticket_minted_for_the_uplink_is_refused_on_the_dashboard():
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="7", audience=AUDIENCE_BROADCASTER)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_HQ)


def test_a_matching_audience_redeems_and_returns_the_user():
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="7", audience=AUDIENCE_BROADCASTER)
    assert store.redeem(ticket, audience=AUDIENCE_BROADCASTER) == "7"


def test_a_wrong_audience_still_spends_the_ticket():
    """Otherwise the mismatch is a free oracle: present a ticket at the wrong
    socket, learn nothing was lost, try the other one."""
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="7", audience=AUDIENCE_HQ)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_BROADCASTER)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_HQ)


def test_an_unknown_audience_is_refused_rather_than_treated_as_a_wildcard():
    store = WebSocketTicketStore()
    with pytest.raises(TicketRejected):
        store.issue(user_id="7", audience="anything-else")


def test_an_audience_is_required_and_has_no_default():
    """A default audience is the bug again with extra steps: whichever value it
    defaulted to would be mintable by any authenticated account."""
    import inspect

    signature = inspect.signature(WebSocketTicketStore.issue)
    assert signature.parameters["audience"].default is inspect.Parameter.empty
    redeem = inspect.signature(WebSocketTicketStore.redeem)
    assert redeem.parameters["audience"].default is inspect.Parameter.empty


def test_expiry_still_applies_per_audience():
    store = WebSocketTicketStore(ttl_seconds=1)
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    ticket = store.issue(user_id="7", audience=AUDIENCE_HQ, now=past)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_HQ)


# ===========================================================================
# The routes
# ===========================================================================
RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("ws-audience")
    database = root / "audience.db"
    container = root / "keys.bin"
    environment = {
        "SPEAKLINK_DB_PATH": str(database),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"aud-{secrets.token_hex(5)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
        "SPEAKLINK_KEY_CONTAINER": str(container),
        "SPEAKLINK_KEY_PROTECTOR": "fake",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    for name in RUNTIME_MODULES:
        sys.modules.pop(name, None)
    db_module = None
    try:
        db_module = importlib.import_module("db")
        server = importlib.import_module("server")
        models = importlib.import_module("models")
        assert Path(db_module.DB_PATH) == database.resolve()
        server.startup_event()
        with db_module.SessionLocal() as db:
            owner = db.query(models.HQUser).first()
            yield SimpleNamespace(server=server, db=db_module, models=models, owner=owner)
    finally:
        if db_module is not None:
            db_module.engine.dispose()
        for name in RUNTIME_MODULES:
            sys.modules.pop(name, None)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _viewer():
    return SimpleNamespace(id=4242, username="a-viewer", role="VIEWER",
                           is_active=True, session_version=1,
                           lifecycle_state="active")


def test_a_viewer_cannot_mint_a_broadcaster_ticket(runtime):
    """The whole point. A read-only account must not be able to obtain the
    credential that opens the microphone uplink."""
    from fastapi import HTTPException
    from schemas import WebSocketTicketRequest

    with pytest.raises((HTTPException, Exception)) as refusal:
        runtime.server.issue_websocket_ticket(
            WebSocketTicketRequest(audience=AUDIENCE_BROADCASTER), user=_viewer())
    assert "permission" in str(refusal.value).lower() or getattr(
        refusal.value, "status_code", None) in (401, 403)


def test_a_viewer_may_still_mint_a_dashboard_ticket(runtime):
    """A VIEWER is supposed to watch. Breaking that would be a different bug."""
    from schemas import WebSocketTicketRequest

    result = runtime.server.issue_websocket_ticket(
        WebSocketTicketRequest(audience=AUDIENCE_HQ), user=_viewer())
    assert result["ticket"]


def test_an_operator_with_start_broadcast_may_mint_a_broadcaster_ticket(runtime):
    from schemas import WebSocketTicketRequest

    result = runtime.server.issue_websocket_ticket(
        WebSocketTicketRequest(audience=AUDIENCE_BROADCASTER), user=runtime.owner)
    assert result["ticket"]


def test_the_broadcaster_socket_re_checks_the_permission_at_handshake(runtime):
    """A permission checked only at mint time is checked once. An account can be
    demoted or disabled in the seconds before the handshake, and the ticket it
    already holds must stop working."""
    import inspect

    source = inspect.getsource(runtime.server.ws_broadcaster)
    assert "AUDIENCE_BROADCASTER" in source, "the socket does not pin its audience"
    assert "START_BROADCAST" in source, "the socket never re-checks the permission"
    # And it must not throw the redeemed identity away, which is what it used to do.
    assert "= ws_ticket_store.redeem(" in source, (
        "the redeemed user id is discarded, so no account can be re-checked")


def test_the_dashboard_socket_pins_its_own_audience(runtime):
    import inspect

    source = inspect.getsource(runtime.server.ws_hq)
    assert "AUDIENCE_HQ" in source


def test_the_ticket_route_is_still_in_the_rbac_matrix():
    matrix = (BACKEND_ROOT / "tests" / "test_rbac_endpoint_matrix.py").read_text(
        encoding="utf-8")
    assert "issue_websocket_ticket" in matrix


def test_the_frontend_asks_for_the_audience_it_needs():
    """Two call sites, two different sockets. A frontend that asked for a
    broadcaster ticket to feed the dashboard would hand every viewer the
    stronger credential again.

    This logic moved from BroadcastConsole.jsx into BroadcastContext.js so the
    live broadcast/session state (and this ticket handshake) survives
    navigating away from the Console and back - see
    frontend/src/contexts/BroadcastContext.js.
    """
    console = (REPOSITORY_ROOT / "frontend" / "src" / "contexts" /
               "BroadcastContext.js").read_text(encoding="utf-8")
    assert console.count("/auth/ws-ticket") == 2
    assert '"hq"' in console or "'hq'" in console
    assert '"broadcaster"' in console or "'broadcaster'" in console
