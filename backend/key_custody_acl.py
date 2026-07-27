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
from pathlib import Path


SERVICE_ACCOUNT = "EchoCastService"
SERVICE_CONTAINER_PATH = Path(r"C:\ProgramData\EchoCast-AI\keys\receiver-hmac-keys.bin")

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
