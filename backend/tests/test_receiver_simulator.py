"""Real-loopback tests for the non-audio receiver protocol simulator."""
import asyncio
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import time

import pytest
import requests


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPOSITORY_ROOT / "backend"
REAL_DATABASE = BACKEND_DIR / "echocast_live.db"
sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.receiver_simulator import (  # noqa: E402
    MessageFactory,
    ReceiverProtocolSimulator,
    SimulatorConfigurationError,
    main,
)


def database_metadata(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def random_loopback_port() -> int:
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        return port_socket.getsockname()[1]


@pytest.fixture(scope="module")
def isolated_server(tmp_path_factory):
    real_database_before = database_metadata(REAL_DATABASE)
    temporary_directory = tmp_path_factory.mktemp("receiver-simulator")
    database_path = temporary_directory / "simulator.db"
    port = random_loopback_port()
    admin_username = f"sim-admin-{secrets.token_hex(8)}"
    admin_password = secrets.token_urlsafe(32)
    environment = os.environ.copy()
    environment.update(
        {
            "ECHOCAST_DB_PATH": str(database_path),
            "JWT_SECRET": secrets.token_urlsafe(48),
            "ADMIN_USERNAME": admin_username,
            "ADMIN_PASSWORD": admin_password,
            "CORS_ORIGINS": "http://localhost:3000",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
        ],
        cwd=BACKEND_DIR,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    http_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Isolated receiver-simulator server exited during startup")
            try:
                if requests.get(f"{http_url}/docs", timeout=0.5).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail("Isolated receiver-simulator server did not become ready")

        login = requests.post(
            f"{http_url}/api/auth/login",
            json={"username": admin_username, "password": admin_password},
            timeout=5,
        )
        assert login.status_code == 200
        admin_token = login.json()["access_token"]
        store_response = requests.post(
            f"{http_url}/api/stores",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "store_code": f"SIM-{secrets.token_hex(5)}",
                "store_name": "Isolated Receiver Simulator",
                "city": "Loopback",
                "region": "Test",
                "is_online_store": True,
            },
            timeout=5,
        )
        assert store_response.status_code == 201
        store = store_response.json()
        yield {
            "database_path": database_path,
            "http_url": http_url,
            "ws_url": f"ws://127.0.0.1:{port}/api/ws/receiver",
            "admin_headers": {"Authorization": f"Bearer {admin_token}"},
            "store_id": store["id"],
            "receiver_token": store["receiver_token"],
        }
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        assert database_metadata(REAL_DATABASE) == real_database_before


def api_request(server, method: str, path: str, **kwargs):
    response = requests.request(
        method,
        f"{server['http_url']}{path}",
        headers=server["admin_headers"],
        timeout=5,
        **kwargs,
    )
    assert response.status_code < 400, response.text
    return response.json()


def create_and_start_session(server, campaign: str) -> int:
    session = api_request(
        server,
        "POST",
        "/api/broadcast/sessions",
        json={
            "campaign_name": campaign,
            "target_mode": "selected",
            "store_ids": [server["store_id"]],
        },
    )
    api_request(server, "POST", f"/api/broadcast/sessions/{session['id']}/start")
    return session["id"]


def target_status(server, session_id: int) -> dict:
    session = api_request(server, "GET", f"/api/broadcast/sessions/{session_id}")
    return session["targets"][0]


def receiver_event_types(server, session_id: int | None = None) -> list[str]:
    del session_id  # Receiver events are store-scoped in the existing schema.
    with sqlite3.connect(server["database_path"]) as connection:
        rows = connection.execute(
            "SELECT event_type FROM receiver_events WHERE store_id = ? ORDER BY id",
            (server["store_id"],),
        ).fetchall()
    return [row[0] for row in rows]


async def wait_until(predicate, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("Timed out waiting for isolated simulator state")


def test_simulator_refuses_unsafe_destinations_and_speaker_claims():
    with pytest.raises(SimulatorConfigurationError):
        ReceiverProtocolSimulator("ws://example.com:80/api/ws/receiver", "credential")
    ReceiverProtocolSimulator(
        "ws://example.com:80/api/ws/receiver",
        "credential",
        allow_non_loopback=True,
    )
    with pytest.raises(SimulatorConfigurationError):
        MessageFactory().build("speaker_verified", session_id=1)
    with pytest.raises(SimulatorConfigurationError):
        MessageFactory().build("audio_receiving")


def test_cli_failure_does_not_print_environment_credential(monkeypatch, capsys):
    receiver_credential = secrets.token_urlsafe(32)
    monkeypatch.setenv("ECHOCAST_RECEIVER_TOKEN", receiver_credential)
    result = main(
        [
            "--url",
            "ws://example.com:80/api/ws/receiver",
            "--scenario",
            "ready-only",
        ]
    )
    assert result == 2
    captured = capsys.readouterr()
    if receiver_credential in captured.out or receiver_credential in captured.err:
        pytest.fail("Simulator output contained a receiver credential", pytrace=False)


def test_real_loopback_receiver_scenarios(isolated_server, capsys):
    server = isolated_server

    async def scenario():
        async with ReceiverProtocolSimulator(
            server["ws_url"], server["receiver_token"]
        ) as simulator:
            await wait_until(
                lambda: next(
                    store
                    for store in api_request(server, "GET", "/api/stores")
                    if store["id"] == server["store_id"]
                )["status"]
                == "online"
            )
            result = await simulator.run_scenario("ready-only")
            assert result.sent_types == ("receiver_ready",)
            await wait_until(lambda: "receiver_ready" in receiver_event_types(server))

        async with ReceiverProtocolSimulator(
            server["ws_url"], server["receiver_token"]
        ) as simulator:
            session_id = create_and_start_session(server, "Simulator successful playback")
            play = await simulator.wait_for_command("play")
            assert play["session_id"] == session_id
            pending = target_status(server, session_id)
            assert pending["play_status"] == "pending"
            assert pending["started_playing_at"] is None

            result = await simulator.run_scenario(
                "successful-playback",
                session_id=session_id,
            )
            assert result.sent_types == (
                "receiver_ready",
                "audio_receiving",
                "playback_confirmed",
            )
            await wait_until(
                lambda: target_status(server, session_id)["play_status"]
                == "playback_confirmed"
            )
            events = receiver_event_types(server)
            assert events.index("audio_receiving") < events.index("playback_confirmed")

            api_request(server, "POST", f"/api/broadcast/sessions/{session_id}/stop")
            stop = await simulator.wait_for_command("stop")
            assert stop["session_id"] == session_id
            await simulator.run_scenario("stopped", session_id=session_id)
            await wait_until(
                lambda: target_status(server, session_id)["play_status"] == "stopped"
            )

        async with ReceiverProtocolSimulator(
            server["ws_url"], server["receiver_token"]
        ) as simulator:
            session_id = create_and_start_session(server, "Simulator playback error")
            await simulator.wait_for_command("play")
            await simulator.run_scenario("playback-error", session_id=session_id)
            await wait_until(
                lambda: target_status(server, session_id)["play_status"]
                == "playback_error"
            )
            api_request(server, "POST", f"/api/broadcast/sessions/{session_id}/stop")

        async with ReceiverProtocolSimulator(
            server["ws_url"], server["receiver_token"]
        ) as simulator:
            await simulator.run_scenario("device-error")
            await wait_until(lambda: "device_error" in receiver_event_types(server))

        async with ReceiverProtocolSimulator(
            server["ws_url"], server["receiver_token"]
        ) as simulator:
            duplicate = await simulator.run_scenario("duplicate-message-rejection")
            assert duplicate.rejections == ("DUPLICATE_MESSAGE",)

        async with ReceiverProtocolSimulator(
            server["ws_url"], server["receiver_token"]
        ) as simulator:
            out_of_order = await simulator.run_scenario("out-of-order-sequence-rejection")
            assert out_of_order.rejections == ("NON_MONOTONIC_SEQUENCE",)

        async with ReceiverProtocolSimulator(
            server["ws_url"], server["receiver_token"]
        ) as simulator:
            session_id = create_and_start_session(server, "Simulator wrong session")
            await simulator.wait_for_command("play")
            wrong_session = await simulator.run_scenario(
                "wrong-session-rejection",
                session_id=session_id,
            )
            assert wrong_session.rejections == ("WRONG_SESSION",)
            api_request(server, "POST", f"/api/broadcast/sessions/{session_id}/stop")

    asyncio.run(scenario())
    captured = capsys.readouterr()
    if (
        isolated_server["receiver_token"] in captured.out
        or isolated_server["receiver_token"] in captured.err
    ):
        pytest.fail("Simulator output contained a receiver credential", pytrace=False)
    assert isolated_server["database_path"].resolve() != REAL_DATABASE.resolve()
