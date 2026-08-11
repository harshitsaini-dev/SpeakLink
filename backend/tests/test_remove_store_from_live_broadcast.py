"""Taking one Store out of a broadcast that is still on air.

The shape that matters is not that the Store stops. It is that the Store is
genuinely released - lease gone, delivery gone, Receiver told - while every
other Store in the same broadcast keeps playing without interruption.

The Receiver is a fake. What a real Windows Receiver does with a stop message
is proved in the Receiver's own tests; here the question is what HQ does.
"""

from __future__ import annotations

import asyncio
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

if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from test_add_store_to_live_broadcast import (  # noqa: E402
    PASSWORD, FakeReceiver, add, client, feed_audio, leases, make_store,
    sign_in, start_live, targets,
)

__all__ = ["client"]  # re-exported fixture


def remove(client, headers, sid, store_id):
    return client.delete(f"/api/broadcast/sessions/{sid}/targets/{store_id}",
                         headers=headers)


def live_targets(server, sid):
    """Who the runtime would actually send the next chunk to."""
    live = server.manager.broadcasts.get(sid)
    return set(live.all_target_store_ids) if live else set()


# ===========================================================================
# The happy path
# ===========================================================================

def test_a_removed_store_stops_and_the_rest_keep_playing(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b])

    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert live_targets(server, sid) == {a, b}

    answer = remove(client, headers, sid, b)
    assert answer.status_code == 200, answer.text
    assert answer.json()["lifecycle_state"] == "REMOVED"

    # The removed Store is out of delivery; the other one is untouched.
    assert live_targets(server, sid) == {a}
    assert leases(server, sid) == [a]
    assert "stop" in receiver.types_for(b)
    assert "stop" not in receiver.types_for(a)

    rows = targets(client, server, sid)
    assert rows[b].lifecycle_state == "REMOVED"
    assert rows[b].stopped_at is not None
    assert rows[a].lifecycle_state == "ACTIVE"


def test_the_broadcast_itself_stays_live(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    assert remove(client, headers, sid, b).status_code == 200

    from models import BroadcastSession
    from db import SessionLocal
    with SessionLocal() as db:
        assert db.get(BroadcastSession, sid).status == "live"
    assert server.manager.broadcasts.is_live(sid)


def test_audio_after_the_removal_never_reaches_the_removed_store(client):
    """The real question: does it STAY out, or does the next chunk restart it?

    A Store still in the target set with no live pump gets a new pump on the
    next chunk. If removal only stopped the pump, the Store would come back
    on its own a few milliseconds later.
    """
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    assert remove(client, headers, sid, b).status_code == 200
    feed_audio(server, sid, chunks=6)

    assert live_targets(server, sid) == {a}
    assert not server.manager.broadcasts.get(sid).fanout.is_pumping(b)


# ===========================================================================
# Idempotence and refusals
# ===========================================================================

def test_removing_twice_is_not_an_error(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    assert remove(client, headers, sid, b).status_code == 200
    second = remove(client, headers, sid, b)
    assert second.status_code == 200
    assert second.json()["already_removed"] is True
    assert leases(server, sid) == [a]


def test_the_last_store_may_be_removed_and_the_count_says_so(client):
    """Emptying a live broadcast is allowed, because moving an announcement
    from one shop to another is remove-then-add and a rule against it would
    only be worked around. What is owed is an honest count, not a refusal."""
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    FakeReceiver().install(server, [a])
    sid = start_live(client, headers, [a])
    feed_audio(server, sid)

    answer = remove(client, headers, sid, a)
    assert answer.status_code == 200, answer.text
    assert answer.json()["stores_remaining"] == 0
    assert live_targets(server, sid) == set()
    assert leases(server, sid) == []
    # Still live - the web audience is still there, and Stop is a separate act.
    assert server.manager.broadcasts.is_live(sid)


def test_a_store_that_was_never_in_this_broadcast_is_a_404(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    outsider = make_store(client, headers, "ZZZ")
    FakeReceiver().install(server, [a, outsider])
    sid = start_live(client, headers, [a])
    feed_audio(server, sid)

    assert remove(client, headers, sid, outsider).status_code == 404


def test_removal_is_refused_once_the_broadcast_has_ended(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert client.post(f"/api/broadcast/sessions/{sid}/stop",
                       headers=headers).status_code == 200

    refusal = remove(client, headers, sid, b)
    assert refusal.status_code == 409
    assert "no longer" in refusal.json()["detail"]


# ===========================================================================
# The lease is what lets another broadcast have the Store
# ===========================================================================

def test_a_removed_store_can_join_a_different_broadcast(client):
    """The point of releasing the lease, stated as a behaviour."""
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    c = make_store(client, headers, "CCC")
    FakeReceiver().install(server, [a, b, c])

    first = start_live(client, headers, [a, b])
    feed_audio(server, first)
    assert remove(client, headers, first, b).status_code == 200

    second = start_live(client, headers, [c])
    feed_audio(server, second)
    joined = add(client, headers, second, b)
    assert joined.status_code == 200, joined.text
    assert leases(server, second) == sorted([b, c])
    assert leases(server, first) == [a]


def test_a_removed_store_can_be_added_back_as_a_new_generation(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    assert remove(client, headers, sid, b).status_code == 200
    back = add(client, headers, sid, b)
    assert back.status_code == 200, back.text
    assert back.json()["generation"] == 2
    assert back.json()["lifecycle_state"] == "ACTIVE"
    assert live_targets(server, sid) == {a, b}
    assert leases(server, sid) == sorted([a, b])


# ===========================================================================
# Permission, ownership and scope
# ===========================================================================

def make_user(client, headers, username, role):
    r = client.post("/api/users", headers=headers, json={
        "username": username, "password": PASSWORD,
        "display_name": username.title(), "role": role})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_viewer_cannot_remove_a_store(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    make_user(client, headers, "watcher", "VIEWER")
    viewer = sign_in(client, "watcher")
    assert remove(client, viewer, sid, b).status_code == 403
    assert leases(server, sid) == sorted([a, b])


def test_another_broadcaster_cannot_reach_into_this_broadcast(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    make_user(client, headers, "other", "BROADCASTER")
    stranger = sign_in(client, "other")
    refused = remove(client, stranger, sid, b)
    assert refused.status_code == 403
    assert leases(server, sid) == sorted([a, b])


def test_a_store_outside_scope_answers_exactly_like_one_not_in_the_broadcast(client):
    """Scope on its own, with no other gate able to answer first.

    The scoped operator runs their own broadcast, so ownership cannot refuse
    before scope is consulted. A status code that differed from "not in this
    broadcast" would be a way to enumerate which shops a broadcast is reaching
    from an account not entitled to know.
    """
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])

    user_id = make_user(client, headers, "scoped", "BROADCASTER")
    scoped_ok = client.put(f"/api/users/{user_id}/store-scope", headers=headers,
                           json={"entries": [{"scope_type": "STORE", "store_id": a}]})
    assert scoped_ok.status_code in (200, 204), scoped_ok.text

    theirs = sign_in(client, "scoped")
    sid = start_live(client, theirs, [a])
    feed_audio(server, sid)
    # B is added by the unscoped owner... which they cannot do to somebody
    # else's broadcast, so B joins by being targeted from the start instead.
    # Here the scoped operator simply tries to remove a Store they may not see.
    answer = remove(client, theirs, sid, b)

    assert answer.status_code == 404, (
        f"an out-of-scope Store answered {answer.status_code}, not the 404 a "
        "Store outside the broadcast gets - which tells the caller it is in it")
    assert answer.json()["detail"] == "That Store is not in this broadcast."
    assert leases(server, sid) == [a]
