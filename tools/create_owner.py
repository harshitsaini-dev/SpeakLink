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
)
from user_schema import UserSchemaError, ensure_user_auth_schema  # noqa: E402


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
        # Every migration the current create_user depends on, in the order
        # application startup uses. This used to be a single call to
        # ensure_user_lifecycle_schema, which adds four of the five missing
        # columns - session_version comes from ensure_rbac_schema, and was
        # simply forgotten. On a database that had already been through startup
        # it worked; on the real HQ database it failed at the insert.
        # promote_missing_owner=False on purpose. The startup safety net
        # promotes the lowest-id active administrator when a database has no
        # OWNER - correct there, wrong here. This command is about to create an
        # OWNER itself, and promoting the existing administrator on the way
        # would change an account the operator was told would not be touched.
        ensure_user_auth_schema(engine, promote_missing_owner=False)
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
    except UserSchemaError as refusal:
        # An old database is an ordinary situation, not a crash. The operator
        # gets a sentence; the driver's traceback naming SQLAlchemy internals
        # helps nobody standing at an HQ machine.
        raise OwnerBootstrapError(str(refusal)) from None
    except UserLifecycleError as refusal:
        raise OwnerBootstrapError(str(refusal)) from None
    except Exception as failure:
        # Anything the layers below did not classify. The class name is kept
        # because it is the only searchable part; the message is not, because a
        # driver message can quote the row it was inserting.
        raise OwnerBootstrapError(
            f"the Owner account could not be created ({failure.__class__.__name__}). "
            "Nothing was changed."
        ) from None
    finally:
        # Not a security control - Python strings are immutable and this does
        # not scrub memory. It just stops the value staying reachable from this
        # frame if something later prints locals during a traceback.
        del password
    return created


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="create_owner",
        description="Create the SpeakLink Owner account. The password is typed, never passed.",
        allow_abbrev=False,
    )
    parser.add_argument("--username", required=True,
                        help="The Owner's username, for example owneradmin.")
    parser.add_argument("--database", default=None,
                        help="Path to the HQ database. Defaults to SPEAKLINK_DB_PATH.")
    # Deliberately no --password, --pass or --secret. See the module docstring.
    return parser


def main(argv: "list[str] | None" = None, *, engine=None, prompt=getpass.getpass) -> int:
    arguments = build_parser().parse_args(argv)

    if engine is None:
        if arguments.database:
            import os

            os.environ["SPEAKLINK_DB_PATH"] = arguments.database
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
