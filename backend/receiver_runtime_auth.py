"""Explicit runtime authentication boundary for Receiver WebSocket handshakes.

The default implementation verifies only active legacy Store tokens.  The
migration-aware implementation is constructed explicitly with an Engine and
HMAC key ring; importing this module does not enable it or read migration state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
import secrets

from receiver_connection_inventory import ConnectionAuthenticationSource


AUTHENTICATION_FAILURE_MESSAGE = "Receiver authentication failed"
CONFIGURATION_FAILURE_MESSAGE = "Receiver authentication configuration is not ready"
MAX_RUNTIME_HASH_KEYS = 16
MAX_PRESENTED_TOKEN_LENGTH = 128


class ReceiverRuntimeAuthenticationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(AUTHENTICATION_FAILURE_MESSAGE)


class ReceiverRuntimeConfigurationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(CONFIGURATION_FAILURE_MESSAGE)


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _require_utc(value: object) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ReceiverRuntimeAuthenticationError()
    return value


@dataclass(frozen=True, slots=True, repr=False)
class ReceiverRuntimeIdentity:
    store_id: int
    authentication_source: ConnectionAuthenticationSource
    device_id: int | None = None
    credential_id: int | None = None

    def __post_init__(self) -> None:
        if not _positive_integer(self.store_id):
            raise ReceiverRuntimeConfigurationError()
        if not isinstance(self.authentication_source, ConnectionAuthenticationSource):
            raise ReceiverRuntimeConfigurationError()
        if self.device_id is not None and not _positive_integer(self.device_id):
            raise ReceiverRuntimeConfigurationError()
        if self.credential_id is not None and not _positive_integer(self.credential_id):
            raise ReceiverRuntimeConfigurationError()
        if (self.device_id is None) != (self.credential_id is None):
            raise ReceiverRuntimeConfigurationError()
        if (
            self.authentication_source
            is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL
            and self.device_id is None
        ):
            raise ReceiverRuntimeConfigurationError()

    def __repr__(self) -> str:
        return (
            "ReceiverRuntimeIdentity("
            f"store_id={self.store_id}, "
            f"authentication_source={self.authentication_source.value!r}, "
            f"device_id={self.device_id!r}, credential_id={self.credential_id!r})"
        )

    __str__ = __repr__


class ReceiverRuntimeAuthenticator(Protocol):
    def authenticate(
        self,
        *,
        presented_token: str,
        authenticated_at: datetime,
    ) -> ReceiverRuntimeIdentity: ...


class LegacyStoreTokenRuntimeAuthenticator:
    """Default authenticator; never reads Receiver credential lifecycle tables."""

    def __init__(self, session_factory: Callable[[], object]) -> None:
        if not callable(session_factory):
            raise ReceiverRuntimeConfigurationError()
        self._session_factory = session_factory

    def __repr__(self) -> str:
        return "LegacyStoreTokenRuntimeAuthenticator()"

    __str__ = __repr__

    def authenticate(
        self,
        *,
        presented_token: str,
        authenticated_at: datetime,
    ) -> ReceiverRuntimeIdentity:
        _require_utc(authenticated_at)
        if (
            not isinstance(presented_token, str)
            or not presented_token
            or len(presented_token) > MAX_PRESENTED_TOKEN_LENGTH
        ):
            raise ReceiverRuntimeAuthenticationError()
        try:
            candidate = presented_token.encode("ascii")
        except UnicodeEncodeError:
            raise ReceiverRuntimeAuthenticationError() from None
        if any(byte < 0x21 or byte > 0x7E for byte in candidate):
            raise ReceiverRuntimeAuthenticationError()

        try:
            from models import Store

            matched_store_id = None
            with self._session_factory() as session:
                active_stores = session.query(Store).filter(Store.is_active.is_(True)).all()
                for store in active_stores:
                    expected = store.receiver_token.encode("ascii")
                    if secrets.compare_digest(expected, candidate):
                        matched_store_id = store.id
            if matched_store_id is None:
                raise ReceiverRuntimeAuthenticationError()
            return ReceiverRuntimeIdentity(
                store_id=matched_store_id,
                authentication_source=ConnectionAuthenticationSource.LEGACY_STORE_TOKEN,
            )
        except ReceiverRuntimeAuthenticationError:
            raise
        except Exception:
            raise ReceiverRuntimeAuthenticationError() from None


class DualRuntimeAuthenticator:
    """Accept a Device credential, or fall back to the legacy Store token.

    This exists for one explicit, temporary reason: Device credentials are new,
    and the Receivers already running - including the one that produced every
    piece of amplifier evidence - authenticate with a shared per-Store token.
    Switching outright would disconnect them at the moment of the switch.

    The Device credential is tried **first**, so a Store stops depending on its
    shared token the moment one of its computers presents a real credential, with
    no configuration change and no cutover flag.

    Two properties matter more than the convenience:

    * a revoked or disabled Device must not be rescued by the legacy path. Both
      are consulted, and both must refuse, or revocation is theatre.
    * the returned identity says which transport proved it, so the dashboard can
      show honestly that a Store is still on a shared token.

    A configuration failure - a missing key ring, say - is re-raised rather than
    swallowed into "bad credential", because that is the operator's problem and
    hiding it would look like every Receiver suddenly having a bad token.

    Removing this class is the documented cutover. See docs/RECEIVER_ENROLMENT.md.
    """

    def __init__(self, *, device: object, legacy: object) -> None:
        if device is None or legacy is None:
            raise ReceiverRuntimeConfigurationError()
        self._device = device
        self._legacy = legacy

    def __repr__(self) -> str:
        return "DualRuntimeAuthenticator(device=<device credential>, legacy=<store token>)"

    __str__ = __repr__

    def authenticate(
        self,
        *,
        presented_token: str,
        authenticated_at: datetime,
    ) -> ReceiverRuntimeIdentity:
        try:
            return self._device.authenticate(
                presented_token=presented_token, authenticated_at=authenticated_at
            )
        except ReceiverRuntimeConfigurationError:
            raise
        except ReceiverRuntimeAuthenticationError:
            pass

        # One refusal shape for both paths: two different errors would tell a
        # caller whether this Store has enrolled Devices.
        return self._legacy.authenticate(
            presented_token=presented_token, authenticated_at=authenticated_at
        )


class MigrationAwareReceiverRuntimeAuthenticator:
    """Explicit read-only adapter over ``authenticate_receiver_credential``."""

    def __init__(self, engine: object, *, hash_keys: Mapping[int, bytes]) -> None:
        if engine is None or not isinstance(hash_keys, Mapping):
            raise ReceiverRuntimeConfigurationError()
        if not 1 <= len(hash_keys) <= MAX_RUNTIME_HASH_KEYS:
            raise ReceiverRuntimeConfigurationError()
        copied_keys: dict[int, bytes] = {}
        for version, key in hash_keys.items():
            if not _positive_integer(version) or not isinstance(key, bytes):
                raise ReceiverRuntimeConfigurationError()
            copied_keys[version] = bytes(key)
        self._engine = engine
        self._hash_keys = copied_keys

    def __repr__(self) -> str:
        return (
            "MigrationAwareReceiverRuntimeAuthenticator("
            f"key_versions={tuple(sorted(self._hash_keys))!r}, key_material=<redacted>)"
        )

    __str__ = __repr__

    def authenticate(
        self,
        *,
        presented_token: str,
        authenticated_at: datetime,
    ) -> ReceiverRuntimeIdentity:
        validated_at = _require_utc(authenticated_at)
        try:
            from receiver_auth_service import authenticate_receiver_credential

            result = authenticate_receiver_credential(
                self._engine,
                presented_token=presented_token,
                hash_keys=self._hash_keys,
                now=validated_at,
            )
            source = ConnectionAuthenticationSource(result.verification_source.value)
            return ReceiverRuntimeIdentity(
                store_id=result.store_id,
                device_id=result.device_id,
                credential_id=result.credential_id,
                authentication_source=source,
            )
        except ReceiverRuntimeConfigurationError:
            raise
        except Exception:
            raise ReceiverRuntimeAuthenticationError() from None
