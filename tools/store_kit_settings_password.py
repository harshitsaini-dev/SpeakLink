"""A local password gate for Store Kit CONFIGURATION changes.

WHAT THIS PROTECTS

A Store PC sits in a shop. Anybody who reaches the keyboard can currently open
the Store Kit and repoint the Receiver at a different HQ, change its audio
output, or replace its Device identity. This is the gate in front of those.

WHAT IT MUST NEVER TOUCH

Receiving announcements. The Receiver Agent auto-starts at logon, authenticates
with its own Device credential, reconnects, heartbeats and plays with nobody
present - and none of that consults anything in this module. A password prompt
that could block playback would be a worse fault than the one it prevents: a
silent Store is the failure this project exists to avoid.

It is also not the HQ login, not the Device credential, not an enrolment code,
not the HMAC key and not a Windows password. It is never sent to HQ.

THE HONEST LIMIT

A Windows Administrator owns the filesystem and can delete this file. This is
app-level protection against ordinary unauthorized Store staff, not a boundary
against the machine's administrator. Saying otherwise would be the kind of
overclaim this project keeps removing.

WHY scrypt

bcrypt is what HQ uses for accounts and is trusted here, but it is a
third-party package and the Store Kit is frozen with PyInstaller onto Store
PCs, where every extra dependency is another thing that can fail to bundle.
``hashlib.scrypt`` is standard library and memory-hard. The algorithm and its
parameters are written into the verifier so a future change can migrate
deliberately instead of guessing what produced an old file.

WHY NOT DPAPI

DPAPI protects secrets that must be RECOVERED - which is exactly why the
Receiver credential uses it. A password verifier is a one-way hash: there is
nothing to recover, so encrypting it adds no secrecy. It would add a real
cost, though, because DPAPI CURRENT_USER binds a blob to the identity that
sealed it, and the Store Kit may run as a different Windows user than the
Agent. That would turn "the technician signed in as themselves" into "the
settings password no longer verifies". So: no DPAPI here, deliberately.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

__all__ = [
    "SettingsPasswordError",
    "SettingsPasswordRefused",
    "SettingsPasswordNotSet",
    "SettingsPasswordCorrupt",
    "SettingsPasswordWeak",
    "MINIMUM_LENGTH",
    "VERIFIER_FILENAME",
    "change_password",
    "default_verifier_path",
    "establish_password",
    "is_configured",
    "read_verifier",
    "require_authorization",
    "verify_password",
]

VERIFIER_FILENAME = "settings-password.json"

#: Short enough that a technician standing at a till can type it, long enough
#: that guessing it at a keyboard is hopeless. Deliberately not the HQ account
#: policy: that governs remote accounts reachable from the network, and this
#: governs a dialog somebody has to be physically present to see.
MINIMUM_LENGTH = 8

#: scrypt parameters. n=2**14 with r=8,p=1 costs about 16 MB and a fraction of
#: a second on a Store PC - unnoticeable when a person types a password once,
#: and punishing for anything trying a wordlist.
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_KEY_BYTES = 32
_SALT_BYTES = 16
_VERSION = 1


class SettingsPasswordError(RuntimeError):
    """Base class, so no caller handles one refusal and misses another.

    No subclass ever carries the attempted password, the stored password, the
    salt or the verifier. These messages are shown on a Store PC screen and
    written to a local log.
    """


class SettingsPasswordRefused(SettingsPasswordError):
    """Wrong password, mismatched confirmation, or an operation that is not
    allowed in the current state."""


class SettingsPasswordNotSet(SettingsPasswordError):
    """No Settings Password has been established on this Store yet.

    Raised rather than returning False so that a caller cannot accidentally
    treat "not configured" as "allowed". That distinction is the whole upgrade
    story: an existing Store has no verifier, and must not therefore be wide
    open.
    """


class SettingsPasswordCorrupt(SettingsPasswordError):
    """The verifier exists but cannot be read.

    FAILS CLOSED for settings changes, and the file is never deleted or
    rewritten - recovering it may be the only way back, and auto-resetting it
    would make corrupting the file the reset mechanism.
    """


class SettingsPasswordWeak(SettingsPasswordError):
    """The proposed password is too short or blank."""


def default_verifier_path() -> Path:
    """Beside the Receiver's own settings, in the per-user profile.

    Imported lazily so this module can be used - and tested - without pulling
    in the Agent and everything it imports.
    """
    from tools.receiver_agent import _agent_state_directory

    return _agent_state_directory() / VERIFIER_FILENAME


def _derive(password: str, salt: bytes, *, n: int, r: int, p: int) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                          dklen=_KEY_BYTES).hex()


def _check_strength(password: str) -> None:
    if password is None or not password.strip():
        raise SettingsPasswordWeak(
            "The Settings Password cannot be blank.")
    if len(password) < MINIMUM_LENGTH:
        raise SettingsPasswordWeak(
            f"The Settings Password must be at least {MINIMUM_LENGTH} "
            "characters.")


def _read(path) -> dict:
    """The stored verifier, or raise. Never returns a partial record."""
    path = Path(path)
    if not path.exists():
        raise SettingsPasswordNotSet(
            "No Settings Password has been set on this Store yet.")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SettingsPasswordCorrupt(
            "The Settings Password file could not be read. Settings changes "
            "are blocked until it is restored. The Receiver keeps running "
            "with its current settings."
        ) from None
    if not isinstance(stored, dict):
        raise SettingsPasswordCorrupt(
            "The Settings Password file is not in the expected format.")
    # An empty or truncated object must read as CORRUPT, not as absent -
    # otherwise a half-written file becomes an open door.
    for field in ("version", "algorithm", "salt", "verifier", "n", "r", "p"):
        if field not in stored:
            raise SettingsPasswordCorrupt(
                "The Settings Password file is incomplete. Settings changes "
                "are blocked until it is restored.")
    if stored.get("algorithm") != "scrypt":
        raise SettingsPasswordCorrupt(
            f"Unknown Settings Password algorithm {stored.get('algorithm')!r}.")
    return stored


def _write(path, stored: dict) -> None:
    """Temporary file then replace - the same convention save_config uses.

    A half-written verifier is a Store that cannot change its settings and
    cannot explain why.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n",
                         encoding="utf-8")
    os.replace(temporary, path)


def _build(password: str) -> dict:
    salt = secrets.token_bytes(_SALT_BYTES)
    return {
        "version": _VERSION,
        "algorithm": "scrypt",
        "n": _SCRYPT_N,
        "r": _SCRYPT_R,
        "p": _SCRYPT_P,
        "salt": salt.hex(),
        "verifier": _derive(password, salt, n=_SCRYPT_N, r=_SCRYPT_R,
                            p=_SCRYPT_P),
    }


def is_configured(path=None) -> bool:
    """Whether this Store has a Settings Password. Never raises.

    A corrupt file counts as configured: something is there, it is simply not
    readable, and reporting "not configured" would invite establishing a new
    one over it.
    """
    path = Path(path) if path is not None else default_verifier_path()
    return path.exists()


def read_verifier(path=None) -> dict:
    """A status-screen description. Carries no salt and no derived key.

    Deliberately not the stored record: a status view has no use for material
    that helps somebody test a guess offline.
    """
    path = Path(path) if path is not None else default_verifier_path()
    if not path.exists():
        return {"configured": False, "readable": False, "algorithm": None}
    try:
        stored = _read(path)
    except SettingsPasswordCorrupt:
        return {"configured": True, "readable": False, "algorithm": None}
    return {"configured": True, "readable": True,
            "algorithm": stored["algorithm"], "version": stored["version"]}


def establish_password(path, password: str, confirmation: str) -> None:
    """Set the FIRST Settings Password on a Store that has none.

    Refuses when one already exists - replacing a known password is
    change_password, which requires the current one. Refuses over a CORRUPT
    file too, because otherwise corrupting the file would be the reset.
    """
    path = Path(path) if path is not None else default_verifier_path()
    if path.exists():
        try:
            _read(path)
        except SettingsPasswordCorrupt:
            raise
        raise SettingsPasswordRefused(
            "A Settings Password is already set on this Store. Use Change "
            "Settings Password instead.")
    if password != confirmation:
        raise SettingsPasswordRefused(
            "The two Settings Password entries do not match.")
    _check_strength(password)
    _write(path, _build(password))


def verify_password(path, password: str) -> bool:
    """True when the password matches. Raises only if nothing can be read.

    Compared with compare_digest so the answer does not depend on how many
    leading characters were right.
    """
    stored = _read(Path(path) if path is not None else default_verifier_path())
    candidate = _derive(password or "", bytes.fromhex(stored["salt"]),
                        n=int(stored["n"]), r=int(stored["r"]),
                        p=int(stored["p"]))
    return hmac.compare_digest(candidate, str(stored["verifier"]))


def require_authorization(path, password: str) -> bool:
    """The ONE gate every protected Store Kit mutation goes through.

    A single function rather than an `if password ==` beside each button:
    scattered checks are how one button ends up unguarded, and the UI is not
    the security boundary - callers that reach the core helpers directly must
    pass through here too.

    Raises rather than returning False so a caller cannot forget to check.
    """
    path = Path(path) if path is not None else default_verifier_path()
    if verify_password(path, password):
        return True
    raise SettingsPasswordRefused(
        "That Settings Password is not correct.")


def change_password(path, current: str, new: str, confirmation: str) -> None:
    """Replace a known Settings Password with another.

    Verifies FIRST and writes only afterwards, so a wrong current password
    leaves the file byte-identical. The salt is rotated: reusing it would let
    anyone holding the old file test a guess against the new one more cheaply.

    Changes nothing else. Not the HQ login, not the Device credential, not the
    enrolment, and it does not restart the Receiver.
    """
    path = Path(path) if path is not None else default_verifier_path()
    require_authorization(path, current)
    if new != confirmation:
        raise SettingsPasswordRefused(
            "The two new Settings Password entries do not match.")
    _check_strength(new)
    _write(path, _build(new))
