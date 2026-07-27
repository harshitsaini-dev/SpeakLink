"""The key container's ACL is the other half of DPAPI.

DPAPI with CURRENT_USER scope means only the account that sealed the container
can open it. That is the decision recorded for EchoCast: one Windows server, one
dedicated local account `EchoCastService`, CURRENT_USER scope under that account,
and the container at `C:\\ProgramData\\EchoCast-AI\\keys\\receiver-hmac-keys.bin`.

DPAPI alone is not the whole control. A file any user can read is still a file
any user can copy, and a copy plus a future weakness is a problem you cannot
take back. So the ACL has to say the same thing the encryption does:
`EchoCastService` reads and writes, Administrators keep recovery access, and
nobody else appears at all - which also means inherited permissions must be off,
because `C:\\ProgramData` grants Users by default.

This file tests the part that is testable anywhere: reading an ACL, deciding
whether it is acceptable, and refusing clearly when it is not. Creating the
account and applying the ACL both need elevation, so they live in an operator
script and are never claimed here as done.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from key_custody_acl import (  # noqa: E402
    SERVICE_ACCOUNT,
    SERVICE_CONTAINER_PATH,
    AclVerdict,
    icacls_grant_plan,
    parse_icacls,
    verify_acl,
)


# Real `icacls` output shapes. The trailing summary line is included because a
# parser that chokes on it fails only in production.
HARDENED = r"""C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin NT AUTHORITY\SYSTEM:(F)
                                                                    BUILTIN\Administrators:(F)
                                                                    HARSHIT-DESKTOP\EchoCastService:(R,W)

Successfully processed 1 files; Failed processing 0 files"""

INHERITED_FROM_PROGRAMDATA = r"""C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin NT AUTHORITY\SYSTEM:(I)(F)
                                                                    BUILTIN\Administrators:(I)(F)
                                                                    BUILTIN\Users:(I)(RX)
                                                                    HARSHIT-DESKTOP\EchoCastService:(R,W)

Successfully processed 1 files; Failed processing 0 files"""

MISSING_SERVICE = r"""C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin NT AUTHORITY\SYSTEM:(F)
                                                                    BUILTIN\Administrators:(F)

Successfully processed 1 files; Failed processing 0 files"""

EVERYONE_CAN_READ = r"""C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin NT AUTHORITY\SYSTEM:(F)
                                                                    BUILTIN\Administrators:(F)
                                                                    Everyone:(R)
                                                                    HARSHIT-DESKTOP\EchoCastService:(R,W)

Successfully processed 1 files; Failed processing 0 files"""

SERVICE_HAS_FULL = r"""C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin NT AUTHORITY\SYSTEM:(F)
                                                                    BUILTIN\Administrators:(F)
                                                                    HARSHIT-DESKTOP\EchoCastService:(F)

Successfully processed 1 files; Failed processing 0 files"""


def _verdict(output: str, account: str = "HARSHIT-DESKTOP\\EchoCastService") -> AclVerdict:
    return verify_acl(parse_icacls(output), service_account=account)


# ---------------------------------------------------------------------------
# Reading an ACL
# ---------------------------------------------------------------------------
def test_a_hardened_acl_parses_into_its_three_principals():
    entries = parse_icacls(HARDENED)
    assert set(entries) == {
        "NT AUTHORITY\\SYSTEM",
        "BUILTIN\\Administrators",
        "HARSHIT-DESKTOP\\EchoCastService",
    }
    assert entries["HARSHIT-DESKTOP\\EchoCastService"].rights == {"R", "W"}


def test_inherited_entries_are_recognised_as_inherited():
    entries = parse_icacls(INHERITED_FROM_PROGRAMDATA)
    assert entries["BUILTIN\\Users"].inherited is True
    assert entries["HARSHIT-DESKTOP\\EchoCastService"].inherited is False


def test_the_icacls_summary_line_is_not_mistaken_for_an_entry():
    entries = parse_icacls(HARDENED)
    assert not any("Successfully processed" in name for name in entries)


def test_empty_output_parses_to_nothing_rather_than_crashing():
    assert parse_icacls("") == {}


# ---------------------------------------------------------------------------
# Judging an ACL
# ---------------------------------------------------------------------------
def test_the_hardened_acl_is_accepted():
    verdict = _verdict(HARDENED)
    assert verdict.acceptable is True
    assert verdict.problems == []


def test_inherited_user_access_is_rejected():
    """C:\\ProgramData grants BUILTIN\\Users by default, so inheritance must be
    broken or the key is world-readable on the host."""
    verdict = _verdict(INHERITED_FROM_PROGRAMDATA)
    assert verdict.acceptable is False
    assert any("Users" in problem for problem in verdict.problems)
    assert any("inherit" in problem.lower() for problem in verdict.problems)


def test_a_missing_service_account_is_rejected():
    verdict = _verdict(MISSING_SERVICE)
    assert verdict.acceptable is False
    assert any("EchoCastService" in problem for problem in verdict.problems)


def test_any_unexpected_principal_is_rejected():
    verdict = _verdict(EVERYONE_CAN_READ)
    assert verdict.acceptable is False
    assert any("Everyone" in problem for problem in verdict.problems)


def test_the_service_account_having_full_control_is_rejected():
    """Read and write is all it needs. Full control lets the running service
    rewrite its own ACL, which removes the point of having one."""
    verdict = _verdict(SERVICE_HAS_FULL)
    assert verdict.acceptable is False
    assert any("full control" in problem.lower() for problem in verdict.problems)


def test_administrators_keep_recovery_access():
    """The ADR requires an explicitly approved recovery path. An ACL that locks
    Administrators out would strand the key."""
    without_admins = HARDENED.replace("BUILTIN\\Administrators:(F)\n", "")
    verdict = _verdict(without_admins)
    assert verdict.acceptable is False
    assert any("Administrators" in problem for problem in verdict.problems)


def test_the_verdict_never_contains_key_material():
    verdict = _verdict(INHERITED_FROM_PROGRAMDATA)
    rendered = " ".join(verdict.problems) + repr(verdict)
    assert "receiver-hmac-keys" in rendered or True  # path is fine; key bytes are not
    for forbidden in ("BEGIN", "-----", "=="):
        assert forbidden not in rendered


# ---------------------------------------------------------------------------
# The plan an operator runs
# ---------------------------------------------------------------------------
def test_the_grant_plan_breaks_inheritance_before_granting():
    """Granting first and removing inheritance afterwards leaves a window in
    which the key exists and Users can still read it."""
    plan = icacls_grant_plan(SERVICE_CONTAINER_PATH, service_account="HOST\\EchoCastService")
    joined = " || ".join(plan)
    assert plan[0].count("/inheritance:r") == 1, "inheritance is not broken first"
    assert joined.index("/inheritance:r") < joined.index("EchoCastService")


def test_the_grant_plan_gives_the_service_account_only_read_and_write():
    plan = " ".join(icacls_grant_plan(SERVICE_CONTAINER_PATH, service_account="HOST\\EchoCastService"))
    assert "EchoCastService:(R,W)" in plan
    assert "EchoCastService:(F)" not in plan


def test_the_grant_plan_keeps_administrators_and_system():
    plan = " ".join(icacls_grant_plan(SERVICE_CONTAINER_PATH, service_account="HOST\\EchoCastService"))
    assert "Administrators:(F)" in plan
    assert "SYSTEM:(F)" in plan


def test_the_grant_plan_never_grants_users_or_everyone():
    plan = " ".join(icacls_grant_plan(SERVICE_CONTAINER_PATH, service_account="HOST\\EchoCastService"))
    for forbidden in ("Users:", "Everyone:", "Authenticated Users:"):
        assert forbidden not in plan


def test_the_plan_quotes_the_path_because_it_contains_no_spaces_today_but_might():
    plan = icacls_grant_plan(Path(r"C:\Program Files\EchoCast\keys\k.bin"), service_account="HOST\\Svc")
    assert all('"C:\\Program Files\\EchoCast\\keys\\k.bin"' in step for step in plan)


# ---------------------------------------------------------------------------
# The agreed location
# ---------------------------------------------------------------------------
def test_the_container_path_matches_the_architecture_decision():
    assert SERVICE_CONTAINER_PATH == Path(r"C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin")


def test_the_container_path_is_outside_the_repository():
    assert REPOSITORY_ROOT not in SERVICE_CONTAINER_PATH.parents


def test_the_container_path_is_not_beside_the_database():
    from db import DB_PATH

    assert SERVICE_CONTAINER_PATH.parent != Path(DB_PATH).parent


def test_the_service_account_name_is_the_agreed_one():
    assert SERVICE_ACCOUNT == "EchoCastService"


# ---------------------------------------------------------------------------
# Multi-word authorities, which is where a naive parser breaks
# ---------------------------------------------------------------------------
def test_a_space_containing_authority_on_the_first_line_is_parsed():
    """Splitting the first line on its last space puts half of
    "NT AUTHORITY\\SYSTEM" into the path. It is the one shape guaranteed to
    appear on a real ACL."""
    entries = parse_icacls(HARDENED)
    assert "NT AUTHORITY\\SYSTEM" in entries
    assert entries["NT AUTHORITY\\SYSTEM"].rights == {"F"}


def test_a_bare_principal_without_a_domain_is_parsed():
    entries = parse_icacls(EVERYONE_CAN_READ)
    assert "Everyone" in entries
    assert entries["Everyone"].rights == {"R"}
