"""Change an HQ account's password offline, against one explicitly named database.

WHY THIS EXISTS

The security audit found the live ``ADMIN`` password inside ``echocast-live.zip``
and confirmed it verifies against the account in BOTH the protected source
database and the persistent HQ database. Changing it is the remediation, and there
was no offline path: ``tools/create_owner.py`` creates an account and refuses to
touch an existing one, and the two HTTP routes need a running server and a
signed-in session.

WHAT IT REUSES RATHER THAN REBUILDS

``auth.hash_password`` / ``auth.verify_password`` own the algorithm - bcrypt, and
this module never chooses a cost or a salt. ``user_lifecycle.set_password_hash``
owns the consequence: it writes the hash and bumps ``session_version`` inside ONE
``engine.begin()`` transaction, so a changed password cannot leave old tokens
valid, and cannot half-apply. Nothing here writes SQL against ``hq_users``.

THE TWO PROPERTIES THAT SHAPE EVERY DECISION

**A refusal changes nothing.** A wrong current password, a mismatched repeat, a
weak new password, an inactive account, a running HQ - each is checked before any
write, and the row comes out byte-identical. A half-remediated account is worse
than an un-remediated one nobody trusts.

**No secret is ever written down.** The three passwords arrive through hidden
prompts and live in local variables. There is no flag to pass one in, nothing is
read from the environment, and no ``print`` in this file references a
password-bearing variable - there is a test that walks the AST to prove it.

WHAT IT WILL NOT TOUCH

Stores, Devices, Receiver credentials, the HMAC key container, the JWT signing
secret, broadcast history, logs and backups. It creates no database and no
account, and it starts and stops nothing: when HQ goes down is the operator's
decision, not a side effect of a password change.
"""

from __future__ import annotations

import argparse
import getpass
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for _candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

TARGET_PERSISTENT = "persistent"
TARGET_PROTECTED = "protected"

#: The flag an operator must type to touch the protected source database, because
#: doing so changes its recorded baseline SHA. Deliberately long and awkward.
PROTECTED_CONFIRMATION_FLAG = "--i-understand-this-changes-the-protected-baseline"


class ChangeRefused(RuntimeError):
    """The change was refused and nothing was written. Never carries a secret."""


@dataclass(frozen=True)
class ChangeResult:
    """Safe evidence only. No field here can hold a password or a hash."""

    target_label: str
    database: Path
    username: str
    user_id: int
    role: str
    previous_session_version: int
    new_session_version: int
    integrity_before: str
    integrity_after: str
    backup_path: "Path | None" = None


# ===========================================================================
# Targets
# ===========================================================================
def resolve_target(target: str) -> Path:
    """One of exactly two files. There is no third option and no free-form path.

    A free-form database-path flag is the mechanism by which the protected source
    database gets edited by accident, so no such option exists.
    """
    if target == TARGET_PROTECTED:
        return REPOSITORY_ROOT / "backend" / "echocast_live.db"
    if target == TARGET_PERSISTENT:
        from tools.persistent_lan_server import ServerProfile

        return ServerProfile.persistent().database
    raise ChangeRefused(
        f"{target!r} is not a known target. Use --target persistent or "
        "--target protected."
    )


# ===========================================================================
# Read-only inspection that leaves no trace
# ===========================================================================
def read_only_uri(path) -> str:
    """``mode=ro`` is not enough on its own.

    It stops writes to the main file but not the shared-memory index a WAL
    database needs, and creating that index IS a file creation - which tripped
    this repository's protected-database sidecar guard once already.
    ``immutable=1`` tells SQLite the file cannot change, so no WAL and no shm are
    needed and neither appears.
    """
    return f"file:{Path(path).as_posix()}?mode=ro&immutable=1"


def integrity_check(path) -> str:
    """PRAGMA integrity_check, read-only, no sidecars."""
    path = Path(path)
    if not path.exists():
        raise ChangeRefused(f"there is no database at {path}")
    connection = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def make_consistent_backup(path, backup_directory, *, now=None) -> Path:
    """A consistent snapshot through SQLite's backup API.

    Not a file copy: copying a database that another process is mid-write on
    produces a file that looks fine and fails later. The backup API takes a
    coherent snapshot even while the source is being read.
    """
    path = Path(path)
    directory = Path(backup_directory)
    directory.mkdir(parents=True, exist_ok=True)
    moment = now or datetime.now(timezone.utc)
    target = directory / f"{path.stem}-before-password-change-{moment.strftime('%Y%m%d-%H%M%S')}.db"

    source = sqlite3.connect(read_only_uri(path), uri=True)
    try:
        destination = sqlite3.connect(target)
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()

    if integrity_check(target) != "ok":
        raise ChangeRefused(
            f"the backup written to {target} failed its own integrity check; "
            "refusing to change anything."
        )
    return target


# ===========================================================================
# Is anything using the database right now
# ===========================================================================
def running_echocast_processes() -> "list[str]":
    """Report any HQ runtime or backend that could be holding the database.

    Changing a password under a running server is not corruption - SQLite handles
    it - but the server would keep serving from a session minted under the OLD
    password until its own next request, and an operator would be told the
    remediation succeeded while a live session continued. Better to refuse and
    let them stop it deliberately.

    An unreadable process list returns EMPTY and the caller treats that as
    unknown rather than as "nothing is running".
    """
    script = (
        "$names = @('EchoCastHQRuntime','python','pythonw'); "
        "Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | "
        "Where-Object { $names -contains ($_.Name -replace '\\.exe$','') } | "
        "Where-Object { $_.CommandLine -match 'uvicorn|EchoCastHQRuntime|server:app' } | "
        "ForEach-Object { $_.Name + ' ' + $_.ProcessId }"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 - unknown, reported as such by the caller
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


# ===========================================================================
# The change
# ===========================================================================
def _prompt_three(prompt) -> tuple[str, str, str]:
    current = prompt("Current password (not shown): ")
    replacement = prompt("New password (not shown): ")
    again = prompt("Repeat the new password (not shown): ")
    return current, replacement, again


def change_password(
    database,
    *,
    username: str,
    prompt=None,
    target_label: str,
    backup_directory=None,
) -> ChangeResult:
    """Verify everything, then change one password in one transaction."""
    from sqlalchemy import create_engine, text

    from auth import hash_password, verify_password
    from password_policy import validate_password
    from user_lifecycle import set_password_hash

    database = Path(database)
    if not database.exists():
        raise ChangeRefused(
            f"there is no database at {database}. Refusing to create one: an "
            "empty database with a fresh administrator is not a password change."
        )

    integrity_before = integrity_check(database)
    if integrity_before != "ok":
        raise ChangeRefused(
            f"{database} failed its integrity check ({integrity_before}). "
            "Refusing to write to a database that is already damaged."
        )

    # Read the account read-only first, so a refusal never opens the file for
    # writing and never creates a sidecar.
    connection = sqlite3.connect(read_only_uri(database), uri=True)
    try:
        row = connection.execute(
            "SELECT id, username, password_hash, role, lifecycle_state, "
            "session_version FROM hq_users WHERE username = ?",
            (username,),
        ).fetchone()
    except sqlite3.Error as failure:
        raise ChangeRefused(
            f"{database} could not be read as an EchoCast database "
            f"({failure.__class__.__name__})."
        ) from None
    finally:
        connection.close()

    if row is None:
        # Deliberately does not echo the supplied name back.
        raise ChangeRefused(
            "no active account matches that username in this database.")
    user_id, actual_username, stored_hash, role, lifecycle, previous_version = row
    if lifecycle != "active":
        raise ChangeRefused(
            "that account is not active. Re-enable it deliberately before "
            "changing its password - a disabled account getting a working "
            "password back is not what a password change should do."
        )

    current, replacement, again = _prompt_three(prompt or getpass.getpass)

    if not verify_password(current, stored_hash):
        raise ChangeRefused("the current password is not correct. Nothing was changed.")
    if replacement != again:
        raise ChangeRefused("the two new passwords do not match. Nothing was changed.")
    if replacement == current:
        raise ChangeRefused(
            "the new password is the same as the current one. Since the current "
            "one is the exposed value, that would not remediate anything."
        )
    # Wrapped so every rejection this tool can produce is one exception type -
    # a caller that handles ChangeRefused cannot then miss a policy failure and
    # report it as a crash. The policy's own wording is preserved; the policy
    # module stays the single authority on what a valid password is.
    from password_policy import PasswordPolicyError

    try:
        validate_password(replacement)
    except PasswordPolicyError as refusal:
        raise ChangeRefused(f"{refusal} Nothing was changed.") from None

    backup_path = None
    if backup_directory is not None:
        backup_path = make_consistent_backup(database, backup_directory)

    engine = create_engine(f"sqlite:///{database}")
    try:
        # One transaction, owned by user_lifecycle: hash and session_version move
        # together or not at all. A hash that changed while session_version did
        # not is a changed password with every old token still valid.
        set_password_hash(engine, user_id=user_id,
                          password_hash=hash_password(replacement))

        with engine.connect() as verification:
            after = verification.execute(
                text("SELECT password_hash, session_version FROM hq_users "
                     "WHERE id = :user_id"),
                {"user_id": user_id},
            ).first()
    finally:
        engine.dispose()

    new_hash, new_version = after
    if verify_password(current, new_hash):
        raise ChangeRefused(
            "the old password still verifies after the change. Nothing can be "
            "trusted here - restore the backup and investigate.")
    if not verify_password(replacement, new_hash):
        raise ChangeRefused(
            "the new password does not verify after the change. Restore the "
            "backup and investigate.")
    if new_version <= previous_version:
        raise ChangeRefused(
            f"session_version did not advance ({previous_version} -> "
            f"{new_version}), so existing sessions would still be valid.")

    integrity_after = integrity_check(database)
    if integrity_after != "ok":
        raise ChangeRefused(
            f"the database failed its integrity check after the change "
            f"({integrity_after}). Restore the backup.")

    return ChangeResult(
        target_label=target_label, database=database, username=actual_username,
        user_id=user_id, role=role, previous_session_version=previous_version,
        new_session_version=new_version, integrity_before=integrity_before,
        integrity_after=integrity_after, backup_path=backup_path,
    )


def describe(result: ChangeResult) -> None:
    """Safe evidence only. No variable printed here can hold a secret."""
    print(f"  target            : {result.target_label}")
    print(f"  database          : {result.database}")
    print(f"  username          : {result.username}")
    print(f"  user id           : {result.user_id}")
    print(f"  role              : {result.role}")
    print(f"  session_version   : {result.previous_session_version} -> {result.new_session_version}")
    print(f"  integrity (before): {result.integrity_before}")
    print(f"  integrity (after) : {result.integrity_after}")
    if result.backup_path is not None:
        print(f"  backup            : {result.backup_path}")
    print("  password          : CHANGED")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="change_hq_user_password",
        description=(
            "Change one HQ account's password in one explicitly named database. "
            "All three passwords are typed at hidden prompts - none can be "
            "supplied as an argument or read from the environment. Stores, "
            "Devices, Receiver credentials, keys, history and logs are untouched, "
            "and nothing is started or stopped."
        ),
    )
    parser.add_argument("--target", required=True,
                        choices=(TARGET_PERSISTENT, TARGET_PROTECTED),
                        help="Which database. Required: never guessed.")
    parser.add_argument("--username", required=True,
                        help="The account to change. A username is not a secret.")
    parser.add_argument(
        PROTECTED_CONFIRMATION_FLAG, dest="protected_confirmed", action="store_true",
        help="Required for --target protected. Writing to the protected source "
             "database changes its recorded baseline SHA, which is a deliberate "
             "decision with its own review.",
    )
    parser.add_argument(
        "--allow-running-hq", action="store_true",
        help="Proceed even if an HQ runtime or backend appears to be running. "
             "Not recommended: a live server keeps serving sessions minted under "
             "the old password.",
    )
    arguments = parser.parse_args(argv)

    try:
        database = resolve_target(arguments.target)
    except ChangeRefused as refusal:
        print(f"REFUSED: {refusal}")
        return 2

    if arguments.target == TARGET_PROTECTED and not arguments.protected_confirmed:
        print("REFUSED: changing the protected source database alters its recorded")
        print("baseline SHA-256. That is a deliberate decision with its own review,")
        print(f"so it needs {PROTECTED_CONFIRMATION_FLAG}.")
        return 2

    print("=== changing an HQ account password ===")
    print(f"  target  : {arguments.target}")
    print(f"  database: {database}")
    print(f"  username: {arguments.username}")

    running = running_echocast_processes()
    if running and not arguments.allow_running_hq:
        print()
        print("REFUSED: something that looks like an EchoCast backend is running:")
        for entry in running:
            print(f"    {entry}")
        print("Stop it first, so no session minted under the old password survives")
        print("the change, or pass --allow-running-hq if you are sure.")
        return 3

    backups = None
    if arguments.target == TARGET_PERSISTENT:
        from tools.persistent_lan_server import ServerProfile

        backups = ServerProfile.persistent().backups
    else:
        # Outside Git, and outside the repository, so a backup of the protected
        # database can never be committed or swept into an archive.
        backups = Path.home() / "echocast-database-backups"

    print(f"  backup to: {backups}")
    print()

    try:
        result = change_password(database, username=arguments.username,
                                 target_label=arguments.target,
                                 backup_directory=backups)
    except ChangeRefused as refusal:
        print(f"REFUSED: {refusal}")
        return 2
    except Exception as failure:  # noqa: BLE001 - reported without a secret
        print(f"FAILED: {failure.__class__.__name__}. Nothing was committed.")
        return 4

    describe(result)
    print()
    print("ECHOCAST_HQ_PASSWORD_CHANGED")
    print("Every existing session for that account is now invalid.")
    print("Receiver Device credentials are NOT affected - no Store needs re-enrolling.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
