"""Create the Owner account, with the password typed rather than passed.

    python tools/create_owner.py --username owneradmin

WHY THERE IS NO --password OPTION

A password on a command line is in the shell history, in the process list of
every user on the machine for as long as the command runs, and in any terminal
recording somebody made of the session. An environment variable is worse in a
different way: it is inherited by every child process. So the only way in is a
hidden prompt, asked twice.

WHY THIS IS NOT DONE AT STARTUP

Nothing creates this account automatically, and no migration seeds it. An owner
account that appears by itself, with a password somebody could look up in the
source or the migration history, is a backdoor with paperwork. Somebody has to
sit down and type it.

WHAT IT WILL NOT DO

It refuses if the username already exists, and refusing means refusing - it
does not quietly reset the existing password, because a create command that
silently becomes a reset command is how an owner account gets taken over by
whoever runs the installer next. It never touches the existing administrator.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from auth import hash_password  # noqa: E402
from password_policy import PasswordPolicyError, validate_password  # noqa: E402
from rbac import Role  # noqa: E402
from user_lifecycle import (  # noqa: E402
    DuplicateUsernameError,
    UserLifecycleError,
    create_user,
    ensure_user_lifecycle_schema,
)


EXIT_OK = 0
EXIT_REFUSED = 1


class OwnerBootstrapError(RuntimeError):
    """The Owner account was not created. Never carries the password."""


def read_new_password(*, prompt=getpass.getpass) -> str:
    """Ask twice, without echoing, and compare.

    ``getpass`` rather than ``input`` because ``input`` puts the password on the
    screen behind whoever happens to be standing there.
    """
    first = prompt("New Owner password (typing is hidden): ")
    again = prompt("Type it again to confirm: ")
    if first != again:
        # Neither value is echoed, not even a length.
        raise OwnerBootstrapError(
            "The two passwords did not match. Nothing was created; run the command again."
        )
    try:
        return validate_password(first)
    except PasswordPolicyError as refusal:
        raise OwnerBootstrapError(str(refusal)) from None


def create_owner(engine, *, username: str, prompt=getpass.getpass) -> dict:
    """Create one OWNER account. Transactional, and refuses a duplicate.

    The password is read first and the row written second, so a mismatch or a
    too-short password leaves nothing behind at all.
    """
    password = read_new_password(prompt=prompt)
    try:
        ensure_user_lifecycle_schema(engine)
        created = create_user(
            engine,
            username=username,
            display_name=username,
            role=Role.OWNER,
            password_hash=hash_password(password),
        )
    except DuplicateUsernameError:
        raise OwnerBootstrapError(
            f"An account named {username!r} already exists. Nothing was changed - "
            "its password was NOT reset. To change an existing Owner's password, "
            "sign in as an Owner and use the User Management page."
        ) from None
    except UserLifecycleError as refusal:
        raise OwnerBootstrapError(str(refusal)) from None
    finally:
        # Not a security control - Python strings are immutable and this does
        # not scrub memory. It just stops the value staying reachable from this
        # frame if something later prints locals during a traceback.
        del password
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_owner",
        description="Create the EchoCast Owner account. The password is typed, never passed.",
        allow_abbrev=False,
    )
    parser.add_argument("--username", required=True,
                        help="The Owner's username, for example owneradmin.")
    parser.add_argument("--database", default=None,
                        help="Path to the HQ database. Defaults to ECHOCAST_DB_PATH.")
    # Deliberately no --password, --pass or --secret. See the module docstring.
    return parser


def main(argv: "list[str] | None" = None, *, engine=None, prompt=getpass.getpass) -> int:
    arguments = build_parser().parse_args(argv)

    if engine is None:
        if arguments.database:
            import os

            os.environ["ECHOCAST_DB_PATH"] = arguments.database
        from db import engine as configured_engine

        engine = configured_engine

    try:
        created = create_owner(engine, username=arguments.username, prompt=prompt)
    except OwnerBootstrapError as refusal:
        print(f"Refused: {refusal}", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        print("Cancelled. Nothing was created.", file=sys.stderr)
        return EXIT_REFUSED

    # Nothing secret: a username, a role, an id. No password, no hash.
    print("Owner account created.")
    print(f"  username : {created['username']}")
    print(f"  role     : {created['role']}")
    print(f"  id       : {created['id']}")
    print()
    print("Sign in with it now to confirm it works. If this password was ever")
    print("typed anywhere it could be read, change it before real rollout.")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
