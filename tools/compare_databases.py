"""Compare candidate databases before choosing one to keep for ever.

    python tools/compare_databases.py <db> <db> [<db> ...]

Three candidates exist on this machine and each holds something the others do
not:

* the repository database   - ``admin``, ``owneradmin``, the real Stores, history
* the newest pilot database - the Stores an operator created most recently
* an older pilot database   - the Receiver Device that last played audible sound

Choosing between them from memory, by hand, at the end of a long day, is how a
Store's identity gets thrown away. This gives the same answer every time.

**It reads and only reads.** Every database is opened ``mode=ro``, no password
hash or Device credential is ever selected, and nothing is written anywhere.

**It recommends but does not decide.** A tool that silently picks is a tool that
picks wrong at six in the evening.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.persistent_lan_server import is_throwaway_pilot_database  # noqa: E402


@dataclass
class CandidateReport:
    """Everything safe to know about one database."""

    path: Path
    readable: bool = False
    error: str = ""
    size: int = 0
    sha256: str = ""
    integrity: str = ""
    throwaway: bool = False
    users: int = 0
    stores: int = 0
    devices: int = 0
    sessions: int = 0
    logs: int = 0
    #: (username, role). Never a password hash.
    accounts: list = field(default_factory=list)
    #: (public_id prefix, display name, status). Never a credential.
    devices_detail: list = field(default_factory=list)

    @property
    def history(self) -> int:
        return self.sessions + self.logs


@dataclass
class Comparison:
    candidates: list
    recommended: "CandidateReport | None"
    devices_elsewhere: list


def inspect_database(path) -> CandidateReport:
    """Read one candidate. Never raises for a bad file - it reports."""
    path = Path(path)
    report = CandidateReport(path=path)
    if not path.exists():
        report.error = "file does not exist"
        return report

    report.size = path.stat().st_size
    report.sha256 = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    report.throwaway = is_throwaway_pilot_database(path)

    try:
        # Immutable as well as read-only: it stops SQLite creating a -wal or
        # -shm beside a database we were only asked to look at.
        con = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as failure:
        report.error = failure.__class__.__name__
        return report

    try:
        try:
            report.integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        except sqlite3.Error as failure:
            report.error = f"not a readable database ({failure.__class__.__name__})"
            return report

        report.readable = True

        def count(sql):
            try:
                return int(con.execute(sql).fetchone()[0])
            except sqlite3.Error:
                return 0

        report.users = count("SELECT COUNT(*) FROM hq_users")
        report.stores = count("SELECT COUNT(*) FROM stores")
        report.devices = count("SELECT COUNT(*) FROM receiver_devices")
        report.sessions = count("SELECT COUNT(*) FROM broadcast_sessions")
        report.logs = count("SELECT COUNT(*) FROM system_logs")

        # Columns named explicitly. SELECT * here would eventually publish a
        # password hash the day somebody adds one to a query.
        try:
            report.accounts = [
                (row[0], row[1]) for row in
                con.execute("SELECT username, role FROM hq_users ORDER BY id")
            ]
        except sqlite3.Error:
            report.accounts = []
        try:
            report.devices_detail = [
                (str(row[0])[:8], row[1], row[2]) for row in
                con.execute("SELECT public_id, display_name, status"
                            " FROM receiver_devices ORDER BY id")
            ]
        except sqlite3.Error:
            report.devices_detail = []
    finally:
        con.close()
    return report


def compare(paths) -> Comparison:
    """Inspect every candidate and suggest one, deterministically."""
    candidates = [inspect_database(path) for path in paths]

    # A throwaway is never recommended: it is rebuilt from scratch on every
    # start, which is the defect this whole exercise exists to end. Among the
    # rest, the one carrying the most operational history wins - ties broken by
    # path so two runs never disagree.
    keepable = [c for c in candidates if c.readable and not c.throwaway]
    recommended = None
    if keepable:
        recommended = sorted(keepable, key=lambda c: (-c.history, -c.users, str(c.path)))[0]

    devices_elsewhere = [
        c for c in candidates
        if c.readable and c.devices > 0 and (recommended is None or c.path != recommended.path)
    ]
    return Comparison(candidates=candidates, recommended=recommended,
                      devices_elsewhere=devices_elsewhere)


def describe_comparison(result: Comparison) -> str:
    lines = [
        "SpeakLink database comparison",
        "============================",
        "Read-only. Nothing has been changed, copied, moved or deleted.",
        "",
    ]

    for candidate in result.candidates:
        lines.append(f"{candidate.path}")
        if not candidate.readable:
            lines.append(f"    UNREADABLE - {candidate.error}")
            lines.append("")
            continue
        kind = "THROWAWAY PILOT (rebuilt on every start)" if candidate.throwaway else "keepable"
        lines += [
            f"    kind      : {kind}",
            f"    size/sha  : {candidate.size} / {candidate.sha256[:16]}...",
            f"    integrity : {candidate.integrity}",
            f"    counts    : {candidate.users} user(s), {candidate.stores} Store(s), "
            f"{candidate.devices} Device(s), {candidate.sessions} session(s), "
            f"{candidate.logs} log(s)",
        ]
        if candidate.accounts:
            listed = ", ".join(f"{name} ({role})" for name, role in candidate.accounts)
            lines.append(f"    accounts  : {listed}")
        if candidate.devices_detail:
            listed = ", ".join(f"{name} [{status}]" for _prefix, name, status
                               in candidate.devices_detail)
            lines.append(f"    Devices   : {listed}")
        lines.append("")

    if result.recommended is None:
        lines += ["No keepable candidate was readable. Nothing can be recommended.", ""]
        return "\n".join(lines)

    lines += [
        "SUGGESTION",
        f"    Use {result.recommended.path}",
        "    It carries the most operational history and is not a throwaway.",
        "",
    ]

    if result.devices_elsewhere:
        lines += [
            "IMPORTANT - Receiver Devices live in a database you would NOT keep:",
        ]
        for candidate in result.devices_elsewhere:
            names = ", ".join(name for _prefix, name, _status in candidate.devices_detail)
            lines.append(f"    {candidate.path}")
            lines.append(f"        {names}")
        lines += [
            "",
            "    A Device's sealed credential is verified with the HMAC key ring of",
            "    the server that issued it. Copying Device rows into another database",
            "    without that key ring produces a Device that exists and can never",
            "    authenticate - which looks exactly like the Store being broken.",
            "",
            "    Unless key compatibility is proven, plan ONE final re-enrolment of",
            "    each Store into the persistent server. After that, a normal restart",
            "    never needs another one.",
            "",
        ]

    lines += [
        "This tool does not choose. You do.",
        "Nothing has been changed by running it.",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:  # pragma: no cover - thin CLI
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 1
    print(describe_comparison(compare([Path(a) for a in argv])))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
