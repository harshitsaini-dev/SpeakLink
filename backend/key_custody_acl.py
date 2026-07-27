"""Reading and judging the ACL on the Receiver HMAC key container.

DPAPI with CURRENT_USER scope means only the account that sealed the container
can open it. That is the recorded decision: one Windows server, one dedicated
local account ``EchoCastService``, CURRENT_USER scope under that account, and the
container at ``C:\\ProgramData\\EchoCast-AI\\keys\\receiver-hmac-keys.bin``.

Encryption is not the whole control. A file any user can read is a file any user
can copy, and a copy plus a future weakness is a problem nobody can take back.
So the ACL has to say what the encryption says: the service account reads and
writes, Administrators keep the recovery access the ADR requires, and nobody
else appears at all.

That last part is the one people miss. ``C:\\ProgramData`` grants
``BUILTIN\\Users`` read access by inheritance, so a container created there is
readable by every account on the host until inheritance is broken.

This module only *reads and judges*. Creating the account and applying the ACL
need elevation and live in ``scripts/Install-EchoCastServiceIdentity.ps1``.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


SERVICE_ACCOUNT = "EchoCastService"

#: The production layout, outside any personal profile. The pilot ran from a
#: Desktop path; a host that several people can reach must not.
ECHOCAST_ROOT = Path(r"C:\ProgramData\EchoCast-AI")
APPLICATION_DIRECTORY = ECHOCAST_ROOT / "app"
KEY_DIRECTORY = ECHOCAST_ROOT / "keys"
DATA_DIRECTORY = ECHOCAST_ROOT / "data"
LOG_DIRECTORY = ECHOCAST_ROOT / "logs"

SERVICE_CONTAINER_PATH = KEY_DIRECTORY / "receiver-hmac-keys.bin"


class DirectoryRole(Enum):
    """What the service is allowed to do in each directory.

    The application directory is read-and-execute only on purpose: a service
    that can rewrite its own code turns any code-execution bug into
    persistence. Everywhere else it needs to write, so it gets exactly that and
    never Full Control, which would let it edit its own ACL.
    """

    APPLICATION = "application"
    KEYS = "keys"
    DATA = "data"
    LOGS = "logs"


DIRECTORY_PATHS = {
    DirectoryRole.APPLICATION: APPLICATION_DIRECTORY,
    DirectoryRole.KEYS: KEY_DIRECTORY,
    DirectoryRole.DATA: DATA_DIRECTORY,
    DirectoryRole.LOGS: LOG_DIRECTORY,
}

#: icacls right sets per role, and the human wording used in refusals.
ROLE_RIGHTS = {
    DirectoryRole.APPLICATION: ("RX", {"RX"}),
    DirectoryRole.KEYS: ("R,W", {"R", "W"}),
    DirectoryRole.DATA: ("R,W", {"R", "W"}),
    DirectoryRole.LOGS: ("R,W", {"R", "W"}),
}

#: Principals that may appear on the container, and the rights they may hold.
#: Anything else is a finding, not a preference.
ALLOWED_SYSTEM = "NT AUTHORITY\\SYSTEM"
ALLOWED_ADMINS = "BUILTIN\\Administrators"

#: The service account needs to read the key and rewrite it on rotation.
#: Full control would let the running service edit its own ACL, which removes
#: the point of having one.
SERVICE_RIGHTS = {"R", "W"}

_ENTRY = re.compile(r"^(?P<principal>[^:]+):(?P<flags>.+)$")
_SUMMARY = re.compile(r"Successfully processed|Failed processing")

#: On the first line the path precedes the principal. Matches a principal that
#: is either a well-known space-containing authority, any DOMAIN\NAME, or a bare
#: name such as "Everyone".
_FIRST_LINE_PRINCIPAL = re.compile(
    r"\s((?:NT AUTHORITY|NT SERVICE|BUILTIN|CREATOR OWNER|[^\s:\\]+)\\[^:]+:\(|[^\s:\\]+:\()"
)


@dataclass(frozen=True)
class AclEntry:
    principal: str
    rights: set[str]
    inherited: bool


@dataclass
class AclVerdict:
    acceptable: bool
    problems: list[str] = field(default_factory=list)


def parse_icacls(output: str) -> dict[str, AclEntry]:
    """Turn ``icacls <file>`` output into principals and rights.

    The first line carries the path before the first principal; later lines are
    indented continuations. The trailing summary line is not an entry, and a
    parser that treats it as one fails only in production.
    """
    entries: dict[str, AclEntry] = {}
    for index, raw in enumerate(output.splitlines()):
        line = raw.strip()
        if not line or _SUMMARY.search(line):
            continue
        if index == 0:
            # "C:\path\file.bin PRINCIPAL:(F)". Splitting on the last space is
            # wrong: "NT AUTHORITY\SYSTEM" and "NT SERVICE\..." contain one, so
            # the well-known multi-word authorities have to be recognised.
            match = _FIRST_LINE_PRINCIPAL.search(line)
            if not match:
                continue
            line = line[match.start(1):].strip()

        match = _ENTRY.match(line)
        if not match:
            continue
        principal = match.group("principal").strip()
        flags = match.group("flags")
        inherited = "(I)" in flags
        rights = {token for token in re.findall(r"\(([^)]+)\)", flags) if token != "I"}
        # icacls writes combined rights as "(R,W)"; split them out.
        expanded: set[str] = set()
        for token in rights:
            expanded.update(part.strip() for part in token.split(",") if part.strip())
        entries[principal] = AclEntry(principal=principal, rights=expanded, inherited=inherited)
    return entries


def verify_acl(entries: dict[str, AclEntry], *, service_account: str) -> AclVerdict:
    """Decide whether this ACL protects the key. Every failure is named."""
    problems: list[str] = []

    service = entries.get(service_account)
    if service is None:
        problems.append(
            f"{service_account} has no access; the backend could not read its own key"
        )
    elif "F" in service.rights:
        problems.append(
            f"{service_account} has full control, which lets the running service "
            "rewrite this ACL; grant only (R,W)"
        )
    elif not SERVICE_RIGHTS <= service.rights:
        missing = sorted(SERVICE_RIGHTS - service.rights)
        problems.append(f"{service_account} is missing {missing}; it needs read and write")

    admins = entries.get(ALLOWED_ADMINS)
    if admins is None or "F" not in admins.rights:
        problems.append(
            f"{ALLOWED_ADMINS} has no full-control recovery access; the ADR requires "
            "an approved recovery path or the key can be stranded"
        )

    allowed = {ALLOWED_SYSTEM, ALLOWED_ADMINS, service_account}
    for principal, entry in entries.items():
        if principal in allowed:
            continue
        how = "inherited" if entry.inherited else "granted"
        problems.append(
            f"{principal} has {how} access to the key container and must not appear at all"
        )

    if any(entry.inherited for entry in entries.values()):
        problems.append(
            "inherited permissions are still enabled; C:\\ProgramData grants "
            "BUILTIN\\Users by default, so inheritance must be broken"
        )

    return AclVerdict(acceptable=not problems, problems=problems)


def icacls_grant_plan(path, *, service_account: str) -> list[str]:
    """The exact commands an elevated operator runs, in the order that is safe.

    Inheritance is broken **first**. Granting first and removing inheritance
    afterwards leaves a window in which the key exists on disk and every user on
    the host can still read it.
    """
    target = f'"{Path(path)}"'
    return [
        f"icacls {target} /inheritance:r",
        f'icacls {target} /grant "{ALLOWED_SYSTEM}:(F)"',
        f'icacls {target} /grant "{ALLOWED_ADMINS}:(F)"',
        f'icacls {target} /grant "{service_account}:(R,W)"',
    ]


def verify_directory_acl(
    entries: dict[str, AclEntry], *, role: DirectoryRole, service_account: str
) -> AclVerdict:
    """Judge one directory's ACL against what that role actually needs."""
    wording, required = ROLE_RIGHTS[role]
    problems: list[str] = []

    service = entries.get(service_account)
    if service is None:
        problems.append(f"{service_account} has no access to the {role.value} directory")
    elif "F" in service.rights:
        problems.append(
            f"{service_account} has full control of the {role.value} directory, which "
            "lets the running service rewrite this ACL; grant only "
            f"({wording})"
        )
    elif role is DirectoryRole.APPLICATION and "W" in service.rights:
        problems.append(
            f"{service_account} can write to the application directory; a service "
            "able to rewrite its own code turns any code-execution bug into "
            "persistence. Grant (RX)"
        )
    elif not required <= service.rights:
        missing = sorted(required - service.rights)
        problems.append(f"{service_account} is missing {missing} on the {role.value} directory")

    admins = entries.get(ALLOWED_ADMINS)
    if admins is None or "F" not in admins.rights:
        problems.append(
            f"{ALLOWED_ADMINS} has no full-control recovery access to the "
            f"{role.value} directory"
        )

    allowed = {ALLOWED_SYSTEM, ALLOWED_ADMINS, service_account}
    for principal, entry in entries.items():
        if principal in allowed:
            continue
        how = "inherited" if entry.inherited else "granted"
        problems.append(
            f"{principal} has {how} access to the {role.value} directory and must "
            "not appear at all"
        )

    if any(entry.inherited for entry in entries.values()):
        problems.append(
            f"inherited permissions are still enabled on the {role.value} directory; "
            "C:\\ProgramData grants BUILTIN\\Users by default"
        )

    return AclVerdict(acceptable=not problems, problems=problems)


def directory_grant_plan(role: DirectoryRole, *, service_account: str) -> list[str]:
    """The elevated commands for one directory, inheritance broken first."""
    target = f'"{DIRECTORY_PATHS[role]}"'
    wording, _ = ROLE_RIGHTS[role]
    return [
        f"icacls {target} /inheritance:r",
        f'icacls {target} /grant "{ALLOWED_SYSTEM}:(OI)(CI)(F)"',
        f'icacls {target} /grant "{ALLOWED_ADMINS}:(OI)(CI)(F)"',
        f'icacls {target} /grant "{service_account}:(OI)(CI)({wording})"',
    ]


def read_acl(path) -> dict[str, AclEntry]:
    """Ask Windows for the current ACL. Windows only, by definition."""
    if sys.platform != "win32":
        raise RuntimeError("ACL inspection requires Windows")
    completed = subprocess.run(
        ["icacls", str(Path(path))], capture_output=True, text=True, timeout=30
    )
    if completed.returncode != 0:
        raise RuntimeError(f"icacls failed: {completed.stderr.strip() or completed.returncode}")
    return parse_icacls(completed.stdout)


__all__ = [
    "ALLOWED_ADMINS",
    "ALLOWED_SYSTEM",
    "SERVICE_ACCOUNT",
    "SERVICE_CONTAINER_PATH",
    "SERVICE_RIGHTS",
    "AclEntry",
    "AclVerdict",
    "icacls_grant_plan",
    "parse_icacls",
    "read_acl",
    "verify_acl",
]
