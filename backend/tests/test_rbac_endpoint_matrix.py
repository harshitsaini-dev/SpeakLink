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
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)
os.environ.setdefault("JWT_SECRET", "test-secret-not-a-real-key")

from rbac import Permission  # noqa: E402


#: endpoint function name -> the permission that must guard it.
#:
#: ``SUPER_ADMIN`` marks the handful reserved for the account that also owns
#: security settings. ``None`` marks routes that are authenticated but need no
#: particular permission - knowing who you are is the whole check.
#:
#: Values are now fine-grained permission_catalog codes (strings) for every
#: route that was split into its own right during the permissions/RBAC work -
#: which is most of them. A few coarse `rbac.Permission` guards remain exactly
#: where the coarse and fine-grained meaning are identical (broadcast
#: start/stop/emergency-stop, and the two remaining VIEW_STATUS call sites,
#: which are broadcast-context now that the Store ones were split out) - see
#: `server._COARSE_TO_FINE`.
EXPECTED: dict[str, object] = {
    # Identity. Any signed-in account may ask who it is and open its own socket.
    "logout": None,
    "me": None,
    "my_permissions": None,
    "issue_websocket_ticket": None,
    # Changing your own password needs no permission beyond being signed in.
    # Read-only does not mean unable to secure your own account - and the
    # current password is required, so knowing who you are is not enough.
    "change_own_password": None,

    # HQ Users. Split from one MANAGE_USERS flag into distinct actions so an
    # override can, for example, let an ADMIN view Users without letting them
    # create one. Which accounts a given role may touch is still a second,
    # narrower check inside each endpoint (_require_may_manage), because ADMIN
    # holds users.update but must not be able to disable an OWNER or promote
    # itself into one.
    "list_hq_users": "menu.users.view",
    "read_hq_user": "menu.users.view",
    "create_hq_user": "users.create",
    "update_hq_user": "users.update",
    "set_hq_user_role": "users.update",
    "disable_hq_user": "users.disable",
    "enable_hq_user": "users.update",
    "archive_hq_user": "users.disable",
    "restore_hq_user": "users.update",
    # Setting somebody else's password is reserved, like restoring an archived
    # Store: it is the action an attacker who took an ADMIN account would use
    # to take an OWNER one.
    "reset_hq_user_password": "OWNER",
    # Per-user permission overrides: OWNER only, and enforced by the same
    # require_super_admin gate as reset_hq_user_password - independent of the
    # override system these routes edit, so an override can never grant an
    # ADMIN a path to grant themselves more.
    "read_user_permission_overrides": "OWNER",
    "write_user_permission_overrides": "OWNER",
    "read_user_store_scope": "OWNER",
    "write_user_store_scope": "OWNER",

    # Dependency summaries, and the dependency-guarded hard deletes.
    "read_store_dependencies": "menu.stores.view",
    "hard_delete_store": "stores.archive",
    "tombstone_store": "stores.delete_permanently",
    "read_store_deletion_events": "menu.stores.view",
    "read_user_dependencies": "menu.users.view",
    "tombstone_user": "users.delete_permanently",
    "read_user_deletion_events": "menu.users.view",
    "hard_delete_user": "users.disable",

    # Receiver Devices: credentials, enrolment, promotion, revocation - each
    # its own action so, for example, an override can permit disabling a
    # Device without permitting credential rotation.
    "read_audio_metrics": "menu.broadcast.view",
    "create_receiver_enrollment_code": "devices.enrollment.create",
    "list_receiver_enrollment_codes": "menu.receivers.view",
    "list_receiver_devices": "menu.receivers.view",
    "read_receiver_device": "menu.receivers.view",
    "read_receiver_device_roles": "menu.receivers.view",
    "disable_receiver_device": "devices.disable",
    "archive_receiver_device": "devices.archive",
    "restore_receiver_device": "devices.archive",
    "read_receiver_device_dependencies": "menu.receivers.view",
    "hard_delete_receiver_device": "devices.delete_permanently",
    "tombstone_receiver_device": "devices.delete_permanently",
    "read_device_deletion_events": "menu.receivers.view",
    "revoke_receiver_device": "devices.revoke",
    "promote_receiver_device": "devices.primary.assign",
    "rotate_receiver_device": "devices.rotate",

    # Stores. Reading the list is how a VIEWER sees which shops are online;
    # create/update/archive are now separate actions.
    "list_stores": "menu.stores.view",
    "stores_meta": "menu.stores.view",
    "create_store": "stores.create",
    "update_store": "stores.update",
    "disable_store_endpoint": "stores.archive",
    "enable_store_endpoint": "stores.update",
    "archive_store_endpoint": "stores.archive",
    "delete_store": "stores.archive",
    "regenerate_token": "stores.update",
    # Un-retiring a Store needs the account that also owns security settings.
    "restore_store_endpoint": "OWNER",

    # Broadcasting. Starting and stopping are separate permissions so a role
    # can be allowed to stop a runaway announcement without being able to begin
    # one. Their route source still says `Permission.START_BROADCAST` etc (the
    # coarse and fine-grained meaning are identical), but require() resolves
    # to the fine-grained code before this test ever observes it - see
    # server._COARSE_TO_FINE - so the expectations here are the codes actually
    # enforced.
    "create_session": "broadcast.start",
    "start_session": "broadcast.start",
    "stop_session": "broadcast.stop",
    "emergency_stop": "broadcast.emergency_stop",
    "current_broadcast": "menu.broadcast.view",
    "broadcast_history": "menu.history.view",
    "session_detail": "menu.history.view",

    "list_logs": "menu.logs.view",
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
            # require() now stores the already-resolved fine-grained CODE
            # (`code`), not the raw argument, precisely so this test observes
            # the same string the resolver actually checks - whether the
            # route was written as require("stores.update") or as the legacy
            # require(Permission.START_BROADCAST).
            closure = inspect.getclosurevars(default.dependency)
            code = closure.nonlocals.get("code")
            return code if code is not None else "guard"
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
    expected_code = expected.value if isinstance(expected, Permission) else expected
    if expected_code is None:
        assert actual is None, f"{function_name} expected authenticated-only, got {actual}"
    elif expected_code == "OWNER":
        assert actual == "OWNER", f"{function_name} expected OWNER, got {actual}"
    else:
        assert actual == expected_code, (
            f"{function_name} is guarded by {actual}, expected {expected_code}"
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
    assert _guard_of(endpoint) == "menu.stores.view"
