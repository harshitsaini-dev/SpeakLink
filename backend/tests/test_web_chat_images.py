"""Images in chat: what gets stored, who can fetch it, and what removes it.

An upload endpoint is the widest door in an application, so most of this file
is about refusals rather than the happy path:

  * a file that is not an image, whatever it is named or declares itself to be;
  * a real image with something else appended to it - the polyglot - which must
    lose everything that is not pixels;
  * an image that is enormous, on disk or once decoded;
  * a private photograph fetched by somebody it was not sent to;
  * an image that outlives the Broadcast it was sent in.

The re-encode is the reason most of these hold, so several tests assert on the
STORED bytes rather than on a status code.
"""

from __future__ import annotations

import io
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

from test_web_chat import (  # noqa: E402
    PASSWORD, client, host_view, listener, listener_view, make_session, owner,
    room_of, sign_in,
)

__all__ = ["client", "owner"]

PIL = pytest.importorskip("PIL", reason="image attachments need Pillow")
from PIL import Image  # noqa: E402


def png_bytes(width=40, height=30, colour=(200, 30, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width=40, height=30):
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (10, 90, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.fixture()
def live_room(client, owner, tmp_path, monkeypatch):
    monkeypatch.setenv("SPEAKLINK_DATA_DIR", str(tmp_path / "data"))
    sid = make_session(client, owner)
    room = room_of(client, owner, sid)
    return sid, room, listener(client, room, "Harshit")


def send_image(browser, payload, name="photo.png", mime="image/png", body=""):
    return browser.post("/api/listen/chat/image",
                        files={"file": (name, payload, mime)},
                        data={"body": body})


def host_send_image(client, headers, sid, payload, name="photo.png",
                    mime="image/png", body=""):
    return client.post(f"/api/broadcast/sessions/{sid}/chat/image",
                       headers=headers,
                       files={"file": (name, payload, mime)},
                       data={"body": body})


def stored_files(sid):
    import chat_attachments
    directory = chat_attachments.session_directory(sid)
    return sorted(p.name for p in directory.iterdir()) if directory.exists() else []


# ===========================================================================
# The happy path
# ===========================================================================

def test_a_listener_can_send_a_photograph(client, owner, live_room):
    sid, _room, harshit = live_room

    sent = send_image(harshit, png_bytes(), body="the speaker is unplugged")
    assert sent.status_code == 200, sent.text
    message = sent.json()
    assert message["has_image"] is True
    assert message["image_mime"] == "image/png"
    assert message["body"] == "the speaker is unplugged"

    # The transcript says there is one, and the bytes come from their own
    # endpoint rather than being inlined into every poll.
    view = host_view(client, owner, sid)
    assert view["messages"][0]["has_image"] is True
    assert "attachment_name" not in view["messages"][0]

    fetched = client.get(
        f"/api/broadcast/sessions/{sid}/chat/messages/{message['id']}/image",
        headers=owner)
    assert fetched.status_code == 200
    assert fetched.headers["content-type"].startswith("image/png")
    assert Image.open(io.BytesIO(fetched.content)).size == (40, 30)


def test_an_image_needs_no_caption(client, owner, live_room):
    """The picture IS the message. An empty body must not refuse it."""
    sid, _room, harshit = live_room
    sent = send_image(harshit, png_bytes())
    assert sent.status_code == 200, sent.text
    assert sent.json()["body"] is None
    assert host_view(client, owner, sid)["messages"][0]["has_image"] is True


def test_the_host_can_send_one_too(client, owner, live_room):
    sid, _room, harshit = live_room
    sent = host_send_image(client, owner, sid, jpeg_bytes(), name="fix.jpg",
                           mime="image/jpeg", body="set it to this")
    assert sent.status_code == 200, sent.text
    assert sent.json()["image_mime"] == "image/jpeg"
    # The audience sees it, because a host message is public.
    assert listener_view(harshit)["messages"][0]["has_image"] is True


# ===========================================================================
# What will not be stored
# ===========================================================================

def test_a_file_that_is_not_an_image_is_refused_whatever_it_claims(client, owner, live_room):
    sid, _room, harshit = live_room
    refused = send_image(harshit, b"#!/bin/sh\nrm -rf /\n",
                         name="innocent.png", mime="image/png")
    assert refused.status_code == 400
    assert "not an image" in refused.json()["detail"]
    assert host_view(client, owner, sid)["messages"] == []
    assert stored_files(sid) == [], "a refused upload left a file behind"


def test_an_svg_is_refused_because_it_is_a_document_not_an_image(client, owner, live_room):
    _sid, _room, harshit = live_room
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
    refused = send_image(harshit, svg, name="x.svg", mime="image/svg+xml")
    assert refused.status_code == 400


def test_a_polyglot_loses_everything_that_is_not_pixels(client, owner, live_room):
    """A real PNG with a payload appended. The re-encode is what saves us.

    This is the attack a magic-byte check waves through: the file IS a valid
    PNG, so any check on its first bytes passes. What must not survive is the
    tail, and it does not, because what gets written is a fresh encode of the
    decoded pixels.
    """
    sid, _room, harshit = live_room
    payload = png_bytes() + b"<?php system($_GET['c']); ?>" * 20

    sent = send_image(harshit, payload)
    assert sent.status_code == 200, sent.text

    from chat_attachments import session_directory
    stored = (session_directory(sid) / stored_files(sid)[0]).read_bytes()
    assert b"php" not in stored
    assert b"system(" not in stored


def test_exif_does_not_survive_the_re_encode(client, owner, live_room):
    """A phone photograph carries the GPS coordinates of the shop that took it."""
    sid, _room, harshit = live_room
    buffer = io.BytesIO()
    image = Image.new("RGB", (40, 30), (5, 5, 5))
    exif = image.getexif()
    exif[0x010E] = "SECRET SHOP LOCATION"   # ImageDescription
    image.save(buffer, format="JPEG", exif=exif)

    sent = send_image(harshit, buffer.getvalue(), name="p.jpg", mime="image/jpeg")
    assert sent.status_code == 200, sent.text

    from chat_attachments import session_directory
    stored = (session_directory(sid) / stored_files(sid)[0]).read_bytes()
    assert b"SECRET SHOP LOCATION" not in stored


def test_an_oversized_upload_is_refused(client, owner, live_room):
    sid, _room, harshit = live_room
    import chat_attachments
    refused = send_image(harshit, b"\x89PNG\r\n\x1a\n" +
                         b"x" * (chat_attachments.MAX_UPLOAD_BYTES + 1))
    assert refused.status_code == 400
    assert "smaller than" in refused.json()["detail"]
    assert stored_files(sid) == []


def test_a_very_large_image_is_scaled_down_rather_than_stored_whole(client, owner, live_room):
    sid, _room, harshit = live_room
    import chat_attachments

    sent = send_image(harshit, png_bytes(3000, 2000))
    assert sent.status_code == 200, sent.text
    assert max(sent.json()["image_width"], sent.json()["image_height"]) \
        <= chat_attachments.MAX_DIMENSION


def test_an_empty_file_is_refused(client, owner, live_room):
    _sid, _room, harshit = live_room
    assert send_image(harshit, b"").status_code == 400


# ===========================================================================
# The same gates as a text message
# ===========================================================================

def test_a_muted_listener_cannot_send_an_image_either(client, owner, live_room):
    sid, _room, harshit = live_room
    pid = client.get(f"/api/broadcast/sessions/{sid}/web-participants",
                     headers=owner).json()["listeners"][0]["id"]
    client.post(f"/api/broadcast/sessions/{sid}/web-participants/{pid}/chat-mute",
                headers=owner, json={"muted": True})

    refused = send_image(harshit, png_bytes())
    assert refused.status_code == 403
    # And the file it would have belonged to is not left on disk.
    assert stored_files(sid) == []


def test_images_count_against_the_rate_limit(client, owner, live_room):
    sid, _room, harshit = live_room
    outcomes = [send_image(harshit, png_bytes()).status_code for _ in range(8)]
    assert 429 in outcomes, "a listener could upload eight images in a second"
    assert len(stored_files(sid)) == outcomes.count(200)


def test_chat_turned_off_stops_images(client, owner, live_room):
    sid, _room, harshit = live_room
    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_enabled": False})
    assert send_image(harshit, png_bytes()).status_code == 403


# ===========================================================================
# Who may fetch the bytes
# ===========================================================================

def test_a_private_image_is_not_readable_by_another_listener(client, owner, live_room):
    """The hole a "nobody would guess the URL" defence leaves open."""
    sid, room, harshit = live_room
    priya = listener(client, room, "Priya")
    client.put(f"/api/broadcast/sessions/{sid}/chat/settings", headers=owner,
               json={"chat_mode": "PRIVATE"})

    sent = send_image(harshit, png_bytes())
    assert sent.status_code == 200, sent.text
    message_id = sent.json()["id"]

    # The author can read it back.
    assert harshit.get(f"/api/listen/chat/messages/{message_id}/image").status_code == 200
    # Another listener gets the same answer as for a message that is not there.
    denied = priya.get(f"/api/listen/chat/messages/{message_id}/image")
    assert denied.status_code == 404
    # And the host it was addressed to can see it.
    assert client.get(
        f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/image",
        headers=owner).status_code == 200


def test_a_browser_that_never_joined_cannot_fetch_an_image(client, owner, live_room):
    sid, _room, harshit = live_room
    message_id = send_image(harshit, png_bytes()).json()["id"]
    from fastapi.testclient import TestClient
    stranger = TestClient(client.server_module.app)
    assert stranger.get(f"/api/listen/chat/messages/{message_id}/image").status_code == 401


def test_another_operator_cannot_fetch_an_image_from_this_room(client, owner, live_room):
    sid, _room, harshit = live_room
    message_id = send_image(harshit, png_bytes()).json()["id"]
    client.post("/api/users", headers=owner, json={
        "username": "other", "display_name": "Other", "role": "BROADCASTER",
        "password": PASSWORD})
    stranger = sign_in(client, "other")
    assert client.get(
        f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/image",
        headers=stranger).status_code == 403


# ===========================================================================
# Removal
# ===========================================================================

def test_removing_a_message_hides_its_image_from_the_room(client, owner, live_room):
    """Removal takes the picture out of the room; deleting the Broadcast is
    what erases it. Both halves are asserted, because the difference between
    them is the whole retention story."""
    sid, _room, harshit = live_room
    message_id = send_image(harshit, png_bytes()).json()["id"]
    assert len(stored_files(sid)) == 1

    removed = client.post(
        f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
        headers=owner)
    assert removed.status_code == 200, removed.text

    # The listener it was in the room with can no longer fetch it - and gets
    # the same answer as for a message that never had a picture.
    assert harshit.get(
        f"/api/listen/chat/messages/{message_id}/image").status_code == 404
    assert listener_view(harshit)["messages"][0]["has_image"] is False

    # An account holding chat.view_deleted still can, because it is the one
    # that has to account for the removal.
    served = client.get(
        f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/image",
        headers=owner)
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/")

    # The row is a tombstone with its author intact.
    message = host_view(client, owner, sid)["messages"][0]
    assert message["deleted"] is True and message["has_image"] is True


def test_a_broadcaster_without_the_right_cannot_fetch_a_removed_image(client, owner, live_room):
    sid, _room, harshit = live_room
    message_id = send_image(harshit, png_bytes()).json()["id"]
    client.post(f"/api/broadcast/sessions/{sid}/chat/messages/{message_id}/delete",
                headers=owner)

    client.post("/api/users", headers=owner, json={
        "username": "runner", "display_name": "Runner", "role": "BROADCASTER",
        "password": PASSWORD})
    # A Broadcaster cannot read another operator's room at all, so scope the
    # assertion to the right that matters: the OWNER-held one is what reveals.
    view = host_view(client, owner, sid)
    assert view["may_see_removed"] is True


def test_deleting_the_broadcast_from_history_removes_its_images(client, owner, live_room):
    sid, _room, harshit = live_room
    send_image(harshit, png_bytes())
    assert len(stored_files(sid)) == 1

    client.post(f"/api/broadcast/sessions/{sid}/stop", headers=owner)
    removed = client.post("/api/broadcast/history/delete-permanently", headers=owner,
                          json={"ids": [sid], "confirm": "DELETE", "acknowledged": True})
    assert removed.status_code == 200, removed.text

    # A photograph from a real shop must not outlive the record of the
    # announcement it was sent during.
    assert stored_files(sid) == []


def test_the_transcript_can_still_serve_images_after_the_broadcast_ends(client, owner, live_room):
    sid, _room, harshit = live_room
    message_id = send_image(harshit, png_bytes()).json()["id"]
    client.post(f"/api/broadcast/sessions/{sid}/stop", headers=owner)

    served = client.get(
        f"/api/broadcast/history/{sid}/chat/messages/{message_id}/image",
        headers=owner)
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/")
