"""Isolated coordinator for controlled Receiver credential cutover rehearsals.

The coordinator has no FastAPI route, application startup hook, environment
key loading, or default database dependency.  It only combines explicitly
injected temporary-database/runtime components and delegates transition rules
to ``receiver_migration_transition_service``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType

from sqlalchemy.engine import Engine

from migrations import PROTECTED_DATABASE_PATH
from receiver_credentials import MIN_HASH_KEY_BYTES
from receiver_migration_transition_service import (
    MAX_HASH_KEY_VERSIONS,
    ActiveReceiverConnectionSummary,
    MigrationTransitionResult,
    RuntimeAction,
    transition_receiver_migration_state,
)
from receiver_runtime_auth import MigrationAwareReceiverRuntimeAuthenticator


class ReceiverCutoverRehearsalError(RuntimeError):
    """Base class for fixed, secret-free rehearsal configuration errors."""


class InvalidCutoverConfigurationError(ReceiverCutoverRehearsalError):
    def __init__(self) -> None:
        super().__init__("Receiver cutover rehearsal configuration is invalid")


class ProtectedCutoverDatabaseError(ReceiverCutoverRehearsalError):
    def __init__(self) -> None:
        super().__init__("Protected Receiver database cannot be rehearsed")


class CutoverStepCode(str, Enum):
    ENABLE_DUAL_VERIFICATION = "enable_dual_verification"
    ENABLE_HASH_ONLY = "enable_hash_only"
    ROLLBACK_TO_DUAL_VERIFICATION = "rollback_to_dual_verification"
    ROLLBACK_TO_BACKFILLED = "rollback_to_backfilled"


@dataclass(frozen=True, slots=True, repr=False)
class ReceiverCutoverStepResult:
    previous_state: str
    new_state: str
    legacy_verification_enabled: int
    legacy_authenticated_count: int
    hashed_authenticated_count: int
    store_count: int
    active_store_count: int
    active_device_count: int
    usable_credential_count: int
    transitioned_at: datetime
    runtime_action: RuntimeAction
    result_code: CutoverStepCode
    succeeded: bool = True

    def __repr__(self) -> str:
        return (
            "ReceiverCutoverStepResult("
            f"previous_state={self.previous_state!r}, new_state={self.new_state!r}, "
            f"legacy_verification_enabled={self.legacy_verification_enabled}, "
            f"legacy_authenticated_count={self.legacy_authenticated_count}, "
            f"hashed_authenticated_count={self.hashed_authenticated_count}, "
            f"store_count={self.store_count}, "
            f"active_store_count={self.active_store_count}, "
            f"active_device_count={self.active_device_count}, "
            f"usable_credential_count={self.usable_credential_count}, "
            f"transitioned_at={self.transitioned_at!r}, "
            f"runtime_action={self.runtime_action.value!r}, "
            f"result_code={self.result_code.value!r}, succeeded={self.succeeded})"
        )

    __str__ = __repr__


def _database_path(engine: Engine) -> Path | None:
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    return Path(database).resolve()


def _positive_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _validate_key_ring(hash_keys: Mapping[int, bytes]) -> Mapping[int, bytes]:
    if not isinstance(hash_keys, Mapping) or not 1 <= len(hash_keys) <= MAX_HASH_KEY_VERSIONS:
        raise InvalidCutoverConfigurationError()
    copied: dict[int, bytes] = {}
    for version, key in hash_keys.items():
        if (
            not _positive_integer(version)
            or not isinstance(key, bytes)
            or len(key) < MIN_HASH_KEY_BYTES
        ):
            raise InvalidCutoverConfigurationError()
        copied[version] = bytes(key)
    return MappingProxyType(copied)


class ReceiverCutoverRehearsal:
    """Coordinate approved adjacent transitions using live source counts."""

    def __init__(
        self,
        *,
        engine: Engine,
        ws_manager: object,
        runtime_authenticator: MigrationAwareReceiverRuntimeAuthenticator,
        hash_keys: Mapping[int, bytes],
        actor_user_id: int,
    ) -> None:
        if not isinstance(engine, Engine):
            raise InvalidCutoverConfigurationError()
        if _database_path(engine) == PROTECTED_DATABASE_PATH.resolve():
            raise ProtectedCutoverDatabaseError()
        if engine.dialect.name != "sqlite":
            raise InvalidCutoverConfigurationError()
        if not _positive_integer(actor_user_id):
            raise InvalidCutoverConfigurationError()
        if not callable(
            getattr(ws_manager, "get_active_receiver_transition_summary", None)
        ):
            raise InvalidCutoverConfigurationError()
        if not isinstance(
            runtime_authenticator, MigrationAwareReceiverRuntimeAuthenticator
        ):
            raise InvalidCutoverConfigurationError()

        validated_keys = _validate_key_ring(hash_keys)
        # Fail closed if independently injected components do not describe the
        # same isolated database and key ring. Values are compared only in
        # memory and are never rendered, logged, or persisted.
        if getattr(runtime_authenticator, "_engine", None) is not engine:
            raise InvalidCutoverConfigurationError()
        if getattr(runtime_authenticator, "_hash_keys", None) != dict(validated_keys):
            raise InvalidCutoverConfigurationError()

        self._engine = engine
        self._ws_manager = ws_manager
        self._runtime_authenticator = runtime_authenticator
        self._hash_keys = validated_keys
        self._actor_user_id = actor_user_id

    def __repr__(self) -> str:
        return (
            "ReceiverCutoverRehearsal("
            f"actor_user_id={self._actor_user_id}, "
            f"key_versions={tuple(sorted(self._hash_keys))!r}, "
            "database=<isolated>, key_material=<redacted>)"
        )

    __str__ = __repr__

    @property
    def runtime_authenticator(self) -> MigrationAwareReceiverRuntimeAuthenticator:
        """Return the exact explicitly validated adapter for isolated app injection."""

        return self._runtime_authenticator

    def connection_summary(
        self, *, captured_at: datetime | None = None
    ) -> ActiveReceiverConnectionSummary:
        capture_time = captured_at or datetime.now(timezone.utc)
        return self._ws_manager.get_active_receiver_transition_summary(
            now=capture_time
        )

    def _transition(
        self,
        *,
        expected_current_state: str,
        target_state: str,
        result_code: CutoverStepCode,
        now: datetime | None,
        summary_captured_at: datetime | None,
        step_hook: Callable[[str], None] | None,
    ) -> ReceiverCutoverStepResult:
        transition_time = now or datetime.now(timezone.utc)
        summary = self.connection_summary(
            captured_at=summary_captured_at or transition_time
        )
        transition = transition_receiver_migration_state(
            self._engine,
            expected_current_state=expected_current_state,
            target_state=target_state,
            actor_user_id=self._actor_user_id,
            hash_keys=self._hash_keys,
            active_connections=summary,
            now=transition_time,
            step_hook=step_hook,
        )
        return self._result(transition, summary, result_code)

    @staticmethod
    def _result(
        transition: MigrationTransitionResult,
        summary: ActiveReceiverConnectionSummary,
        result_code: CutoverStepCode,
    ) -> ReceiverCutoverStepResult:
        return ReceiverCutoverStepResult(
            previous_state=transition.previous_state,
            new_state=transition.new_state,
            legacy_verification_enabled=transition.legacy_verification_enabled,
            legacy_authenticated_count=summary.legacy_authenticated_count,
            hashed_authenticated_count=summary.hashed_authenticated_count,
            store_count=transition.store_count,
            active_store_count=transition.active_store_count,
            active_device_count=transition.active_device_count,
            usable_credential_count=transition.usable_credential_count,
            transitioned_at=transition.transitioned_at,
            runtime_action=transition.runtime_action,
            result_code=result_code,
        )

    def transition_to_dual_verify(
        self,
        *,
        now: datetime | None = None,
        summary_captured_at: datetime | None = None,
        step_hook: Callable[[str], None] | None = None,
    ) -> ReceiverCutoverStepResult:
        return self._transition(
            expected_current_state="backfilled",
            target_state="dual_verify",
            result_code=CutoverStepCode.ENABLE_DUAL_VERIFICATION,
            now=now,
            summary_captured_at=summary_captured_at,
            step_hook=step_hook,
        )

    def transition_to_hash_only(
        self,
        *,
        now: datetime | None = None,
        summary_captured_at: datetime | None = None,
        step_hook: Callable[[str], None] | None = None,
    ) -> ReceiverCutoverStepResult:
        return self._transition(
            expected_current_state="dual_verify",
            target_state="hash_only",
            result_code=CutoverStepCode.ENABLE_HASH_ONLY,
            now=now,
            summary_captured_at=summary_captured_at,
            step_hook=step_hook,
        )

    def rollback_to_dual_verify(
        self,
        *,
        now: datetime | None = None,
        summary_captured_at: datetime | None = None,
        step_hook: Callable[[str], None] | None = None,
    ) -> ReceiverCutoverStepResult:
        return self._transition(
            expected_current_state="hash_only",
            target_state="dual_verify",
            result_code=CutoverStepCode.ROLLBACK_TO_DUAL_VERIFICATION,
            now=now,
            summary_captured_at=summary_captured_at,
            step_hook=step_hook,
        )

    def rollback_to_backfilled(
        self,
        *,
        now: datetime | None = None,
        summary_captured_at: datetime | None = None,
        step_hook: Callable[[str], None] | None = None,
    ) -> ReceiverCutoverStepResult:
        return self._transition(
            expected_current_state="dual_verify",
            target_state="backfilled",
            result_code=CutoverStepCode.ROLLBACK_TO_BACKFILLED,
            now=now,
            summary_captured_at=summary_captured_at,
            step_hook=step_hook,
        )
