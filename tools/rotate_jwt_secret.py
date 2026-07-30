"""Rotate the HQ JWT signing secret without ever handling it in the open.

WHY THIS EXISTS

The security audit found the live ``JWT_SECRET`` inside ``echocast-live.zip`` in
the repository folder. Rotating it is the remediation, and nothing here could do
it: every ``rotate`` in this repository is Receiver **Device credential**
rotation, which is a different key with different consequences and is deliberately
untouched by this tool.

WHAT THIS TOOL DOES NOT DO, ON PURPOSE

* It does not rotate Receiver Device credentials. Rotating the signing secret
  costs everybody one sign-in; rotating a Device credential costs a Store a
  re-enrolment, and there is no reason to do the second while fixing the first.
* It does not touch the database, the HMAC key container, Stores, Users, history,
  logs or backups.
* It does not create a database or an administrator.
* It does not stop or start anything. Deciding when HQ goes down is the
  operator's call, not a side effect of editing a file.

THE CONSTRAINTS THAT SHAPE IT

The secret never appears in a command-line argument - ``tasklist`` shows those to
every user on the machine - nor in PowerShell history, a log, Git, or this tool's
own output. It is generated in-process, written straight to the file, and the only
thing reported is a truncated SHA-256 fingerprint: enough to confirm the value
CHANGED, useless for reconstructing it.

Replacement is atomic. A half-written secret file is an HQ that can neither mint
nor verify a token, and that failure would surface at the next sign-in rather than
at the moment of the edit.

The backup is REDACTED. "Back up the config first" is ordinary advice that here
would mean writing a second copy of the exposed secret somewhere new - the exact
opposite of the remediation. The backup keeps the keys, comments and ordering and
replaces every secret VALUE with a placeholder, so it records the shape of the
configuration and none of its secrets.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: 48 bytes url-safe, matching what Start-EchoCastPersistentLanServer.ps1 already
#: mints, so a rotated secret is indistinguishable in shape from a fresh one.
SECRET_BYTES = 48

#: Values that must never survive into a backup copy. Keys, not patterns: the
#: question "is this line a secret" is answered by what it is called, and
#: guessing from the value's shape is how a redactor misses one.
SECRET_KEYS = frozenset({
    "JWT_SECRET",
    "ADMIN_PASSWORD",
    "ADMIN_USERNAME",
    "MONGO_URL",
    "SECRET_KEY",
    "ECHOCAST_KEY_PROTECTOR_SECRET",
})

REDACTED_PLACEHOLDER = "<redacted-by-rotate_jwt_secret>"

FINGERPRINT_LENGTH = 12


class RotationError(RuntimeError):
    """The rotation was refused. Never carries a secret."""


@dataclass(frozen=True)
class RotationResult:
    """What happened, in terms nobody can reverse into a secret."""

    path: Path
    previous_fingerprint: str
    new_fingerprint: "str | None"
    backup_path: "Path | None" = None


def generate_secret() -> str:
    """One strong secret, in memory only.

    ``token_urlsafe`` is chosen over ``token_hex`` for density and over
    ``token_bytes`` because the value has to survive a KEY=VALUE line and a
    PowerShell environment assignment without quoting games. It emits no '='
    padding, so it cannot break the parsing of the file it is written into.
    """
    return secrets.token_urlsafe(SECRET_BYTES)


def fingerprint(value: str) -> str:
    """A short, one-way tag so an operator can see that a value changed."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def replace_env_value(text: str, key: str, value: str) -> str:
    """Rewrite exactly one KEY=VALUE line and nothing else.

    Line-oriented rather than a regex over the whole file, so a comment that
    happens to mention the key is left alone and every unrelated line comes out
    byte-identical - including ordering, blank lines and quoting style.

    A missing key is REFUSED rather than appended. Appending would silently
    create a second source of truth in a file the operator believes they
    understand, and the failure would show up as "the rotation did not take".
    """
    lines = text.split("\n")
    found = False
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            # Split the line ending off FIRST. The real backend/.env is CRLF, and
            # splitting the file on "\n" leaves a trailing "\r" on every line - so
            # the first version of this saw '"value"\r', decided it was not
            # quoted, and rewrote the line WITHOUT the \r. The result was one
            # LF line among five CRLF ones and the quotes silently dropped.
            #
            # It went unnoticed because my fixture used "\n" and the real file
            # does not. A fixture that does not look like the file it stands in
            # for is a fixture that cannot catch this.
            body = line
            ending = ""
            if body.endswith("\r"):
                body, ending = body[:-1], "\r"

            existing = body.split("=", 1)[1]
            # Preserve the quoting style. python-dotenv accepts either, but
            # rewriting one line unquoted while its neighbours stay quoted is a
            # silent style change in a file an operator reads by eye, and any
            # naive parser expecting quotes would read the new value with
            # different edges.
            for quote in ('"', "'"):
                if len(existing) >= 2 and existing.startswith(quote) and existing.endswith(quote):
                    lines[index] = f"{key}={quote}{value}{quote}{ending}"
                    break
            else:
                lines[index] = f"{key}={value}{ending}"
            found = True
    if not found:
        raise RotationError(
            f"{key} is not set in that file, so there is nothing to rotate. "
            "Refusing to append it: that would create a second source of truth."
        )
    return "\n".join(lines)


def redacted_backup_text(text: str) -> str:
    """The configuration's shape, with every secret value removed."""
    lines = text.split("\n")
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in SECRET_KEYS:
            lines[index] = f"{key}={REDACTED_PLACEHOLDER}"
    return "\n".join(lines)


def _atomic_write(path: Path, data: bytes) -> None:
    """Write through a sibling temporary file, then rename.

    Same directory on purpose: ``os.replace`` is only atomic within one
    filesystem, and a temp file in %TEMP% could be on another volume. The temp
    file is removed on any failure, so a crash cannot leave a stray copy of a
    secret lying next to the real one.
    """
    temporary = path.with_name(path.name + f".rotating-{os.getpid()}")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_with_bom(path: Path) -> tuple[str, bool]:
    raw = path.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    if has_bom:
        raw = raw[3:]
    return raw.decode("utf-8"), has_bom


def rotate_env_file(
    path,
    *,
    key: str = "JWT_SECRET",
    backup_directory=None,
    dry_run: bool = False,
    now: "datetime | None" = None,
) -> RotationResult:
    """Rotate one KEY=VALUE secret inside a dotenv-style file."""
    path = Path(path)
    if not path.exists():
        raise RotationError(f"there is no configuration file at {path}")

    text, has_bom = _read_with_bom(path)
    try:
        previous = next(
            line.split("=", 1)[1]
            for line in text.split("\n")
            if line.startswith(f"{key}=")
        )
    except StopIteration:
        raise RotationError(f"{key} is not set in {path}") from None

    previous_tag = fingerprint(previous)
    if dry_run:
        return RotationResult(path=path, previous_fingerprint=previous_tag,
                              new_fingerprint=None)

    backup_path = None
    if backup_directory is not None:
        moment = now or datetime.now(timezone.utc)
        directory = Path(backup_directory)
        directory.mkdir(parents=True, exist_ok=True)
        backup_path = directory / f"{path.name}.redacted-{moment.strftime('%Y%m%d-%H%M%S')}.txt"
        backup_path.write_text(redacted_backup_text(text), encoding="utf-8")

    replacement = generate_secret()
    updated = replace_env_value(text, key, replacement)
    payload = updated.encode("utf-8")
    if has_bom:
        payload = b"\xef\xbb\xbf" + payload
    _atomic_write(path, payload)

    return RotationResult(path=path, previous_fingerprint=previous_tag,
                          new_fingerprint=fingerprint(replacement),
                          backup_path=backup_path)


def rotate_secret_file(path, *, dry_run: bool = False) -> RotationResult:
    """Rotate a file whose ENTIRE body is the secret.

    This is the persistent HQ's ``keys/jwt-secret.txt``. Written with no trailing
    newline: the readers strip, but a file whose bytes differ from the value it
    represents is a file somebody eventually compares wrongly.
    """
    path = Path(path)
    if not path.exists():
        raise RotationError(f"there is no secret file at {path}")

    previous = path.read_text(encoding="utf-8").strip()
    if not previous:
        raise RotationError(f"{path} is empty; refusing to treat that as a secret")
    previous_tag = fingerprint(previous)
    if dry_run:
        return RotationResult(path=path, previous_fingerprint=previous_tag,
                              new_fingerprint=None)

    replacement = generate_secret()
    _atomic_write(path, replacement.encode("utf-8"))
    return RotationResult(path=path, previous_fingerprint=previous_tag,
                          new_fingerprint=fingerprint(replacement))


def default_persistent_secret_path() -> Path:
    """The persistent HQ's own secret file, resolved the way the runtime does."""
    from tools.persistent_lan_server import ServerProfile

    return ServerProfile.persistent().key_container.parent / "jwt-secret.txt"


def _report(label: str, result: RotationResult) -> None:
    """Fingerprints only. There is deliberately no code path here that could
    write a secret to stdout."""
    print(f"  {label}")
    print(f"    file            : {result.path}")
    print(f"    previous (sha256): {result.previous_fingerprint}...")
    if result.new_fingerprint is None:
        print("    new             : (dry run - nothing was written)")
    else:
        print(f"    new      (sha256): {result.new_fingerprint}...")
    if result.backup_path is not None:
        print(f"    redacted backup : {result.backup_path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="rotate_jwt_secret",
        description=(
            "Rotate the HQ JWT signing secret. The new value is generated in "
            "process, written atomically, and never printed - only a short "
            "SHA-256 fingerprint is reported. Receiver Device credentials, the "
            "HMAC key container, the database, Stores, Users, history, logs and "
            "backups are all untouched."
        ),
    )
    parser.add_argument(
        "--target", required=True, choices=("env", "persistent", "both"),
        help="Which secret to rotate. Required: nothing about a signing secret "
             "should be rotated by default.",
    )
    parser.add_argument("--env-path", type=Path, default=REPOSITORY_ROOT / "backend" / ".env")
    parser.add_argument("--persistent-path", type=Path, default=None)
    parser.add_argument(
        "--backup-directory", type=Path, default=None,
        help="Where to write a REDACTED copy of the configuration. Secret values "
             "are replaced with a placeholder, so the backup records the shape "
             "and none of the secrets.",
    )
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args(argv)

    print("=== rotating the HQ JWT signing secret ===")
    if arguments.dry_run:
        print("  DRY RUN: nothing will be written.")

    results: list[tuple[str, RotationResult]] = []
    try:
        if arguments.target in ("env", "both"):
            results.append((
                "backend .env (development and test signing secret)",
                rotate_env_file(arguments.env_path,
                                backup_directory=arguments.backup_directory,
                                dry_run=arguments.dry_run),
            ))
        if arguments.target in ("persistent", "both"):
            path = arguments.persistent_path or default_persistent_secret_path()
            results.append((
                "persistent HQ keys/jwt-secret.txt",
                rotate_secret_file(path, dry_run=arguments.dry_run),
            ))
    except RotationError as refusal:
        print(f"\nREFUSED: {refusal}")
        return 2

    print()
    for label, result in results:
        _report(label, result)

    print()
    if arguments.dry_run:
        print("Dry run complete. Nothing was changed.")
        return 0

    print("ECHOCAST_JWT_SECRET_ROTATED")
    print("Every existing HQ browser session is now invalid and must sign in again.")
    print("Receiver Device credentials are NOT affected - no Store needs re-enrolling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
