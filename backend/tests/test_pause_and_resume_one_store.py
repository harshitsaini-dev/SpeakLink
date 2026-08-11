"""Pausing and resuming ONE Store while the rest of the Broadcast plays on.

The property that separates Pause from Remove is the LEASE. A removed Store is
released and another Broadcast may claim it; a paused Store is still this
Broadcast's, waiting. If that ever stops being true, a colleague's announcement
can take a shop over during a thirty-second pause - so it is asserted from the
lease table rather than from a status field.

The other three:

  * the Receiver is told to STAND DOWN, never to stop. Stop is terminal and
    would make a resume a fresh arrival;
  * a resume is a NEW GENERATION, so a late acknowledgement from before the
    pause can be recognised and dropped;
  * every other Store in the same Broadcast is untouched throughout.
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
    PASSWORD, FakeReceiver, add, client, feed_audio, leases, make_store,
    sign_in, start_live, targets,
)

__all__ = ["client"]


def pause(client, headers, sid, store_id):
    return client.post(
        f"/api/broadcast/sessions/{sid}/targets/{store_id}/pause", headers=headers)


def resume(client, headers, sid, store_id):
    return client.post(
        f"/api/broadcast/sessions/{sid}/targets/{store_id}/resume", headers=headers)


def remove(client, headers, sid, store_id):
    return client.delete(f"/api/broadcast/sessions/{sid}/targets/{store_id}",
                         headers=headers)


def live_targets(server, sid):
    live = server.manager.broadcasts.get(sid)
    return set(live.all_target_store_ids) if live else set()


# ===========================================================================
# Pause
# ===========================================================================

def test_pausing_one_store_leaves_the_others_playing(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    answer = pause(client, headers, sid, b)
    assert answer.status_code == 200, answer.text
    assert answer.json()["lifecycle_state"] == "PAUSED"

    assert live_targets(server, sid) == {a}
    assert "stand_down" in receiver.types_for(b)
    assert "stop" not in receiver.types_for(b), (
        "a paused Store was told to STOP, which is terminal - it would have to "
        "rejoin as a stranger")
    assert receiver.types_for(a) == [t for t in receiver.types_for(a)
                                     if t not in ("stand_down", "stop")]


def test_a_paused_store_keeps_its_lease(client):
    """The whole difference from Remove.

    A released Store can be claimed by another Broadcast, and a pause the
    operator intends to end in thirty seconds must not be an opening for one.
    """
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    assert pause(client, headers, sid, b).status_code == 200
    assert leases(server, sid) == sorted([a, b])


def test_another_broadcast_cannot_take_a_paused_store(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    c = make_store(client, headers, "CCC")
    FakeReceiver().install(server, [a, b, c])
    first = start_live(client, headers, [a, b])
    feed_audio(server, first)
    assert pause(client, headers, first, b).status_code == 200

    second = start_live(client, headers, [c])
    feed_audio(server, second)
    refused = add(client, headers, second, b)

    assert refused.status_code == 409
    assert "already live in another broadcast" in refused.json()["detail"]


def test_pausing_twice_is_not_an_error(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    assert pause(client, headers, sid, b).status_code == 200
    second = pause(client, headers, sid, b)
    assert second.status_code == 200
    assert second.json()["already_paused"] is True


def test_a_store_that_is_not_receiving_cannot_be_paused(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert remove(client, headers, sid, b).status_code == 200

    refused = pause(client, headers, sid, b)
    assert refused.status_code == 409
    assert "currently receiving" in refused.json()["detail"]


def test_audio_after_a_pause_never_reaches_the_paused_store(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    assert pause(client, headers, sid, b).status_code == 200
    feed_audio(server, sid, chunks=6)

    assert live_targets(server, sid) == {a}
    assert not server.manager.broadcasts.get(sid).fanout.is_pumping(b)


# ===========================================================================
# Resume
# ===========================================================================

def test_resuming_brings_the_store_back_on_a_new_generation(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert pause(client, headers, sid, b).status_code == 200

    answer = resume(client, headers, sid, b)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["lifecycle_state"] == "ACTIVE"
    assert body["generation"] == 2, (
        "the resumed participation reused its generation, so a late "
        "acknowledgement from before the pause could not be told apart")

    assert live_targets(server, sid) == {a, b}
    assert "resume" in receiver.types_for(b)
    assert leases(server, sid) == sorted([a, b])


def test_resuming_a_store_that_never_paused_is_idempotent(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    answer = resume(client, headers, sid, b)
    assert answer.status_code == 200
    assert answer.json()["already_active"] is True


def test_a_removed_store_cannot_be_resumed(client):
    """Resume is for a Store that is still in the Broadcast. A removed one has
    to be added back, which is a different act with a different lease."""
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert remove(client, headers, sid, b).status_code == 200

    refused = resume(client, headers, sid, b)
    assert refused.status_code == 409
    assert "Only a paused Store" in refused.json()["detail"]


def test_a_store_whose_receiver_went_offline_stays_paused(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    receiver = FakeReceiver()
    receiver.install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert pause(client, headers, sid, b).status_code == 200

    # The shop's PC is switched off during the pause.
    receiver.install(server, [a])

    refused = resume(client, headers, sid, b)
    assert refused.status_code == 409
    assert "no Receiver connected" in refused.json()["detail"]
    # Still paused, still leased, still this Broadcast's Store.
    assert targets(client, server, sid)[b].lifecycle_state == "PAUSED"
    assert leases(server, sid) == sorted([a, b])


def test_a_paused_store_can_still_be_removed(client):
    """Otherwise Pause would be a way to make a shop unremovable."""
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)
    assert pause(client, headers, sid, b).status_code == 200

    removed = remove(client, headers, sid, b)
    assert removed.status_code == 200
    assert leases(server, sid) == [a]


# ===========================================================================
# Who may do it
# ===========================================================================

def make_user(client, headers, username, role):
    r = client.post("/api/users", headers=headers, json={
        "username": username, "password": PASSWORD,
        "display_name": username.title(), "role": role})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_a_viewer_cannot_pause_or_resume(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    make_user(client, headers, "watcher", "VIEWER")
    viewer = sign_in(client, "watcher")
    assert pause(client, viewer, sid, b).status_code == 403
    assert resume(client, viewer, sid, b).status_code == 403
    assert targets(client, server, sid)[b].lifecycle_state == "ACTIVE"


def test_another_broadcaster_cannot_pause_this_broadcast(client):
    server = client.server_module
    headers = sign_in(client)
    a = make_store(client, headers, "AAA")
    b = make_store(client, headers, "BBB")
    FakeReceiver().install(server, [a, b])
    sid = start_live(client, headers, [a, b])
    feed_audio(server, sid)

    make_user(client, headers, "other", "BROADCASTER")
    stranger = sign_in(client, "other")
    assert pause(client, stranger, sid, b).status_code == 403


def test_a_store_outside_scope_answers_like_one_not_in_the_broadcast(client):
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

    answer = pause(client, theirs, sid, b)
    assert answer.status_code == 404
    assert answer.json()["detail"] == "That Store is not in this broadcast."
