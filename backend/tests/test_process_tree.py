"""Stopping a pilot must stop the whole owned process tree, and nothing else.

Found while shutting down the one-Store Bluetooth amplifier live test.
Stop-SpeakLinkLocalPilot.ps1 recorded the PID of the ``cmd.exe /c yarn start``
wrapper and stopped only that, then printed "Frontend : stopped." The real dev
server was five levels below it and kept port 3000 bound:

    7688  cmd.exe    /c yarn start            <- the only PID in frontend.pid
    +- 11884 node.exe   corepack yarn.js start
       +- 19904 cmd.exe    /d /s /c "craco start"
          +- 14304 node.exe   craco
             +- 2968  cmd.exe   /d /s /c "node ..."
                +- 16356 node.exe   <- actually listening on 3000

The same shape applies to the backend and the Receiver, where a venv
``python.exe`` launcher spawns the base interpreter as a child.

These tests drive pure functions over an injected process table. Nothing here
enumerates, signals or terminates a real process, and no port is opened.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools.process_tree import (  # noqa: E402
    ProcessEntry,
    OwnershipRefused,
    _entries_from_json,
    descendant_pids,
    plan_stop,
    verify_ownership,
)


# ---------------------------------------------------------------------------
# The exact tree observed during the live test, plus unrelated bystanders
# ---------------------------------------------------------------------------
FRONTEND_TREE = [
    ProcessEntry(7688, 1, "cmd.exe", r'"C:\Windows\system32\cmd.exe" /c yarn start', "2026-07-26T11:56:24"),
    ProcessEntry(5512, 7688, "conhost.exe", r"\??\C:\Windows\system32\conhost.exe 0x4", "2026-07-26T11:56:24"),
    ProcessEntry(11884, 7688, "node.exe", r'"node" "C:\Users\admin\AppData\Roaming\npm\node_modules\corepack\dist\yarn.js" start', "2026-07-26T11:56:25"),
    ProcessEntry(19904, 11884, "cmd.exe", r'cmd.exe /d /s /c "craco start"', "2026-07-26T11:56:26"),
    ProcessEntry(14304, 19904, "node.exe", r'"C:\Program Files\nodejs\node.exe" "...\node_modules\.bin\craco" start', "2026-07-26T11:56:27"),
    ProcessEntry(2968, 14304, "cmd.exe", r'cmd.exe /d /s /c "node ..."', "2026-07-26T11:56:28"),
    ProcessEntry(16356, 2968, "node.exe", r'"C:\Program Files\nodejs\node.exe" "...\scripts\start.js"', "2026-07-26T11:56:29"),
]

BYSTANDERS = [
    # A completely unrelated Node process the operator is using for other work.
    ProcessEntry(4242, 1, "node.exe", r'"C:\Program Files\nodejs\node.exe" some-other-project\server.js', "2026-07-26T09:00:00"),
    # An unrelated Python process. Killing by name would take this out.
    ProcessEntry(4243, 1, "python.exe", r'python.exe C:\work\unrelated_script.py', "2026-07-26T09:00:00"),
    # A second, unrelated yarn dev server. Name matching alone would take it out.
    ProcessEntry(4244, 1, "cmd.exe", r'"C:\Windows\system32\cmd.exe" /c yarn start', "2026-07-26T09:00:00"),
]

TABLE = FRONTEND_TREE + BYSTANDERS

FRONTEND_MARKERS = ("yarn", "craco", "react-scripts")


# ---------------------------------------------------------------------------
# The whole tree must be found
# ---------------------------------------------------------------------------
def test_every_descendant_of_the_recorded_pid_is_found():
    found = descendant_pids(7688, TABLE)
    assert found == {5512, 11884, 19904, 14304, 2968, 16356}, (
        "the listening dev server sits five levels below the recorded PID; "
        "missing it is exactly the defect this guards"
    )


def test_the_actual_listener_is_included():
    """16356 is the process that holds port 3000. It must never be left behind."""
    assert 16356 in descendant_pids(7688, TABLE)


def test_intermediate_cmd_and_node_processes_are_both_included():
    found = descendant_pids(7688, TABLE)
    assert {19904, 2968} <= found, "intermediate cmd.exe hops were skipped"
    assert {11884, 14304} <= found, "intermediate node.exe hops were skipped"


def test_unrelated_processes_are_never_included():
    found = descendant_pids(7688, TABLE)
    for stranger in (4242, 4243, 4244):
        assert stranger not in found, f"unrelated process {stranger} would have been stopped"


def test_a_leaf_process_has_no_descendants():
    assert descendant_pids(16356, TABLE) == set()


def test_a_parent_cycle_cannot_hang_the_walk():
    """A corrupt table must not send the walk into an infinite loop."""
    looped = [
        ProcessEntry(100, 101, "a.exe", "a", "t"),
        ProcessEntry(101, 100, "b.exe", "b", "t"),
    ]
    assert descendant_pids(100, looped) == {101}


def test_a_process_is_never_its_own_descendant():
    selfish = [ProcessEntry(200, 200, "a.exe", "a", "t")]
    assert descendant_pids(200, selfish) == set()


# ---------------------------------------------------------------------------
# Ownership must be proven before anything is signalled
# ---------------------------------------------------------------------------
def test_ownership_accepts_the_real_pilot_root():
    assert verify_ownership(7688, TABLE, FRONTEND_MARKERS) is True


def test_ownership_refuses_an_unrelated_node_process():
    with pytest.raises(OwnershipRefused):
        verify_ownership(4242, TABLE, FRONTEND_MARKERS)


def test_ownership_refuses_an_unrelated_python_process():
    with pytest.raises(OwnershipRefused):
        verify_ownership(4243, TABLE, FRONTEND_MARKERS)


def test_ownership_refuses_a_pid_that_is_not_running():
    with pytest.raises(OwnershipRefused):
        verify_ownership(999999, TABLE, FRONTEND_MARKERS)


def test_ownership_checks_the_command_line_not_the_process_name():
    """cmd.exe and node.exe are far too common to identify by name."""
    disguised = [ProcessEntry(300, 1, "node.exe", "node totally-unrelated.js", "t")]
    with pytest.raises(OwnershipRefused):
        verify_ownership(300, disguised, FRONTEND_MARKERS)


# ---------------------------------------------------------------------------
# PID reuse
# ---------------------------------------------------------------------------
def test_pid_reuse_is_detected_when_the_creation_time_moved():
    """Windows recycles PIDs. A recorded PID whose process started later than
    we recorded is a different process wearing the same number."""
    with pytest.raises(OwnershipRefused):
        verify_ownership(
            7688, TABLE, FRONTEND_MARKERS, expected_created_at="2026-07-26T11:00:00"
        )


def test_a_matching_creation_time_is_accepted():
    assert verify_ownership(
        7688, TABLE, FRONTEND_MARKERS, expected_created_at="2026-07-26T11:56:24"
    ) is True


def test_creation_time_check_is_optional_for_older_pid_files():
    """A PID file written before this fix records only the number."""
    assert verify_ownership(7688, TABLE, FRONTEND_MARKERS, expected_created_at=None) is True


# ---------------------------------------------------------------------------
# The stop order and the refusal set
# ---------------------------------------------------------------------------
def test_stop_plan_lists_children_before_their_parents():
    plan = plan_stop(7688, TABLE, FRONTEND_MARKERS)
    order = plan.ordered_pids
    assert order[-1] == 7688, "the recorded root must be stopped last"
    assert order.index(16356) < order.index(2968) < order.index(14304)
    assert order.index(14304) < order.index(19904) < order.index(11884)


def test_stop_plan_covers_the_whole_tree_exactly_once():
    plan = plan_stop(7688, TABLE, FRONTEND_MARKERS)
    assert sorted(plan.ordered_pids) == sorted({7688, 5512, 11884, 19904, 14304, 2968, 16356})
    assert len(plan.ordered_pids) == len(set(plan.ordered_pids))


def test_stop_plan_refuses_an_unowned_root_and_plans_nothing():
    with pytest.raises(OwnershipRefused):
        plan_stop(4242, TABLE, FRONTEND_MARKERS)


def test_stop_plan_never_targets_a_bystander():
    plan = plan_stop(7688, TABLE, FRONTEND_MARKERS)
    for stranger in (4242, 4243, 4244):
        assert stranger not in plan.ordered_pids


# ---------------------------------------------------------------------------
# Reading the table PowerShell actually sends
# ---------------------------------------------------------------------------
POWERSHELL_ROWS = (
    '[{"ProcessId":7688,"ParentProcessId":1,"Name":"cmd.exe",'
    '"CommandLine":"cmd.exe /c yarn start","CreationDate":"2026-07-26T11:56:24.0000000Z"},'
    '{"ProcessId":11884,"ParentProcessId":7688,"Name":"node.exe",'
    '"CommandLine":"node yarn.js start","CreationDate":"2026-07-26T11:56:25.0000000Z"}]'
)


def test_a_utf8_bom_from_powershell_is_tolerated():
    """PowerShell prefixes a BOM when piping to a native command. Every unit test
    passed while the live pipe failed with "Unexpected UTF-8 BOM" during a real
    shutdown, so the wire format is pinned here."""
    entries = _entries_from_json("﻿" + POWERSHELL_ROWS)
    assert [entry.pid for entry in entries] == [7688, 11884]


def test_a_plain_table_without_a_bom_still_parses():
    entries = _entries_from_json(POWERSHELL_ROWS)
    assert entries[0].command_line == "cmd.exe /c yarn start"
    assert entries[1].parent_pid == 7688


def test_a_single_row_is_accepted_as_an_object():
    """ConvertTo-Json emits a bare object, not an array, when there is one row."""
    entries = _entries_from_json(
        '{"ProcessId":5,"ParentProcessId":1,"Name":"a.exe","CommandLine":"a","CreationDate":null}'
    )
    assert len(entries) == 1 and entries[0].pid == 5


def test_a_missing_command_line_does_not_crash_the_parser():
    """Win32_Process returns a null CommandLine for processes we cannot read."""
    entries = _entries_from_json('[{"ProcessId":9,"ParentProcessId":1,"Name":"x.exe","CommandLine":null}]')
    assert entries[0].command_line == ""
    with pytest.raises(OwnershipRefused):
        verify_ownership(9, entries, FRONTEND_MARKERS)
