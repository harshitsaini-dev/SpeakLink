"""Installing the service identity must be rehearsable, idempotent and recoverable.

The first version of the installer had four faults, each of which only shows up
when someone actually uses it:

* ``-WhatIfOnly`` asserted elevation before it checked the flag, so the one mode
  meant for looking without touching could not be run by anyone who was only
  looking.
* it generated a random password and threw it away. The account then exists but
  nobody can configure a scheduled task or a service to log on as it, because
  that needs the password. An unusable account is worse than no account: it
  looks done.
* it hardened only the key directory. The application, data and log directories
  under ``C:\\ProgramData\\SpeakLink`` were left inheriting ``BUILTIN\\Users``.
* it ran ``icacls`` and reported success without ever reading back what the ACL
  became.

These tests cover the two halves that are testable off an elevated Windows
session: the directory ACL policy, and the shape of the installer script itself.
Creating the account and applying the ACL still need elevation and remain a
manual gate.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
SCRIPTS = REPOSITORY_ROOT / "scripts"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from key_custody_acl import (  # noqa: E402
    APPLICATION_DIRECTORY,
    DATA_DIRECTORY,
    KEY_DIRECTORY,
    LOG_DIRECTORY,
    DirectoryRole,
    directory_grant_plan,
    parse_icacls,
    verify_directory_acl,
)


INSTALLER = SCRIPTS / "Install-SpeakLinkServiceIdentity.ps1"
SERVICE = "HOST\\SpeakLinkService"


def _acl(*lines: str) -> dict:
    body = "\n".join(f"                        {line}" for line in lines)
    return parse_icacls(f"C:\\some\\dir {lines[0]}\n{body}\n\nSuccessfully processed 1 files")


HARDENED_RW = _acl(
    "NT AUTHORITY\\SYSTEM:(OI)(CI)(F)",
    "BUILTIN\\Administrators:(OI)(CI)(F)",
    "HOST\\SpeakLinkService:(OI)(CI)(R,W)",
)
HARDENED_RX = _acl(
    "NT AUTHORITY\\SYSTEM:(OI)(CI)(F)",
    "BUILTIN\\Administrators:(OI)(CI)(F)",
    "HOST\\SpeakLinkService:(OI)(CI)(RX)",
)
WITH_USERS = _acl(
    "NT AUTHORITY\\SYSTEM:(I)(OI)(CI)(F)",
    "BUILTIN\\Administrators:(I)(OI)(CI)(F)",
    "BUILTIN\\Users:(I)(OI)(CI)(RX)",
    "HOST\\SpeakLinkService:(OI)(CI)(R,W)",
)


# ---------------------------------------------------------------------------
# Directory policy: least privilege, per role
# ---------------------------------------------------------------------------
def test_the_four_directories_are_the_agreed_ones():
    assert APPLICATION_DIRECTORY == Path(r"C:\ProgramData\SpeakLink\app")
    assert KEY_DIRECTORY == Path(r"C:\ProgramData\SpeakLink\keys")
    assert DATA_DIRECTORY == Path(r"C:\ProgramData\SpeakLink\data")
    assert LOG_DIRECTORY == Path(r"C:\ProgramData\SpeakLink\logs")


def test_none_of_them_sits_in_a_personal_profile():
    """The pilot ran from a Desktop path. A production host must not."""
    for directory in (APPLICATION_DIRECTORY, KEY_DIRECTORY, DATA_DIRECTORY, LOG_DIRECTORY):
        lowered = str(directory).lower()
        assert "desktop" not in lowered
        assert "users\\" not in lowered
        assert REPOSITORY_ROOT not in directory.parents


@pytest.mark.parametrize("role", [DirectoryRole.KEYS, DirectoryRole.DATA, DirectoryRole.LOGS])
def test_writable_roles_accept_read_write(role: DirectoryRole):
    verdict = verify_directory_acl(HARDENED_RW, role=role, service_account=SERVICE)
    assert verdict.acceptable is True, verdict.problems


def test_the_application_directory_is_read_and_execute_only():
    """The service runs the code; it must not be able to rewrite it. A service
    that can edit its own binaries turns any code-execution bug into
    persistence."""
    verdict = verify_directory_acl(HARDENED_RX, role=DirectoryRole.APPLICATION, service_account=SERVICE)
    assert verdict.acceptable is True, verdict.problems

    writable = verify_directory_acl(HARDENED_RW, role=DirectoryRole.APPLICATION, service_account=SERVICE)
    assert writable.acceptable is False
    assert any("write" in problem.lower() for problem in writable.problems)


@pytest.mark.parametrize(
    "role", [DirectoryRole.APPLICATION, DirectoryRole.KEYS, DirectoryRole.DATA, DirectoryRole.LOGS]
)
def test_inherited_user_access_is_rejected_everywhere(role: DirectoryRole):
    verdict = verify_directory_acl(WITH_USERS, role=role, service_account=SERVICE)
    assert verdict.acceptable is False
    assert any("Users" in problem for problem in verdict.problems)


@pytest.mark.parametrize(
    "role", [DirectoryRole.APPLICATION, DirectoryRole.KEYS, DirectoryRole.DATA, DirectoryRole.LOGS]
)
def test_administrators_keep_recovery_access_everywhere(role: DirectoryRole):
    stripped = {k: v for k, v in HARDENED_RW.items() if "Administrators" not in k}
    verdict = verify_directory_acl(stripped, role=role, service_account=SERVICE)
    assert verdict.acceptable is False
    assert any("Administrators" in problem for problem in verdict.problems)


def test_the_service_never_gets_full_control_on_the_key_directory():
    full = _acl(
        "NT AUTHORITY\\SYSTEM:(OI)(CI)(F)",
        "BUILTIN\\Administrators:(OI)(CI)(F)",
        "HOST\\SpeakLinkService:(OI)(CI)(F)",
    )
    verdict = verify_directory_acl(full, role=DirectoryRole.KEYS, service_account=SERVICE)
    assert verdict.acceptable is False
    assert any("full control" in problem.lower() for problem in verdict.problems)


def test_every_grant_plan_breaks_inheritance_first():
    for role in DirectoryRole:
        plan = directory_grant_plan(role, service_account=SERVICE)
        assert "/inheritance:r" in plan[0], f"{role} grants before breaking inheritance"


def test_no_grant_plan_ever_mentions_users_or_everyone():
    for role in DirectoryRole:
        joined = " ".join(directory_grant_plan(role, service_account=SERVICE))
        for forbidden in ("Users:", "Everyone:", "Authenticated Users:"):
            assert forbidden not in joined


# ---------------------------------------------------------------------------
# The installer script itself
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def installer() -> str:
    assert INSTALLER.exists(), "the installer script is missing"
    return INSTALLER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def installer_code(installer: str) -> str:
    """The script with its comment-based help and comments stripped.

    Prose mentions icacls and Set-LocalUser legitimately; only executable lines
    should be judged.
    """
    without_help = re.sub(r"<#.*?#>", "", installer, flags=re.DOTALL)
    return "\n".join(re.sub(r"#.*$", "", line) for line in without_help.splitlines())


def test_whatifonly_is_handled_before_elevation_is_required(installer_code: str):
    """The one mode meant for looking must not require the rights meant for
    changing.

    Compared against the CALL site, not the function definition: Assert-Elevated
    is naturally declared near the top and that says nothing about when it runs.
    """
    whatif_exit = installer_code.index("if ($WhatIfOnly)")
    calls = [
        match.start()
        for match in re.finditer(r"^\s*Assert-Elevated\s*$", installer_code, re.MULTILINE)
    ]
    assert calls, "Assert-Elevated is never called"
    assert min(calls) > whatif_exit, (
        "elevation is asserted before -WhatIfOnly exits, so a dry run cannot be "
        "performed by someone who is only looking"
    )


def test_a_real_installation_still_requires_elevation(installer: str):
    assert "Assert-Elevated" in installer
    assert "IsInRole" in installer


def test_no_password_is_generated_and_discarded(installer: str):
    """An account whose password nobody knows cannot be configured to log on,
    so the installer would leave something that looks finished and is not."""
    for generator in ("RNGCryptoServiceProvider", "GetBytes", "ToBase64String"):
        assert generator not in installer, (
            f"the installer still generates a password ({generator}); accept a "
            "credential from the administrator instead"
        )


def test_the_credential_is_supplied_by_the_administrator(installer: str):
    assert "PSCredential" in installer or "SecureString" in installer
    assert "Read-Host" in installer and "-AsSecureString" in installer


def test_the_password_is_never_printed_or_written(installer: str):
    lowered = installer.lower()
    for leak in ("write-output $password", "write-host $password", "out-file", "set-content $password"):
        assert leak not in lowered


def test_an_existing_account_password_is_never_reset(installer_code: str):
    """Resetting it would break a scheduled task already configured with the
    old one. Telling the operator HOW to change it deliberately is fine, and the
    script does that - inside a Write-Output, which is not an execution."""
    executions = [
        line.strip()
        for line in installer_code.splitlines()
        if "Set-LocalUser" in line and "Write-Output" not in line and "Write-Host" not in line
    ]
    assert not executions, f"the installer actually runs Set-LocalUser: {executions}"


def test_the_operator_is_told_how_to_change_it_deliberately(installer: str):
    assert "Set-LocalUser" in installer
    assert "-AsSecureString" in installer


def test_the_installer_is_idempotent_about_the_account(installer: str):
    assert "Get-LocalUser" in installer
    assert "already exists" in installer


def test_all_four_directories_are_prepared(installer_code: str):
    """Built with Join-Path from a single root, so look for the leaf names in
    the directory table rather than for a hard-coded path."""
    for directory in ("app", "keys", "data", "logs"):
        assert re.search(rf"Join-Path \$Root '{directory}'", installer_code), (
            f"the {directory} directory is not prepared"
        )


def test_each_directory_carries_the_right_it_should(installer_code: str):
    table = installer_code[installer_code.index("$directories = @("):]
    table = table[: table.index("\n)")]
    assert re.search(r"Name = 'app';.*Rights = 'RX'", table), (
        "the application directory is not read-and-execute only; a service able "
        "to rewrite its own code turns any code-execution bug into persistence"
    )
    for writable in ("keys", "data", "logs"):
        assert re.search(rf"Name = '{writable}';.*Rights = 'R,W'", table), (
            f"the {writable} directory does not grant read/write"
        )
    assert "Rights = 'F'" not in table, "a directory grants the service Full Control"


def test_the_directory_table_is_not_a_dictionary_of_dictionaries(installer_code: str):
    """PowerShell member enumeration turns $table.Keys on a dictionary of
    dictionaries into the INNER keys. That made every path interpolate as empty
    and would have run `icacls ""` under elevation. An array of objects has no
    such trap."""
    assert "$directories = @(" in installer_code
    assert "[ordered]@{" not in installer_code
    assert "$directories.Keys" not in installer_code


def test_acls_are_verified_rather_than_assumed(installer_code: str):
    """Running icacls and printing 'Done' proves nothing about the result."""
    assert "verify_directory_acl" in installer_code
    assert "Test-SpeakLinkDirectoryAcl -Path" in installer_code, (
        "the installer never reads back the ACL it applied"
    )


def test_no_service_is_configured_while_password_custody_is_unresolved(installer: str):
    for premature in ("New-Service", "sc.exe create", "Register-ScheduledTask"):
        assert premature not in installer, (
            f"the installer configures {premature} before Log On As A Service "
            "rights and password custody are resolved"
        )


def test_uninstall_guidance_is_present(installer: str):
    lowered = installer.lower()
    assert "uninstall" in lowered or "remove-localuser" in lowered


def test_the_installer_quotes_every_path_it_passes_to_icacls(installer_code: str):
    """Only executable lines: the help text says "Running icacls and printing",
    which is prose, not a command."""
    for match in re.finditer(r"^\s*&?\s*icacls\s+(\S+)", installer_code, re.MULTILINE):
        argument = match.group(1)
        assert argument.startswith('"') or argument.startswith("$"), (
            f"unquoted icacls path: {argument}"
        )


def test_no_password_reaches_a_child_process_command_line(installer_code: str):
    """Anyone on the host can read another process's command line."""
    for line in installer_code.splitlines():
        if "New-LocalUser" in line or "icacls" in line:
            assert "$Credential.Password" not in line
            assert "ConvertFrom-SecureString" not in line
