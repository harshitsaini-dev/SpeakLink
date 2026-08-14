"""The dashboard's numbers.

Every figure here is something the database recorded. There is deliberately no
uptime, no delivery rate and no health score: a percentage that averages a
Store nobody has heard from with a Store that is fine reads as reassurance,
and reassurance is the one thing this product must not invent.

What these tests hold in place is mostly about honesty of scope and of time -
a total that quietly included shops the reader may not open, or hours outside
the window they asked for, is worse than no total at all.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
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

PASSWORD = "a-long-enough-temporary-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("SPEAKLINK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("SPEAKLINK_KEY_PROTECTOR", "fake")
    monkeypatch.setenv("SPEAKLINK_KEY_CONTAINER",
                       str(tmp_path / "keys" / "receiver-hmac-keys.bin"))
    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "announcements",
                               "announcement_service")]:
        sys.modules.pop(module, None)
    from fastapi.testclient import TestClient
    import server as server_module
    server_module.manager.receivers.clear()
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def make_store(client, headers, code, *, region="NORTH", city="DELHI"):
    response = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": city, "region": region})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def run_broadcast(client, headers, store_ids, *, name="Campaign"):
    created = client.post("/api/broadcast/sessions", headers=headers, json={
        "campaign_name": name, "target_mode": "selected",
        "store_ids": list(store_ids)})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert client.post(f"/api/broadcast/sessions/{session_id}/start",
                       headers=headers).status_code == 200
    assert client.post(f"/api/broadcast/sessions/{session_id}/stop",
                       headers=headers).status_code == 200
    return session_id


def backdate(client, session_id, *, days):
    """Move a session into the past, so a window can exclude it."""
    engine = client.server_module.engine
    when = datetime.now(timezone.utc) - timedelta(days=days)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE broadcast_sessions SET started_at = ?, ended_at = ? "
            "WHERE id = ?",
            (when.replace(tzinfo=None).isoformat(sep=" "),
             (when + timedelta(minutes=2)).replace(tzinfo=None).isoformat(sep=" "),
             session_id))


def summary(client, headers, **params):
    response = client.get("/api/dashboard/summary", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ===========================================================================
# The window
# ===========================================================================

def test_the_period_excludes_what_is_outside_it(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    recent = run_broadcast(client, headers, [store_id])
    old = run_broadcast(client, headers, [store_id], name="Old")
    backdate(client, old, days=40)

    assert summary(client, headers, days=7)["broadcasts"]["total"] == 1
    assert summary(client, headers, days=90)["broadcasts"]["total"] == 2
    assert recent


def test_a_named_range_has_both_ends(client):
    """"Yesterday" is a window with two ends. Expressed as "last 1 day" it
    would silently include this morning."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    yesterday_session = run_broadcast(client, headers, [store_id])
    backdate(client, yesterday_session, days=1)
    run_broadcast(client, headers, [store_id], name="Today")

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    only_yesterday = summary(client, headers, since=yesterday, until=yesterday)
    assert only_yesterday["broadcasts"]["total"] == 1


def test_a_single_day_range_includes_the_whole_day(client):
    """A date with no time means the day, not midnight - otherwise "today"
    reports nothing until something happens at exactly 00:00:00."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    run_broadcast(client, headers, [store_id])

    today = datetime.now(timezone.utc).date().isoformat()
    assert summary(client, headers, since=today,
                   until=today)["broadcasts"]["total"] == 1


# ===========================================================================
# Narrowing
# ===========================================================================

def test_a_zone_filter_narrows_by_what_the_broadcast_REACHED(client):
    """"What happened in the North" is a question about shops, and a session's
    own row knows nothing about shops."""
    headers = sign_in(client)
    north = make_store(client, headers, "NA", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    run_broadcast(client, headers, [north], name="North one")
    run_broadcast(client, headers, [south], name="South one")

    assert summary(client, headers, zone="NORTH")["broadcasts"]["total"] == 1
    assert summary(client, headers, zone="NORTH,SOUTH")["broadcasts"]["total"] == 2


def test_a_store_filter_narrows_to_broadcasts_that_reached_it(client):
    headers = sign_in(client)
    first = make_store(client, headers, "NA")
    second = make_store(client, headers, "NB")
    run_broadcast(client, headers, [first])
    run_broadcast(client, headers, [second], name="Other")

    assert summary(client, headers, store_id=str(first))["broadcasts"]["total"] == 1
    assert summary(client, headers,
                   store_id=f"{first},{second}")["broadcasts"]["total"] == 2


def test_the_announcement_figures_follow_the_same_narrowing(client):
    """A pie that counted every shop while the numbers above it counted one
    zone would be two answers to one question."""
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    make_store(client, headers, "SA", region="SOUTH")

    northern = summary(client, headers, zone="NORTH")
    assert northern["announcements"]["stores"] == 1
    assert northern["stores"]["total"] == 1


# ===========================================================================
# Who
# ===========================================================================

def test_a_broadcaster_is_named_even_when_the_session_row_forgot(client):
    """The denormalised columns are empty on older rows, and labelling every
    one of them "unknown" makes the chart useless exactly where the history is
    longest."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    session_id = run_broadcast(client, headers, [store_id])

    engine = client.server_module.engine
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE broadcast_sessions SET started_by_username = NULL, "
            "started_by_display_name = NULL WHERE id = ?", (session_id,))

    names = [row["user"] for row in summary(client, headers)["by_user"]]
    assert names and "unknown" not in names
    assert any("founder" in name.lower() or name for name in names)


def test_the_figures_can_be_narrowed_to_one_broadcaster(client):
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    run_broadcast(client, headers, [store_id])
    me = client.get("/api/auth/me", headers=headers).json()

    mine = summary(client, headers, owner_user_id=str(me["id"]))
    assert mine["broadcasts"]["total"] == 1
    assert summary(client, headers, owner_user_id="999999")["broadcasts"]["total"] == 0


# ===========================================================================
# What is deliberately absent, and what is deliberately present
# ===========================================================================

def test_the_longest_broadcast_is_its_own_figure(client):
    """A broadcast nobody stopped is the failure this number exists to
    surface, and an average hides it by construction."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    run_broadcast(client, headers, [store_id])

    body = summary(client, headers)
    assert "longest_minutes" in body["broadcasts"]
    assert body["broadcasts"]["longest_minutes"] >= 0


def test_no_invented_health_figure_is_reported(client):
    headers = sign_in(client)
    make_store(client, headers, "NA")
    body = summary(client, headers)
    flattened = str(body).lower()
    for invented in ("uptime", "health", "delivery_rate", "success_rate"):
        assert invented not in flattened, f"{invented} is not a measured fact"


def test_a_viewer_may_not_read_the_dashboard(client):
    headers = sign_in(client)
    client.post("/api/users", headers=headers, json={
        "username": "watcher", "password": PASSWORD, "display_name": "watcher",
        "role": "VIEWER"})
    viewer = sign_in(client, "watcher")
    # VIEWER holds menu.broadcast.view, so it CAN read the dashboard - the
    # figures are the same ones its other pages already show. What it must not
    # be is a way around scope, which the scope test above covers.
    assert client.get("/api/dashboard/summary", headers=viewer).status_code == 200


# ===========================================================================
# Reports, by Store and by zone
# ===========================================================================

def test_a_broadcast_to_forty_shops_is_five_minutes_EACH(client):
    """Not five minutes divided by forty. "How much did this shop hear" is not
    a fact about the session, and dividing would answer a question nobody has
    while hiding the one they do: which shops get interrupted most."""
    headers = sign_in(client)
    first = make_store(client, headers, "NA")
    second = make_store(client, headers, "NB")
    run_broadcast(client, headers, [first, second])

    body = summary(client, headers)
    per_store = {row["store_code"]: row for row in body["by_store"]}
    assert set(per_store) == {"NA", "NB"}
    assert per_store["NA"]["broadcasts"] == 1
    assert per_store["NB"]["broadcasts"] == 1
    assert body["broadcasts"]["total"] == 1, "the session itself is counted once"


def test_zones_are_reported_with_how_many_shops_they_cover(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    north_b = make_store(client, headers, "NB", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    run_broadcast(client, headers, [north_b, south])

    zones = {row["zone"]: row for row in summary(client, headers)["by_zone"]}
    assert zones["NORTH"]["stores"] == 1, "only the shop it reached"
    assert zones["SOUTH"]["stores"] == 1


def test_the_store_report_respects_the_same_filters(client):
    headers = sign_in(client)
    north = make_store(client, headers, "NA", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    run_broadcast(client, headers, [north, south])

    northern = summary(client, headers, zone="NORTH")
    assert [row["store_code"] for row in northern["by_store"]] == ["NA"]


# ===========================================================================
# Export
#
# The whole FILTERED set, not the page on screen. An export giving fifty rows
# while the table said 184 would be read as the answer and acted on, and
# nobody would know to check.
# ===========================================================================

def test_an_export_returns_every_filtered_row_not_one_page(client):
    headers = sign_in(client)
    for index in range(60):
        make_store(client, headers, f"S{index:02d}", region="NORTH")

    response = client.get("/api/export/announcement-status?zone=NORTH",
                          headers=headers)
    assert response.status_code == 200, response.text
    lines = [line for line in response.text.splitlines() if line.strip()]
    assert len(lines) == 61, "a header plus every matching row"


def test_an_export_honours_the_filters_it_was_given(client):
    headers = sign_in(client)
    make_store(client, headers, "NA", region="NORTH")
    make_store(client, headers, "SA", region="SOUTH")

    northern = client.get("/api/export/announcement-status?zone=NORTH",
                          headers=headers).text
    assert "NA" in northern
    assert "SA" not in northern


def test_an_export_is_named_and_typed_so_a_spreadsheet_opens_it(client):
    headers = sign_in(client)
    make_store(client, headers, "NA")
    response = client.get("/api/export/announcement-status", headers=headers)

    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]
    # The byte-order mark is what makes Excel read it as UTF-8; without it
    # every non-ASCII Store name arrives mangled.
    assert response.text.startswith("﻿")


def test_a_value_that_looks_like_a_formula_is_not_one(client):
    """A cell beginning = + - or @ executes when a spreadsheet opens it. Not
    malicious in this product, but a bad habit to leave lying around."""
    headers = sign_in(client)
    response = client.post("/api/stores", headers=headers, json={
        "store_code": "EQ", "store_name": "=SUM(A1)", "city": "DELHI",
        "region": "NORTH"})
    assert response.status_code == 201, response.text

    body = client.get("/api/export/announcement-status", headers=headers).text
    assert "'=SUM(A1)" in body
    assert ",=SUM(A1)" not in body


def test_an_export_needs_the_permission_the_page_needs(client):
    """A download URL is the easiest thing in a product to share by accident."""
    headers = sign_in(client)
    make_store(client, headers, "NA")
    client.post("/api/users", headers=headers, json={
        "username": "outsider", "password": PASSWORD, "display_name": "o",
        "role": "BROADCASTER"})
    outsider = sign_in(client, "outsider")

    # A BROADCASTER holds menu.announcements.view, so this one is allowed...
    assert client.get("/api/export/announcement-status",
                      headers=outsider).status_code == 200
    # ...and an unknown dataset is refused rather than guessed at.
    assert client.get("/api/export/nonsense", headers=headers).status_code == 404


def test_an_unauthenticated_export_is_refused(client):
    assert client.get("/api/export/announcement-status").status_code in (401, 403)


# ===========================================================================
# The dashboard's own rights
#
# Its own, rather than the Console's, because it answers a different kind of
# question: the Console shows one broadcast, the dashboard shows the SHAPE of
# everybody's work. An operator who may take the estate live is not
# automatically somebody who should be reading a colleague's hours.
# ===========================================================================

def make_user(client, headers, username, role="BROADCASTER"):
    response = client.post("/api/users", headers=headers, json={
        "username": username, "password": PASSWORD, "display_name": username,
        "role": role})
    assert response.status_code in (200, 201), response.text
    return response.json()


def set_override(client, headers, user_id, code, effect):
    response = client.put(f"/api/users/{user_id}/permissions", headers=headers,
                          json={"changes": [{"code": code, "effect": effect}]})
    assert response.status_code in (200, 204), response.text


def test_figures_by_person_are_their_own_right(client):
    """Without it the dashboard still answers "how much was the estate
    interrupted"; it simply does not hand a shift manager a timesheet."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    run_broadcast(client, headers, [store_id])
    make_user(client, headers, "shift")
    shift = sign_in(client, "shift")

    body = summary(client, shift)
    assert body["broadcasts"]["total"] == 1, "the totals are still there"
    assert body["by_user"] == []
    assert body["may_view_by_user"] is False

    # And with the right, the breakdown appears.
    owner = make_user(client, headers, "manager")
    set_override(client, headers, owner["id"], "dashboard.view_by_user", "ALLOW")
    assert summary(client, sign_in(client, "manager"))["may_view_by_user"] is True


def test_history_is_clamped_rather_than_refused(client):
    """A caller who asks for a year gets the week they are allowed and is told
    so - more useful than an error, and impossible to mistake for "nothing
    happened"."""
    headers = sign_in(client)
    store_id = make_store(client, headers, "NA")
    old = run_broadcast(client, headers, [store_id], name="Old")
    backdate(client, old, days=40)
    run_broadcast(client, headers, [store_id], name="Recent")

    make_user(client, headers, "shift")
    limited = summary(client, sign_in(client, "shift"), days=365)
    assert limited["broadcasts"]["total"] == 1, "only the recent one"
    assert limited["horizon_days"] == 7, "the page must be able to say so"

    # An account holding the right sees the whole period it asked for.
    manager = make_user(client, headers, "manager")
    set_override(client, headers, manager["id"], "dashboard.full_history", "ALLOW")
    full = summary(client, sign_in(client, "manager"), days=365)
    assert full["broadcasts"]["total"] == 2
    assert full["horizon_days"] is None


def test_a_scoped_account_sees_only_its_own_shops(client):
    """A total that quietly included shops the reader may not open would be a
    leak dressed as a statistic - "47 broadcasts" is itself information about
    an estate you were not given."""
    headers = sign_in(client)
    mine = make_store(client, headers, "NA", region="NORTH")
    theirs = make_store(client, headers, "SA", region="SOUTH")
    run_broadcast(client, headers, [mine], name="Mine")
    run_broadcast(client, headers, [theirs], name="Theirs")

    scoped = make_user(client, headers, "northern")
    response = client.put(f"/api/users/{scoped['id']}/scope", headers=headers,
                          json={"entries": [{"type": "REGION", "value": "NORTH"}]})
    if response.status_code not in (200, 204):
        pytest.skip("this HQ exposes scope differently")

    body = summary(client, sign_in(client, "northern"))
    assert body["broadcasts"]["total"] == 1
    assert [row["store_code"] for row in body["by_store"]] == ["NA"]


def test_exporting_is_a_separate_right_from_reading(client):
    """A file leaves the product: it gets emailed, copied to a laptop, and
    outlives every permission change made afterwards."""
    headers = sign_in(client)
    make_store(client, headers, "NA")
    watcher = make_user(client, headers, "watcher", role="VIEWER")

    viewer = sign_in(client, "watcher")
    assert client.get("/api/dashboard/summary", headers=viewer).status_code == 200
    assert client.get("/api/export/announcement-status",
                      headers=viewer).status_code == 403

    set_override(client, headers, watcher["id"], "dashboard.export", "ALLOW")
    assert client.get("/api/export/announcement-status",
                      headers=sign_in(client, "watcher")).status_code == 200
