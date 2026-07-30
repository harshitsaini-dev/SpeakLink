"""The per-Store audio queue metrics, exposed so somebody can actually read them.

WHY THIS EXISTS

``WSManager.audio_metrics()`` has always existed and was reachable from nowhere:
no route, no CLI, no log line. The bounded-queue counters that tell an operator
"Store 12 is nearly dropping audio" were computed every broadcast and thrown
away. A metric nobody can read is a metric that does not operationally exist -
and it is also the evidence a load test needs, so the load report was reduced to
inferring drops from what synthetic Receivers happened to receive, which cannot
see a chunk the server dropped before sending.

WHAT IT MUST NOT BECOME

A metrics endpoint is a lovely place to leak things. It returns integers only -
no audio payload, no Store token, no Device credential, no connection id - and a
test asserts the shape rather than trusting the implementation to stay careful.
"""

from __future__ import annotations

import importlib
import os
import secrets
import sys
import tempfile
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

RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")


@pytest.fixture(scope="module")
def runtime(tmp_path_factory):
    root = tmp_path_factory.mktemp("audio-metrics")
    database = root / "metrics.db"
    container = root / "keys.bin"

    environment = {
        "SPEAKLINK_DB_PATH": str(database),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"metrics-{secrets.token_hex(5)}",
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
            operator = db.query(models.HQUser).first()
            yield SimpleNamespace(server=server, db=db_module, operator=operator)
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


def test_the_endpoint_exists_and_reports_a_mapping(runtime):
    result = runtime.server.read_audio_metrics(user=runtime.operator)
    assert isinstance(result, dict)
    assert "stores" in result


def test_with_no_live_session_the_store_list_is_empty(runtime):
    result = runtime.server.read_audio_metrics(user=runtime.operator)
    assert result["stores"] == []


def test_every_reported_value_is_an_integer(runtime):
    """A metrics response of integers cannot carry a payload, a token or a name.
    Asserted rather than trusted, because a future field is exactly how one
    would get in."""
    import asyncio

    async def scenario():
        await runtime.server.manager.audio_fanout.start_store(7, lambda _c: None)
        try:
            return runtime.server.read_audio_metrics(user=runtime.operator)
        finally:
            await runtime.server.manager.audio_fanout.stop_all()

    result = asyncio.run(scenario())
    assert result["stores"], "the scenario built no queue, so it proves nothing"
    for entry in result["stores"]:
        for key, value in entry.items():
            assert isinstance(value, int), f"{key} is {type(value).__name__}, not an int"


def test_the_reported_keys_are_the_bounded_queue_counters(runtime):
    import asyncio

    async def scenario():
        await runtime.server.manager.audio_fanout.start_store(7, lambda _c: None)
        try:
            return runtime.server.read_audio_metrics(user=runtime.operator)
        finally:
            await runtime.server.manager.audio_fanout.stop_all()

    result = asyncio.run(scenario())
    entry = result["stores"][0]
    for expected in ("store_id", "capacity", "depth", "max_depth", "delivered",
                     "dropped", "enqueued"):
        assert expected in entry, f"{expected} is missing from the metrics"


def test_no_forbidden_field_can_appear(runtime):
    import asyncio

    async def scenario():
        await runtime.server.manager.audio_fanout.start_store(7, lambda _c: None)
        try:
            return runtime.server.read_audio_metrics(user=runtime.operator)
        finally:
            await runtime.server.manager.audio_fanout.stop_all()

    result = asyncio.run(scenario())
    import json

    body = json.dumps(result).lower()
    for forbidden in ("token", "credential", "secret", "password", "connection_id",
                      "receiver_token", "chunk", "payload"):
        assert forbidden not in body, f"{forbidden} appears in the metrics response"


def test_a_viewer_may_read_broadcast_status_metrics(runtime):
    """VIEW_STATUS, not MANAGE_*: this is operational health an on-call person
    needs, and it carries nothing sensitive.

    ``is_active=True`` is not decoration. effective_permissions() checks it
    BEFORE it looks at the role, so an inactive account gets the empty set
    whatever it claims to be - my first version of this fixture left it off and
    the permission was correctly refused. Failing closed on a malformed user is
    the behaviour worth having, so the fixture was wrong, not the code.
    """
    from rbac import Permission, require_permission

    viewer = SimpleNamespace(id=999, username="viewer", role="VIEWER",
                             is_active=True, session_version=1,
                             lifecycle_state="active")
    require_permission(viewer, Permission.VIEW_STATUS)  # must not raise


def test_an_inactive_viewer_is_refused_whatever_its_role_says(runtime):
    """The other half, and the reason the check is ordered that way: a disabled
    account is a disabled account before it is a VIEWER."""
    from rbac import Permission, PermissionDenied, require_permission

    disabled = SimpleNamespace(id=999, username="viewer", role="VIEWER",
                               is_active=False, session_version=1,
                               lifecycle_state="disabled")
    with pytest.raises(PermissionDenied):
        require_permission(disabled, Permission.VIEW_STATUS)


def test_the_route_declares_view_status(runtime):
    import inspect

    source = inspect.getsource(runtime.server.read_audio_metrics)
    assert "require(Permission.VIEW_STATUS)" in source


def test_the_endpoint_is_registered_in_the_rbac_matrix():
    """The repository's own guard requires every authenticated route to be
    listed. A new route that is not is a route nobody reviewed."""
    matrix = (BACKEND_ROOT / "tests" / "test_rbac_endpoint_matrix.py").read_text(
        encoding="utf-8")
    assert "read_audio_metrics" in matrix
