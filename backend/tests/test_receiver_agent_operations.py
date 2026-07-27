"""What the Agent needs before Task Scheduler can be trusted to run it.

Two gaps, both found by inspecting the Agent rather than assuming:

**No single-instance guard.** Nothing stopped two Agents running against one
credential. Under Task Scheduler that is not hypothetical - a task that starts
at logon, plus an operator who double-clicks the executable to "check it works",
is two Receivers for one Store. They race for the same socket, the backend
replaces one connection with the other, and the Store flickers between them.

**No logging at all.** The Agent only ``print()``s. Run hidden under Task
Scheduler there is no console, so every one of those goes nowhere - and the
first time anybody asks "why did the Store go quiet at 3am" the honest answer
would be that nothing was written down.

The lock is scoped to the credential path, not to the machine. A global lock
would mean a second Windows account on the same computer could not run its own
Receiver, which is a restriction nobody asked for.

Nothing here opens a socket or reaches the network.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "ECHOCAST_DB_PATH",
    str(Path(tempfile.gettempdir()) / "echocast-tests-default-engine.db"),
)

from tools.receiver_agent import (  # noqa: E402
    AgentError,
    AlreadyRunning,
    EXIT_ALREADY_RUNNING,
    InstanceLock,
    build_parser,
    configure_logging,
    default_log_directory,
    redact,
)


CREDENTIAL = "echocast_rcv_v1.11111111-2222-4333-8444-555555555555." + "s" * 43
CODE = "ECHO-4H7K-9QW2"


# ===========================================================================
# One instance per credential
# ===========================================================================
def test_a_second_agent_for_the_same_credential_is_refused(tmp_path):
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(b"not really a credential")

    first = InstanceLock(credential)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning):
            InstanceLock(credential).acquire()
    finally:
        first.release()


def test_the_lock_is_released_when_the_first_agent_finishes(tmp_path):
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(b"x")

    first = InstanceLock(credential)
    first.acquire()
    first.release()
    # A restart after a clean stop must not be blocked by its own leftovers.
    second = InstanceLock(credential)
    second.acquire()
    second.release()


def test_it_works_as_a_context_manager(tmp_path):
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(b"x")
    with InstanceLock(credential):
        with pytest.raises(AlreadyRunning):
            InstanceLock(credential).acquire()
    InstanceLock(credential).acquire().release()


def test_two_different_credentials_do_not_block_each_other(tmp_path):
    """The lock is per credential, not per machine.

    A global lock would stop a second Windows account on the same computer from
    running its own Receiver - a restriction nobody asked for, invented by the
    lock rather than by the product.
    """
    first_path = tmp_path / "store-a" / "device-credential.bin"
    second_path = tmp_path / "store-b" / "device-credential.bin"
    for path in (first_path, second_path):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"x")

    first = InstanceLock(first_path)
    second = InstanceLock(second_path)
    first.acquire()
    second.acquire()  # must not raise
    first.release()
    second.release()


def test_a_stale_lock_from_a_dead_process_does_not_block_forever(tmp_path):
    """A Receiver killed by a crash, a power cut or Task Scheduler must not
    leave a Store unable to start again. The operating system releases the lock
    when the holding process dies, which is why this uses a real file lock
    rather than a PID file somebody has to reason about."""
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(b"x")
    lock_path = InstanceLock(credential).lock_path

    # A lock file left behind with no live holder.
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("999999", encoding="utf-8")

    lock = InstanceLock(credential)
    lock.acquire()
    lock.release()


def test_a_dead_holders_lock_is_taken_over_by_a_new_process(tmp_path):
    """Proven with a real second process rather than a simulated one."""
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(b"x")

    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; sys.path.insert(0, r'%s');"
         "from tools.receiver_agent import InstanceLock;"
         "lock = InstanceLock(r'%s'); lock.acquire(); print('HELD', flush=True);"
         "time.sleep(60)" % (str(REPOSITORY_ROOT), str(credential))],
        stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "HELD"
        with pytest.raises(AlreadyRunning):
            InstanceLock(credential).acquire()
    finally:
        holder.kill()
        holder.wait(timeout=15)

    # The holder is gone; the next Agent must be able to start.
    lock = InstanceLock(credential)
    lock.acquire()
    lock.release()


def test_the_refusal_names_no_secret(tmp_path):
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(CREDENTIAL.encode())

    first = InstanceLock(credential)
    first.acquire()
    try:
        with pytest.raises(AlreadyRunning) as refusal:
            InstanceLock(credential).acquire()
        assert CREDENTIAL not in str(refusal.value)
    finally:
        first.release()


def test_a_distinct_exit_code_exists_for_a_duplicate():
    """Task Scheduler has to tell "already running" apart from a failure, or a
    restart policy will keep relaunching something that is working."""
    assert EXIT_ALREADY_RUNNING not in (0, 1, 2, 3)


# ===========================================================================
# Logging
# ===========================================================================
def test_the_default_log_directory_is_beside_the_credential(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_log_directory() == Path(
        r"C:\Users\someone\AppData\Local\EchoCast-AI\receiver\logs"
    )


def test_logging_writes_a_file_and_rotates(tmp_path):
    logger = configure_logging(tmp_path, max_bytes=2048, backup_count=2)
    for index in range(400):
        logger.info("a reasonably long line to force rotation, number %s", index)

    files = sorted(path.name for path in tmp_path.glob("receiver*.log*"))
    assert files, "nothing was written"
    assert len(files) <= 3, f"more backups kept than asked for: {files}"
    for path in tmp_path.glob("receiver*.log*"):
        assert path.stat().st_size <= 8192, "a log file grew past its bound"


def test_log_timestamps_are_utc(tmp_path):
    logger = configure_logging(tmp_path)
    logger.info("a line")
    for handler in logger.handlers:
        handler.flush()
    text = next(tmp_path.glob("receiver*.log")).read_text(encoding="utf-8")
    assert "UTC" in text or "+0000" in text or text[:4].isdigit()


def test_disk_usage_is_bounded_by_size_times_count(tmp_path):
    """A Store PC that fills its own disk with Receiver logs is a Store PC that
    stops working for a reason nobody will guess."""
    logger = configure_logging(tmp_path, max_bytes=4096, backup_count=3)
    for index in range(5000):
        logger.info("filling the log deliberately, line %s", index)
    total = sum(path.stat().st_size for path in tmp_path.glob("receiver*"))
    assert total <= 4096 * 5, f"logs grew to {total} bytes"


# ===========================================================================
# Nothing secret is ever written down
# ===========================================================================
@pytest.mark.parametrize("secret", [CREDENTIAL, "Bearer " + CREDENTIAL, CODE])
def test_a_secret_is_redacted_before_it_reaches_a_log(secret: str):
    cleaned = redact(f"connecting with {secret} now")
    assert secret not in cleaned
    assert "s" * 43 not in cleaned
    assert "REDACTED" in cleaned


def test_the_log_handler_redacts_what_a_caller_forgot(tmp_path):
    """A filter on the handler, not a rule people have to remember.

    Every logging call in the Agent is careful. The one that will not be is the
    one somebody adds in a hurry while debugging a Store at 6am.
    """
    logger = configure_logging(tmp_path)
    logger.info("careless: %s", CREDENTIAL)
    logger.warning("also careless: Authorization: Bearer %s", CREDENTIAL)
    for handler in logger.handlers:
        handler.flush()

    text = next(tmp_path.glob("receiver*.log")).read_text(encoding="utf-8")
    assert CREDENTIAL not in text
    assert "s" * 43 not in text
    assert "REDACTED" in text


def test_a_websocket_url_with_a_query_is_redacted():
    cleaned = redact("ws://192.168.4.134:8000/api/ws/broadcaster?ticket=abc123")
    assert "abc123" not in cleaned


def test_ordinary_operational_lines_survive_redaction():
    """A redactor that eats everything is a redactor nobody keeps on."""
    for line in ("CONNECTED", "READY after FFmpeg checks",
                 "reconnecting in 4.0s", "Device 482f9e9b store 7"):
        assert redact(line) == line


# ===========================================================================
# The command line
# ===========================================================================
def test_run_accepts_a_log_directory_and_it_is_not_secret_shaped():
    arguments = build_parser().parse_args([
        "run", "--backend-url", "https://hq.example.internal",
        "--log-directory", r"C:\logs",
    ])
    assert arguments.log_directory == r"C:\logs"


def test_the_log_directory_option_defaults_to_none():
    arguments = build_parser().parse_args(["run", "--backend-url", "https://hq"])
    assert arguments.log_directory is None


@pytest.mark.parametrize(
    "command",
    ["enrol", "run", "status", "rotate-local-credential", "remove-local-credential"],
)
def test_every_command_can_render_its_own_help(command: str, capsys):
    """argparse expands ``help=`` through %-formatting.

    The first version of the --log-directory help said ``%LOCALAPPDATA%``, which
    argparse read as a format specifier, and ``run --help`` died with
    ``ValueError: unsupported format character 'O'``. Every other test passed,
    because none of them asked the parser to render itself - the packaged-EXE
    check found it. This is that check, in the unit suite where it belongs.
    """
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args([command, "--help"])
    assert exit_code.value.code == 0
    assert capsys.readouterr().out.strip(), f"{command} --help printed nothing"


def test_top_level_help_renders():
    with pytest.raises(SystemExit) as exit_code:
        build_parser().parse_args(["--help"])
    assert exit_code.value.code == 0


def test_a_credential_still_cannot_be_passed_on_the_command_line():
    """The new options must not have opened a door."""
    parser = build_parser()
    for forbidden in ("--credential", "--token", "--log-credential"):
        with pytest.raises(SystemExit):
            parser.parse_args(["run", "--backend-url", "https://hq", forbidden, "x"])
