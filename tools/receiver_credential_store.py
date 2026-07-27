"""One Receiver Device credential, kept on the Store computer that owns it.

The Agent enrols once with a code an administrator read out, and must then
reconnect for months without ever seeing that code again. So the credential has
to live on disk, which makes *how* it sits there the entire problem.

Three failures are being designed against, each of which goes wrong quietly:

* **A readable credential.** Windows DPAPI seals it to the logged-on user, so a
  copied file is worthless on another machine or under another account. Nothing
  else in the file is secret - the Device id, the Store id and the backend origin
  are all things the operator is entitled to see when asking "what is this
  computer?".

* **A half-written file replacing a working one.** That leaves a Store computer
  that can neither authenticate nor re-enrol, and someone has to drive there. The
  write goes to a temporary file beside the target and is moved into place in one
  step, so an interrupted write leaves the previous credential exactly as it was.

* **Confusion with the backend's HMAC key container.** Different secret,
  different lifetime, different owner - but also a DPAPI file, often belonging to
  the same person on the same machine. Ordinary care is not enough here, so this
  store passes distinct DPAPI **entropy**. Crossing the two is then impossible
  rather than merely unlikely: neither file can be opened by the other's code
  path, and the tests prove it in both directions.

This module deliberately depends on nothing from ``backend/``. The Agent runs on
a till in a Store; dragging SQLAlchemy and a database engine onto it to read one
file would be absurd, and would couple a shop-floor computer to HQ's schema.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


#: Deliberately not ``key_custody.MAGIC``. The first bytes of the file say which
#: of the two secrets this is, before anything tries to decrypt it.
MAGIC = b"ECHOCAST-RCVDEV\x00"
FORMAT_VERSION = 1

#: Not a secret - it is in the source. It is *domain separation*: DPAPI will only
#: unseal a blob when the same entropy is supplied, so a key container and a
#: credential file cannot be opened by each other's code even by accident.
DPAPI_ENTROPY = b"EchoCast-AI/receiver-device-credential/v1"

MAX_ORIGIN_LENGTH = 512

#: The same shape ``backend/receiver_credentials.py`` issues. It is restated
#: rather than imported so the Agent needs no part of the backend; the two are
#: pinned together by a test that runs a really-issued credential through both.
_CREDENTIAL_PATTERN = re.compile(
    r"^echocast_rcv_v[1-9][0-9]*\."
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\."
    r"[A-Za-z0-9_-]{43,128}$"
)


class CredentialStoreError(Exception):
    """Base class, so no caller handles one failure mode and misses another."""


class CredentialMissing(CredentialStoreError):
    """Nothing is stored here. Deliberately not the same as "enrol me"."""


class CredentialExists(CredentialStoreError):
    """Something is already stored and will not be silently replaced."""


class CredentialCorrupt(CredentialStoreError):
    """Truncated, foreign, or failing its integrity check."""


class CredentialStoreUnavailable(CredentialStoreError):
    """The platform cannot protect or unprotect right now, or refused to."""


# ---------------------------------------------------------------------------
# Protectors
# ---------------------------------------------------------------------------
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DeviceCredentialProtector:
    """DPAPI CURRENT_USER, with this store's own entropy.

    CURRENT_USER and not LOCAL_MACHINE: the credential belongs to the account the
    Agent runs as, and LOCAL_MACHINE would let every account and every service on
    that computer read it, including anything that arrives later.
    """

    def __repr__(self) -> str:
        return "DeviceCredentialProtector(scope='current_user', entropy=<redacted>)"

    __str__ = __repr__

    def protect(self, payload: bytes) -> bytes:
        return self._call(payload, protect=True)

    def unprotect(self, payload: bytes) -> bytes:
        return self._call(payload, protect=False)

    def _call(self, payload: bytes, *, protect: bool) -> bytes:
        if sys.platform != "win32":
            raise CredentialStoreUnavailable("DPAPI is only available on Windows")

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
        entropy = blob(DPAPI_ENTROPY)
        result = DATA_BLOB()
        function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData

        succeeded = function(
            ctypes.byref(source),
            None,                    # description: nothing identifying beside the secret
            ctypes.byref(entropy),   # domain separation from the HMAC key container
            None,
            None,
            CRYPTPROTECT_UI_FORBIDDEN,  # headless: a prompt would hang a Store computer
            ctypes.byref(result),
        )
        if not succeeded:
            code = ctypes.get_last_error()
            # The Windows error code is safe to report. The payload is not.
            raise CredentialStoreUnavailable(
                f"DPAPI {'protect' if protect else 'unprotect'} failed (error {code}). "
                "A credential sealed for another Windows account, or on another "
                "computer, cannot be opened here - enrol this computer again."
            )
        try:
            return ctypes.string_at(result.pbData, result.cbData)
        finally:
            kernel32.LocalFree(result.pbData)


class FakeCredentialProtector:
    """Deterministic stand-in so the file logic is testable off Windows.

    Emphatically not encryption. It exists so that format, integrity, atomic
    replacement and "sealed by a different identity" can be proven on any
    platform. Its marker differs from the key custody fake for the same reason
    the real entropy does.
    """

    def __init__(self, identity: str = "test-computer") -> None:
        self._identity = identity.encode("utf-8")

    def __repr__(self) -> str:
        return "FakeCredentialProtector(identity=<redacted>)"

    __str__ = __repr__

    def _marker(self) -> bytes:
        return hashlib.sha256(DPAPI_ENTROPY + self._identity).digest()[:8]

    def protect(self, payload: bytes) -> bytes:
        return self._marker() + bytes(byte ^ 0x3C for byte in payload)

    def unprotect(self, payload: bytes) -> bytes:
        if len(payload) < 8 or payload[:8] != self._marker():
            raise CredentialStoreUnavailable(
                "this credential was sealed by a different identity"
            )
        return bytes(byte ^ 0x3C for byte in payload[8:])


# ---------------------------------------------------------------------------
# The stored record
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, repr=False)
class DeviceCredentialRecord:
    """What this computer is, plus the secret that proves it.

    ``__repr__`` is written by hand. The default would print the credential into
    any traceback that happened to include this object - which is exactly the
    kind of log a support ticket gets pasted into.
    """

    device_public_id: str
    store_id: int
    backend_origin: str
    created_at: datetime
    updated_at: datetime
    _credential: str

    def credential(self) -> str:
        """The raw value, for an Authorization header and nothing else."""
        return self._credential

    def __repr__(self) -> str:
        return (
            "DeviceCredentialRecord("
            f"device_public_id={self.device_public_id!r}, store_id={self.store_id}, "
            f"backend_origin={self.backend_origin!r}, "
            f"updated_at={self.updated_at.isoformat()!r}, credential=<redacted>)"
        )

    __str__ = __repr__


def default_credential_path() -> Path:
    """Outside the repository, and beside no database.

    ``%LOCALAPPDATA%`` on Windows, an equivalent user data directory elsewhere so
    development on another platform does not scatter secrets into a checkout.
    """
    base = os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), ".local", "share"
    )
    return Path(base) / "EchoCast-AI" / "receiver" / "device-credential.bin"


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------
def _require_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise CredentialStoreError(f"{field_name} must be timezone-aware UTC")
    return value


def _require_credential(value: object) -> str:
    """Check the shape before writing.

    A value that is not a credential must fail here, not months later at 6am in a
    Store with a message about authentication. The value itself is never quoted
    back - an error string ends up in logs.
    """
    if not isinstance(value, str) or _CREDENTIAL_PATTERN.fullmatch(value) is None:
        raise CredentialStoreError(
            "that value is not a Receiver Device credential; nothing was written"
        )
    return value


def _encode(payload: dict) -> bytes:
    """Canonical JSON plus a digest over it.

    DPAPI already authenticates its own output, so this is not the only integrity
    check - it is the one that still works with any protector, and it turns
    "unsealed to nonsense" into a named error instead of a JSON crash.
    """
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return json.dumps(
        {
            "body": base64.b64encode(body).decode("ascii"),
            "digest": hashlib.sha256(body).hexdigest(),
        },
        separators=(",", ":"),
    ).encode("utf-8")


def _decode(raw: bytes) -> dict:
    try:
        envelope = json.loads(raw.decode("utf-8"))
        body = base64.b64decode(envelope["body"])
        digest = envelope["digest"]
    except Exception:
        raise CredentialCorrupt("the stored credential did not decode") from None
    if hashlib.sha256(body).hexdigest() != digest:
        raise CredentialCorrupt("the stored credential failed its integrity check")
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        raise CredentialCorrupt("the stored credential body was unreadable") from None


def _write_atomically(path: Path, data: bytes) -> None:
    """Write beside the target, then replace in one step.

    A half-written credential is worse than none: the computer can neither
    authenticate nor re-enrol, and recovering it means physically visiting a
    Store. If anything fails, the temporary file is removed and the previous
    credential is left exactly as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=".rcvdev-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _restrict_permissions(Path(temporary))
        os.replace(temporary, path)
        temporary = None
    except CredentialStoreError:
        raise
    except OSError as failure:
        raise CredentialStoreUnavailable(
            f"the credential could not be written ({failure.__class__.__name__}); "
            "any previous credential is unchanged"
        ) from None
    finally:
        if temporary is not None and os.path.exists(temporary):
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _restrict_permissions(path: Path) -> None:
    """Best effort, and honest about it.

    DPAPI is what actually protects the contents; this only narrows who can read
    the sealed bytes. On Windows the useful tool is icacls, which needs a
    subprocess and can fail on a redirected profile, so a failure here is not
    allowed to abort a rotation - the credential is still sealed either way.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _seal(path: Path, payload: dict, protector) -> None:
    try:
        sealed = protector.protect(_encode(payload))
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreUnavailable("the credential could not be protected") from None
    _write_atomically(path, MAGIC + sealed)


def _open(path: Path, protector) -> dict:
    path = Path(path)
    if not path.exists():
        raise CredentialMissing(
            f"no Receiver Device credential at {path}. This computer has not been "
            "enrolled, or its credential was removed. Run the Agent's enrol "
            "command with a fresh code from an administrator."
        )
    raw = path.read_bytes()
    if len(raw) <= len(MAGIC) or not raw.startswith(MAGIC):
        raise CredentialCorrupt(
            "that file is not an EchoCast Receiver Device credential"
        )
    try:
        unsealed = protector.unprotect(raw[len(MAGIC):])
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialStoreUnavailable("the credential could not be unprotected") from None
    return _decode(unsealed)


# ---------------------------------------------------------------------------
# The operations
# ---------------------------------------------------------------------------
def save_credential(
    path,
    *,
    credential: str,
    device_public_id: str,
    store_id: int,
    backend_origin: str,
    protector,
    now: datetime | None = None,
) -> None:
    """Store a freshly issued credential. Refuses to replace an existing one.

    Enrolling twice by accident must not discard the credential this computer is
    currently authenticating with - the second enrolment would succeed at HQ and
    the first Device would be stranded.
    """
    path = Path(path)
    _require_credential(credential)
    moment = _require_utc(now or datetime.now(timezone.utc), "now")

    if not isinstance(device_public_id, str) or not device_public_id.strip():
        raise CredentialStoreError("a Device public identifier is required")
    if isinstance(store_id, bool) or not isinstance(store_id, int) or store_id <= 0:
        raise CredentialStoreError("a positive Store identifier is required")
    if not isinstance(backend_origin, str) or not backend_origin.strip():
        raise CredentialStoreError("the backend origin is required")
    if len(backend_origin) > MAX_ORIGIN_LENGTH:
        raise CredentialStoreError("the backend origin is unreasonably long")

    if path.exists():
        raise CredentialExists(
            f"a Receiver Device credential already exists at {path}. Enrolling "
            "again would strand the Device this computer is currently using; "
            "remove it deliberately, or rotate the credential instead."
        )

    _seal(
        path,
        {
            "format": FORMAT_VERSION,
            "device_public_id": device_public_id.strip(),
            "store_id": store_id,
            "backend_origin": backend_origin.strip(),
            "created_at": moment.isoformat(),
            "updated_at": moment.isoformat(),
            "credential": credential,
        },
        protector,
    )


def replace_credential(path, *, credential: str, protector, now: datetime | None = None) -> None:
    """Rotation. Changes the secret, never which Device this computer is.

    The existing record is read first: if it cannot be opened, the rotation stops
    rather than writing a file whose Device identity would be a guess.
    """
    path = Path(path)
    _require_credential(credential)
    moment = _require_utc(now or datetime.now(timezone.utc), "now")

    existing = _open(path, protector)
    payload = dict(existing)
    payload["credential"] = credential
    payload["updated_at"] = moment.isoformat()
    _seal(path, payload, protector)


def load_credential(path, *, protector) -> DeviceCredentialRecord:
    """Read the credential, or fail with a named error."""
    payload = _open(Path(path), protector)
    try:
        record = DeviceCredentialRecord(
            device_public_id=str(payload["device_public_id"]),
            store_id=int(payload["store_id"]),
            backend_origin=str(payload["backend_origin"]),
            created_at=datetime.fromisoformat(payload["created_at"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
            _credential=str(payload["credential"]),
        )
    except CredentialStoreError:
        raise
    except Exception:
        raise CredentialCorrupt("the stored credential is missing its fields") from None
    _require_credential(record.credential())
    return record


def delete_credential(path) -> None:
    """Remove it deliberately. The Device still exists at HQ until revoked."""
    path = Path(path)
    if not path.exists():
        raise CredentialMissing(f"there is no Receiver Device credential at {path}")
    try:
        os.unlink(path)
    except OSError as failure:
        raise CredentialStoreUnavailable(
            f"the credential could not be removed ({failure.__class__.__name__})"
        ) from None


__all__ = [
    "MAGIC",
    "CredentialCorrupt",
    "CredentialExists",
    "CredentialMissing",
    "CredentialStoreError",
    "CredentialStoreUnavailable",
    "DeviceCredentialProtector",
    "DeviceCredentialRecord",
    "FakeCredentialProtector",
    "default_credential_path",
    "delete_credential",
    "load_credential",
    "replace_credential",
    "save_credential",
]
