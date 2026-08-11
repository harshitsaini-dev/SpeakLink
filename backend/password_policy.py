"""One place that decides how long a human password must be.

It was in four: two Pydantic schemas, two React forms, and a PowerShell guard.
Four copies of a number is four chances for them to disagree, and the way that
disagreement shows up is the worst possible one - the browser accepts what the
server then rejects, or the browser refuses something the server would have been
happy with. Either way the person typing is told nothing useful.

The backend is the source of truth. The frontend reads the same number so the
message it shows matches what will actually happen, but it decides nothing: a
client that skips the check entirely still hits this on the server.

THE MINIMUM IS 8

Down from 12. Length is the only property that reliably buys anything here, but
a minimum somebody cannot remember is a minimum they write on a sticky note
under the till, which is a worse outcome than a shorter one they keep in their
head. 8 is the floor, not a target; the sign-in box is also rate-limited and
account-locked, which is what actually stops guessing at this scale.

This is for HUMAN passwords only. Receiver Device credentials are 32 bytes of
`secrets.token_urlsafe` and are not affected by anything in this file.
"""

from __future__ import annotations


#: The floor for a human password, everywhere: local, private-LAN pilot,
#: production, API validation, forms, CLI and tests.
PASSWORD_MIN_LENGTH = 8

#: Unchanged. bcrypt only reads the first 72 bytes, so this is not about
#: strength - it stops somebody posting a megabyte into a hashing function.
PASSWORD_MAX_LENGTH = 200


class PasswordPolicyError(ValueError):
    """The password does not meet policy. Never carries the password."""


def validate_password(password: object) -> str:
    """Return the password unchanged, or refuse it.

    **Unchanged** is load-bearing. Trimming whitespace would mean a password
    typed with a trailing space is stored as a different password from the one
    the person believes they chose, and they find that out at the sign-in box
    with no idea why. What was typed is what is hashed.
    """
    if not isinstance(password, str):
        raise PasswordPolicyError("A password is required.")
    # Deliberately measuring what was typed, not a cleaned-up version of it.
    if len(password) < PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(
            f"A password must be at least {PASSWORD_MIN_LENGTH} characters."
        )
    if len(password) > PASSWORD_MAX_LENGTH:
        raise PasswordPolicyError(
            f"A password must be at most {PASSWORD_MAX_LENGTH} characters."
        )
    return password


def describe_policy() -> str:
    """The sentence shown to a person. Kept here so the UI cannot drift."""
    return f"At least {PASSWORD_MIN_LENGTH} characters."
