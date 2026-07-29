"""Where the persistent HQ server keeps its data, for ever, at one fixed path.

THE P0 THIS ENDS

``Start-EchoCastLanPilot.ps1`` builds its data root from the clock::

    $pilotRoot = ...\lan-pilot\$(Get-Date -Format 'yyyyMMdd-HHmmss')

and mints a fresh random ``lan-pilot-xxxxxx`` administrator to go with it. Every
start is therefore a brand-new, empty database. That is exactly right for a
throwaway pilot and exactly wrong for a daily server, and it produced both of
the failures that were reported as separate bugs:

* **the Store went OFFLINE after a restart** - the Device it enrolled as does
  not exist in the database the running backend now uses;
* **``owneradmin`` could not sign in** - that account lives in
  ``backend/echocast_live.db``, and the running backend was looking at a
  throwaway pilot file.

Neither was a Receiver bug and neither was an authentication bug. The server
forgot, and both symptoms follow from that.

Measured on this machine: eight pilot roots, each with a different generated
administrator, and the Store's working Device sitting in one of the older ones.

WHAT THIS MODULE IS

Path resolution and mode identification only. It creates nothing, opens no
database and starts no process - the PowerShell scripts do that, and they ask
here so that they cannot disagree about where the data lives.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


#: Shown in the API and the dashboard so nobody has to guess which server they
#: are looking at. A persistent HQ and a throwaway pilot render identically
#: otherwise, and that is how an operator ends up enrolling a Store into a
#: database that will be gone tomorrow.
PERSISTENT_MARKER = "PERSISTENT LAN SERVER"
THROWAWAY_MARKER = "THROWAWAY PILOT"

#: Fixed. Deliberately no date, no run id, nothing that changes between starts.
PERSISTENT_ROOT_NAME = "persistent-lan-server"

#: Database filenames that belong to something designed to be discarded. A
#: persistent server pointed at one of these would adopt data that was never
#: meant to survive, and would look like it was working right up until the
#: folder was cleaned up.
_THROWAWAY_FILENAMES = frozenset({
    "lan-pilot.db",
    "staging.db",
    "manual-ui.db",
    "echocast_local_pilot.db",
})

#: Directory names that mark a throwaway tree even when the file inside is
#: named something else.
_THROWAWAY_PARENTS = frozenset({"lan-pilot", "manual-tests", "local-pilot"})

_FOLDERS = ("data", "config", "keys", "logs", "backups", "runtime",
            "migration-reports")


class PersistentServerError(RuntimeError):
    """The persistent server refused. Never carries a secret."""


def resolve_persistent_root() -> Path:
    """The one fixed data root. Same answer on every call, every day."""
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / "AppData" / "Local"
    return base / "EchoCast-AI" / PERSISTENT_ROOT_NAME


def is_throwaway_pilot_database(path) -> bool:
    """Is this file part of something designed to be thrown away?

    Checked by filename *and* by the directory it sits in, because a pilot tree
    can hold a file with any name. Fails towards caution: anything it recognises
    is treated as disposable, and the persistent server refuses to adopt it.
    """
    path = Path(path)
    if path.name.lower() in _THROWAWAY_FILENAMES:
        return True
    return any(part.lower() in _THROWAWAY_PARENTS for part in path.parts)


def server_mode(database_path) -> str:
    """Which kind of server a given database belongs to."""
    database_path = Path(database_path)
    try:
        persistent = ServerProfile.persistent().database
    except Exception:  # pragma: no cover - resolution cannot realistically fail
        return THROWAWAY_MARKER
    if database_path == persistent:
        return PERSISTENT_MARKER
    if is_throwaway_pilot_database(database_path):
        return THROWAWAY_MARKER
    # Anything else - the protected database, a copy, an operator's own path -
    # is not claimed either way rather than guessed at.
    return "UNKNOWN SERVER PROFILE"


@dataclass(frozen=True)
class ServerProfile:
    """Every stable path the persistent server uses."""

    root: Path
    mode: str

    @classmethod
    def persistent(cls) -> "ServerProfile":
        return cls(root=resolve_persistent_root(), mode=PERSISTENT_MARKER)

    def folder(self, name: str) -> Path:
        if name not in _FOLDERS:
            raise PersistentServerError(f"{name!r} is not part of the server layout.")
        return self.root / name

    @property
    def database(self) -> Path:
        return self.root / "data" / "echocast.db"

    @property
    def key_container(self) -> Path:
        return self.root / "keys" / "receiver-hmac-keys.bin"

    @property
    def config(self) -> Path:
        return self.root / "config" / "server.json"

    @property
    def lock(self) -> Path:
        return self.root / "runtime" / "server.lock"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    def require_not_throwaway(self, database_path) -> None:
        """Refuse to treat a disposable database as the permanent one."""
        if is_throwaway_pilot_database(database_path):
            raise PersistentServerError(
                f"{Path(database_path).name} belongs to a throwaway pilot, which is "
                "rebuilt from scratch on every start. Adopting it would make the "
                "persistent server forget everything the next time somebody tidies "
                "that folder. Run Initialize-EchoCastPersistentLanServer.ps1 and "
                "name a real source database instead."
            )


def describe_profile(profile: ServerProfile) -> str:
    """What a technician reads. Paths only - no key, no secret, no password."""
    return "\n".join([
        f"mode        : {profile.mode}",
        f"root        : {profile.root}",
        f"database    : {profile.database}",
        f"keys        : {profile.key_container.parent}",
        f"logs        : {profile.logs}",
        f"backups     : {profile.backups}",
        f"lock        : {profile.lock}",
        "",
        "This root is fixed. It does not change between restarts, which is the",
        "whole difference from the throwaway pilot.",
    ])
