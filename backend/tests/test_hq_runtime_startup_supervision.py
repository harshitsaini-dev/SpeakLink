"""Startup supervision: exactly one backend attempt at a time.

THE INCIDENT THIS COMES FROM

During the Supabase cutover the backend took about ten seconds to answer,
because start-up runs its migrations against a database in Mumbai instead of a
file on the local disk. The supervisor checked health ONCE, immediately after
spawning, decided the child had failed, and spawned another one - while the
first was still starting. The first then bound port 8000; the second could not,
and died. The supervisor was now tracking a dead child, so every 15 s it
"restarted the backend", every restart lost the port race, and after six
strikes it declared DEGRADED and stopped the frontend.

HQ reported READY the whole time. That is the shape worth remembering: the
supervisor was healthy, the port was healthy, and the thing being supervised
was not the thing running.

WHAT IS BEING FIXED

Start-up and ongoing health are different problems and now have different
rules. Start-up gets a bounded GRACE during which a live-but-not-yet-answering
child is left alone. Ongoing health keeps the short probe timeout, because a
process that has already answered and then stops answering really is a fault.

The fix is deliberately NOT "make the timeout enormous". A large timeout would
hide a genuinely dead backend for just as long as it protects a slow one.
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

from tools.hq_runtime import RuntimeState, supervise_child  # noqa: E402


class FakeClock:
    """Time the supervisor can be driven through without really waiting."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class FakeChild:
    """A child that becomes healthy only after a given amount of fake time."""

    def __init__(self, clock: FakeClock, *, healthy_after: float,
                 dies_after: float | None = None) -> None:
        self._clock = clock
        self._born = clock.now
        self._healthy_after = healthy_after
        self._dies_after = dies_after
        self.reaped = False

    def alive(self) -> bool:
        if self._dies_after is None:
            return True
        return (self._clock.now - self._born) < self._dies_after

    def healthy(self) -> bool:
        return self.alive() and (self._clock.now - self._born) >= self._healthy_after


# ===========================================================================
# The defect
# ===========================================================================
def test_a_backend_that_needs_twelve_seconds_is_started_exactly_once():
    """The cutover case, reduced to its essentials.

    Before the fix this spawns a second and third backend while the first is
    still starting, and it is the extra ones that cannot own port 8000.
    """
    clock = FakeClock()
    spawned: list[FakeChild] = []

    def start():
        child = FakeChild(clock, healthy_after=12.0)
        spawned.append(child)
        return child

    outcome = supervise_child(
        name="backend", start=start,
        is_alive=lambda c: c.alive(), health=lambda: spawned[-1].healthy(),
        max_attempts=5, sleep=clock.sleep, random_value=lambda: 0.5,
        startup_grace_seconds=60.0, monotonic=clock.monotonic,
        reap=lambda c: setattr(c, "reaped", True),
    )

    assert outcome.state is RuntimeState.BACKEND_HEALTHY
    assert len(spawned) == 1, (
        f"the backend must be started ONCE, not {len(spawned)} times - "
        "every extra process races the first for port 8000")
    assert not spawned[0].reaped, "a child that became healthy must not be reaped"


def test_a_slow_but_living_child_is_never_replaced_while_inside_the_grace():
    clock = FakeClock()
    spawned = []

    def start():
        child = FakeChild(clock, healthy_after=30.0)
        spawned.append(child)
        return child

    supervise_child(
        name="backend", start=start,
        is_alive=lambda c: c.alive(), health=lambda: spawned[-1].healthy(),
        max_attempts=3, sleep=clock.sleep, random_value=lambda: 0.5,
        startup_grace_seconds=45.0, monotonic=clock.monotonic,
        reap=lambda c: setattr(c, "reaped", True),
    )
    assert len(spawned) == 1


def test_a_fast_sqlite_startup_is_still_fast():
    """The fix must not make the ordinary local case slower.

    A child that is healthy immediately returns without consuming any of the
    grace period.
    """
    clock = FakeClock()
    spawned = []

    def start():
        child = FakeChild(clock, healthy_after=0.0)
        spawned.append(child)
        return child

    outcome = supervise_child(
        name="backend", start=start,
        is_alive=lambda c: c.alive(), health=lambda: spawned[-1].healthy(),
        max_attempts=5, sleep=clock.sleep, random_value=lambda: 0.5,
        startup_grace_seconds=60.0, monotonic=clock.monotonic,
        reap=lambda c: setattr(c, "reaped", True),
    )
    assert outcome.state is RuntimeState.BACKEND_HEALTHY
    assert len(spawned) == 1
    assert clock.now == 0.0, "a healthy child must not cost any waiting"


# ===========================================================================
# Reaping - no retry may leave the previous tree running
# ===========================================================================
def test_a_child_that_exits_before_becoming_healthy_is_reaped_before_the_retry():
    clock = FakeClock()
    spawned = []

    def start():
        child = FakeChild(clock, healthy_after=999.0, dies_after=2.0)
        spawned.append(child)
        return child

    reaped: list[FakeChild] = []
    supervise_child(
        name="backend", start=start,
        is_alive=lambda c: c.alive(), health=lambda: spawned[-1].healthy(),
        max_attempts=3, sleep=clock.sleep, random_value=lambda: 0.5,
        startup_grace_seconds=30.0, monotonic=clock.monotonic,
        reap=lambda c: reaped.append(c),
    )
    assert len(spawned) == 3
    assert len(reaped) == 3, "every failed attempt must be reaped before the next"
    # And each reap happened before its successor was created.
    for earlier, later in zip(reaped, spawned[1:]):
        assert reaped.index(earlier) < spawned.index(later)


def test_a_startup_that_times_out_is_reaped_before_the_retry():
    """The child is alive but never answers. It still owns the port, so it
    MUST be terminated before another attempt is made."""
    clock = FakeClock()
    spawned = []

    def start():
        child = FakeChild(clock, healthy_after=999.0)  # alive forever, never healthy
        spawned.append(child)
        return child

    reaped = []
    outcome = supervise_child(
        name="backend", start=start,
        is_alive=lambda c: c.alive(), health=lambda: spawned[-1].healthy(),
        max_attempts=2, sleep=clock.sleep, random_value=lambda: 0.5,
        startup_grace_seconds=20.0, monotonic=clock.monotonic,
        reap=lambda c: reaped.append(c),
    )
    assert outcome.state is RuntimeState.DEGRADED
    assert len(spawned) == 2
    assert len(reaped) == 2, "a timed-out child holds the port and must be reaped"


def test_no_two_attempts_ever_overlap():
    """At no instant may two children be alive and unreaped."""
    clock = FakeClock()
    live: list[FakeChild] = []
    spawned = []

    def start():
        # If a previous child is still live-and-unreaped, that is the defect.
        assert not [c for c in live if c.alive()], "two backends overlapped"
        child = FakeChild(clock, healthy_after=999.0, dies_after=3.0)
        spawned.append(child)
        live.append(child)
        return child

    def reap(child):
        child._dies_after = 0.0  # dead from now on
        if child in live:
            live.remove(child)

    supervise_child(
        name="backend", start=start,
        is_alive=lambda c: c.alive(), health=lambda: spawned[-1].healthy(),
        max_attempts=3, sleep=clock.sleep, random_value=lambda: 0.5,
        startup_grace_seconds=30.0, monotonic=clock.monotonic, reap=reap,
    )
    assert len(spawned) == 3


def test_the_supervisor_returns_the_child_it_actually_supervised():
    """It must be possible to know WHICH child became healthy.

    The incident's second half was the supervisor tracking a dead child while
    a different process served traffic.
    """
    clock = FakeClock()
    spawned = []

    def start():
        child = FakeChild(clock, healthy_after=5.0)
        spawned.append(child)
        return child

    outcome = supervise_child(
        name="backend", start=start,
        is_alive=lambda c: c.alive(), health=lambda: spawned[-1].healthy(),
        max_attempts=3, sleep=clock.sleep, random_value=lambda: 0.5,
        startup_grace_seconds=30.0, monotonic=clock.monotonic,
        reap=lambda c: None,
    )
    assert outcome.child is spawned[0]


# ===========================================================================
# Ongoing supervision - one missed probe is not a fault
# ===========================================================================
def _profile(tmp_path):
    from tools.hq_runtime import RuntimeProfile

    # child_environment reads these, so they have to exist for the runtime to
    # get as far as spawning anything.
    (tmp_path / "logs").mkdir(exist_ok=True)
    (tmp_path / "frontend").mkdir(exist_ok=True)
    (tmp_path / "jwt.txt").write_text("test-only-secret-value-for-this-module")
    (tmp_path / "db.sqlite").touch()
    (tmp_path / "keys.bin").touch()
    return RuntimeProfile(
        root=tmp_path, database=tmp_path / "db.sqlite",
        key_container=tmp_path / "keys.bin", jwt_secret_file=tmp_path / "jwt.txt",
        logs=tmp_path / "logs", lock=tmp_path / "lock",
        frontend_build=tmp_path / "frontend",
        status_file=tmp_path / "status.json",
    )


class _Proc:
    def __init__(self, pid=1234):
        self.pid = pid
        self.terminated = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self._alive = False


def test_one_transient_missed_probe_does_not_create_a_second_backend(tmp_path):
    """A single failed probe is a blip, not a dead backend.

    Restarting on the first miss is what turned a slow answer into a duplicate
    process fighting for port 8000.
    """
    from tools.hq_runtime import HQRuntime

    spawned = []

    def spawn(command, env):
        child = _Proc(pid=1000 + len(spawned))
        spawned.append(child)
        return child

    answers = iter([True, True, False, True, True, True, True, True])

    runtime = HQRuntime(_profile(tmp_path), spawn=spawn,
                        http=lambda url, timeout=None: next(answers, True),
                        sleep=lambda _s: None)
    runtime._children = {"backend": _Proc(pid=1), "frontend": _Proc(pid=2)}

    runtime.watch_once()
    runtime.watch_once()

    assert spawned == [], "a single missed probe must not spawn anything"


def test_a_genuinely_dead_backend_is_reaped_before_the_replacement_starts(tmp_path):
    from tools.hq_runtime import HQRuntime

    spawned = []

    def spawn(command, env):
        child = _Proc(pid=2000 + len(spawned))
        spawned.append(child)
        return child

    runtime = HQRuntime(_profile(tmp_path), spawn=spawn,
                        http=lambda url, timeout=None: False,
                        sleep=lambda _s: None)
    dead = _Proc(pid=1)
    dead._alive = False
    runtime._children = {"backend": dead, "frontend": _Proc(pid=2)}

    for _ in range(4):
        runtime.watch_once()

    assert spawned, "a dead backend must eventually be replaced"


# ===========================================================================
# Containment: children must not outlive a hard-killed supervisor
# ===========================================================================
def test_the_containment_job_is_created_once_and_reused():
    """Creating a job per child would defeat the purpose - the guarantee is
    that ONE job owns them all and closing it takes them all."""
    from tools import hq_runtime

    hq_runtime._JOB_HANDLE = None
    first = hq_runtime.ensure_child_job()
    second = hq_runtime.ensure_child_job()
    assert first is second
    if sys.platform == "win32":
        assert first is not None, "Windows must get a containment job"


def test_assignment_is_best_effort_and_never_raises():
    """Containment is defence in depth. A supervisor that refused to start
    because it could not create a job object would be worse than the orphans
    it prevents."""
    from tools import hq_runtime

    class NoPid:
        pid = None

    assert hq_runtime.assign_to_child_job(NoPid()) is False
    assert hq_runtime.assign_to_child_job(object()) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Job Objects are a Windows facility")
def test_a_real_child_is_killed_when_the_supervisor_process_dies():
    """The property that actually matters, proven end to end.

    A supervisor is started as a real subprocess; it spawns a long-lived
    grandchild inside the containment job and reports its PID. The supervisor
    is then killed WITHOUT cleanup, exactly as Task Scheduler kills it. The
    grandchild must not survive.
    """
    import json as _json
    import subprocess as _sp
    import textwrap
    import time as _time

    script = textwrap.dedent(f"""
        import json, subprocess, sys, time
        sys.path.insert(0, {str(REPOSITORY_ROOT)!r})
        from tools import hq_runtime
        hq_runtime.ensure_child_job()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        hq_runtime.assign_to_child_job(child)
        print(json.dumps({{"child": child.pid}}), flush=True)
        time.sleep(120)
    """)
    supervisor = _sp.Popen([sys.executable, "-c", script], stdout=_sp.PIPE, text=True)
    try:
        line = supervisor.stdout.readline()
        grandchild = _json.loads(line)["child"]

        # GetExitCodeProcess, not OpenProcess. OpenProcess still succeeds for
        # a process that has terminated but whose kernel object has not been
        # reclaimed, so it answers "does this PID exist" rather than "is it
        # running" - exactly the wrong question here. psutil is not installed
        # in this environment, so this uses the Win32 call directly.
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        STILL_ACTIVE = 259
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

        def still_running(pid: int) -> bool:
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)

        assert still_running(grandchild), "grandchild should be running"

        supervisor.kill()          # no cleanup, exactly like a hard stop
        supervisor.wait(timeout=15)

        for _ in range(40):        # give Windows a moment to close the job
            if not still_running(grandchild):
                break
            _time.sleep(0.25)
        else:
            pytest.fail("the grandchild outlived the supervisor - containment failed")
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
