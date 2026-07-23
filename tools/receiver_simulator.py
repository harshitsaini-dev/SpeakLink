"""Local non-audio EchoCast receiver protocol simulator.

This tool exercises acknowledgement semantics only. It never captures, sends,
decodes, or plays audio.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import websockets


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIRECTORY = REPOSITORY_ROOT / "backend"
if str(BACKEND_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIRECTORY))

from receiver_contract import parse_receiver_ack  # noqa: E402


SUPPORTED_MESSAGE_TYPES = frozenset(
    {
        "heartbeat",
        "receiver_ready",
        "audio_receiving",
        "playback_confirmed",
        "playback_error",
        "device_error",
        "stopped",
    }
)
SESSION_SCOPED_TYPES = frozenset(
    {"audio_receiving", "playback_confirmed", "playback_error", "stopped"}
)
SCENARIOS = (
    "ready-only",
    "successful-playback",
    "playback-error",
    "device-error",
    "duplicate-message-rejection",
    "out-of-order-sequence-rejection",
    "wrong-session-rejection",
    "stopped",
)


class SimulatorError(Exception):
    """Base class for credential-safe simulator errors."""


class SimulatorConfigurationError(SimulatorError):
    """Raised for invalid destinations, scenarios, or acknowledgement inputs."""


class SimulatorConnectionError(SimulatorError):
    """Raised when a socket operation fails without exposing its credential URL."""


@dataclass(frozen=True, slots=True)
class ScenarioResult:
    name: str
    sent_types: tuple[str, ...]
    rejections: tuple[str, ...] = ()


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_endpoint(url: str, allow_non_loopback: bool) -> str:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise SimulatorConfigurationError("receiver URL is invalid") from None

    if parsed.scheme != "ws":
        raise SimulatorConfigurationError("receiver URL must use ws://")
    if not parsed.hostname or port is None:
        raise SimulatorConfigurationError("receiver URL requires an explicit host and port")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SimulatorConfigurationError("receiver URL must not contain credentials or query data")

    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = False
    if not is_loopback and not allow_non_loopback:
        raise SimulatorConfigurationError(
            "non-loopback receiver URL refused; use explicit non-loopback opt-in"
        )
    return url.rstrip("/")


class MessageFactory:
    """Build strict receiver messages with UUID IDs, UTC time, and monotonic sequence."""

    def __init__(self) -> None:
        self._next_sequence = 1

    def build(
        self,
        message_type: str,
        *,
        session_id: int | None = None,
        sequence: int | None = None,
        message_id: UUID | None = None,
        error_code: str | None = None,
        details: str | None = None,
        recoverable: bool = False,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if message_type not in SUPPORTED_MESSAGE_TYPES:
            raise SimulatorConfigurationError("unsupported receiver message type")
        if message_type in SESSION_SCOPED_TYPES and session_id is None:
            raise SimulatorConfigurationError("scenario requires an explicit session ID")

        if sequence is None:
            sequence = self._next_sequence
            self._next_sequence += 1
        payload: dict[str, Any] = {
            "protocol_version": "1.0",
            "type": message_type,
            "message_id": str(message_id or uuid4()),
            "occurred_at": _utc_timestamp(),
            "sequence": sequence,
        }
        if message_type in SESSION_SCOPED_TYPES:
            payload["session_id"] = session_id
        if message_type == "receiver_ready":
            payload.update(
                {
                    "software_checks_passed": True,
                    "output_device_checks_passed": True,
                }
            )
        if message_type in {"playback_error", "device_error"}:
            payload.update(
                {
                    "error_code": error_code or "SIMULATED_ERROR",
                    "details": details or "Simulator generated failure.",
                    "recoverable": recoverable,
                }
            )
        if message_type == "stopped" and reason is not None:
            payload["reason"] = reason

        try:
            acknowledgement = parse_receiver_ack(payload)
        except Exception:
            raise SimulatorConfigurationError(
                "receiver message does not satisfy the protocol contract"
            ) from None
        return acknowledgement.model_dump(mode="json")


class ReceiverProtocolSimulator:
    """Credential-safe WebSocket client for deterministic receiver scenarios."""

    def __init__(
        self,
        url: str,
        token: str,
        *,
        allow_non_loopback: bool = False,
        timeout: float = 5.0,
    ) -> None:
        if not token:
            raise SimulatorConfigurationError("receiver credential is required")
        self._endpoint = _validated_endpoint(url, allow_non_loopback)
        self._token = token
        self._timeout = timeout
        self._factory = MessageFactory()
        self._websocket = None

    async def __aenter__(self) -> "ReceiverProtocolSimulator":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.close()

    async def connect(self) -> None:
        if self._websocket is not None:
            raise SimulatorConnectionError("receiver simulator is already connected")
        try:
            self._websocket = await websockets.connect(
                self._endpoint,
                additional_headers={"Authorization": f"Bearer {self._token}"},
                open_timeout=self._timeout,
            )
        except Exception:
            raise SimulatorConnectionError("receiver WebSocket connection failed") from None

    async def close(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            try:
                await websocket.close()
            except Exception:
                pass

    def _require_connection(self):
        if self._websocket is None:
            raise SimulatorConnectionError("receiver simulator is not connected")
        return self._websocket

    async def _send_payload(self, payload: dict[str, Any]) -> None:
        websocket = self._require_connection()
        try:
            await websocket.send(json.dumps(payload))
        except Exception:
            raise SimulatorConnectionError("receiver message send failed") from None

    async def send_acknowledgement(self, message_type: str, **fields) -> dict[str, Any]:
        payload = self._factory.build(message_type, **fields)
        await self._send_payload(payload)
        return payload

    async def _receive_json(self) -> dict[str, Any]:
        websocket = self._require_connection()
        try:
            message = await asyncio.wait_for(websocket.recv(), timeout=self._timeout)
            if not isinstance(message, str):
                raise ValueError
            parsed = json.loads(message)
            if not isinstance(parsed, dict):
                raise ValueError
            return parsed
        except Exception:
            raise SimulatorConnectionError("receiver response was unavailable or invalid") from None

    async def wait_for_command(self, command_type: str) -> dict[str, Any]:
        while True:
            message = await self._receive_json()
            if message.get("type") == command_type:
                return message
            if message.get("type") == "ack_rejected":
                raise SimulatorConnectionError("server rejected an acknowledgement")

    async def _expect_rejection(self) -> str:
        message = await self._receive_json()
        if message.get("type") != "ack_rejected" or not isinstance(message.get("code"), str):
            raise SimulatorConnectionError("server did not return a safe rejection code")
        return message["code"]

    @staticmethod
    def _require_session(session_id: int | None) -> int:
        if session_id is None or session_id <= 0:
            raise SimulatorConfigurationError("scenario requires an explicit session ID")
        return session_id

    async def run_scenario(
        self,
        name: str,
        *,
        session_id: int | None = None,
    ) -> ScenarioResult:
        if name not in SCENARIOS:
            raise SimulatorConfigurationError("unknown simulator scenario")

        sent: list[str] = []
        rejections: list[str] = []

        async def send(message_type: str, **fields) -> dict[str, Any]:
            payload = await self.send_acknowledgement(message_type, **fields)
            sent.append(message_type)
            return payload

        if name == "ready-only":
            await send("receiver_ready")
        elif name == "successful-playback":
            active_session = self._require_session(session_id)
            await send("receiver_ready")
            await send("audio_receiving", session_id=active_session)
            await send("playback_confirmed", session_id=active_session)
        elif name == "playback-error":
            active_session = self._require_session(session_id)
            await send("receiver_ready")
            await send("audio_receiving", session_id=active_session)
            await send(
                "playback_error",
                session_id=active_session,
                error_code="SIMULATED_PLAYBACK_ERROR",
                details="Simulator playback pipeline failure.",
                recoverable=True,
            )
        elif name == "device-error":
            await send(
                "device_error",
                error_code="SIMULATED_DEVICE_ERROR",
                details="Simulator output device failure.",
            )
        elif name == "duplicate-message-rejection":
            heartbeat = await send("heartbeat")
            await self._send_payload(heartbeat)
            sent.append("heartbeat")
            rejections.append(await self._expect_rejection())
        elif name == "out-of-order-sequence-rejection":
            await send("heartbeat")
            await send("heartbeat", sequence=0)
            rejections.append(await self._expect_rejection())
        elif name == "wrong-session-rejection":
            active_session = self._require_session(session_id)
            await send("receiver_ready")
            await send("audio_receiving", session_id=active_session + 1)
            rejections.append(await self._expect_rejection())
        elif name == "stopped":
            active_session = self._require_session(session_id)
            await send("stopped", session_id=active_session, reason="simulator_requested")

        return ScenarioResult(name, tuple(sent), tuple(rejections))


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        required=True,
        help="Receiver endpoint base URL, for example ws://127.0.0.1:8000/api/ws/receiver",
    )
    parser.add_argument(
        "--token",
        help="Receiver credential; defaults to ECHOCAST_RECEIVER_TOKEN",
    )
    parser.add_argument("--scenario", required=True, choices=SCENARIOS)
    parser.add_argument("--session-id", type=int)
    parser.add_argument(
        "--allow-non-loopback",
        action="store_true",
        help="Explicitly allow a receiver URL outside the local loopback interface",
    )
    return parser


async def _run_from_arguments(arguments: argparse.Namespace) -> ScenarioResult:
    token = arguments.token or os.environ.get("ECHOCAST_RECEIVER_TOKEN")
    if not token:
        raise SimulatorConfigurationError(
            "receiver credential is required via --token or ECHOCAST_RECEIVER_TOKEN"
        )
    async with ReceiverProtocolSimulator(
        arguments.url,
        token,
        allow_non_loopback=arguments.allow_non_loopback,
    ) as simulator:
        return await simulator.run_scenario(
            arguments.scenario,
            session_id=arguments.session_id,
        )


def main(argv: list[str] | None = None) -> int:
    arguments = build_argument_parser().parse_args(argv)
    try:
        result = asyncio.run(_run_from_arguments(arguments))
    except SimulatorError as error:
        print(f"Receiver simulator failed: {error}", file=sys.stderr)
        return 2
    except Exception:
        print("Receiver simulator failed unexpectedly", file=sys.stderr)
        return 2

    rejection_summary = ",".join(result.rejections) if result.rejections else "none"
    print(
        f"Scenario {result.name} completed; messages={len(result.sent_types)}; "
        f"rejections={rejection_summary}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
