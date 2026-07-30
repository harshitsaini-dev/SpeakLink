"""Rotating the HQ JWT signing secret, without ever handling it in the open.

WHY THIS TOOL EXISTS

The security audit found the live `JWT_SECRET` inside an archive in the
repository folder. Rotating it is the remediation, and there was no tool for it:
every `rotate` in this repository is Receiver **Device credential** rotation,
which is a completely different key and must not be touched here.

THE CONSTRAINTS THAT SHAPE IT

The secret must never appear in a command-line argument (visible in `tasklist`
to every user on the machine), in PowerShell history, in a log, in Git, or in the
tool's own output. So it is generated in-process, written straight to the file,
and the only thing reported is a truncated SHA-256 fingerprint - enough for an
operator to confirm that the value CHANGED, useless for reconstructing it.

The replacement is atomic. A half-written secret file is an HQ that cannot mint
or verify any token at all, and the failure would arrive at the next sign-in
rather than at the moment of the edit.

The backup is deliberately REDACTED. "Back up the config before changing it" is
ordinary advice that here would mean writing a second copy of the exposed secret
to a new place - the opposite of the remediation. So the backup keeps the keys,
the comments and the order, and replaces every secret VALUE with a placeholder.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.rotate_jwt_secret import (  # noqa: E402
    SECRET_KEYS,
    RotationError,
    fingerprint,
    generate_secret,
    redacted_backup_text,
    replace_env_value,
    rotate_env_file,
    rotate_secret_file,
)


ENV_SAMPLE = (
    "# SpeakLink development configuration\n"
    'DB_NAME="test_database"\n'
    'CORS_ORIGINS="http://localhost:3000"\n'
    "JWT_SECRET=the-old-exposed-value-aaaaaaaaaaaaaaaaaaaa\n"
    "ADMIN_USERNAME=admin\n"
    "ADMIN_PASSWORD=an-old-password\n"
)


# ===========================================================================
# Generating
# ===========================================================================
def test_a_generated_secret_is_long_and_unguessable():
    secret = generate_secret()
    assert len(secret) >= 43, "shorter than 32 bytes of entropy base64-encoded"
    assert secret != generate_secret(), "two calls returned the same value"


def test_a_generated_secret_is_url_safe_so_it_survives_an_env_file():
    secret = generate_secret()
    assert "\n" not in secret and "\r" not in secret
    assert "=" not in secret, "a padding character would break KEY=VALUE parsing"
    assert " " not in secret


# ===========================================================================
# Fingerprints, not values
# ===========================================================================
def test_a_fingerprint_is_short_and_not_the_secret():
    secret = generate_secret()
    printed = fingerprint(secret)
    assert len(printed) <= 16
    assert secret not in printed
    assert printed == hashlib.sha256(secret.encode()).hexdigest()[: len(printed)]


def test_no_print_interpolates_a_secret_bearing_variable():
    """Walked through the AST, not grepped.

    My first version searched each line for the word "secret" and flagged
    `print("=== rotating the HQ JWT signing secret ===")` - a fixed string
    literal that exposes nothing. That is the third time in this project a text
    scan has been loudest about prose describing the very thing it guards.

    The property that actually matters is narrower and only a parser can express
    it: no `print` may reference a VARIABLE whose name suggests a secret, unless
    the reference goes through `fingerprint()`. A constant string is data; a
    variable is the thing that could hold the value.
    """
    import ast

    source = (REPOSITORY_ROOT / "tools" / "rotate_jwt_secret.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    suspicious = ("secret", "password", "replacement", "previous", "token")
    offenders = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "print"):
            continue
        # Names referenced anywhere inside this print call...
        referenced = {
            inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)
        }
        # ...unless the value was routed through fingerprint() first.
        fingerprinted = any(
            isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            and inner.func.id == "fingerprint"
            for inner in ast.walk(node)
        )
        risky = {name for name in referenced
                 if any(marker in name.lower() for marker in suspicious)}
        if risky and not fingerprinted:
            offenders.append((getattr(node, "lineno", "?"), sorted(risky)))

    assert offenders == [], (
        f"a print references a possibly-secret variable without fingerprinting it: {offenders}")


def test_the_tool_accepts_no_secret_on_the_command_line():
    source = (REPOSITORY_ROOT / "tools" / "rotate_jwt_secret.py").read_text(encoding="utf-8")
    for forbidden in ("--secret", "--jwt-secret", "--value", "--password"):
        assert forbidden not in source, f"{forbidden} would put a secret in argv"


# ===========================================================================
# Rewriting an env file
# ===========================================================================
def test_only_the_named_key_changes_and_every_other_line_is_byte_identical():
    updated = replace_env_value(ENV_SAMPLE, "JWT_SECRET", "brand-new-value")
    old_lines = ENV_SAMPLE.splitlines()
    new_lines = updated.splitlines()
    assert len(old_lines) == len(new_lines)
    for old, new in zip(old_lines, new_lines):
        if old.startswith("JWT_SECRET="):
            assert new == "JWT_SECRET=brand-new-value"
        else:
            assert old == new, "an unrelated line was rewritten"


def test_a_comment_mentioning_the_key_is_left_alone():
    text = "# JWT_SECRET is set below\nJWT_SECRET=old\n"
    updated = replace_env_value(text, "JWT_SECRET", "new")
    assert updated.splitlines()[0] == "# JWT_SECRET is set below"
    assert updated.splitlines()[1] == "JWT_SECRET=new"


def test_a_missing_key_is_refused_rather_than_appended():
    """Appending would silently create a second source of truth in a file the
    operator believes they understand."""
    with pytest.raises(RotationError):
        replace_env_value("DB_NAME=x\n", "JWT_SECRET", "new")


def test_a_trailing_newline_is_preserved_either_way():
    assert replace_env_value("JWT_SECRET=old\n", "JWT_SECRET", "n").endswith("\n")
    assert not replace_env_value("JWT_SECRET=old", "JWT_SECRET", "n").endswith("\n")


# ===========================================================================
# The redacted backup
# ===========================================================================
def test_the_backup_keeps_structure_and_drops_every_secret_value():
    backup = redacted_backup_text(ENV_SAMPLE)
    assert "the-old-exposed-value" not in backup
    assert "an-old-password" not in backup
    assert "JWT_SECRET=" in backup, "the key must still be recorded"
    assert 'DB_NAME="test_database"' in backup, "a non-secret value is kept"
    assert "# SpeakLink development configuration" in backup


@pytest.mark.parametrize("key", sorted(SECRET_KEYS))
def test_every_declared_secret_key_is_redacted(key):
    backup = redacted_backup_text(f"{key}=a-real-value-here\n")
    assert "a-real-value-here" not in backup
    assert key in backup


# ===========================================================================
# Rotating a real file, atomically
# ===========================================================================
def test_rotating_an_env_file_changes_only_the_secret(tmp_path):
    target = tmp_path / ".env"
    target.write_text(ENV_SAMPLE, encoding="utf-8")
    backups = tmp_path / "backups"

    result = rotate_env_file(target, backup_directory=backups)

    updated = target.read_text(encoding="utf-8")
    assert "the-old-exposed-value" not in updated
    assert "an-old-password" in updated, "the password line must be untouched"
    assert 'DB_NAME="test_database"' in updated
    assert result.previous_fingerprint != result.new_fingerprint


def test_rotating_writes_a_redacted_backup_next_to_nothing_sensitive(tmp_path):
    target = tmp_path / ".env"
    target.write_text(ENV_SAMPLE, encoding="utf-8")
    backups = tmp_path / "backups"

    result = rotate_env_file(target, backup_directory=backups)

    assert result.backup_path.exists()
    backup_text = result.backup_path.read_text(encoding="utf-8")
    assert "the-old-exposed-value" not in backup_text
    assert "an-old-password" not in backup_text


def test_a_dry_run_changes_nothing(tmp_path):
    target = tmp_path / ".env"
    target.write_text(ENV_SAMPLE, encoding="utf-8")
    before = target.read_bytes()

    result = rotate_env_file(target, backup_directory=tmp_path / "b", dry_run=True)

    assert target.read_bytes() == before
    assert result.new_fingerprint is None
    assert not (tmp_path / "b").exists() or not any((tmp_path / "b").iterdir())


def test_rotating_leaves_no_temporary_file_behind(tmp_path):
    target = tmp_path / ".env"
    target.write_text(ENV_SAMPLE, encoding="utf-8")
    rotate_env_file(target, backup_directory=tmp_path / "b")
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".env.")]
    assert leftovers == [], f"temporary files survived: {leftovers}"


def test_a_utf8_bom_is_preserved(tmp_path):
    """backend/.env on this machine carries a BOM. Losing it would change how
    every other tool reads the first key."""
    target = tmp_path / ".env"
    target.write_bytes(b"\xef\xbb\xbf" + ENV_SAMPLE.encode("utf-8"))
    rotate_env_file(target, backup_directory=tmp_path / "b")
    assert target.read_bytes().startswith(b"\xef\xbb\xbf")


def test_rotating_a_bare_secret_file_replaces_the_whole_content(tmp_path):
    """The persistent HQ keeps its secret as the entire file body, not KEY=VALUE."""
    target = tmp_path / "jwt-secret.txt"
    target.write_text("old-persistent-secret", encoding="utf-8")

    result = rotate_secret_file(target)

    assert target.read_text(encoding="utf-8").strip() != "old-persistent-secret"
    assert result.previous_fingerprint == fingerprint("old-persistent-secret")
    assert result.new_fingerprint != result.previous_fingerprint


def test_rotating_a_missing_file_is_refused(tmp_path):
    with pytest.raises(RotationError):
        rotate_secret_file(tmp_path / "absent.txt")


def test_a_secret_file_gets_no_trailing_newline_that_would_change_the_value(tmp_path):
    """The readers do .strip(), but a file whose bytes differ from the value is
    a file somebody will eventually compare wrongly."""
    target = tmp_path / "jwt-secret.txt"
    target.write_text("old", encoding="utf-8")
    rotate_secret_file(target)
    written = target.read_text(encoding="utf-8")
    assert written == written.strip(), "trailing whitespace was written"


# ===========================================================================
# The CLI refuses to guess
# ===========================================================================
def _run(arguments: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPOSITORY_ROOT / "tools" / "rotate_jwt_secret.py")] + arguments,
        capture_output=True, text=True, timeout=120, cwd=str(REPOSITORY_ROOT),
    )


def test_the_cli_requires_an_explicit_target():
    """Nothing about rotating a signing secret should happen by default."""
    result = _run([])
    assert result.returncode != 0
    assert "target" in (result.stdout + result.stderr).lower()


def test_the_cli_help_renders():
    result = _run(["--help"])
    assert result.returncode == 0
    assert "rotate" in result.stdout.lower()


def test_the_cli_never_writes_a_secret_to_its_own_output(tmp_path):
    target = tmp_path / ".env"
    target.write_text(ENV_SAMPLE, encoding="utf-8")
    result = _run(["--target", "env", "--env-path", str(target),
                   "--backup-directory", str(tmp_path / "b")])
    assert result.returncode == 0, result.stderr
    written = target.read_text(encoding="utf-8")
    new_value = next(
        line.split("=", 1)[1] for line in written.splitlines()
        if line.startswith("JWT_SECRET=")
    )
    assert new_value not in result.stdout
    assert new_value not in result.stderr


# ===========================================================================
# Quoting style survives a rotation
# ===========================================================================
def test_double_quoted_values_stay_double_quoted():
    """The real backend/.env quotes its values. Rewriting one line unquoted
    while its neighbours stay quoted is a silent style change in a file an
    operator reads by eye - and any naive parser expecting quotes would then
    read the new value with different edges."""
    updated = replace_env_value('JWT_SECRET="old-value"\n', "JWT_SECRET", "new-value")
    assert updated.splitlines()[0] == 'JWT_SECRET="new-value"'


def test_single_quoted_values_stay_single_quoted():
    updated = replace_env_value("JWT_SECRET='old'\n", "JWT_SECRET", "new")
    assert updated.splitlines()[0] == "JWT_SECRET='new'"


def test_an_unquoted_value_stays_unquoted():
    updated = replace_env_value("JWT_SECRET=old\n", "JWT_SECRET", "new")
    assert updated.splitlines()[0] == "JWT_SECRET=new"


def test_the_rotated_value_is_readable_by_dotenv_exactly_as_generated(tmp_path):
    """End to end through the real parser: whatever quoting the file used, the
    value dotenv hands the application must be the value that was generated."""
    from dotenv import dotenv_values

    for original in ('JWT_SECRET="old"\n', "JWT_SECRET='old'\n", "JWT_SECRET=old\n"):
        target = tmp_path / ".env"
        target.write_text(original, encoding="utf-8")
        rotate_env_file(target, backup_directory=None)

        written_line = target.read_text(encoding="utf-8").splitlines()[0]
        raw = written_line.split("=", 1)[1]
        stripped = raw[1:-1] if raw[:1] in ('"', "'") else raw

        parsed = dotenv_values(target)["JWT_SECRET"]
        assert parsed == stripped, (
            f"dotenv read {parsed!r} but the file holds {stripped!r} "
            f"(original style: {original!r})")
        assert len(parsed) >= 43, "the secret lost characters through quoting"
