"""A reusable credential must travel in a header, never in a URL.

``_extract_token`` accepted an access token from ``?token=`` on ordinary HTTP
requests. The comment called it a WebSocket fallback, and it was - until the HQ
sockets moved to single-use tickets. After that it protected nothing and was
simply a second, worse way to authenticate seventeen authenticated routes.

A URL is the least private part of a request. It reaches application access
logs, reverse-proxy logs, browser history, copied links, monitoring tools,
screenshots and Referer headers. A reusable JWT sitting in one is a session
anybody who reads a log line can take over - and unlike the WebSocket handshake,
ordinary HTTP has no excuse: browsers and every client in this repository can
set an Authorization header perfectly well.

Nothing here binds a port, and no real token, password or hash is printed.
"""

from __future__ import annotations

import os
import tempfile
from datetime import timedelta
from pathlib import Path
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
FRONTEND_SOURCE = REPOSITORY_ROOT / "frontend" / "src"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)
os.environ.setdefault("JWT_SECRET", "test-only-jwt-secret-not-a-real-one")

from auth import create_access_token, get_current_user, hash_password  # noqa: E402
from db import Base  # noqa: E402
from models import HQUser  # noqa: E402


PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"
OPERATOR = "pilot-operator"
PASSPHRASE = "test-only-passphrase-not-real"


class FakeRequest:
    """Only the two things ``_extract_token`` reads."""

    def __init__(self, headers=None, query_params=None) -> None:
        self.headers = headers or {}
        self.query_params = query_params or {}


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'transport.db').as_posix()}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    with factory() as db:
        db.add(HQUser(username=OPERATOR, password_hash=hash_password(PASSPHRASE), role="admin"))
        db.commit()
    yield factory
    engine.dispose()


@pytest.fixture()
def valid_token(session_factory) -> str:
    with session_factory() as db:
        user = db.query(HQUser).one()
        return create_access_token(user.id, user.username)


def _authenticate(session_factory, request: FakeRequest):
    from fastapi import HTTPException

    with session_factory() as db:
        try:
            return get_current_user(request, db=db), None
        except HTTPException as refusal:
            return None, refusal


# ---------------------------------------------------------------------------
# A token in a URL must not authenticate anything
# ---------------------------------------------------------------------------
def test_a_query_token_alone_is_rejected(session_factory, valid_token):
    user, refusal = _authenticate(
        session_factory, FakeRequest(query_params={"token": valid_token})
    )
    assert user is None
    assert refusal.status_code == 401


def test_even_a_perfectly_valid_jwt_is_rejected_in_the_query_string(session_factory, valid_token):
    """The token is genuine and unexpired. The transport is what is refused."""
    header_user, header_refusal = _authenticate(
        session_factory, FakeRequest(headers={"Authorization": f"Bearer {valid_token}"})
    )
    assert header_refusal is None and header_user is not None

    query_user, query_refusal = _authenticate(
        session_factory, FakeRequest(query_params={"token": valid_token})
    )
    assert query_user is None
    assert query_refusal.status_code == 401


def test_a_query_token_cannot_rescue_a_malformed_header(session_factory, valid_token):
    user, refusal = _authenticate(
        session_factory,
        FakeRequest(
            headers={"Authorization": "Basic something"},
            query_params={"token": valid_token},
        ),
    )
    assert user is None
    assert refusal.status_code == 401


@pytest.mark.parametrize("parameter", ["access_token", "jwt", "auth", "api_key", "ticket"])
def test_no_other_query_parameter_authenticates_either(session_factory, valid_token, parameter):
    user, refusal = _authenticate(
        session_factory, FakeRequest(query_params={parameter: valid_token})
    )
    assert user is None
    assert refusal.status_code == 401


def test_the_refusal_never_echoes_the_supplied_value(session_factory, valid_token):
    _, refusal = _authenticate(session_factory, FakeRequest(query_params={"token": valid_token}))
    rendered = f"{refusal.detail} {getattr(refusal, 'headers', None)}"
    assert valid_token not in rendered
    assert valid_token[:16] not in rendered


def test_a_refused_request_produces_no_user_context(session_factory, valid_token):
    user, refusal = _authenticate(
        session_factory, FakeRequest(query_params={"token": valid_token})
    )
    assert user is None
    assert refusal is not None


def test_nothing_in_the_auth_module_logs_or_prints(session_factory, valid_token, capsys):
    _authenticate(session_factory, FakeRequest(query_params={"token": valid_token}))
    captured = capsys.readouterr()
    for stream in (captured.out, captured.err):
        assert valid_token not in stream


# ---------------------------------------------------------------------------
# The header path must keep working exactly as it did
# ---------------------------------------------------------------------------
def test_a_bearer_header_still_authenticates(session_factory, valid_token):
    user, refusal = _authenticate(
        session_factory, FakeRequest(headers={"Authorization": f"Bearer {valid_token}"})
    )
    assert refusal is None
    assert user is not None and user.username == OPERATOR


def test_a_missing_header_is_the_existing_controlled_refusal(session_factory):
    user, refusal = _authenticate(session_factory, FakeRequest())
    assert user is None
    assert refusal.status_code == 401
    assert refusal.detail == "Not authenticated"


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "bearer", "Basic abc", "Token abc", "Bearer  ", "BearerX abc"],
)
def test_a_malformed_authorization_header_is_rejected(session_factory, header):
    user, refusal = _authenticate(session_factory, FakeRequest(headers={"Authorization": header}))
    assert user is None
    assert refusal.status_code == 401


def test_an_expired_token_is_still_rejected_in_the_header(session_factory):
    """Minted directly with an exp in the past.

    Monkeypatching auth.datetime worked serially and was flaky under xdist, and
    it was testing the clock rather than the rule. Signing a token that says it
    expired yesterday is both simpler and exactly the case that matters.
    """
    import jwt
    from datetime import datetime, timezone

    from auth import ACCESS_TOKEN_EXPIRE_HOURS, JWT_ALGORITHM, get_jwt_secret

    with session_factory() as db:
        user = db.query(HQUser).one()
        user_id, username = user.id, user.username

    issued = datetime.now(timezone.utc) - timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS + 24)
    stale = jwt.encode(
        {
            "sub": str(user_id),
            "username": username,
            "exp": issued + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS),
            "type": "access",
        },
        get_jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )

    user, refusal = _authenticate(
        session_factory, FakeRequest(headers={"Authorization": f"Bearer {stale}"})
    )
    assert user is None
    assert refusal.status_code == 401


# ---------------------------------------------------------------------------
# Source-level guards: the fallback must not come back
# ---------------------------------------------------------------------------
def test_the_auth_module_never_reads_a_token_from_the_query_string():
    import re

    source = (BACKEND_ROOT / "auth.py").read_text(encoding="utf-8")
    assert not re.search(r"query_params", source), (
        "auth.py reads the query string again; a reusable credential in a URL "
        "reaches every access log that request passes through"
    )


def test_no_http_route_declares_a_credential_query_parameter():
    import re

    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    offenders = []
    for match in re.finditer(r"^@api\.(get|post|put|patch|delete)\(.*?\n(?:def|async def) \w+\((.*?)\):",
                             source, re.DOTALL | re.MULTILINE):
        signature = match.group(2)
        for secret in ("token", "password", "secret", "authorization", "ticket"):
            if re.search(rf"\b{secret}\b\s*:\s*[^=]+=\s*Query", signature):
                offenders.append(f"{match.group(0).splitlines()[0]} -> {secret}")
    assert not offenders, "credentials must not be HTTP query parameters: " + "; ".join(offenders)


# ---------------------------------------------------------------------------
# What must NOT change: the WebSocket and Receiver transports
# ---------------------------------------------------------------------------
def test_the_hq_sockets_still_use_single_use_tickets():
    import re

    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    for route in ("/api/ws/hq", "/api/ws/broadcaster"):
        match = re.search(
            rf'@app\.websocket\("{re.escape(route)}"\)\s*\n\s*async def \w+\((.*?)\):',
            source,
            re.DOTALL,
        )
        assert match, f"{route} is missing"
        assert "ticket" in match.group(1)
        assert "token" not in match.group(1)


def test_a_websocket_ticket_is_still_single_use_and_expiring():
    from ws_tickets import AUDIENCE_HQ, TicketRejected, WebSocketTicketStore, TICKET_TTL_SECONDS
    from datetime import datetime, timezone

    base = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    store = WebSocketTicketStore()

    once = store.issue(user_id="1", audience=AUDIENCE_HQ, now=base)
    assert store.redeem(once, audience=AUDIENCE_HQ, now=base) == "1"
    with pytest.raises(TicketRejected):
        store.redeem(once, audience=AUDIENCE_HQ, now=base)

    later = store.issue(user_id="1", audience=AUDIENCE_HQ, now=base)
    with pytest.raises(TicketRejected):
        store.redeem(later, audience=AUDIENCE_HQ, now=base + timedelta(seconds=TICKET_TTL_SECONDS + 1))


def test_the_receiver_socket_still_authenticates_by_header():
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    assert "_receiver_bearer_token" in source
    receiver_source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    # The helper reads headers only; a query read there would be the same defect
    # in a different place.
    helper = receiver_source[receiver_source.index("def _receiver_bearer_token"):]
    helper = helper[: helper.index("\n\n\n")]
    assert "query_params" not in helper


# ---------------------------------------------------------------------------
# The frontend must not build an authenticated URL carrying a reusable token
# ---------------------------------------------------------------------------
def test_the_frontend_sends_its_token_in_an_authorization_header():
    api = (FRONTEND_SOURCE / "lib" / "api.js").read_text(encoding="utf-8")
    assert "config.headers.Authorization" in api
    assert "Bearer" in api


def test_no_frontend_source_puts_a_reusable_token_in_an_api_url():
    import re

    offenders = []
    for path in FRONTEND_SOURCE.rglob("*.js*"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r"[?&]token=\$\{([^}]+)\}", source):
            expression = match.group(1)
            # The Store Management page builds a link to the in-browser Receiver
            # page carrying a Store credential. That is a separate, already
            # recorded finding about that page - not an API authentication URL.
            if "receiver" in source[max(0, match.start() - 120):match.start()].lower():
                continue
            offenders.append(f"{path.relative_to(REPOSITORY_ROOT)} -> {expression}")
    assert not offenders, "a reusable token is being placed in a URL: " + "; ".join(offenders)


def test_the_frontend_websocket_urls_carry_only_a_ticket():
    # This logic moved from BroadcastConsole.jsx into BroadcastContext.js so
    # the live broadcast/session state (and this ticket handshake) survives
    # navigating away from the Console and back - see
    # frontend/src/contexts/BroadcastContext.js.
    console = (FRONTEND_SOURCE / "contexts" / "BroadcastContext.js").read_text(encoding="utf-8")
    assert "ticket=" in console
    assert "/ws/hq\")}?token=" not in console
    assert "/ws/broadcaster\")}?token=" not in console


# ---------------------------------------------------------------------------
# The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_untouched(session_factory, valid_token):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    _authenticate(session_factory, FakeRequest(query_params={"token": valid_token}))
    assert metadata() == before
    for sidecar in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + sidecar).exists()
