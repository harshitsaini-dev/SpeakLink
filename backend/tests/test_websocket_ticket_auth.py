"""A reusable credential must never travel in a WebSocket URL.

Found while reading the backend access log during the one-Store Bluetooth
amplifier live test. Uvicorn logged, in full:

    INFO: ('127.0.0.1', 56087) - "WebSocket /api/ws/hq?token=<a complete JWT>" [accepted]
    INFO: ('127.0.0.1', 57575) - "WebSocket /api/ws/broadcaster?token=<a complete JWT>" [accepted]

The token in those lines is a real, reusable access token. A URL is the least
private part of a request: it reaches access logs, proxy logs, crash reports and
browser history, and it is not covered by TLS at the proxy boundary. Anyone who
can read a log line can replay the session.

The Receiver socket was already correct - it authenticates with an Authorization
header - so only the two HQ sockets were affected.

The fix is a single-use, short-lived ticket. Browsers cannot set headers on a
WebSocket handshake, so a query parameter is unavoidable; what travels in it
must therefore be worthless once used and worthless after a few seconds.

Nothing here opens a socket, starts a server or touches a database.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from ws_tickets import (  # noqa: E402
    AUDIENCE_HQ,
    TICKET_TTL_SECONDS,
    WebSocketTicketStore,
    TicketRejected,
)


SERVER_SOURCE = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
BASE = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _endpoint_signature(path: str) -> str:
    """The decorator and def line for one websocket route."""
    match = re.search(
        rf'@app\.websocket\("{re.escape(path)}"\)\s*\n\s*async def \w+\((.*?)\):',
        SERVER_SOURCE,
        re.DOTALL,
    )
    assert match, f"no websocket endpoint found for {path}"
    return match.group(1)


# ---------------------------------------------------------------------------
# No reusable credential in a URL
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/api/ws/hq", "/api/ws/broadcaster"])
def test_hq_sockets_do_not_accept_a_token_query_parameter(path: str):
    signature = _endpoint_signature(path)
    assert "token" not in signature, (
        f"{path} still takes a token in its query string: {signature.strip()!r}. "
        "Uvicorn writes the full URL to its access log, so the credential is "
        "logged in clear on every connection."
    )


@pytest.mark.parametrize("path", ["/api/ws/hq", "/api/ws/broadcaster"])
def test_hq_sockets_authenticate_with_a_single_use_ticket(path: str):
    signature = _endpoint_signature(path)
    assert "ticket" in signature, f"{path} does not take a ticket: {signature.strip()!r}"


def test_the_receiver_socket_still_uses_an_authorization_header():
    """It was never affected, and must not regress into the URL."""
    signature = _endpoint_signature("/api/ws/receiver")
    assert "token" not in signature and "ticket" not in signature, (
        "the Receiver socket must keep authenticating with its Bearer header"
    )
    assert "_receiver_bearer_token" in SERVER_SOURCE


def test_no_websocket_route_declares_a_query_credential():
    """A catch-all: any future socket that adds one fails here."""
    offenders = []
    for match in re.finditer(
        r'@app\.websocket\("([^"]+)"\)\s*\n\s*async def \w+\((.*?)\):', SERVER_SOURCE, re.DOTALL
    ):
        route, signature = match.group(1), match.group(2)
        for secret in ("token", "password", "secret", "authorization"):
            if re.search(rf"\b{secret}\b\s*:\s*str\s*=\s*Query", signature):
                offenders.append(f"{route} -> {secret}")
    assert not offenders, "credentials must not be Query parameters: " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# What the ticket actually guarantees
# ---------------------------------------------------------------------------
def test_a_ticket_can_be_redeemed_once():
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="1", audience=AUDIENCE_HQ, now=BASE)
    assert store.redeem(ticket, audience=AUDIENCE_HQ, now=BASE) == "1"


def test_a_ticket_cannot_be_redeemed_twice():
    """Replaying a logged URL must not work, even within the TTL."""
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="1", audience=AUDIENCE_HQ, now=BASE)
    store.redeem(ticket, audience=AUDIENCE_HQ, now=BASE)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_HQ, now=BASE)


def test_an_expired_ticket_is_refused():
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="1", audience=AUDIENCE_HQ, now=BASE)
    later = BASE + timedelta(seconds=TICKET_TTL_SECONDS + 1)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_HQ, now=later)


def test_a_ticket_is_still_valid_at_the_edge_of_its_window():
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="1", audience=AUDIENCE_HQ, now=BASE)
    edge = BASE + timedelta(seconds=TICKET_TTL_SECONDS - 1)
    assert store.redeem(ticket, audience=AUDIENCE_HQ, now=edge) == "1"


def test_an_unknown_ticket_is_refused():
    store = WebSocketTicketStore()
    with pytest.raises(TicketRejected):
        store.redeem("never-issued", audience=AUDIENCE_HQ, now=BASE)


def test_an_empty_ticket_is_refused():
    store = WebSocketTicketStore()
    for value in ("", None):
        with pytest.raises(TicketRejected):
            store.redeem(value, audience=AUDIENCE_HQ, now=BASE)


def test_the_ttl_is_short_enough_to_be_worth_little_if_logged():
    """It is a handshake credential, not a session. Seconds, not minutes."""
    assert 0 < TICKET_TTL_SECONDS <= 60


def test_a_ticket_carries_no_readable_claims():
    """Unlike a JWT, a logged ticket must reveal nothing about the user.

    The identifier is deliberately long and distinctive. An earlier version of
    this test used ``user_id="pilot-operator-42"`` and asserted ``"42" not in
    ticket`` - but a ticket is ~43 random URL-safe characters, so a two-character
    substring turns up by chance in roughly one run in sixteen. It failed once in
    a full-suite run here and passed five times in a row afterwards, which is the
    worst way for a test to behave: it looks like a real regression and is not.

    A substring that cannot plausibly occur by accident tests the property the
    name claims; a short one tests the random number generator's mood.
    """
    store = WebSocketTicketStore()
    identifier = "pilot-operator-distinctive-user-identifier-9f3a7c"
    ticket = store.issue(user_id=identifier, audience=AUDIENCE_HQ, now=BASE)

    assert identifier not in ticket
    for fragment in ("pilot-operator", "distinctive", "9f3a7c"):
        assert fragment not in ticket
    # A JWT is three base64 segments separated by dots.
    assert ticket.count(".") == 0
    assert not ticket.startswith("eyJ")


def test_no_ticket_in_a_large_batch_embeds_the_user_identifier():
    """The same property, asserted often enough that a one-off pass is not luck."""
    store = WebSocketTicketStore()
    identifier = "operator-b7d2e4a1"
    for _ in range(200):
        assert identifier not in store.issue(user_id=identifier, audience=AUDIENCE_HQ, now=BASE)


def test_tickets_are_unique_and_unguessable_in_length():
    store = WebSocketTicketStore()
    issued = {store.issue(user_id="1", audience=AUDIENCE_HQ, now=BASE) for _ in range(200)}
    assert len(issued) == 200, "tickets collided"
    assert all(len(value) >= 32 for value in issued)


def test_expired_tickets_do_not_accumulate_forever():
    """The store is in-memory in a single worker; it must not grow unbounded."""
    store = WebSocketTicketStore()
    for _ in range(50):
        store.issue(user_id="1", audience=AUDIENCE_HQ, now=BASE)
    assert store.outstanding(now=BASE) == 50

    later = BASE + timedelta(seconds=TICKET_TTL_SECONDS + 1)
    store.issue(user_id="1", audience=AUDIENCE_HQ, now=later)
    assert store.outstanding(now=later) == 1, "expired tickets were never purged"


def test_redeeming_returns_the_issuing_user():
    store = WebSocketTicketStore()
    first = store.issue(user_id="7", audience=AUDIENCE_HQ, now=BASE)
    second = store.issue(user_id="9", audience=AUDIENCE_HQ, now=BASE)
    assert store.redeem(second, audience=AUDIENCE_HQ, now=BASE) == "9"
    assert store.redeem(first, audience=AUDIENCE_HQ, now=BASE) == "7"


# ---------------------------------------------------------------------------
# What an access log would now contain
# ---------------------------------------------------------------------------
def test_a_logged_handshake_url_reveals_no_reusable_credential():
    store = WebSocketTicketStore()
    ticket = store.issue(user_id="1", audience=AUDIENCE_HQ, now=BASE)
    logged = f'"WebSocket /api/ws/hq?ticket={ticket}" [accepted]'

    # Whatever a log scraper finds, it cannot be replayed once the socket opened.
    store.redeem(ticket, audience=AUDIENCE_HQ, now=BASE)
    with pytest.raises(TicketRejected):
        store.redeem(ticket, audience=AUDIENCE_HQ, now=BASE)

    assert "eyJ" not in logged, "a JWT reached the log line"
    assert "Bearer" not in logged


def test_the_ticket_endpoint_exists_and_is_authenticated():
    match = re.search(
        r'@api\.post\("/auth/ws-ticket".*?\)\s*\ndef \w+\((.*?)\):', SERVER_SOURCE, re.DOTALL
    )
    assert match, "there is no POST /api/auth/ws-ticket endpoint"
    assert "get_current_user" in match.group(1), (
        "the ticket endpoint must require an authenticated caller, or it is a "
        "free credential dispenser"
    )
