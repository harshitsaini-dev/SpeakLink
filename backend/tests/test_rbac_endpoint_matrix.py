"""Every authenticated route names a permission, and names the right one.

The matrix in ``rbac.py`` is only a policy if the routes actually consult it.
This file reads the running app's own routing table and asserts, endpoint by
endpoint, which permission guards it.

It exists because of a real mistake. Wiring the guards with a regex that
mis-matched ``response_model=List[...]`` applied ``MANAGE_DEVICES`` to *every*
authenticated route - including ``GET /api/stores`` and ``GET /api/logs``.
Nothing failed: the app compiled, the tests passed, and a VIEWER simply could
not read a Store list while a BROADCASTER could rotate Receiver credentials. A
table that spells out the expected permission per route is the only thing that
catches that, because the failure looks exactly like a working system.

Two properties, and the second is what makes this durable:

* every route in ``EXPECTED`` is guarded by the permission stated here;
* every authenticated route in the app appears in ``EXPECTED``. A route added
  later without a permission fails this file rather than shipping open.
"""

from __future__ import annotations

import inspect
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

from rbac import Permission  # noqa: E402


#: endpoint function name -> the permission that must guard it.
#:
#: ``SUPER_ADMIN`` marks the handful reserved for the account that also owns
#: security settings. ``None`` marks routes that are authenticated but need no
#: particular permission - knowing who you are is the whole check.
EXPECTED: dict[str, object] = {
    # Identity. Any signed-in account may ask who it is and open its own socket.
    "logout": None,
    "me": None,
    "issue_websocket_ticket": None,
    # Changing your own password needs no permission beyond being signed in.
    # Read-only does not mean unable to secure your own account - and the
    # current password is required, so knowing who you are is not enough.
    "change_own_password": None,

    # HQ Users. MANAGE_USERS covers the lifecycle; which accounts a given role
    # may touch is a second, narrower check inside each endpoint
    # (_require_may_manage), because ADMIN holds MANAGE_USERS but must not be
    # able to disable a SUPER_ADMIN or promote itself into one.
    "list_hq_users": Permission.MANAGE_USERS,
    "read_hq_user": Permission.MANAGE_USERS,
    "create_hq_user": Permission.MANAGE_USERS,
    "update_hq_user": Permission.MANAGE_USERS,
    "set_hq_user_role": Permission.MANAGE_USERS,
    "disable_hq_user": Permission.MANAGE_USERS,
    "enable_hq_user": Permission.MANAGE_USERS,
    "archive_hq_user": Permission.MANAGE_USERS,
    "restore_hq_user": Permission.MANAGE_USERS,
    # Setting somebody else's password is reserved, like restoring an archived
    # Store: it is the action an attacker who took an ADMIN account would use
    # to take a SUPER_ADMIN one.
    "reset_hq_user_password": "OWNER",

    # Dependency summaries, and the dependency-guarded hard deletes.
    #
    # MANAGE_STORES / MANAGE_USERS is the right gate rather than OWNER: the
    # protection lives in what the operation refuses - anything with Devices,
    # targets, events, enrolment codes or recorded history - and
    # _require_may_manage still stops an ADMIN reaching an OWNER account. A role
    # cannot buy its way past a referential check.
    "read_store_dependencies": Permission.MANAGE_STORES,
    "hard_delete_store": Permission.MANAGE_STORES,
    "read_user_dependencies": Permission.MANAGE_USERS,
    "hard_delete_user": Permission.MANAGE_USERS,

    # Receiver Devices: credentials, enrolment, promotion, revocation.
    "create_receiver_enrollment_code": Permission.MANAGE_DEVICES,
    "list_receiver_devices": Permission.MANAGE_DEVICES,
    "read_receiver_device": Permission.MANAGE_DEVICES,
    "read_receiver_device_roles": Permission.MANAGE_DEVICES,
    "disable_receiver_device": Permission.MANAGE_DEVICES,
    "revoke_receiver_device": Permission.MANAGE_DEVICES,
    "promote_receiver_device": Permission.MANAGE_DEVICES,
    "rotate_receiver_device": Permission.MANAGE_DEVICES,

    # Stores. Reading the list is how a VIEWER sees which shops are online, so
    # it is VIEW_STATUS; everything that changes one is MANAGE_STORES.
    "list_stores": Permission.VIEW_STATUS,
    "stores_meta": Permission.VIEW_STATUS,
    "create_store": Permission.MANAGE_STORES,
    "update_store": Permission.MANAGE_STORES,
    "disable_store_endpoint": Permission.MANAGE_STORES,
    "enable_store_endpoint": Permission.MANAGE_STORES,
    "archive_store_endpoint": Permission.MANAGE_STORES,
    "delete_store": Permission.MANAGE_STORES,
    "regenerate_token": Permission.MANAGE_STORES,
    # Un-retiring a Store needs the account that also owns security settings.
    "restore_store_endpoint": "OWNER",

    # Broadcasting. Starting and stopping are separate permissions so a role
    # can be allowed to stop a runaway announcement without being able to begin
    # one.
    "create_session": Permission.START_BROADCAST,
    "start_session": Permission.START_BROADCAST,
    "stop_session": Permission.STOP_BROADCAST,
    "emergency_stop": Permission.EMERGENCY_STOP,
    "current_broadcast": Permission.VIEW_STATUS,
    "broadcast_history": Permission.VIEW_HISTORY,
    "session_detail": Permission.VIEW_HISTORY,

    "list_logs": Permission.VIEW_LOGS,
}

#: Routes that take no HTTP session, each for a stated reason.
DELIBERATELY_UNAUTHENTICATED = {
    "root",            # a liveness probe
    "login",           # you cannot be signed in yet
    "enroll_receiver", # a Receiver computer has no credential yet; the code is the proof
}

#: WebSocket routes. They authenticate, just not through ``get_current_user``,
#: because a browser cannot set an Authorization header on a WebSocket
#: handshake:
#:
#:   ws_receiver     Authorization: Bearer, verified by the Receiver runtime
#:                   authenticator - Device credential first, legacy Store token
#:                   second, and a revoked Device refused by both.
#:   ws_hq           a single-use ticket, redeemed once, expiring in seconds.
#:   ws_broadcaster  the same ticket mechanism.
#:
#: Listed rather than ignored, so a fourth socket appearing has to be a decision
#: somebody writes down here.
WEBSOCKET_ROUTES = {"ws_receiver", "ws_hq", "ws_broadcaster"}


def _endpoints():
    import server

    seen = {}
    for route in server.app.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not getattr(route, "path", "").startswith("/api"):
            continue
        seen[endpoint.__name__] = (route, endpoint)
    return seen


def _guard_of(endpoint) -> str | None:
    """The dependency guarding this endpoint, read from its own signature."""
    from fastapi import params

    for parameter in inspect.signature(endpoint).parameters.values():
        default = parameter.default
        if not isinstance(default, params.Depends) or default.dependency is None:
            continue
        name = getattr(default.dependency, "__name__", "")
        if name == "guard":
            # require(...) returns a closure; read the permission it captured.
            closure = inspect.getclosurevars(default.dependency)
            permission = closure.nonlocals.get("permission")
            return permission.value if permission is not None else "guard"
        if name == "require_super_admin":
            return "OWNER"
        if name == "get_current_user":
            return None
    return "UNGUARDED"


@pytest.mark.parametrize("function_name", sorted(EXPECTED))
def test_each_route_is_guarded_by_the_expected_permission(function_name: str):
    endpoints = _endpoints()
    assert function_name in endpoints, f"{function_name} is not a routed endpoint"
    _, endpoint = endpoints[function_name]

    expected = EXPECTED[function_name]
    actual = _guard_of(endpoint)
    if expected is None:
        assert actual is None, f"{function_name} expected authenticated-only, got {actual}"
    elif expected == "OWNER":
        assert actual == "OWNER", f"{function_name} expected SUPER_ADMIN, got {actual}"
    else:
        assert actual == expected.value, (
            f"{function_name} is guarded by {actual}, expected {expected.value}"
        )


def test_no_authenticated_route_is_missing_from_this_table():
    """A route added later without a permission fails here rather than shipping
    open. This is the half that keeps the file honest as the app grows."""
    endpoints = _endpoints()
    unaccounted = []
    for name, (route, endpoint) in endpoints.items():
        if name in EXPECTED or name in DELIBERATELY_UNAUTHENTICATED or name in WEBSOCKET_ROUTES:
            continue
        # Only routes that authenticate at all are in scope.
        if _guard_of(endpoint) != "UNGUARDED":
            unaccounted.append(f"{name} ({getattr(route, 'path', '?')})")
    assert unaccounted == [], (
        f"authenticated routes with no entry in EXPECTED: {unaccounted}"
    )


def test_no_route_is_accidentally_unguarded():
    """An authenticated route whose signature lost its dependency entirely."""
    offenders = []
    for name, (route, endpoint) in _endpoints().items():
        if name in DELIBERATELY_UNAUTHENTICATED or name in WEBSOCKET_ROUTES:
            continue
        if _guard_of(endpoint) == "UNGUARDED":
            offenders.append(f"{name} ({getattr(route, 'path', '?')})")
    assert offenders == [], f"routes with no authentication at all: {offenders}"


def test_the_unauthenticated_routes_are_the_ones_we_chose():
    """Each of the three has a reason; a fourth appearing is a decision."""
    unauthenticated = {
        name for name, (_, endpoint) in _endpoints().items()
        if _guard_of(endpoint) == "UNGUARDED"
    }
    assert unauthenticated == DELIBERATELY_UNAUTHENTICATED | WEBSOCKET_ROUTES


def test_reading_stores_is_not_a_device_permission():
    """The exact mistake that prompted this file.

    A VIEWER must be able to see which Stores are online; that is the whole
    point of the role. Guarding the Store list with MANAGE_DEVICES took it away
    from VIEWER and BROADCASTER alike, and nothing failed.
    """
    _, endpoint = _endpoints()["list_stores"]
    assert _guard_of(endpoint) == Permission.VIEW_STATUS.value
