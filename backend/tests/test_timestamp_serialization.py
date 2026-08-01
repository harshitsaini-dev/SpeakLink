"""API timestamps must never let a browser guess the wrong timezone.

Real evidence: the very first live broadcast showed an elapsed timer of
roughly 05:30:28 immediately after "Start Live Broadcast" - not ~00:00:00.
That offset is exactly UTC+05:30, the IST offset of the browser that observed
it. Root cause: SQLite drops tzinfo on round-trip, so ``BroadcastSession.
started_at`` came back from the ORM as a naive Python ``datetime`` (UTC by this
project's storage convention). Pydantic's default JSON serialization of a
naive datetime omits any offset - e.g. "2026-08-01T05:30:00.123456" - and a
browser's ``new Date(...)`` parses a string with no offset as LOCAL time. On
an IST browser that silently shifted every timestamp by UTC+05:30.

These tests pin the fix at the level the defect lives: the literal JSON string
the API sends, not just the underlying datetime value.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from datetime import datetime, timezone
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

PASSWORD = "a-long-enough-temporary-password"

# A UTC ISO-8601 timestamp either ends in "Z" or carries an explicit numeric
# offset. Neither of these was true of the old (broken) output.
_HAS_EXPLICIT_UTC_MARKER = re.compile(r"(Z|[+-]\d{2}:\d{2})$")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    database = tmp_path / "hq.db"
    monkeypatch.setenv("ECHOCAST_DB_PATH", str(database))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)

    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas")]:
        sys.modules.pop(module, None)

    from fastapi.testclient import TestClient
    import server as server_module

    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client):
    response = client.post("/api/auth/login",
                            json={"username": "founder", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _start_and_stop_one_session(client, headers):
    stores = client.get("/api/stores", headers=headers).json()
    assert stores, "the seeded catalog must be non-empty for this test to mean anything"
    store_id = stores[0]["id"]

    created = client.post(
        "/api/broadcast/sessions",
        json={"campaign_name": "timestamp regression", "target_mode": "selected",
              "store_ids": [store_id]},
        headers=headers,
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    started = client.post(f"/api/broadcast/sessions/{session_id}/start", headers=headers)
    assert started.status_code == 200, started.text

    stopped = client.post(f"/api/broadcast/sessions/{session_id}/stop", headers=headers)
    assert stopped.status_code == 200, stopped.text
    return session_id


def test_session_out_started_at_carries_an_explicit_utc_marker(client):
    headers = sign_in(client)
    session_id = _start_and_stop_one_session(client, headers)

    detail = client.get(f"/api/broadcast/sessions/{session_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()

    for field in ("started_at", "ended_at", "created_at"):
        value = body[field]
        assert value is not None, field
        assert _HAS_EXPLICIT_UTC_MARKER.search(value), (
            f"{field}={value!r} has no explicit UTC marker - a browser's "
            "new Date(...) would silently parse this as local time"
        )


def test_broadcast_history_timestamps_carry_an_explicit_utc_marker(client):
    headers = sign_in(client)
    _start_and_stop_one_session(client, headers)

    history = client.get("/api/broadcast/history", headers=headers)
    assert history.status_code == 200, history.text
    rows = history.json()
    assert rows
    for row in rows:
        assert _HAS_EXPLICIT_UTC_MARKER.search(row["started_at"])
        assert _HAS_EXPLICIT_UTC_MARKER.search(row["created_at"])


def test_system_log_timestamps_carry_an_explicit_utc_marker(client):
    headers = sign_in(client)
    _start_and_stop_one_session(client, headers)  # generates at least one log row

    logs = client.get("/api/logs", headers=headers)
    assert logs.status_code == 200, logs.text
    rows = logs.json()
    assert rows
    for row in rows:
        assert _HAS_EXPLICIT_UTC_MARKER.search(row["created_at"])


def test_no_utc_plus_0530_regression_for_a_session_started_right_now(client):
    """The exact regression: parse the API's own string as a browser would.

    ``datetime.fromisoformat`` understands both "Z" and "+00:00"/"+05:30"
    suffixes (Python 3.11+), which is the same class of unambiguous parsing a
    correct browser Date parse relies on. If the API ever regresses to naive
    output, this parses as a naive datetime with no tzinfo and the interval
    computed below silently drifts to ~5.5 hours - the exact prior defect.
    """
    headers = sign_in(client)
    before = datetime.now(timezone.utc)
    session_id = _start_and_stop_one_session(client, headers)
    after = datetime.now(timezone.utc)

    detail = client.get(f"/api/broadcast/sessions/{session_id}", headers=headers).json()
    parsed = datetime.fromisoformat(detail["started_at"])

    assert parsed.tzinfo is not None, "a timezone-naive value is exactly the prior defect"
    assert before - pytest_relaxed_delta() <= parsed <= after + pytest_relaxed_delta()


def pytest_relaxed_delta():
    from datetime import timedelta
    return timedelta(seconds=5)
