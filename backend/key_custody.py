"""DPAPI-protected custody for the versioned Receiver HMAC key ring.

`docs/RECEIVER_HOSTING_KEY_STORAGE_ADR.md` chose this shape and rejected the
alternatives explicitly: a key in `.env` fails secret protection entirely if the
host or a backup is exposed, and a key in SQLite destroys the separation between
"database compromise" and "key compromise" that the whole credential design
rests on. So the key lives in its own file, protected by Windows DPAPI, outside
Git and outside the database, and only non-secret version metadata belongs in
ordinary configuration.

Two decisions the ADR deliberately left open (section 10) are left open here
too. The DPAPI **protection scope** - per-user or per-machine - is an explicit
argument with no default that quietly picks one. The **dedicated Windows service
identity** does not exist yet, so nothing in this module or its tests claims
anything about how the container behaves under a service account; that is a
production gate, not something a unit test can assert.

Failure is always closed. A missing container raises rather than generating a
fresh key, because silently minting one would orphan every credential already
hashed with the old key: the database would still hold verifiers nothing could
check, and every Receiver would fail authentication with no visible cause.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


MAGIC = b"SPEAKLINK-RCVKEY\x00"
FORMAT_VERSION = 1
MIN_KEY_BYTES = 32
MAX_KEY_VERSIONS = 16


class KeyCustodyError(Exception):
    """Base class, so no caller can handle one failure mode and miss another."""


class KeyContainerMissing(KeyCustodyError):
    """No container exists. Deliberately not the same as "make me one"."""


class KeyContainerExists(KeyCustodyError):
    """A container is already there and will not be overwritten."""


class KeyContainerCorrupt(KeyCustodyError):
    """Truncated, foreign, or failing its integrity check."""


class KeyCustodyUnavailable(KeyCustodyError):
    """The platform cannot protect or unprotect right now."""


class ProtectionScope(Enum):
    """Who can unprotect the container.

    CURRENT_USER is what the controlled pilot runs as. LOCAL_MACHINE is what a
    dedicated service identity will need. The ADR has not chosen between them,
    so neither is a default.
    """

    CURRENT_USER = "current_user"
    LOCAL_MACHINE = "local_machine"


class Protector(Protocol):
    """Anything that can seal and unseal bytes for this host."""

    scope: ProtectionScope

    def protect(self, payload: bytes) -> bytes: ...

    def unprotect(self, payload: bytes) -> bytes: ...


# ---------------------------------------------------------------------------
# Windows DPAPI
# ---------------------------------------------------------------------------
CRYPTPROTECT_LOCAL_MACHINE = 0x04
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DpapiProtector:
    """CryptProtectData / CryptUnprotectData through ctypes.

    ctypes rather than a third-party package: this is two well-documented calls,
    and a dependency that touches key material is a dependency worth not having.
    """

    def __init__(self, scope: ProtectionScope) -> None:
        if not isinstance(scope, ProtectionScope):
            raise KeyCustodyError("a DPAPI protection scope must be chosen explicitly")
        self.scope = scope

    def _flags(self) -> int:
        # UI_FORBIDDEN because this runs headless: a prompt would hang a service.
        flags = CRYPTPROTECT_UI_FORBIDDEN
        if self.scope is ProtectionScope.LOCAL_MACHINE:
            flags |= CRYPTPROTECT_LOCAL_MACHINE
        return flags

    def protect(self, payload: bytes) -> bytes:
        return self._call(payload, protect=True)

    def unprotect(self, payload: bytes) -> bytes:
        return self._call(payload, protect=False)

    def _call(self, payload: bytes, *, protect: bool) -> bytes:
        if sys.platform != "win32":
            raise KeyCustodyUnavailable("DPAPI is only available on Windows")

        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

        def blob(data: bytes) -> DATA_BLOB:
            buffer = ctypes.create_string_buffer(data, len(data))
            return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))

        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        source = blob(payload)
        result = DATA_BLOB()
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData

        succeeded = function(
            ctypes.byref(source),
            None,          # description: nothing identifying goes next to the key
            None,          # no additional entropy: custody is the file's ACL
            None,
            None,
            self._flags(),
            ctypes.byref(result),
        )
        if not succeeded:
            code = ctypes.get_last_error()
            # The Windows error code is safe to report; the payload is not.
            raise KeyCustodyUnavailable(
                f"DPAPI {'protect' if protect else 'unprotect'} failed (error {code})"
            )
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(result.pbData)


class FakeProtector:
    """Deterministic stand-in so the container logic is testable off Windows.

    It is emphatically not encryption. It exists so that container format,
    integrity, rotation and atomic-replacement behaviour can be proven on any
    platform, and so a "different host" can be simulated by changing identity.
    """

    scope = ProtectionScope.CURRENT_USER

    def __init__(self, identity: str = "test-host") -> None:
        self._identity = identity.encode("utf-8")

    def protect(self, payload: bytes) -> bytes:
        marker = hashlib.sha256(self._identity).digest()[:8]
        return marker + bytes(byte ^ 0x5A for byte in payload)

    def unprotect(self, payload: bytes) -> bytes:
        marker = hashlib.sha256(self._identity).digest()[:8]
        if len(payload) < 8 or payload[:8] != marker:
            raise KeyCustodyUnavailable("this container was sealed by a different identity")
        return bytes(byte ^ 0x5A for byte in payload[8:])


# ---------------------------------------------------------------------------
# The key ring
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KeyRing:
    """Every key the backend may verify with, and the one it signs with.

    ``__repr__`` is written by hand. The default would print every key into any
    traceback that happened to include this object.
    """

    active_version: int
    _keys: dict[int, bytes]

    def versions(self) -> list[int]:
        return sorted(self._keys)

    def key(self, version: int) -> bytes:
        try:
            return self._keys[version]
        except KeyError:
            raise KeyCustodyError(f"key version {version} is not in this ring") from None

    def signing_key(self) -> tuple[int, bytes]:
        """The version new credentials must be hashed with."""
        return self.active_version, self.key(self.active_version)

    def as_mapping(self) -> dict[int, bytes]:
        """A copy, for the credential services that take a key mapping."""
        return dict(self._keys)

    def __repr__(self) -> str:  # pragma: no cover - exercised through tests
        return (
            f"KeyRing(active_version={self.active_version}, "
            f"versions={self.versions()}, keys=<redacted>)"
        )

    __str__ = __repr__


def default_container_path() -> Path:
    """Outside the repository, and outside the database directory.

    %LOCALAPPDATA% on Windows; an equivalent user data directory elsewhere so
    development on another platform does not scatter files into the checkout.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / "SpeakLink" / "keys" / "receiver-hmac-keys.bin"


def _encode(payload: dict) -> bytes:
    """Canonical JSON plus a digest over it.

    DPAPI already authenticates its output, so this digest is not the only
    integrity check - it is the one that still works with any protector, and it
    turns "decrypted to nonsense" into a named error instead of a JSON crash.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return json.dumps(
        {"body": base64.b64encode(body).decode("ascii"), "digest": digest},
        separators=(",", ":"),
    ).encode("utf-8")


def _decode(raw: bytes) -> dict:
    try:
        envelope = json.loads(raw.decode("utf-8"))
        body = base64.b64decode(envelope["body"])
        digest = envelope["digest"]
    except Exception:
        raise KeyContainerCorrupt("the key container did not decode") from None
    if hashlib.sha256(body).hexdigest() != digest:
        raise KeyContainerCorrupt("the key container failed its integrity check")
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise KeyContainerCorrupt("the key container body was unreadable") from None


def _write_atomically(path: Path, data: bytes) -> None:
    """Write beside the target, then replace in one step.

    A half-written container is worse than none: every Receiver credential in
    the database becomes unverifiable, and the operator has no obvious way back.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".rcvkey-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _seal(path: Path, payload: dict, protector: Protector) -> None:
    _write_atomically(path, MAGIC + protector.protect(_encode(payload)))


def _open(path: Path, protector: Protector) -> dict:
    if not path.exists():
        raise KeyContainerMissing(
            f"no Receiver key container at {path}. SpeakLink will not invent one: "
            "credentials already in the database are hashed with the missing key, "
            "and a fresh key would make every one of them unverifiable."
        )
    raw = path.read_bytes()
    if len(raw) <= len(MAGIC) or not raw.startswith(MAGIC):
        raise KeyContainerCorrupt("that file is not an SpeakLink key container")
    return _decode(protector.unprotect(raw[len(MAGIC):]))


def create_key_container(path, *, protector: Protector) -> None:
    """Create the first key. Refuses to replace an existing container."""
    path = Path(path)
    if path.exists():
        raise KeyContainerExists(
            f"a key container already exists at {path}; rotate it instead of replacing it"
        )
    payload = {
        "format": FORMAT_VERSION,
        "scope": protector.scope.value,
        "active_version": 1,
        "keys": {"1": base64.b64encode(secrets.token_bytes(MIN_KEY_BYTES)).decode("ascii")},
    }
    _seal(path, payload, protector)


def load_key_ring(path, *, protector: Protector) -> KeyRing:
    """Read every key, or fail closed."""
    payload = _open(Path(path), protector)
    try:
        keys = {int(version): base64.b64decode(value) for version, value in payload["keys"].items()}
        active = int(payload["active_version"])
    except Exception:
        raise KeyContainerCorrupt("the key container is missing its key material") from None
    if active not in keys:
        raise KeyContainerCorrupt("the container's signing version is not in its own key ring")
    return KeyRing(active_version=active, _keys=keys)


def rotate_signing_key(path, *, protector: Protector) -> int:
    """Add a new signing key, keeping every previous one for verification.

    Old keys are retained deliberately: credentials hashed under them are still
    valid until they are themselves rotated, and discarding the key would revoke
    every Receiver at once with no way to undo it.
    """
    path = Path(path)
    payload = _open(path, protector)
    keys = dict(payload["keys"])
    if len(keys) >= MAX_KEY_VERSIONS:
        raise KeyCustodyError(
            f"this key ring already holds {len(keys)} versions; retire old credentials "
            "before adding another"
        )
    next_version = max(int(version) for version in keys) + 1
    keys[str(next_version)] = base64.b64encode(secrets.token_bytes(MIN_KEY_BYTES)).decode("ascii")
    payload["keys"] = keys
    payload["active_version"] = next_version
    _seal(path, payload, protector)
    return next_version


__all__ = [
    "MIN_KEY_BYTES",
    "DpapiProtector",
    "FakeProtector",
    "KeyContainerCorrupt",
    "KeyContainerExists",
    "KeyContainerMissing",
    "KeyCustodyError",
    "KeyCustodyUnavailable",
    "KeyRing",
    "ProtectionScope",
    "Protector",
    "create_key_container",
    "default_container_path",
    "load_key_ring",
    "rotate_signing_key",
]
