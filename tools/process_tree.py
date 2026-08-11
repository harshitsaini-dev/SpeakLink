"""Work out which processes a pilot really owns, before anything is stopped.

Stopping a local pilot on Windows is not one process. ``yarn start`` becomes a
chain of ``cmd.exe`` and ``node.exe`` hops, and a venv ``python.exe`` launcher
spawns the base interpreter as a child. Stopping only the PID that was recorded
at launch leaves the process that actually holds the port running, while the
operator is told everything stopped.

This module contains only the decision: given a process table, which PIDs does
the recorded root own, and in what order should they be stopped. It never
enumerates, signals or terminates anything itself. The caller does that, so the
destructive step stays visible in the script the operator reads.

``backend/tests/test_process_tree.py`` drives these functions with the exact
tree observed during the one-Store Bluetooth amplifier live test.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass, field


class OwnershipRefused(Exception):
    """The recorded PID could not be proven to belong to this pilot.

    Raised instead of returning False so a caller cannot accidentally treat a
    refusal as "nothing to do" and carry on terminating things.
    """


@dataclass(frozen=True)
class ProcessEntry:
    """One row of a process table. ``created_at`` guards against PID reuse."""

    pid: int
    parent_pid: int
    name: str
    command_line: str
    created_at: str | None = None


@dataclass
class StopPlan:
    """Which PIDs to stop, deepest first, with the recorded root stopped last."""

    root_pid: int
    ordered_pids: list[int] = field(default_factory=list)


def _by_pid(table) -> dict[int, ProcessEntry]:
    return {entry.pid: entry for entry in table}


def descendant_pids(root_pid: int, table) -> set[int]:
    """Every process below ``root_pid``, at any depth.

    A breadth-first walk with a visited set, so a corrupt table that reports a
    parent cycle cannot hang the shutdown path. A process is never treated as
    its own descendant.
    """
    children: dict[int, list[int]] = {}
    for entry in table:
        children.setdefault(entry.parent_pid, []).append(entry.pid)

    found: set[int] = set()
    queue = deque(children.get(root_pid, []))
    while queue:
        pid = queue.popleft()
        if pid == root_pid or pid in found:
            continue
        found.add(pid)
        queue.extend(children.get(pid, []))
    return found


def verify_ownership(
    pid: int,
    table,
    markers,
    expected_created_at: str | None = None,
) -> bool:
    """Prove the recorded PID is this pilot's process, or refuse.

    Identity comes from the command line, never the process name: ``cmd.exe``
    and ``node.exe`` are far too common, and matching on a name is how an
    unrelated process gets killed.

    ``expected_created_at`` closes the PID-reuse hole. Windows recycles PIDs, so
    a recorded number can later belong to something else entirely. It is
    optional because PID files written before this fix hold only the number.
    """
    entry = _by_pid(table).get(pid)
    if entry is None:
        raise OwnershipRefused(
            f"process {pid} is not running; refusing to act on a stale PID"
        )

    command_line = entry.command_line or ""
    if not any(marker in command_line for marker in markers):
        raise OwnershipRefused(
            f"process {pid} ({entry.name}) does not look like this pilot: its "
            f"command line matches none of {list(markers)}. Refusing to stop it."
        )

    if expected_created_at is not None and entry.created_at != expected_created_at:
        raise OwnershipRefused(
            f"process {pid} started at {entry.created_at!r}, but this pilot "
            f"recorded {expected_created_at!r}. The PID has been reused by a "
            "different process. Refusing to stop it."
        )

    return True


def plan_stop(root_pid: int, table, markers, expected_created_at: str | None = None) -> StopPlan:
    """Order the owned tree for shutdown: deepest descendants first, root last.

    Children go before parents so a supervisor cannot notice a dead child and
    restart it, and so the process holding the port releases it before its
    parent disappears and orphans it.
    """
    verify_ownership(root_pid, table, markers, expected_created_at)

    parents = {entry.pid: entry.parent_pid for entry in table}
    owned = descendant_pids(root_pid, table)

    def depth(pid: int) -> int:
        steps = 0
        current = pid
        seen = {pid}
        while current != root_pid and steps < len(table) + 1:
            current = parents.get(current, root_pid)
            if current in seen:
                break
            seen.add(current)
            steps += 1
        return steps

    # Deepest first; pid as a stable tie-break so the plan is reproducible.
    ordered = sorted(owned, key=lambda pid: (-depth(pid), pid))
    ordered.append(root_pid)
    return StopPlan(root_pid=root_pid, ordered_pids=ordered)


# ---------------------------------------------------------------------------
# CLI: PowerShell hands in the process table, this prints the plan
# ---------------------------------------------------------------------------
def _entries_from_json(payload: str) -> list[ProcessEntry]:
    # PowerShell prefixes a UTF-8 BOM when it pipes text to a native command,
    # and json.loads refuses it. Found by dry-running the planner against a real
    # process table: every unit test passed while the live pipe raised
    # "Unexpected UTF-8 BOM" - at shutdown, which is the worst moment to fail.
    rows = json.loads(payload.lstrip("﻿").strip())
    if isinstance(rows, dict):
        rows = [rows]
    entries = []
    for row in rows:
        entries.append(
            ProcessEntry(
                pid=int(row["ProcessId"]),
                parent_pid=int(row.get("ParentProcessId") or 0),
                name=str(row.get("Name") or ""),
                command_line=str(row.get("CommandLine") or ""),
                created_at=(str(row["CreationDate"]) if row.get("CreationDate") else None),
            )
        )
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="process_tree",
        description=(
            "Read a Win32_Process table as JSON on stdin and print the PIDs to "
            "stop, one per line, deepest descendant first and the recorded root "
            "last. Prints nothing and exits non-zero if ownership is refused."
        ),
    )
    parser.add_argument("--root", type=int, required=True, help="the recorded pilot PID")
    parser.add_argument(
        "--marker",
        action="append",
        required=True,
        help="a command-line fragment that proves ownership; repeatable",
    )
    parser.add_argument(
        "--created-at",
        default=None,
        help="the creation timestamp recorded at launch, to detect PID reuse",
    )
    arguments = parser.parse_args(argv)

    table = _entries_from_json(sys.stdin.read())
    try:
        plan = plan_stop(arguments.root, table, arguments.marker, arguments.created_at)
    except OwnershipRefused as refusal:
        print(f"OWNERSHIP_REFUSED: {refusal}", file=sys.stderr)
        return 2

    for pid in plan.ordered_pids:
        print(pid)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
