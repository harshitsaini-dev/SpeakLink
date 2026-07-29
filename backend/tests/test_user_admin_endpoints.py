"""User administration over the API, including the refusals that matter.

The rule this file exists to enforce: **every restriction is enforced on the
server**. The frontend hides buttons an account may not use, but hiding a button
is a courtesy to the person looking at the screen, not a control - anybody can
open the network tab and issue the request themselves. So every test here calls
the endpoint directly, as the wrong role, and expects 403 rather than a hidden
control.

Each test builds its own database under tmp_path. The protected database is
never opened.
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

from rbac import Role  # noqa: E402


PASSWORD = "a-long-enough-temporary-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """A server whose database exists only for this test."""
    database = tmp_path / "hq.db"
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(database))
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


def sign_in(client, username: str, password: str = PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def founder(client):
    return sign_in(client, "founder")


def make_user(client, headers, username, role=Role.VIEWER.value, password=PASSWORD):
    return client.post("/api/users", headers=headers, json={
        "username": username, "display_name": username.title(),
        "role": role, "password": password})


# ===========================================================================
# A fresh install must have somebody who can administer it
# ===========================================================================
def test_a_brand_new_install_has_a_super_admin(client, founder):
    """The defect this catches was silent and total.

    The role migration ran at startup **before** seed_admin, so on an empty
    database it found nobody to promote; seed_admin then inserted the first
    administrator with the legacy role string 'admin'. Measured on a fresh
    install, the only account was ``{'username': 'founder', 'role': 'admin'}``
    - not even normalised - and the system had no SUPER_ADMIN at all. Every
    require_super_admin endpoint was unreachable by anybody: restoring an
    archived Store, and now resetting a password. Recovery would have meant
    editing the database by hand.

    Nothing failed loudly. The endpoints simply answered 403 to everyone.
    """
    assert client.get("/api/auth/me", headers=founder).json()["role"] == "OWNER"


def test_the_seeded_administrator_can_reach_a_super_admin_only_endpoint(client, founder):
    """The property that actually matters, exercised rather than inferred."""
    user_id = make_user(client, founder, "priya").json()["id"]
    assert client.post(f"/api/users/{user_id}/reset-password", headers=founder,
                       json={"new_password": "another-long-temporary-password"}
                       ).status_code == 200


def test_the_seeded_administrator_has_a_display_name_and_state(client, founder):
    """Both columns are added by a migration that runs before seeding, so the
    seeded row would otherwise carry NULLs - and a NULL display name fails the
    response model on the first request to /api/users."""
    me = client.get("/api/users", headers=founder).json()[0]
    assert me["display_name"]
    assert me["lifecycle_state"] == "active"


# ===========================================================================
# The shape of what comes back
# ===========================================================================
def test_a_user_response_never_carries_a_password_hash(client, founder):
    make_user(client, founder, "priya")
    body = client.get("/api/users", headers=founder).text
    assert "password_hash" not in body
    assert "$2b$" not in body, "a bcrypt hash reached the response body"


def test_a_user_response_never_carries_the_session_version(client, founder):
    make_user(client, founder, "priya")
    assert "session_version" not in client.get("/api/users", headers=founder).text


def test_a_user_response_never_carries_a_password_or_token(client, founder):
    make_user(client, founder, "priya")
    body = client.get("/api/users", headers=founder).text
    for forbidden in (PASSWORD, "eyJ", "Bearer "):
        assert forbidden not in body


def test_creating_a_user_does_not_echo_the_password(client, founder):
    created = make_user(client, founder, "priya")
    assert created.status_code == 201
    assert PASSWORD not in created.text


def test_listing_shows_every_lifecycle_state(client, founder):
    priya = make_user(client, founder, "priya").json()
    rahul = make_user(client, founder, "rahul").json()
    client.post(f"/api/users/{priya['id']}/disable", headers=founder)
    client.post(f"/api/users/{rahul['id']}/archive", headers=founder)
    states = {row["username"]: row["lifecycle_state"]
              for row in client.get("/api/users", headers=founder).json()}
    assert states["priya"] == "disabled"
    assert states["rahul"] == "archived"


# ===========================================================================
# Lifecycle
# ===========================================================================
def test_the_full_lifecycle(client, founder):
    user_id = make_user(client, founder, "priya").json()["id"]
    for action, expected in (("disable", "disabled"), ("enable", "active"),
                             ("archive", "archived"), ("restore", "disabled")):
        response = client.post(f"/api/users/{user_id}/{action}", headers=founder)
        assert response.status_code == 200, f"{action}: {response.text}"
        assert response.json()["lifecycle_state"] == expected


def test_enable_cannot_undo_an_archive(client, founder):
    user_id = make_user(client, founder, "priya").json()["id"]
    client.post(f"/api/users/{user_id}/archive", headers=founder)
    assert client.post(f"/api/users/{user_id}/enable", headers=founder).status_code == 409


def test_a_duplicate_username_is_a_409(client, founder):
    make_user(client, founder, "priya")
    assert make_user(client, founder, "priya").status_code == 409


def test_a_user_is_never_deleted(client, founder):
    user_id = make_user(client, founder, "priya").json()["id"]
    client.post(f"/api/users/{user_id}/archive", headers=founder)
    assert client.get(f"/api/users/{user_id}", headers=founder).status_code == 200


def test_there_is_no_delete_endpoint(client):
    paths = {route.path for route in client.server_module.app.routes
             if "users" in getattr(route, "path", "")}
    for route in client.server_module.app.routes:
        if getattr(route, "path", "").startswith("/api/users"):
            assert "DELETE" not in getattr(route, "methods", set()), (
                f"{route.path} can be deleted; HQ Users are archived, never removed")
    assert paths, "no user routes are registered at all"


# ===========================================================================
# A disabled or archived account cannot sign in
# ===========================================================================
@pytest.mark.parametrize("action", ["disable", "archive"])
def test_a_switched_off_account_cannot_log_in(client, founder, action):
    make_user(client, founder, "priya")
    user_id = client.get("/api/users", headers=founder).json()[-1]["id"]
    client.post(f"/api/users/{user_id}/{action}", headers=founder)
    assert client.post("/api/auth/login",
                       json={"username": "priya", "password": PASSWORD}).status_code != 200


# ===========================================================================
# Sessions really end
# ===========================================================================
@pytest.mark.parametrize("action", ["disable", "archive"])
def test_an_existing_token_stops_working_immediately(client, founder, action):
    """The point of the session counter.

    A JWT here lasts eight hours and carries no server state. Without this,
    disabling somebody at 09:05 leaves whoever holds their token broadcasting to
    44 Stores until five in the evening.
    """
    make_user(client, founder, "priya", role=Role.BROADCASTER.value)
    theirs = sign_in(client, "priya")
    assert client.get("/api/auth/me", headers=theirs).status_code == 200

    user_id = client.get("/api/users", headers=founder).json()[-1]["id"]
    client.post(f"/api/users/{user_id}/{action}", headers=founder)
    assert client.get("/api/auth/me", headers=theirs).status_code == 401


def test_a_role_change_ends_existing_sessions(client, founder):
    """A token minted a moment ago still carries the old permissions."""
    make_user(client, founder, "priya", role=Role.BROADCASTER.value)
    theirs = sign_in(client, "priya")
    user_id = client.get("/api/users", headers=founder).json()[-1]["id"]
    client.post(f"/api/users/{user_id}/role", headers=founder, json={"role": "VIEWER"})
    assert client.get("/api/auth/me", headers=theirs).status_code == 401


def test_a_password_reset_ends_existing_sessions(client, founder):
    make_user(client, founder, "priya", role=Role.BROADCASTER.value)
    theirs = sign_in(client, "priya")
    user_id = client.get("/api/users", headers=founder).json()[-1]["id"]
    client.post(f"/api/users/{user_id}/reset-password", headers=founder,
                json={"new_password": "another-long-temporary-password"})
    assert client.get("/api/auth/me", headers=theirs).status_code == 401


def test_changing_your_own_password_ends_your_own_sessions(client, founder):
    """Including the token that made the request.

    "Change your password, you may have been compromised" achieves nothing for
    the next eight hours otherwise.
    """
    make_user(client, founder, "priya", role=Role.BROADCASTER.value)
    theirs = sign_in(client, "priya")
    changed = client.post("/api/auth/change-password", headers=theirs, json={
        "current_password": PASSWORD, "new_password": "a-different-long-password"})
    assert changed.status_code == 200
    assert client.get("/api/auth/me", headers=theirs).status_code == 401


def test_renaming_somebody_does_not_sign_them_out(client, founder):
    """A typo in a display name must not end a broadcast."""
    make_user(client, founder, "priya", role=Role.BROADCASTER.value)
    theirs = sign_in(client, "priya")
    user_id = client.get("/api/users", headers=founder).json()[-1]["id"]
    client.patch(f"/api/users/{user_id}", headers=founder, json={"display_name": "Priya S"})
    assert client.get("/api/auth/me", headers=theirs).status_code == 200


# ===========================================================================
# Passwords
# ===========================================================================
def test_changing_your_password_requires_the_current_one(client, founder):
    """Without it, an unattended signed-in desktop is a permanent account
    takeover rather than a session somebody can end."""
    make_user(client, founder, "priya", role=Role.BROADCASTER.value)
    theirs = sign_in(client, "priya")
    refused = client.post("/api/auth/change-password", headers=theirs, json={
        "current_password": "not-the-right-one", "new_password": "a-different-long-password"})
    assert refused.status_code == 403
    assert client.get("/api/auth/me", headers=theirs).status_code == 200, (
        "a failed password change must not end the session")


def test_the_new_password_actually_works(client, founder):
    make_user(client, founder, "priya", role=Role.BROADCASTER.value)
    theirs = sign_in(client, "priya")
    client.post("/api/auth/change-password", headers=theirs, json={
        "current_password": PASSWORD, "new_password": "a-different-long-password"})
    sign_in(client, "priya", "a-different-long-password")


def test_a_reset_returns_nothing_reusable(client, founder):
    """No password echoed, no reset link, no token.

    A value in a response body is a value in a browser's memory, in a proxy log,
    and in the screenshot somebody pastes into a chat.
    """
    user_id = make_user(client, founder, "priya").json()["id"]
    reset = client.post(f"/api/users/{user_id}/reset-password", headers=founder,
                        json={"new_password": "another-long-temporary-password"})
    assert reset.status_code == 200
    body = reset.text
    assert "another-long-temporary-password" not in body
    for forbidden in ("$2b$", "token", "reset_link", "secret"):
        assert forbidden not in body.lower()


def test_a_short_password_is_refused(client, founder):
    assert make_user(client, founder, "priya", password="short").status_code == 422


# ===========================================================================
# Locking the organisation out
# ===========================================================================
def test_the_last_super_admin_cannot_be_disabled(client, founder):
    founder_id = client.get("/api/auth/me", headers=founder).json()["id"]
    second = make_user(client, founder, "second", role=Role.OWNER.value).json()
    other = sign_in(client, "second")
    # Only one is needed to demonstrate the rule; disable the founder first.
    assert client.post(f"/api/users/{founder_id}/disable", headers=other).status_code == 200
    # Now "second" is the only active SUPER_ADMIN, and nobody can remove them.
    assert client.post(f"/api/users/{second['id']}/disable",
                       headers=other).status_code == 409


def test_the_last_super_admin_cannot_be_demoted(client, founder):
    founder_id = client.get("/api/auth/me", headers=founder).json()["id"]
    assert client.post(f"/api/users/{founder_id}/role", headers=founder,
                       json={"role": "ADMIN"}).status_code == 409


def test_you_cannot_disable_yourself(client, founder):
    founder_id = client.get("/api/auth/me", headers=founder).json()["id"]
    make_user(client, founder, "second", role=Role.OWNER.value)
    assert client.post(f"/api/users/{founder_id}/disable", headers=founder).status_code == 409


def test_you_cannot_archive_yourself(client, founder):
    founder_id = client.get("/api/auth/me", headers=founder).json()["id"]
    make_user(client, founder, "second", role=Role.OWNER.value)
    assert client.post(f"/api/users/{founder_id}/archive", headers=founder).status_code == 409


# ===========================================================================
# The role matrix, enforced at the endpoint
# ===========================================================================
@pytest.fixture()
def roles(client, founder):
    """One account of each non-SUPER_ADMIN role, plus signed-in headers."""
    made = {}
    for role in (Role.ADMIN, Role.BROADCASTER, Role.VIEWER):
        username = role.value.lower()
        record = make_user(client, founder, username, role=role.value).json()
        made[role] = {"record": record, "headers": sign_in(client, username)}
    return made


@pytest.mark.parametrize("role", [Role.BROADCASTER, Role.VIEWER])
def test_a_broadcaster_or_viewer_cannot_even_list_users(client, roles, role):
    assert client.get("/api/users", headers=roles[role]["headers"]).status_code == 403


@pytest.mark.parametrize("role", [Role.BROADCASTER, Role.VIEWER])
def test_a_broadcaster_or_viewer_cannot_create_a_user(client, roles, role):
    response = client.post("/api/users", headers=roles[role]["headers"], json={
        "username": "sneaky", "display_name": "Sneaky", "role": "VIEWER",
        "password": "a-long-enough-temporary-password"})
    assert response.status_code == 403


@pytest.mark.parametrize("action", ["disable", "archive", "restore", "enable"])
def test_a_broadcaster_cannot_change_anybody_s_state(client, roles, action):
    target = roles[Role.VIEWER]["record"]["id"]
    assert client.post(f"/api/users/{target}/{action}",
                       headers=roles[Role.BROADCASTER]["headers"]).status_code == 403


def test_an_admin_cannot_create_a_super_admin(client, roles):
    """Being able to promote yourself makes every other restriction decorative."""
    response = client.post("/api/users", headers=roles[Role.ADMIN]["headers"], json={
        "username": "climber", "display_name": "Climber", "role": "OWNER",
        "password": "a-long-enough-temporary-password"})
    assert response.status_code == 403


def test_an_admin_cannot_promote_anybody_to_super_admin(client, roles):
    target = roles[Role.VIEWER]["record"]["id"]
    assert client.post(f"/api/users/{target}/role", headers=roles[Role.ADMIN]["headers"],
                       json={"role": "OWNER"}).status_code == 403


def test_an_admin_cannot_touch_a_super_admin(client, founder, roles):
    founder_id = client.get("/api/auth/me", headers=founder).json()["id"]
    admin = roles[Role.ADMIN]["headers"]
    for action in ("disable", "archive"):
        assert client.post(f"/api/users/{founder_id}/{action}",
                           headers=admin).status_code == 403
    assert client.post(f"/api/users/{founder_id}/role", headers=admin,
                       json={"role": "VIEWER"}).status_code == 403


def test_an_admin_cannot_manage_another_admin(client, founder, roles):
    """Two administrators disabling each other is a support call nobody wins."""
    other = make_user(client, founder, "second-admin", role=Role.ADMIN.value).json()
    assert client.post(f"/api/users/{other['id']}/disable",
                       headers=roles[Role.ADMIN]["headers"]).status_code == 403


def test_an_admin_can_manage_a_broadcaster(client, roles):
    target = roles[Role.BROADCASTER]["record"]["id"]
    assert client.post(f"/api/users/{target}/disable",
                       headers=roles[Role.ADMIN]["headers"]).status_code == 200


def test_only_a_super_admin_may_reset_a_password(client, roles):
    target = roles[Role.VIEWER]["record"]["id"]
    assert client.post(f"/api/users/{target}/reset-password",
                       headers=roles[Role.ADMIN]["headers"],
                       json={"new_password": "another-long-temporary-password"}
                       ).status_code == 403


def test_anybody_signed_in_may_change_their_own_password(client, roles):
    """Read-only does not mean unable to secure your own account."""
    response = client.post("/api/auth/change-password",
                           headers=roles[Role.VIEWER]["headers"], json={
                               "current_password": PASSWORD,
                               "new_password": "a-different-long-password"})
    assert response.status_code == 200


def test_an_unauthenticated_caller_gets_nothing(client):
    for method, path in (("get", "/api/users"), ("post", "/api/users/1/disable")):
        assert getattr(client, method)(path).status_code in (401, 403)


def test_a_refusal_does_not_name_the_missing_permission(client, roles):
    """Naming it tells a caller the shape of the system, and tells an attacker
    which account is worth taking next."""
    body = client.get("/api/users", headers=roles[Role.VIEWER]["headers"]).text
    assert "MANAGE_USERS" not in body
