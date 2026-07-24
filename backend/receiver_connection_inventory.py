"""Pure, process-local inventory of authenticated Receiver connections.

The inventory records handshake identity and authentication source only.  It
does not store sockets, credentials, receiver health, playback, or acoustic
state, and no module-level inventory instance is created here.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock
from typing import Callable, TypeVar


DEFAULT_MAX_CONNECTIONS = 256
MIN_MAX_CONNECTIONS = 1
MAX_MAX_CONNECTIONS = 4096
MAX_CONNECTION_ID_LENGTH = 64

_CONNECTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_SECRET_LIKE_MARKERS = (
    "authorization",
    "bearer",
    "password",
    "secret",
    "echocast_rcv",
)


class ReceiverConnectionInventoryError(RuntimeError):
    """Base class for fixed, secret-free inventory failures."""


class InvalidConnectionRecordError(ReceiverConnectionInventoryError):
    def __init__(self) -> None:
        super().__init__("Invalid Receiver connection record")


class InvalidInventoryCapacityError(ReceiverConnectionInventoryError):
    def __init__(self) -> None:
        super().__init__("Invalid Receiver connection inventory capacity")


class ConnectionInventoryCapacityError(ReceiverConnectionInventoryError):
    def __init__(self) -> None:
        super().__init__("Receiver connection inventory capacity reached")


class ConnectionRegistrationConflictError(ReceiverConnectionInventoryError):
    def __init__(self) -> None:
        super().__init__("Receiver connection registration conflict")


class InvalidSnapshotTimeError(ReceiverConnectionInventoryError):
    def __init__(self) -> None:
        super().__init__("Invalid Receiver connection snapshot time")


class ConnectionAuthenticationSource(str, Enum):
    LEGACY_STORE_TOKEN = "legacy_store_token"
    HASHED_DEVICE_CREDENTIAL = "hashed_device_credential"


class RegistrationDisposition(str, Enum):
    REGISTERED = "registered"
    UNCHANGED = "unchanged"


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_utc(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == timedelta(0)
    )


def _validate_connection_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or len(value) > MAX_CONNECTION_ID_LENGTH
        or not _CONNECTION_ID_PATTERN.fullmatch(value)
    ):
        raise InvalidConnectionRecordError()
    folded = value.casefold()
    if any(marker in folded for marker in _SECRET_LIKE_MARKERS):
        raise InvalidConnectionRecordError()
    return value


@dataclass(frozen=True, slots=True)
class AuthenticatedReceiverConnection:
    """Non-secret immutable identity for one authenticated handshake."""

    connection_id: str
    store_id: int
    device_id: int | None
    credential_id: int | None
    authentication_source: ConnectionAuthenticationSource
    authenticated_at: datetime

    def __post_init__(self) -> None:
        _validate_connection_id(self.connection_id)
        if not _is_positive_int(self.store_id):
            raise InvalidConnectionRecordError()
        if self.device_id is not None and not _is_positive_int(self.device_id):
            raise InvalidConnectionRecordError()
        if self.credential_id is not None and not _is_positive_int(self.credential_id):
            raise InvalidConnectionRecordError()
        if not isinstance(self.authentication_source, ConnectionAuthenticationSource):
            raise InvalidConnectionRecordError()
        if not _is_utc(self.authenticated_at):
            raise InvalidConnectionRecordError()

        has_device = self.device_id is not None
        has_credential = self.credential_id is not None
        if has_device != has_credential:
            raise InvalidConnectionRecordError()
        if (
            self.authentication_source
            is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
            and not has_device
        ):
            raise InvalidConnectionRecordError()


@dataclass(frozen=True, slots=True)
class ConnectionRegistrationResult:
    disposition: RegistrationDisposition
    record: AuthenticatedReceiverConnection
    generation: int


@dataclass(frozen=True, slots=True)
class ConnectionRemovalResult:
    connection_id: str
    removed: bool
    record: AuthenticatedReceiverConnection | None
    generation: int


@dataclass(frozen=True, slots=True, order=True)
class StoreConnectionCount:
    store_id: int
    connection_count: int


@dataclass(frozen=True, slots=True)
class ActiveReceiverConnectionSnapshot:
    total_active_count: int
    legacy_authenticated_count: int
    hashed_authenticated_count: int
    store_counts: tuple[StoreConnectionCount, ...]
    captured_at: datetime
    records: tuple[AuthenticatedReceiverConnection, ...]
    generation: int


SummaryT = TypeVar("SummaryT")


class ActiveReceiverConnectionInventory:
    """A bounded, lock-protected inventory with no connection history."""

    def __init__(self, *, max_connections: int = DEFAULT_MAX_CONNECTIONS) -> None:
        if (
            isinstance(max_connections, bool)
            or not isinstance(max_connections, int)
            or not MIN_MAX_CONNECTIONS <= max_connections <= MAX_MAX_CONNECTIONS
        ):
            raise InvalidInventoryCapacityError()
        self._max_connections = max_connections
        self._records: dict[str, AuthenticatedReceiverConnection] = {}
        self._generation = 0
        self._lock = RLock()

    @property
    def max_connections(self) -> int:
        return self._max_connections

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def register(
        self,
        record: AuthenticatedReceiverConnection,
    ) -> ConnectionRegistrationResult:
        if not isinstance(record, AuthenticatedReceiverConnection):
            raise InvalidConnectionRecordError()

        with self._lock:
            existing = self._records.get(record.connection_id)
            if existing is not None:
                if existing == record:
                    return ConnectionRegistrationResult(
                        RegistrationDisposition.UNCHANGED,
                        existing,
                        self._generation,
                    )
                raise ConnectionRegistrationConflictError()
            if len(self._records) >= self._max_connections:
                raise ConnectionInventoryCapacityError()

            self._records[record.connection_id] = record
            self._generation += 1
            return ConnectionRegistrationResult(
                RegistrationDisposition.REGISTERED,
                record,
                self._generation,
            )

    def remove(self, connection_id: str) -> ConnectionRemovalResult:
        validated_id = _validate_connection_id(connection_id)
        with self._lock:
            record = self._records.pop(validated_id, None)
            if record is None:
                return ConnectionRemovalResult(
                    validated_id,
                    False,
                    None,
                    self._generation,
                )
            self._generation += 1
            return ConnectionRemovalResult(
                validated_id,
                True,
                record,
                self._generation,
            )

    def get(self, connection_id: str) -> AuthenticatedReceiverConnection | None:
        validated_id = _validate_connection_id(connection_id)
        with self._lock:
            return self._records.get(validated_id)

    def snapshot(
        self,
        *,
        captured_at: datetime | None = None,
    ) -> ActiveReceiverConnectionSnapshot:
        capture_time = datetime.now(timezone.utc) if captured_at is None else captured_at
        if not _is_utc(capture_time):
            raise InvalidSnapshotTimeError()

        with self._lock:
            records = tuple(sorted(self._records.values(), key=lambda item: item.connection_id))
            by_store = Counter(record.store_id for record in records)
            legacy_count = sum(
                record.authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN
                for record in records
            )
            hashed_count = sum(
                record.authentication_source
                is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
                for record in records
            )
            store_counts = tuple(
                StoreConnectionCount(store_id, by_store[store_id])
                for store_id in sorted(by_store)
            )
            return ActiveReceiverConnectionSnapshot(
                total_active_count=len(records),
                legacy_authenticated_count=legacy_count,
                hashed_authenticated_count=hashed_count,
                store_counts=store_counts,
                captured_at=capture_time,
                records=records,
                generation=self._generation,
            )

    def build_transition_summary(
        self,
        summary_factory: Callable[..., SummaryT],
        *,
        captured_at: datetime | None = None,
    ) -> SummaryT:
        """Build a transition-service summary from one atomic snapshot.

        The caller supplies ``ActiveReceiverConnectionSummary`` (or a compatible
        constructor), keeping this pure module independent from SQLAlchemy and
        the migration transition implementation.
        """

        if not callable(summary_factory):
            raise InvalidSnapshotTimeError()
        snapshot = self.snapshot(captured_at=captured_at)
        return summary_factory(
            legacy_authenticated_count=snapshot.legacy_authenticated_count,
            hashed_authenticated_count=snapshot.hashed_authenticated_count,
            captured_at=snapshot.captured_at,
        )
