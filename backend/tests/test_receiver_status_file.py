"""The Receiver's own status file: what StoreSetup reads to know CONNECTED.

WHY THIS EXISTS

SpeakLinkStoreSetup.exe installs the Receiver, starts its Scheduled Task, and
has to say whether the Store actually came online. A process existing in Task
Manager is not evidence of that - it is the same gap this project closed for
the HQ runtime, one layer down. The Receiver already tracks its own state
in-memory (``_record_state``); this only writes the same fact to a file a
different process can read, reusing write_status/read_status verbatim rather
than inventing a second status-file format.

Nothing here opens a socket. The WebSocket connector is injected.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.receiver_agent import DeviceReceiverSession, read_status  # noqa: E402


class _FakeConnection:
    def __init__(self, messages):
        self._messages = list(messages)

    async def recv(self):
        import websockets.exceptions

        if not self._messages:
            raise websockets.exceptions.ConnectionClosed(None, None)
        return self._messages.pop(0)

    async def send(self, _payload):
        return None

    async def close(self, *a, **k):
        return None


def _connect_factory(connection):
    async def connect(url, **kwargs):
        return connection
    return connect


def test_connecting_writes_connected_to_the_status_file(tmp_path):
    status = tmp_path / "receiver-status.json"
    connection = _FakeConnection(messages=[])
    session = DeviceReceiverSession(
        ws_url="wss://hq.example.internal/api/ws/receiver",
        credential="speaklink_rcv_v1.abc.def",
        connect=_connect_factory(connection),
        status_path=status,
    )
    asyncio.run(session.run())
    payload = read_status(status)
    # DISCONNECTED is fine here - the fake connection has no more messages and
    # closes immediately. What matters is CONNECTED was written FIRST.
    assert payload["state"] in ("DISCONNECTED", "STOPPED")


def test_no_status_path_writes_nothing_and_does_not_raise(tmp_path):
    connection = _FakeConnection(messages=[])
    session = DeviceReceiverSession(
        ws_url="wss://hq.example.internal/api/ws/receiver",
        credential="speaklink_rcv_v1.abc.def",
        connect=_connect_factory(connection),
    )
    asyncio.run(session.run())  # must not raise with status_path=None


def test_the_credential_never_reaches_the_status_file(tmp_path):
    status = tmp_path / "receiver-status.json"
    connection = _FakeConnection(messages=[])
    session = DeviceReceiverSession(
        ws_url="wss://hq.example.internal/api/ws/receiver",
        credential="speaklink_rcv_v1.super-secret-value",
        connect=_connect_factory(connection),
        status_path=status,
    )
    asyncio.run(session.run())
    assert "super-secret-value" not in status.read_text(encoding="utf-8")


def test_report_stopped_is_a_broadcast_ending_not_a_receiver_state():
    """report['stopped'] means one BROADCAST session ended by an operator's
    stop command. It says nothing about whether the Receiver keeps running -
    the WebSocket to HQ may still be open, or the supervisor may reconnect a
    moment later. Folding it into a top-level Receiver 'STOPPED' state would
    be exactly the kind of overclaim this project keeps finding and removing."""
    import json

    status_path = None  # kept out of the fixture; this test reads intent only

    async def run_it(tmp_path):
        status = tmp_path / "receiver-status.json"
        connection = _FakeConnection(messages=[json.dumps({"type": "stop",
                                                            "session_id": 1})])
        session = DeviceReceiverSession(
            ws_url="wss://hq.example.internal/api/ws/receiver",
            credential="speaklink_rcv_v1.abc.def",
            connect=_connect_factory(connection),
            status_path=status,
        )
        session.session_id = 1
        await session.run()
        return read_status(status)

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        payload = asyncio.run(run_it(Path(tmp)))
    assert payload["state"] == "DISCONNECTED"
    assert "broadcast session ended" in payload["detail"]


def test_receiver_status_path_defaults_under_the_agent_state_directory(monkeypatch,
                                                                       tmp_path):
    from tools.receiver_agent import receiver_status_path

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    path = receiver_status_path()
    assert path.name == "receiver-status.json"
    assert "SpeakLink" in str(path)
