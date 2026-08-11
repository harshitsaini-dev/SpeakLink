"""The Receiver HMAC key must live somewhere a database copy cannot reach.

`docs/RECEIVER_HOSTING_KEY_STORAGE_ADR.md` already decided how: a DPAPI-protected
versioned key container, outside Git and outside SQLite, with only non-secret
key-version metadata in ordinary configuration. A key in `.env` and a key in
SQLite were both explicitly rejected - the second because it destroys the
separation between "database compromise" and "key compromise" that the whole
credential design rests on.

Nothing could issue a device credential until that container existed. This is
it.

Two things the ADR leaves open, and this module therefore does not pretend to
have settled (section 10): the DPAPI protection scope (per-user versus
per-machine) and the dedicated Windows service identity. CurrentUser scope is
implemented and tested because that is what the controlled pilot runs as.
LocalMachine is implemented but is only reachable by explicit choice, and no
test here claims anything about a service account's ACLs.

Most tests use an injected fake protector so they are deterministic everywhere.
The real DPAPI round trip is a Windows-only test, skipped elsewhere rather than
faked. No key material is ever printed.
"""

from __future__ import annotations

import json
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

from key_custody import (  # noqa: E402
    MIN_KEY_BYTES,
    DpapiProtector,
    FakeProtector,
    KeyContainerCorrupt,
    KeyContainerExists,
    KeyContainerMissing,
    KeyCustodyError,
    KeyCustodyUnavailable,
    KeyRing,
    ProtectionScope,
    create_key_container,
    load_key_ring,
    rotate_signing_key,
)


PROTECTED_DATABASE = BACKEND_ROOT / "speaklink_live.db"
ON_WINDOWS = sys.platform == "win32"


@pytest.fixture()
def container(tmp_path) -> Path:
    return tmp_path / "receiver-hmac-keys.bin"


@pytest.fixture()
def protector() -> FakeProtector:
    return FakeProtector()


# ---------------------------------------------------------------------------
# Creating a container
# ---------------------------------------------------------------------------
def test_a_new_container_holds_one_signing_key(container, protector):
    create_key_container(container, protector=protector)
    ring = load_key_ring(container, protector=protector)

    assert ring.active_version == 1
    assert ring.versions() == [1]
    assert len(ring.key(1)) >= MIN_KEY_BYTES


def test_the_key_never_appears_in_the_container_file(container, protector):
    create_key_container(container, protector=protector)
    ring = load_key_ring(container, protector=protector)

    raw = container.read_bytes()
    assert ring.key(1) not in raw, "the raw key is recoverable straight from the file"
    import base64

    assert base64.b64encode(ring.key(1)) not in raw
    assert base64.urlsafe_b64encode(ring.key(1)) not in raw


def test_the_container_is_not_readable_as_plain_json(container, protector):
    create_key_container(container, protector=protector)
    with pytest.raises(Exception):
        json.loads(container.read_bytes().decode("utf-8", errors="ignore"))


def test_an_existing_container_is_never_silently_overwritten(container, protector):
    create_key_container(container, protector=protector)
    original = container.read_bytes()

    with pytest.raises(KeyContainerExists):
        create_key_container(container, protector=protector)

    assert container.read_bytes() == original, "the existing key container was replaced"


def test_a_missing_container_is_an_explicit_refusal_not_a_new_key(container, protector):
    """Silently generating a key would orphan every credential already hashed
    with the missing one - the database would still hold verifiers nothing can
    check, and every Receiver would fail authentication for no visible reason."""
    with pytest.raises(KeyContainerMissing):
        load_key_ring(container, protector=protector)
    assert not container.exists(), "loading a missing container created one"


# ---------------------------------------------------------------------------
# Integrity
# ---------------------------------------------------------------------------
def test_a_truncated_container_is_rejected(container, protector):
    create_key_container(container, protector=protector)
    raw = container.read_bytes()
    container.write_bytes(raw[: len(raw) // 2])

    with pytest.raises(KeyContainerCorrupt):
        load_key_ring(container, protector=protector)


def test_a_tampered_container_is_rejected(container, protector):
    create_key_container(container, protector=protector)
    raw = bytearray(container.read_bytes())
    raw[-1] ^= 0xFF
    container.write_bytes(bytes(raw))

    with pytest.raises(KeyCustodyError):
        load_key_ring(container, protector=protector)


def test_an_empty_container_is_rejected(container, protector):
    container.write_bytes(b"")
    with pytest.raises(KeyContainerCorrupt):
        load_key_ring(container, protector=protector)


def test_a_container_with_a_foreign_header_is_rejected(container, protector):
    container.write_bytes(b"NOT-AN-SPEAKLINK-KEY-CONTAINER-AT-ALL")
    with pytest.raises(KeyContainerCorrupt):
        load_key_ring(container, protector=protector)


def test_a_container_protected_by_someone_else_cannot_be_read(container):
    """Standing in for a different DPAPI user or machine: the protector that
    cannot unprotect it must fail closed, not return nonsense."""
    create_key_container(container, protector=FakeProtector(identity="host-a"))
    with pytest.raises(KeyCustodyError):
        load_key_ring(container, protector=FakeProtector(identity="host-b"))


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------
def test_rotation_adds_a_new_signing_version(container, protector):
    create_key_container(container, protector=protector)
    rotate_signing_key(container, protector=protector)
    ring = load_key_ring(container, protector=protector)

    assert ring.active_version == 2
    assert ring.versions() == [1, 2]


def test_the_previous_key_remains_available_for_verification(container, protector):
    create_key_container(container, protector=protector)
    before = load_key_ring(container, protector=protector).key(1)
    rotate_signing_key(container, protector=protector)
    after = load_key_ring(container, protector=protector)

    assert after.key(1) == before, "rotation destroyed the key older credentials were hashed with"
    assert after.key(2) != before


def test_the_retired_key_can_no_longer_sign(container, protector):
    create_key_container(container, protector=protector)
    rotate_signing_key(container, protector=protector)
    ring = load_key_ring(container, protector=protector)

    signing_version, signing_key = ring.signing_key()
    assert signing_version == 2
    assert signing_key == ring.key(2)
    assert signing_key != ring.key(1)


def test_rotating_repeatedly_keeps_every_verification_key(container, protector):
    create_key_container(container, protector=protector)
    for _ in range(4):
        rotate_signing_key(container, protector=protector)
    ring = load_key_ring(container, protector=protector)

    assert ring.active_version == 5
    assert ring.versions() == [1, 2, 3, 4, 5]
    assert len({bytes(ring.key(version)) for version in ring.versions()}) == 5


def test_rotation_refuses_a_missing_container(container, protector):
    with pytest.raises(KeyContainerMissing):
        rotate_signing_key(container, protector=protector)


def test_an_unknown_key_version_is_refused(container, protector):
    create_key_container(container, protector=protector)
    ring = load_key_ring(container, protector=protector)
    with pytest.raises(KeyCustodyError):
        ring.key(99)


# ---------------------------------------------------------------------------
# Atomic replacement
# ---------------------------------------------------------------------------
def test_a_failed_rotation_leaves_the_previous_container_intact(container, protector, monkeypatch):
    """If the replace step dies, the operator must still have a working key -
    a half-written container means every Receiver credential is unverifiable."""
    create_key_container(container, protector=protector)
    original = container.read_bytes()

    import key_custody

    def explode(*args, **kwargs):
        raise OSError("simulated failure during replace")

    monkeypatch.setattr(key_custody.os, "replace", explode)
    with pytest.raises(OSError):
        rotate_signing_key(container, protector=protector)

    assert container.read_bytes() == original
    ring = load_key_ring(container, protector=protector)
    assert ring.active_version == 1


def test_no_temporary_files_are_left_behind(container, protector):
    create_key_container(container, protector=protector)
    rotate_signing_key(container, protector=protector)
    leftovers = [p.name for p in container.parent.iterdir() if p.name != container.name]
    assert leftovers == [], f"temporary key files were left on disk: {leftovers}"


# ---------------------------------------------------------------------------
# Nothing leaks
# ---------------------------------------------------------------------------
def test_the_key_ring_hides_its_material_when_displayed(container, protector):
    create_key_container(container, protector=protector)
    ring = load_key_ring(container, protector=protector)
    key = ring.key(1)

    for rendering in (repr(ring), str(ring)):
        assert key.hex() not in rendering
        assert str(key) not in rendering


def test_errors_never_carry_key_material(container, protector):
    create_key_container(container, protector=protector)
    ring = load_key_ring(container, protector=protector)
    key = ring.key(1)
    try:
        ring.key(99)
    except KeyCustodyError as refusal:
        assert key.hex() not in str(refusal)
    else:
        pytest.fail("an unknown version was accepted")


def test_loading_prints_nothing(container, protector, capsys):
    create_key_container(container, protector=protector)
    ring = load_key_ring(container, protector=protector)
    captured = capsys.readouterr()
    assert ring.key(1).hex() not in captured.out
    assert ring.key(1).hex() not in captured.err


def test_the_key_is_not_placed_in_the_process_environment(container, protector):
    create_key_container(container, protector=protector)
    ring = load_key_ring(container, protector=protector)
    joined = " ".join(f"{name}={value}" for name, value in os.environ.items())
    assert ring.key(1).hex() not in joined


# ---------------------------------------------------------------------------
# Fail closed
# ---------------------------------------------------------------------------
def test_an_unavailable_protector_fails_closed(container):
    class BrokenProtector:
        scope = ProtectionScope.CURRENT_USER

        def protect(self, payload: bytes) -> bytes:
            raise KeyCustodyUnavailable("protection is unavailable on this host")

        def unprotect(self, payload: bytes) -> bytes:
            raise KeyCustodyUnavailable("protection is unavailable on this host")

    with pytest.raises(KeyCustodyUnavailable):
        create_key_container(container, protector=BrokenProtector())
    assert not container.exists()


def test_scopes_are_explicit_and_named():
    """Per-user versus per-machine is an open decision in the ADR, so it must be
    chosen deliberately rather than defaulted into."""
    assert ProtectionScope.CURRENT_USER.value != ProtectionScope.LOCAL_MACHINE.value
    assert {scope.value for scope in ProtectionScope} == {"current_user", "local_machine"}


# ---------------------------------------------------------------------------
# Real DPAPI, only where it exists
# ---------------------------------------------------------------------------
@pytest.mark.skipif(not ON_WINDOWS, reason="DPAPI is a Windows API")
def test_real_dpapi_round_trips_under_the_current_user(container):
    real = DpapiProtector(scope=ProtectionScope.CURRENT_USER)
    create_key_container(container, protector=real)
    ring = load_key_ring(container, protector=real)

    assert ring.active_version == 1
    assert len(ring.key(1)) >= MIN_KEY_BYTES
    assert ring.key(1) not in container.read_bytes()


@pytest.mark.skipif(not ON_WINDOWS, reason="DPAPI is a Windows API")
def test_real_dpapi_survives_rotation(container):
    real = DpapiProtector(scope=ProtectionScope.CURRENT_USER)
    create_key_container(container, protector=real)
    first = load_key_ring(container, protector=real).key(1)
    rotate_signing_key(container, protector=real)
    ring = load_key_ring(container, protector=real)

    assert ring.active_version == 2
    assert ring.key(1) == first


@pytest.mark.skipif(not ON_WINDOWS, reason="DPAPI is a Windows API")
def test_real_dpapi_rejects_a_tampered_container(container):
    real = DpapiProtector(scope=ProtectionScope.CURRENT_USER)
    create_key_container(container, protector=real)
    raw = bytearray(container.read_bytes())
    raw[-1] ^= 0xFF
    container.write_bytes(bytes(raw))

    with pytest.raises(KeyCustodyError):
        load_key_ring(container, protector=real)


def test_a_local_machine_protector_can_be_constructed_but_is_not_claimed_tested():
    """LocalMachine is implemented for a future service identity. Nothing here
    asserts it works under a service account, because nothing here runs as one.
    That validation is a production gate, not a unit test."""
    protector = DpapiProtector(scope=ProtectionScope.LOCAL_MACHINE)
    assert protector.scope is ProtectionScope.LOCAL_MACHINE


# ---------------------------------------------------------------------------
# The protected database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_untouched(container, protector):
    def metadata():
        if not PROTECTED_DATABASE.exists():
            return None
        stat = PROTECTED_DATABASE.stat()
        return stat.st_size, stat.st_mtime_ns

    before = metadata()
    create_key_container(container, protector=protector)
    rotate_signing_key(container, protector=protector)
    assert metadata() == before
    for sidecar in ("-wal", "-shm"):
        assert not Path(str(PROTECTED_DATABASE) + sidecar).exists()


def test_the_container_lives_outside_the_repository_by_default():
    from key_custody import default_container_path

    path = default_container_path()
    assert REPOSITORY_ROOT not in path.parents, (
        "the key container defaults to a path inside the repository, where it "
        "could be committed"
    )
