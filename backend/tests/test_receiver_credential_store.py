"""Keeping one Receiver Device credential on the Store computer that owns it.

The Agent enrols once and must reconnect for months afterwards without ever
seeing the code again. So the credential has to survive on disk - which makes
*how* it sits there the whole problem.

Three things are being defended, and each has a way of going wrong quietly:

* **The credential must not be readable.** Windows DPAPI seals it to the logged-on
  user, so a copied file is worthless on another machine or under another
  account. A test asserts the raw value is absent from the bytes, because a
  format change could reintroduce it without any error appearing.

* **A half-written file must never replace a working one.** The Agent would then
  be unable to authenticate and unable to re-enrol, needing a physical visit to a
  Store. Writes go to a temporary file and are moved into place in one step, and
  a test makes that move fail to prove the previous credential is still there.

* **It must never be confused with the backend's HMAC key container.** They are
  different secrets with different lifetimes and different owners. Both use
  DPAPI, so ordinary care is not enough: this store passes distinct DPAPI
  entropy, which makes crossing them impossible rather than merely unlikely.
  Two tests prove neither file can be opened by the other's code path.

The real DPAPI tests are skipped off Windows and nowhere else. If they are
skipped on this machine, nothing here has proven anything about DPAPI.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.receiver_credential_store import (  # noqa: E402
    CredentialCorrupt,
    CredentialExists,
    CredentialMissing,
    CredentialStoreError,
    CredentialStoreUnavailable,
    DeviceCredentialProtector,
    FakeCredentialProtector,
    default_credential_path,
    delete_credential,
    load_credential,
    replace_credential,
    save_credential,
)


CREDENTIAL = "speaklink_rcv_v1.11111111-2222-4333-8444-555555555555." + "s" * 43
ROTATED = "speaklink_rcv_v1.11111111-2222-4333-8444-555555555555." + "r" * 43
DEVICE_PUBLIC_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)


@pytest.fixture()
def protector() -> FakeCredentialProtector:
    return FakeCredentialProtector("this-computer")


@pytest.fixture()
def path(tmp_path: Path) -> Path:
    return tmp_path / "receiver" / "device-credential.bin"


def _save(path: Path, protector, credential: str = CREDENTIAL) -> None:
    save_credential(
        path,
        credential=credential,
        device_public_id=DEVICE_PUBLIC_ID,
        store_id=7,
        backend_origin="https://hq.example.internal",
        protector=protector,
        now=NOW,
    )


# ---------------------------------------------------------------------------
# It survives a round trip
# ---------------------------------------------------------------------------
def test_a_saved_credential_can_be_loaded_back(path: Path, protector):
    _save(path, protector)
    record = load_credential(path, protector=protector)
    assert record.credential() == CREDENTIAL
    assert record.device_public_id == DEVICE_PUBLIC_ID
    assert record.store_id == 7
    assert record.backend_origin == "https://hq.example.internal"


def test_the_metadata_survives_but_stays_non_secret(path: Path, protector):
    """The Agent shows the operator which Device and Store it is. None of that
    is a secret, and none of it is enough to authenticate."""
    _save(path, protector)
    record = load_credential(path, protector=protector)
    assert record.created_at == NOW
    assert record.updated_at == NOW


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows API")
def test_real_windows_dpapi_round_trip(path: Path):
    """Not a fake. If this is skipped, DPAPI has been proven nothing about."""
    real = DeviceCredentialProtector()
    _save(path, real)
    assert load_credential(path, protector=real).credential() == CREDENTIAL


# ---------------------------------------------------------------------------
# The credential is not sitting in the file
# ---------------------------------------------------------------------------
def test_the_raw_credential_is_absent_from_the_file_bytes(path: Path, protector):
    _save(path, protector)
    raw = path.read_bytes()
    assert CREDENTIAL.encode() not in raw
    assert b"s" * 43 not in raw, "the secret half is in the file in the clear"


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows API")
def test_the_raw_credential_is_absent_under_real_dpapi(path: Path):
    _save(path, DeviceCredentialProtector())
    assert CREDENTIAL.encode() not in path.read_bytes()


def test_a_different_identity_cannot_open_it(path: Path, protector):
    """Standing in for another Windows account: DPAPI CURRENT_USER refuses, and
    the store must turn that into a named error rather than a crash."""
    _save(path, protector)
    with pytest.raises(CredentialStoreError):
        load_credential(path, protector=FakeCredentialProtector("another-computer"))


# ---------------------------------------------------------------------------
# Damage is detected, not acted upon
# ---------------------------------------------------------------------------
def test_a_missing_file_is_a_named_error(path: Path, protector):
    with pytest.raises(CredentialMissing):
        load_credential(path, protector=protector)


def test_a_truncated_file_is_rejected(path: Path, protector):
    _save(path, protector)
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(CredentialStoreError):
        load_credential(path, protector=protector)


def test_a_foreign_file_is_rejected(path: Path, protector):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"this is somebody else's file entirely")
    with pytest.raises(CredentialCorrupt):
        load_credential(path, protector=protector)


def test_a_tampered_body_is_rejected(path: Path, protector):
    """Flip one byte of the sealed payload. Nothing may be returned."""
    _save(path, protector)
    raw = bytearray(path.read_bytes())
    raw[-1] ^= 0xFF
    path.write_bytes(bytes(raw))
    with pytest.raises(CredentialStoreError):
        load_credential(path, protector=protector)


def test_a_credential_that_is_not_one_is_refused_on_save(path: Path, protector):
    """The format is checked before writing, so a bad value cannot be stored and
    then fail confusingly months later at 6am in a Store."""
    with pytest.raises(CredentialStoreError):
        save_credential(
            path, credential="not-a-credential", device_public_id=DEVICE_PUBLIC_ID,
            store_id=7, backend_origin="https://hq.example.internal",
            protector=protector, now=NOW,
        )
    assert not path.exists()


# ---------------------------------------------------------------------------
# Overwriting is deliberate
# ---------------------------------------------------------------------------
def test_saving_over_an_existing_credential_is_refused(path: Path, protector):
    """Enrolling twice by accident must not silently discard the credential this
    computer is currently authenticating with."""
    _save(path, protector)
    with pytest.raises(CredentialExists):
        _save(path, protector, credential=ROTATED)
    assert load_credential(path, protector=protector).credential() == CREDENTIAL


def test_explicit_replacement_succeeds(path: Path, protector):
    _save(path, protector)
    replace_credential(
        path, credential=ROTATED, protector=protector,
        now=datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc),
    )
    record = load_credential(path, protector=protector)
    assert record.credential() == ROTATED
    assert record.updated_at > record.created_at


def test_replacement_keeps_the_device_identity(path: Path, protector):
    """Rotation changes the secret, not which Device this computer is."""
    _save(path, protector)
    replace_credential(path, credential=ROTATED, protector=protector, now=NOW)
    record = load_credential(path, protector=protector)
    assert record.device_public_id == DEVICE_PUBLIC_ID
    assert record.store_id == 7


def test_replacing_a_credential_that_is_not_there_is_refused(path: Path, protector):
    with pytest.raises(CredentialMissing):
        replace_credential(path, credential=ROTATED, protector=protector, now=NOW)


def test_a_failed_replace_leaves_the_old_credential_working(path: Path, protector, monkeypatch):
    """The failure that would otherwise need a van to a Store: the rotation
    breaks halfway and the computer can neither authenticate nor re-enrol."""
    _save(path, protector)

    def refuse(*args, **kwargs):
        raise OSError("the disk said no")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(CredentialStoreError):
        replace_credential(path, credential=ROTATED, protector=protector, now=NOW)
    monkeypatch.undo()

    assert load_credential(path, protector=protector).credential() == CREDENTIAL


def test_a_failed_write_leaves_no_temporary_file_behind(path: Path, protector, monkeypatch):
    _save(path, protector)
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
    with pytest.raises(CredentialStoreError):
        replace_credential(path, credential=ROTATED, protector=protector, now=NOW)
    monkeypatch.undo()
    leftovers = [entry.name for entry in path.parent.iterdir() if entry.name != path.name]
    assert leftovers == [], f"temporary files were left behind: {leftovers}"


# ---------------------------------------------------------------------------
# Nothing prints it
# ---------------------------------------------------------------------------
def test_the_record_never_renders_the_credential(path: Path, protector):
    _save(path, protector)
    record = load_credential(path, protector=protector)
    for rendering in (repr(record), str(record), f"{record}"):
        assert CREDENTIAL not in rendering
        assert "s" * 43 not in rendering
    assert DEVICE_PUBLIC_ID in repr(record), "the safe identity should still be visible"


def test_errors_never_carry_the_credential(path: Path, protector):
    _save(path, protector)
    with pytest.raises(CredentialExists) as refusal:
        _save(path, protector, credential=ROTATED)
    assert ROTATED not in str(refusal.value)

    with pytest.raises(CredentialStoreError) as failure:
        save_credential(
            path.parent / "other.bin", credential="bad", device_public_id=DEVICE_PUBLIC_ID,
            store_id=7, backend_origin="x", protector=protector, now=NOW,
        )
    assert "bad" not in str(failure.value).replace("bad credential", "")


def test_the_protector_never_renders_anything_useful():
    for rendering in (repr(FakeCredentialProtector("x")), repr(DeviceCredentialProtector())):
        assert "entropy" not in rendering.lower() or "redacted" in rendering.lower()


# ---------------------------------------------------------------------------
# It cannot be crossed with the backend key container
# ---------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows API")
def test_the_backend_key_container_cannot_be_opened_as_a_credential(tmp_path: Path):
    """Both are DPAPI files owned by the same person on the same machine. The
    distinct entropy is what makes mixing them impossible instead of unlikely."""
    from key_custody import DpapiProtector, ProtectionScope, create_key_container

    container = tmp_path / "receiver-hmac-keys.bin"
    create_key_container(container, protector=DpapiProtector(ProtectionScope.CURRENT_USER))
    with pytest.raises(CredentialStoreError):
        load_credential(container, protector=DeviceCredentialProtector())


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is a Windows API")
def test_a_credential_file_cannot_be_opened_as_a_key_container(path: Path):
    from key_custody import DpapiProtector, KeyCustodyError, ProtectionScope, load_key_ring

    _save(path, DeviceCredentialProtector())
    with pytest.raises(KeyCustodyError):
        load_key_ring(path, protector=DpapiProtector(ProtectionScope.CURRENT_USER))


def test_the_two_files_do_not_share_a_magic_header(path: Path, protector):
    from key_custody import MAGIC as KEY_MAGIC

    _save(path, protector)
    assert not path.read_bytes().startswith(KEY_MAGIC)


# ---------------------------------------------------------------------------
# Where it lives
# ---------------------------------------------------------------------------
def test_the_default_path_is_outside_the_repository():
    """A credential inside the checkout is one `git add` from being published."""
    default = default_credential_path()
    assert REPOSITORY_ROOT not in default.parents
    assert default != REPOSITORY_ROOT


def test_the_default_path_is_the_agreed_one(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_credential_path() == Path(
        r"C:\Users\someone\AppData\Local\SpeakLink\receiver\device-credential.bin"
    )


def test_the_default_path_is_not_the_key_container_path(monkeypatch):
    from key_custody import default_container_path

    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_credential_path() != default_container_path()


# ---------------------------------------------------------------------------
# Removal is deliberate too
# ---------------------------------------------------------------------------
def test_deleting_a_credential_removes_it(path: Path, protector):
    _save(path, protector)
    delete_credential(path)
    assert not path.exists()
    with pytest.raises(CredentialMissing):
        load_credential(path, protector=protector)


def test_deleting_a_credential_that_is_not_there_is_a_named_error(path: Path):
    with pytest.raises(CredentialMissing):
        delete_credential(path)


# ---------------------------------------------------------------------------
# The protected pilot database is never involved
# ---------------------------------------------------------------------------
def test_the_protected_database_is_never_touched(path: Path, protector):
    protected = REPOSITORY_ROOT / "backend" / "speaklink_live.db"
    before = protected.stat().st_mtime_ns if protected.exists() else None
    _save(path, protector)
    load_credential(path, protector=protector)
    after = protected.stat().st_mtime_ns if protected.exists() else None
    assert before == after


def test_the_module_imports_no_database_machinery():
    """The Agent runs on a Store computer. It has no database, and importing one
    would drag SQLAlchemy and the backend's engine onto a till.

    Read as imports rather than as text: the module's own docstring explains why
    SQLAlchemy is absent, and a substring search would flag that explanation.
    """
    import ast

    source = (REPOSITORY_ROOT / "tools" / "receiver_credential_store.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"sqlalchemy", "fastapi", "db", "models", "schemas", "server", "key_custody"}
    assert not (imported & forbidden), f"the Agent must not import {imported & forbidden}"


def test_a_really_issued_credential_is_accepted(path: Path, protector):
    """The store restates the credential format instead of importing it, so that
    a Store computer needs no part of the backend. This is what stops the two
    definitions drifting apart in silence."""
    from receiver_credentials import generate_receiver_credential

    for _ in range(20):
        issued = generate_receiver_credential()
        target = path.parent / f"{issued.public_id}.bin"
        save_credential(
            target, credential=issued.raw_token, device_public_id=issued.public_id,
            store_id=7, backend_origin="https://hq.example.internal",
            protector=protector, now=NOW,
        )
        assert load_credential(target, protector=protector).credential() == issued.raw_token


def test_a_legacy_store_token_is_not_accepted_as_a_device_credential(path: Path, protector):
    """The shared per-Store token is what Devices exist to replace. Storing one
    here would quietly turn the Agent back into the thing being retired."""
    with pytest.raises(CredentialStoreError):
        save_credential(
            path, credential="a" * 32, device_public_id=DEVICE_PUBLIC_ID, store_id=7,
            backend_origin="https://hq.example.internal", protector=protector, now=NOW,
        )
    assert not path.exists()


def test_the_stored_payload_is_not_plain_json(path: Path, protector):
    """A JSON file with a "credential" key would be readable by anything that
    opened it, which is the failure this whole module exists to prevent."""
    _save(path, protector)
    with pytest.raises(Exception):
        json.loads(path.read_text(encoding="utf-8", errors="replace"))
