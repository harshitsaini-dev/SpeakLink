"""Pure tests for the process-local Receiver connection inventory."""

from __future__ import annotations

import ast
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

import receiver_connection_inventory as inventory_module
from receiver_connection_inventory import (
    ActiveReceiverConnectionInventory,
    AuthenticatedReceiverConnection,
    ConnectionAuthenticationSource,
    ConnectionInventoryCapacityError,
    ConnectionRegistrationConflictError,
    InvalidConnectionRecordError,
    InvalidInventoryCapacityError,
    InvalidSnapshotTimeError,
    RegistrationDisposition,
)


UTC_NOW = datetime(2026, 7, 24, 8, 30, tzinfo=timezone.utc)


def _record(
    connection_id: str = "connection-001",
    *,
    store_id: int = 1,
    device_id: int | None = None,
    credential_id: int | None = None,
    source: ConnectionAuthenticationSource = ConnectionAuthenticationSource.LEGACY_STORE_TOKEN,
    authenticated_at: datetime = UTC_NOW,
) -> AuthenticatedReceiverConnection:
    return AuthenticatedReceiverConnection(
        connection_id=connection_id,
        store_id=store_id,
        device_id=device_id,
        credential_id=credential_id,
        authentication_source=source,
        authenticated_at=authenticated_at,
    )


def _hashed_record(
    connection_id: str = "hashed-001",
    *,
    store_id: int = 1,
    device_id: int = 11,
    credential_id: int = 21,
    authenticated_at: datetime = UTC_NOW,
) -> AuthenticatedReceiverConnection:
    return _record(
        connection_id,
        store_id=store_id,
        device_id=device_id,
        credential_id=credential_id,
        source=ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL,
        authenticated_at=authenticated_at,
    )


def test_fresh_inventory_is_empty_and_restart_is_process_local():
    first = ActiveReceiverConnectionInventory()
    first.register(_record())
    restarted = ActiveReceiverConnectionInventory()

    snapshot = restarted.snapshot(captured_at=UTC_NOW)
    assert snapshot.total_active_count == 0
    assert snapshot.legacy_authenticated_count == 0
    assert snapshot.hashed_authenticated_count == 0
    assert snapshot.store_counts == ()
    assert snapshot.records == ()
    assert snapshot.generation == 0


def test_legacy_registration_allows_absent_or_complete_canonical_identity():
    inventory = ActiveReceiverConnectionInventory()
    plain = inventory.register(_record())
    canonical = inventory.register(
        _record("legacy-canonical", device_id=12, credential_id=22)
    )

    assert plain.disposition is RegistrationDisposition.REGISTERED
    assert plain.record.device_id is None
    assert canonical.record.device_id == 12
    assert canonical.record.credential_id == 22
    assert canonical.record.authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN


@pytest.mark.parametrize(
    ("device_id", "credential_id"),
    [(None, None), (11, None), (None, 21)],
)
def test_hashed_registration_requires_device_and_credential_ids(device_id, credential_id):
    with pytest.raises(InvalidConnectionRecordError):
        _record(
            source=ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL,
            device_id=device_id,
            credential_id=credential_id,
        )


def test_legacy_canonical_identity_requires_both_optional_ids():
    with pytest.raises(InvalidConnectionRecordError):
        _record(device_id=11)
    with pytest.raises(InvalidConnectionRecordError):
        _record(credential_id=21)


@pytest.mark.parametrize("field", ["store_id", "device_id", "credential_id"])
@pytest.mark.parametrize("value", [True, False, 0, -1, 1.5, "1"])
def test_identity_ids_must_be_positive_non_boolean_integers(field, value):
    arguments = {
        "store_id": 1,
        "device_id": 11,
        "credential_id": 21,
    }
    arguments[field] = value
    with pytest.raises(InvalidConnectionRecordError):
        _hashed_record(**arguments)


@pytest.mark.parametrize(
    "authenticated_at",
    [
        datetime(2026, 7, 24, 8, 30),
        datetime(2026, 7, 24, 14, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        "2026-07-24T08:30:00Z",
    ],
)
def test_authenticated_at_must_be_timezone_aware_utc(authenticated_at):
    with pytest.raises(InvalidConnectionRecordError):
        _record(authenticated_at=authenticated_at)


@pytest.mark.parametrize(
    "connection_id",
    ["", " leading", "trailing ", "contains space", "line\nbreak", "x" * 65,
     "authorization-header", "Bearer-secret", "password-value", "echocast_rcv_abc"],
)
def test_connection_id_rejects_malformed_oversized_control_and_secret_like_values(connection_id):
    with pytest.raises(InvalidConnectionRecordError):
        _record(connection_id)


def test_identical_duplicate_is_idempotent_and_does_not_increment_generation():
    inventory = ActiveReceiverConnectionInventory()
    record = _record()
    first = inventory.register(record)
    duplicate = inventory.register(record)

    assert first.disposition is RegistrationDisposition.REGISTERED
    assert duplicate.disposition is RegistrationDisposition.UNCHANGED
    assert duplicate.record is first.record
    assert duplicate.generation == first.generation == 1
    assert inventory.snapshot(captured_at=UTC_NOW).total_active_count == 1


@pytest.mark.parametrize(
    "replacement",
    [
        _record(store_id=2),
        _record(device_id=11, credential_id=21),
        _hashed_record("connection-001"),
        _record(authenticated_at=UTC_NOW + timedelta(seconds=1)),
    ],
)
def test_conflicting_duplicate_fails_without_replacing_original(replacement):
    inventory = ActiveReceiverConnectionInventory()
    original = _record()
    inventory.register(original)

    with pytest.raises(ConnectionRegistrationConflictError):
        inventory.register(replacement)

    assert inventory.get(original.connection_id) is original
    assert inventory.snapshot(captured_at=UTC_NOW).generation == 1


def test_remove_is_exact_idempotent_and_updates_counts_and_generation():
    inventory = ActiveReceiverConnectionInventory()
    legacy = _record()
    hashed = _hashed_record()
    inventory.register(legacy)
    inventory.register(hashed)

    removed = inventory.remove(legacy.connection_id)
    missing = inventory.remove(legacy.connection_id)
    snapshot = inventory.snapshot(captured_at=UTC_NOW)

    assert removed.removed is True
    assert removed.record is legacy
    assert removed.generation == 3
    assert missing.removed is False
    assert missing.record is None
    assert missing.generation == 3
    assert snapshot.total_active_count == 1
    assert snapshot.legacy_authenticated_count == 0
    assert snapshot.hashed_authenticated_count == 1


def test_snapshot_is_deterministic_reconciled_and_immutable_over_time():
    inventory = ActiveReceiverConnectionInventory()
    inventory.register(_hashed_record("z-last", store_id=2, device_id=12, credential_id=22))
    inventory.register(_record("a-first", store_id=1))
    inventory.register(_record("m-middle", store_id=2))
    before = inventory.snapshot(captured_at=UTC_NOW)
    inventory.remove("m-middle")
    after = inventory.snapshot(captured_at=UTC_NOW + timedelta(seconds=1))

    assert [record.connection_id for record in before.records] == ["a-first", "m-middle", "z-last"]
    assert before.total_active_count == before.legacy_authenticated_count + before.hashed_authenticated_count == 3
    assert sum(item.connection_count for item in before.store_counts) == before.total_active_count
    assert [(item.store_id, item.connection_count) for item in before.store_counts] == [(1, 1), (2, 2)]
    assert before.total_active_count == 3
    assert after.total_active_count == 2
    with pytest.raises(FrozenInstanceError):
        before.total_active_count = 99


@pytest.mark.parametrize(
    "captured_at",
    [
        datetime(2026, 7, 24, 8, 30),
        datetime(2026, 7, 24, 14, 0, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        "not-a-time",
    ],
)
def test_snapshot_time_must_be_timezone_aware_utc(captured_at):
    with pytest.raises(InvalidSnapshotTimeError):
        ActiveReceiverConnectionInventory().snapshot(captured_at=captured_at)


def test_transition_summary_adapter_uses_one_atomic_snapshot():
    @dataclass(frozen=True)
    class Summary:
        legacy_authenticated_count: int
        hashed_authenticated_count: int
        captured_at: datetime

    inventory = ActiveReceiverConnectionInventory()
    inventory.register(_record())
    inventory.register(_hashed_record())

    summary = inventory.build_transition_summary(Summary, captured_at=UTC_NOW)

    assert summary == Summary(1, 1, UTC_NOW)


def test_records_and_snapshots_do_not_model_receiver_health_axes():
    forbidden = {
        "readiness", "ready", "playback", "speaker", "acoustic", "audio_receiving",
        "playback_confirmed", "speaker_verified",
    }
    record_fields = set(AuthenticatedReceiverConnection.__dataclass_fields__)
    snapshot_fields = set(type(ActiveReceiverConnectionInventory().snapshot()).__dataclass_fields__)
    assert record_fields.isdisjoint(forbidden)
    assert snapshot_fields.isdisjoint(forbidden)


def test_capacity_is_enforced_exactly():
    inventory = ActiveReceiverConnectionInventory(max_connections=2)
    inventory.register(_record("one"))
    inventory.register(_record("two", store_id=2))

    with pytest.raises(ConnectionInventoryCapacityError):
        inventory.register(_record("three", store_id=3))
    assert inventory.snapshot(captured_at=UTC_NOW).total_active_count == 2


@pytest.mark.parametrize("capacity", [True, False, 0, -1, 4097, 1.5, "256"])
def test_invalid_capacity_is_rejected(capacity):
    with pytest.raises(InvalidInventoryCapacityError):
        ActiveReceiverConnectionInventory(max_connections=capacity)


def test_concurrent_unique_registrations_cannot_exceed_capacity():
    capacity = 32
    attempts = 96
    inventory = ActiveReceiverConnectionInventory(max_connections=capacity)
    barrier = Barrier(attempts)

    def register(index: int) -> str:
        barrier.wait()
        try:
            inventory.register(_record(f"race-{index:03d}", store_id=(index % 40) + 1))
            return "registered"
        except ConnectionInventoryCapacityError:
            return "full"

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = list(pool.map(register, range(attempts)))

    snapshot = inventory.snapshot(captured_at=UTC_NOW)
    assert outcomes.count("registered") == capacity
    assert outcomes.count("full") == attempts - capacity
    assert snapshot.total_active_count == capacity
    assert snapshot.generation == capacity


def test_concurrent_identical_registration_creates_one_record():
    attempts = 40
    inventory = ActiveReceiverConnectionInventory()
    record = _record()
    barrier = Barrier(attempts)

    def register(_index: int) -> RegistrationDisposition:
        barrier.wait()
        return inventory.register(record).disposition

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = list(pool.map(register, range(attempts)))

    assert outcomes.count(RegistrationDisposition.REGISTERED) == 1
    assert outcomes.count(RegistrationDisposition.UNCHANGED) == attempts - 1
    assert inventory.snapshot(captured_at=UTC_NOW).generation == 1


def test_concurrent_conflicts_keep_one_accepted_immutable_record():
    attempts = 30
    inventory = ActiveReceiverConnectionInventory()
    barrier = Barrier(attempts)

    def register(index: int) -> str:
        barrier.wait()
        try:
            inventory.register(_record("shared-id", store_id=index + 1))
            return "registered"
        except ConnectionRegistrationConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = list(pool.map(register, range(attempts)))

    assert outcomes.count("registered") == 1
    assert outcomes.count("conflict") == attempts - 1
    assert inventory.snapshot(captured_at=UTC_NOW).generation == 1


def test_concurrent_registration_removal_and_snapshots_preserve_invariants():
    inventory = ActiveReceiverConnectionInventory(max_connections=128)
    for index in range(40):
        inventory.register(_record(f"base-{index:03d}", store_id=index + 1))

    def mutate(index: int):
        if index % 3 == 0:
            inventory.remove(f"base-{index:03d}")
        elif index % 3 == 1:
            inventory.register(_hashed_record(
                f"extra-{index:03d}",
                store_id=(index % 40) + 1,
                device_id=100 + index,
                credential_id=200 + index,
            ))
        snapshot = inventory.snapshot(captured_at=UTC_NOW)
        return (
            snapshot.total_active_count,
            snapshot.legacy_authenticated_count + snapshot.hashed_authenticated_count,
            sum(item.connection_count for item in snapshot.store_counts),
        )

    with ThreadPoolExecutor(max_workers=24) as pool:
        observations = list(pool.map(mutate, range(40)))

    assert all(total == by_source == by_store for total, by_source, by_store in observations)
    final = inventory.snapshot(captured_at=UTC_NOW)
    assert final.total_active_count == final.legacy_authenticated_count + final.hashed_authenticated_count


def test_generation_changes_only_for_real_mutations():
    inventory = ActiveReceiverConnectionInventory()
    record = _record()
    assert inventory.generation == 0
    inventory.register(record)
    assert inventory.generation == 1
    inventory.register(record)
    assert inventory.generation == 1
    inventory.remove("missing")
    assert inventory.generation == 1
    inventory.remove(record.connection_id)
    assert inventory.generation == 2


def test_representative_forty_store_load_is_bounded_and_deterministic():
    inventory = ActiveReceiverConnectionInventory(max_connections=256)
    for store_id in range(1, 41):
        inventory.register(_record(f"store-{store_id:02d}-legacy", store_id=store_id))
        if store_id % 5 == 0:
            inventory.register(_hashed_record(
                f"store-{store_id:02d}-hashed",
                store_id=store_id,
                device_id=1000 + store_id,
                credential_id=2000 + store_id,
            ))

    snapshot = inventory.snapshot(captured_at=UTC_NOW)
    assert snapshot.total_active_count == 48
    assert snapshot.legacy_authenticated_count == 40
    assert snapshot.hashed_authenticated_count == 8
    assert tuple(record.connection_id for record in snapshot.records) == tuple(
        sorted(record.connection_id for record in snapshot.records)
    )


def test_types_are_immutable_and_representations_contain_no_secret_material(capsys, caplog):
    secret_values = (
        "super-secret-value",
        "Bearer abc123",
        "authorization-value",
        "hmac-key-value",
        "password-value",
    )
    inventory = ActiveReceiverConnectionInventory()
    record = _hashed_record()
    registration = inventory.register(record)
    snapshot = inventory.snapshot(captured_at=UTC_NOW)
    removal = inventory.remove(record.connection_id)

    for value in (record, registration, snapshot, removal):
        rendered = repr(value) + str(value)
        assert all(secret not in rendered for secret in secret_values)
        with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
            value.unexpected = "change"

    for operation in (
        lambda: _record("Bearer-secret"),
        lambda: ActiveReceiverConnectionInventory(max_connections=0),
        lambda: inventory.snapshot(captured_at=datetime(2026, 7, 24)),
    ):
        with pytest.raises(Exception) as captured:
            operation()
        assert all(secret not in str(captured.value) for secret in secret_values)

    output = capsys.readouterr()
    rendered_logs = " ".join(record.getMessage() for record in caplog.records)
    assert output.out == output.err == ""
    assert rendered_logs == ""


def test_module_is_pure_and_has_no_uncontrolled_global_inventory():
    tree = ast.parse(inspect.getsource(inventory_module))
    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert imported_roots.isdisjoint(
        {"server", "fastapi", "sqlalchemy", "uvicorn", "starlette", "websockets"}
    )
    assert not any(
        isinstance(value, ActiveReceiverConnectionInventory)
        for value in vars(inventory_module).values()
    )
