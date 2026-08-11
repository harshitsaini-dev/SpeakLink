"""One action across a Zone, a City, or a named list of Stores.

A Zone action almost never succeeds uniformly - a shop is offline, one is in
somebody else's broadcast, another is already paused - so the shape that
matters is the per-Store answer. These tests are mostly about partial success:
that one refusal never stops the rest, that the response says which Store
refused and why, and that a scoped operator gets their own Stores acted on
rather than a list of refusals about shops they may not know exist.
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
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from test_add_store_to_live_broadcast import (  # noqa: E402
    PASSWORD, FakeReceiver, client, feed_audio, leases, sign_in, start_live,
    targets,
)

__all__ = ["client"]


def make_store(client, headers, code, *, region="NORTH", city="DELHI"):
    r = client.post("/api/stores", headers=headers, json={
        "store_code": code, "store_name": f"Store {code}",
        "city": city, "region": region})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def bulk(client, headers, sid, action, **selector):
    return client.post(f"/api/broadcast/sessions/{sid}/targets/bulk",
                       headers=headers, json={"action": action, **selector})


def states(client, server, sid):
    return {store_id: row.lifecycle_state
            for store_id, row in targets(client, server, sid).items()}


def live_targets(server, sid):
    live = server.manager.broadcasts.get(sid)
    return set(live.all_target_store_ids) if live else set()


# ===========================================================================
# A whole Zone
# ===========================================================================

def test_pausing_a_zone_pauses_only_that_zone(client):
    server = client.server_module
    headers = sign_in(client)
    north_a = make_store(client, headers, "NA", region="NORTH")
    north_b = make_store(client, headers, "NB", region="NORTH")
    south = make_store(client, headers, "SA", region="SOUTH")
    FakeReceiver().install(server, [north_a, north_b, south])
    sid = start_live(client, headers, [north_a, north_b, south])
    feed_audio(server, sid)

    answer = bulk(client, headers, sid, "pause", region="NORTH")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["requested"] == 2 and body["succeeded"] == 2

    current = states(client, server, sid)
    assert current[north_a] == "PAUSED" and current[north_b] == "PAUSED"
    assert current[south] == "ACTIVE", "a Store outside the Zone was touched"
    # Paused, not released: the whole Zone is still this Broadcast's.
    assert leases(server, sid) == sorted([north_a, north_b, south])


def test_resuming_a_zone_brings_it_back(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA", region="NORTH")
    b = make_store(client, headers, "NB", region="NORTH")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert bulk(client, headers, sid, "pause", region="NORTH").json()["succeeded"] == 2

    answer = bulk(client, headers, sid, "resume", region="NORTH")
    assert answer.status_code == 200, answer.text
    assert answer.json()["succeeded"] == 2
    assert set(states(client, server, sid).values()) == {"ACTIVE"}
    assert live_targets(server, sid) == {a, b}


def test_adding_a_city_adds_every_reachable_store_in_it(client):
    server = client.server_module
    headers = sign_in(client)
    started = make_store(client, headers, "AAA", city="DELHI")
    also = make_store(client, headers, "BBB", city="DELHI")
    elsewhere = make_store(client, headers, "CCC", city="MUMBAI")
    FakeReceiver().install(server, [started, also, elsewhere])
    sid = start_live(client, headers, [started])
    feed_audio(server, sid)

    answer = bulk(client, headers, sid, "add", city="DELHI")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["requested"] == 2, "the City selector picked up another city"
    assert body["succeeded"] == 2
    assert live_targets(server, sid) == {started, also}


def test_removing_a_zone_releases_those_stores(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA", region="NORTH")
    b = make_store(client, headers, "NB", region="NORTH")
    keep = make_store(client, headers, "SA", region="SOUTH")
    FakeReceiver().install(server, [a, b, keep])
    sid = start_live(client, headers, [a, b, keep])
    feed_audio(server, sid)

    assert bulk(client, headers, sid, "remove", region="NORTH").json()["succeeded"] == 2

    # Removal releases, unlike pause - that is the distinction the two actions
    # exist to preserve.
    assert leases(server, sid) == [keep]
    assert live_targets(server, sid) == {keep}


# ===========================================================================
# Partial success is the normal case
# ===========================================================================

def test_one_refusal_does_not_stop_the_rest(client):
    """An operator silencing a Zone because something is wrong needs the other
    shops silenced even if one is unreachable."""
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA", region="NORTH")
    b = make_store(client, headers, "NB", region="NORTH")
    c = make_store(client, headers, "NC", region="NORTH")
    FakeReceiver().install(server, [a, b, c])
    sid = start_live(client, headers, [a, b, c])
    feed_audio(server, sid)
    # B is already paused, so pausing it again is a no-op rather than a fault;
    # take it out instead, which makes a later pause a genuine refusal.
    assert client.delete(f"/api/broadcast/sessions/{sid}/targets/{b}",
                         headers=headers).status_code == 200

    answer = bulk(client, headers, sid, "pause", region="NORTH")
    body = answer.json()

    assert body["requested"] == 3
    assert body["succeeded"] == 2
    refused = [row for row in body["results"] if not row["ok"]]
    assert len(refused) == 1 and refused[0]["store_id"] == b
    assert "currently receiving" in refused[0]["detail"]
    # The other two really are paused.
    current = states(client, server, sid)
    assert current[a] == "PAUSED" and current[c] == "PAUSED"


def test_every_store_gets_its_own_answer(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA", region="NORTH")
    b = make_store(client, headers, "NB", region="NORTH")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    results = bulk(client, headers, sid, "pause", region="NORTH").json()["results"]
    assert {row["store_id"] for row in results} == {a, b}
    assert all(row["lifecycle_state"] == "PAUSED" for row in results)


def test_a_zone_with_nothing_in_it_is_not_an_error(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA", region="NORTH")
    FakeReceiver().install(server, [a])
    sid = start_live(client, headers, [a])
    feed_audio(server, sid)

    answer = bulk(client, headers, sid, "pause", region="WEST")
    assert answer.status_code == 200
    assert answer.json() == {"session_id": sid, "action": "pause",
                             "requested": 0, "succeeded": 0, "results": []}


# ===========================================================================
# What cannot be asked for
# ===========================================================================

def test_a_request_with_no_selector_is_refused(client):
    """An empty selector would mean the whole estate, and nobody should be able
    to ask for that by forgetting a field."""
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA")
    FakeReceiver().install(server, [a])
    sid = start_live(client, headers, [a])
    feed_audio(server, sid)

    assert client.post(f"/api/broadcast/sessions/{sid}/targets/bulk",
                       headers=headers, json={"action": "pause"}).status_code == 422


def test_an_unknown_action_is_refused(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA")
    FakeReceiver().install(server, [a])
    sid = start_live(client, headers, [a])
    feed_audio(server, sid)

    assert bulk(client, headers, sid, "delete_everything",
                region="NORTH").status_code == 422


def test_a_viewer_cannot_act_on_a_zone(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "NA")
    FakeReceiver().install(server, [a])
    sid = start_live(client, headers, [a])
    feed_audio(server, sid)
    client.post("/api/users", headers=headers, json={
        "username": "watcher", "display_name": "Watcher", "role": "VIEWER",
        "password": PASSWORD})
    viewer = sign_in(client, "watcher")

    assert bulk(client, viewer, sid, "pause", region="NORTH").status_code == 403
    assert states(client, server, sid)[a] == "ACTIVE"


def test_a_scoped_operator_acts_only_on_their_own_stores(client):
    """Scope is applied when the list is RESOLVED, not by refusing each Store.

    A scoped operator asking for a Zone should have their shops acted on, not
    receive a list of refusals naming shops they are not entitled to know
    about.
    """
    server = client.server_module
    headers = sign_in(client)
    mine = make_store(client, headers, "NA", region="NORTH")
    theirs = make_store(client, headers, "NB", region="NORTH")
    FakeReceiver().install(server, [mine, theirs])

    made = client.post("/api/users", headers=headers, json={
        "username": "scoped", "display_name": "Scoped", "role": "BROADCASTER",
        "password": PASSWORD})
    assert made.status_code == 201, made.text
    scoped_ok = client.put(f"/api/users/{made.json()['id']}/store-scope",
                           headers=headers,
                           json={"entries": [{"scope_type": "STORE", "store_id": mine}]})
    assert scoped_ok.status_code in (200, 204), scoped_ok.text

    theirs_headers = sign_in(client, "scoped")
    sid = start_live(client, theirs_headers, [mine])
    feed_audio(server, sid)

    answer = bulk(client, theirs_headers, sid, "pause", region="NORTH")
    body = answer.json()
    assert body["requested"] == 1, "a scoped operator was offered a Store outside scope"
    assert [row["store_id"] for row in body["results"]] == [mine]
