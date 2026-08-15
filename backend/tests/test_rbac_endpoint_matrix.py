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
    # Per-user permission overrides: the permission whose label is "Manage
    # User Rights", NOT a role test.
    #
    # These were "OWNER", enforced by require_super_admin. That made the
    # permission inert - an OWNER could grant it to an ADMIN and the ADMIN
    # still got 403 - so the role test was replaced by the capability, and the
    # escalation guards it stood in for became explicit: an OWNER target is
    # refused, self-editing is refused, granting a permission the actor does
    # not hold is refused, and rbac.may_manage_role decides valid targets.
    "read_user_permission_overrides": "users.permissions.manage",
    "write_user_permission_overrides": "users.permissions.manage",
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
    "search_stores": "menu.stores.view",
    # Same permission as the list it filters, and the same Store Scope. A
    # search that needed a weaker permission than the list would be a way to
    # read the catalog without being allowed to see it.
    "store_filter_options": "menu.stores.view",
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
    # The broadcast TARGET catalog. Deliberately menu.broadcast.view and not
    # menu.stores.view: Store MANAGEMENT visibility must not decide whether an
    # operator can see the Stores they may broadcast to.
    # The physical Store inventory for broadcasting IS physical-delivery
    # information, so it moved off "may open the Console" and onto "may
    # deliver to a Store" when link-only broadcasting arrived.
    "list_broadcast_target_stores": "broadcast.store_delivery",
    # Live per-Store output volume. store_audio.control rather than
    # broadcast.start: starting a broadcast and steering one are different
    # acts, and ownership of the session is enforced inside the route on top
    # of this permission - neither stop_any nor active_view reaches it.
    "read_store_audio_control": "store_audio.control",
    "set_store_audio_control": "store_audio.control",
    "create_session": "broadcast.start",
    "start_session": "broadcast.start",
    "stop_session": "broadcast.stop",
    "emergency_stop": "broadcast.emergency_stop",
    "active_broadcasts": "menu.broadcast.view",
    "current_broadcast": "menu.broadcast.view",

    # Active Broadcasts supervision. All three are guarded by the PAGE
    # permission, and the finer questions - may I see the broadcaster, the
    # Stores, or stop somebody else's session - are answered inside each
    # handler, because they shape the RESPONSE rather than deciding whether
    # the route may be called at all. A single route-level permission cannot
    # express "you may call this but will be told less", which is exactly
    # what these endpoints do.
    #
    # broadcast.view_targets additionally gates active_management_stores as a
    # hard refusal inside the handler, and broadcast.stop_any gates the
    # cross-owner branch of active_management_stop. Both have their own tests
    # in test_active_broadcast_management.py.
    "active_management_list": "broadcast.active_view",
    "active_management_stores": "broadcast.active_view",
    "active_management_stop": "broadcast.active_view",
    # Adding a Store to a live broadcast is a Console control, not a
    # supervision one, so it sits beside the output-volume route rather than
    # under active management - and carries the physical-delivery permission
    # rather than the supervision page's. Two more gates live inside the
    # handler and cannot be expressed as one code: session ownership, and
    # Store Scope. Both have their own tests.
    "add_store_to_live_broadcast": "broadcast.store_delivery",
    # The chat routes. The host half is gated on the broadcast permission and
    # then narrowed to the session's own operator inside the handler; the
    # transcript in History is readable by anyone who may read that history.
    "read_broadcast_chat": "broadcast.start",
    "post_broadcast_chat_message": "broadcast.start",
    "update_broadcast_chat_settings": "broadcast.start",
    "delete_broadcast_chat_message": "broadcast.start",
    "set_web_participant_chat_mute": "broadcast.start",
    "read_broadcast_history_chat": "menu.history.view",
    # The Store Kit. Whoever can fetch it can install SpeakLink on any machine
    # they like, so it is its own right rather than riding on a page.
    "list_store_kits": "store_kit.download",
    # Much stronger than downloading: whoever can upload decides what software
    # every Store installs next.
    "upload_store_kit": "store_kit.manage",
    "delete_store_kit": "store_kit.manage",
    "download_latest_store_kit": "store_kit.download",
    "download_store_kit": "store_kit.download",
    "post_broadcast_chat_image": "broadcast.start",
    "read_broadcast_chat_image": "broadcast.start",
    "read_history_chat_image": "menu.history.view",
    # Removal is the mirror of the add, and carries the same permission and
    # the same in-handler gates: session ownership and Store Scope.
    "remove_store_from_live_broadcast": "broadcast.store_delivery",
    # Pause and Resume are the same authority as Add and Remove: they change
    # what one Store is doing inside a broadcast the caller owns.
    "pause_store_in_live_broadcast": "broadcast.store_delivery",
    "resume_store_in_live_broadcast": "broadcast.store_delivery",
    # A Zone action is the same authority applied many times, so it carries the
    # same permission - and applies Store Scope when it resolves the list.
    "bulk_target_action": "broadcast.store_delivery",
    "broadcast_history": "menu.history.view",
    # A recording is the audio of a broadcast this account is already entitled
    # to read about, so it shares History's permission rather than inventing a
    # second one that could let somebody see a recording exists and never be
    # allowed to hear it. Store Scope is applied inside both routes.
    "read_broadcast_recording": "menu.history.view",
    "stream_broadcast_recording": "menu.history.view",
    "download_broadcast_recording": "menu.history.view",
    "session_detail": "menu.history.view",

    "list_logs": "menu.logs.view",

    # Broadcast History / System Log lifecycle. Archiving is reversible and
    # stays with ADMIN; permanent deletion is SUPER ADMIN-only.
    "archive_broadcast_sessions": "broadcast_history.archive",
    "unarchive_broadcast_sessions": "broadcast_history.archive",
    "delete_broadcast_sessions": "broadcast_history.delete_permanently",
    "archive_system_logs": "system_logs.archive",
    "unarchive_system_logs": "system_logs.archive",
    "delete_system_logs": "system_logs.delete_permanently",
    "read_admin_deletion_events": "menu.logs.view",

    # Server-side search/filter/pagination. Same permission as the list each
    # one narrows - a filtered view is still a view.
    "search_logs": "menu.logs.view",
    "search_broadcast_history": "menu.history.view",
    "search_users": "menu.users.view",
    "search_receiver_status": "menu.receivers.view",
    "receiver_filter_options": "menu.receivers.view",
    "search_receiver_devices": "menu.receivers.view",

    # Web audience. START_BROADCAST is the declared permission; OWNERSHIP of the
    # Broadcast is enforced inside each handler, because approving and removing
    # listeners is part of running your own announcement and must not be
    # reachable through the supervision codes.
    "read_web_room": "broadcast.start",
    "rotate_web_room_password": "broadcast.start",
    "set_web_room_auto_approve": "broadcast.start",
    "list_web_participants": "broadcast.start",
    "approve_web_participant": "broadcast.start",
    "deny_web_participant": "broadcast.start",
    "kick_web_participant": "broadcast.start",

    # Active Broadcast web audience supervision. The declared permission opens
    # the supervision PAGE; whether this caller may reach into somebody else's
    # room is decided per request by _authorize_web_audience, which needs
    # broadcast.manage_web_audience plus the same Store Scope containment a
    # cross-owner stop requires.
    "active_management_web_audience": "broadcast.active_view",
    "supervised_approve_participant": "broadcast.active_view",
    "supervised_deny_participant": "broadcast.active_view",
    "supervised_kick_participant": "broadcast.active_view",
    "supervised_set_auto_approve": "broadcast.active_view",

    # ---- Remote speaker switching -------------------------------------
    # Reading which output a shop is using is part of watching the estate;
    # CHANGING it reaches into a shop and moves the sound to another device,
    # which is why it sits behind the Store-management right rather than the
    # viewing one.
    "get_store_audio_output": "menu.receivers.view",
    # Asking a shop to re-enumerate its devices, and then moving its sound to
    # one of them, are both the same act of reaching into that shop - so both
    # sit behind the right that names it rather than a general Store edit.
    "refresh_store_audio_output": "receiver.set_output_device",
    "set_store_audio_output": "receiver.set_output_device",

    # ---- Group broadcasting -------------------------------------------
    # Joining is gated INSIDE the route rather than here: somebody holding
    # broadcast.join walks in, and somebody without it may still ask the host.
    # A permission on the route itself would remove the second path entirely,
    # which is the whole feature.
    "list_group_participants": "menu.broadcast.view",
    # Coarse here, decided in the route: broadcast.start says this account
    # broadcasts at all; whether THIS person walks in or has to ask the host
    # is broadcast.join, checked per request. A single permission on the route
    # would delete the "ask the host" path, which is the whole feature.
    "join_group_broadcast": "broadcast.start",
    "leave_group_broadcast": "menu.broadcast.view",
    # Answering a request is the host's job, and hosting is its own right.
    "approve_group_request": "broadcast.group_host",
    "deny_group_request": "broadcast.group_host",

    # ---- Export -------------------------------------------------------
    # One route, many datasets. It cannot name a single permission here: each
    # dataset carries the permission of the PAGE it exports, checked per
    # request against EXPORTS - so exporting Users needs the right to read
    # Users, not a blanket "may export".
    "export_dataset": None,

    # ---- Dashboard ----------------------------------------------------
    "dashboard_summary": "menu.dashboard.view",

    # ---- Recorded announcements ---------------------------------------
    "list_announcement_audio": "menu.announcements.view",
    "list_announcement_templates": "menu.announcements.view",
    "announcement_status": "menu.announcements.view",
    "announcement_history": "menu.announcements.view",
    "stream_announcement_audio": "menu.announcements.view",

    "upload_announcement_audio": "announcements.upload",
    # Renaming a recording is librarian's work, not campaign work.
    "update_announcement_audio": "announcements.upload",
    "archive_announcement_audio": "announcements.upload",
    "archive_announcement_audio_bulk": "announcements.upload",

    "create_announcement_template": "announcements.templates.manage",
    "update_announcement_template": "announcements.templates.manage",
    "archive_announcement_template": "announcements.templates.manage",
    "archive_announcement_templates_bulk": "announcements.templates.manage",
    "archive_announcement_history": "announcements.templates.manage",
    "archive_announcement_history_bulk": "announcements.templates.manage",
    "unarchive_announcement_history_bulk": "announcements.templates.manage",

    # Permanent deletion is its own right everywhere in this product: archive
    # is recoverable and this is not.
    "delete_announcement_audio_permanently": "announcements.delete_permanently",
    "delete_announcement_audio_bulk": "announcements.delete_permanently",
    "delete_announcement_template_permanently": "announcements.delete_permanently",
    "delete_announcement_templates_bulk": "announcements.delete_permanently",
    "delete_announcement_history_permanently": "announcements.delete_permanently",
    "delete_announcement_history_bulk": "announcements.delete_permanently",

    # Running a campaign in ONE shop.
    "play_announcement_template": "announcements.control",
    "play_announcement_in_store": "announcements.control",
    "pause_announcement_in_store": "announcements.control",
    "stop_announcement_in_store": "announcements.control",
    "set_announcement_volume": "announcements.volume",

    # Reaching EVERY shop in one action. Separate from announcements.control
    # on purpose: it has the reach of an emergency stop, and should be
    # grantable without it.
    "play_all_announcements": "announcements.control_all",
    "pause_all_announcements": "announcements.control_all",
    "stop_all_announcements": "announcements.control_all",

    # Listening links. Reading them is part of the page; opening one hands a
    # campaign to somebody with no account, from anywhere, until it is closed
    # - so it is its own right, and so is throwing somebody off one.
    "list_announcement_rooms": "menu.announcements.view",
    "list_announcement_room_listeners": "menu.announcements.view",
    "create_announcement_room": "announcements.rooms.manage",
    "close_announcement_room": "announcements.rooms.manage",
    "remove_announcement_room_listener": "announcements.rooms.manage",
}

#: Routes that take no HTTP session, each for a stated reason.
DELIBERATELY_UNAUTHENTICATED = {
    "root",            # a liveness probe
    "login",           # you cannot be signed in yet
    "enroll_receiver", # a Receiver computer has no credential yet; the code is the proof

    # The PUBLIC listener surface. These are reached by people with no HQ
    # account at all, which is the entire point of a shareable link. Each is
    # rate limited, none reveals anything about the estate, and the credential
    # they issue is scoped to one participant in one room and is accepted
    # nowhere else.
    "public_room_lookup",         # "does this Broadcast exist" - nothing more

    # The announcement listening link, for the same reason as the broadcast
    # one: whoever holds it is not a user of this product and must never need
    # to be. The token they get names one room, dies when that room is closed,
    # and opens no recording except the one that room plays.
    "join_announcement_room",
    "announcement_room_state",
    "announcement_room_audio",
    "leave_announcement_room",

    # A Store Receiver fetching a recording it was told to play. Not an HQ
    # session: it presents its own device credential, checked inside the
    # route, and the same computer is already trusted to receive live audio.
    "download_announcement_for_receiver",
    "public_room_join",           # the join password is the authorisation
    "public_room_request_access", # asking the broadcaster to be let in
    "listener_admission_state",   # a listener's own state, via its own cookie
    # Discards this browser's own listener cookies so it can start over after
    # being removed. It grants nothing and reads nothing: requiring an account
    # to throw away your own session would be requiring an account to leave.
    "listener_forget",
    # Chat, for somebody already admitted to a room. Both resolve the listener
    # cookie through web_rooms.authenticate_listener and answer 401 to anything
    # that is not a currently-admitted listener of a currently-open room - so
    # they are authenticated, just not by an HQ account. The read is filtered
    # to what THAT listener may see, in the query rather than afterwards.
    "read_listener_chat",
    "post_listener_chat",
    # Images, same cookie and the same visibility rule applied to the bytes:
    # a private photograph is not readable by guessing a message id.
    "post_listener_chat_image",
    "read_listener_chat_image",
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
#:   ws_listener     the listener cookie, which is HttpOnly and same-origin.
#:                   No ticket and no query parameter: this URL is logged
#:                   in full, so a credential in it would be a credential
#:                   in a log file.
WEBSOCKET_ROUTES = {"ws_receiver", "ws_hq", "ws_broadcaster", "ws_listener"}


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


def test_every_publicly_reachable_route_is_actually_rate_limited():
    """DELIBERATELY_UNAUTHENTICATED justifies itself, in prose, with "each is
    rate limited". This asserts it.

    An audit found /api/announce/join listed there with no limiter at all -
    the sentence had been true when it was written about the other routes and
    was simply never checked for the new one. A safety argument that lives in
    a comment the test does not read is an argument that keeps passing after
    the property it names has gone.
    """
    import inspect
    import server as server_module

    # Routes where the credential IS the request: a password, a code, or a
    # token presented by somebody with no account. Health probes and the
    # Receiver's own credentialled routes are not in this class.
    guessable = {
        "login": "login_limiter",
        "public_room_lookup": "web_lookup_limiter",
        "join_web_room": "web_join_limiter",
        "request_web_room_access": "web_join_limiter",
        "enroll_receiver": "enrollment_limiter",
        "join_announcement_room": "announce_join_limiter",
    }

    missing = []
    for name, limiter in guessable.items():
        route = getattr(server_module, name, None)
        if route is None:
            continue                     # renamed or removed; other tests cover that
        source = inspect.getsource(route)
        # Either through the shared helper or by asking the limiter directly -
        # login and enrolment do the latter because they answer with their own
        # wording. What is asserted is that the route CONSULTS its budget, not
        # which spelling it uses.
        consults = limiter in source and (
            "_refuse_if_limited" in source or "retry_after" in source)
        if not consults:
            missing.append(f"{name} (expected {limiter})")
    assert not missing, (
        "these public routes can be guessed at without limit: " + ", ".join(missing))
