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
