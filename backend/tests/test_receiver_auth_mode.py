"""HQ must not refuse a Device credential it issued five minutes earlier.

THE REAL FAILURE, FROM THE SECOND PC

    finished state=AUTHENTICATION_REFUSED attempts=1
    authentication refused: HQ refused this Device credential.

Device 3b1ff11f, Store 31 (Bindapur, active), enabled, credential present and not
revoked. Nothing was wrong with it.

``build_receiver_runtime_authenticator()`` ran once, at import, and its answer was
frozen for the life of the process. It degrades to legacy Store tokens when the
phase-one Device tables or the key container are missing AT THAT MOMENT. The
backend had started before ``run_receiver_credential_phase_one`` created those
tables, so it served the legacy-only authenticator - and would have kept doing so
until somebody restarted it.

What makes it expensive is that half the system kept working. Enrolment is an HTTP
route with its own session, so codes were redeemed and Device rows written exactly
as expected. Only the WebSocket handshake consulted the frozen object. The HQ page
showed "code redeemed: yes, device connected: no" and nothing explained why.
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

from receiver_auth_mode import ReceiverAuthMode  # noqa: E402


class Recorder:
    def __init__(self, name):
        self.name = name
        self.calls = 0

    def authenticate(self, *_a, **_k):
        self.calls += 1
        return self.name


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


# ===========================================================================
# 1. The frozen decision
# ===========================================================================
def test_it_starts_on_legacy_when_the_device_tables_are_missing():
    legacy = Recorder("legacy")
    mode = ReceiverAuthMode(build=lambda: None, legacy=legacy)

    assert mode.device_credentials_accepted is False
    assert mode.authenticate() == "legacy"


def test_it_upgrades_once_the_preconditions_appear(monkeypatch):
    """THE FIX. The migration ran under a live backend; the next attempt must
    succeed instead of waiting for a restart nobody knows to perform."""
    legacy, device = Recorder("legacy"), Recorder("device")
    clock = Clock()
    ready = {"yes": False}

    mode = ReceiverAuthMode(build=lambda: device if ready["yes"] else None,
                            legacy=legacy, clock=clock, recheck_seconds=5.0)
    assert mode.authenticate() == "legacy"

    ready["yes"] = True          # phase-one migration completes
    clock.advance(6)

    assert mode.authenticate() == "device", (
        "HQ is still refusing Device credentials after the tables appeared"
    )
    assert mode.device_credentials_accepted is True


def test_the_recheck_is_rate_limited():
    """A refused Receiver retries. Re-probing per attempt would turn one bad
    credential into database load."""
    legacy = Recorder("legacy")
    clock = Clock()
    builds = {"count": 0}

    def build():
        builds["count"] += 1
        return None

    mode = ReceiverAuthMode(build=build, legacy=legacy, clock=clock,
                            recheck_seconds=5.0)
    first = builds["count"]

    for _ in range(20):
        mode.authenticate()

    assert builds["count"] == first, "every attempt re-probed the database"

    clock.advance(6)
    mode.authenticate()
    assert builds["count"] == first + 1


# ===========================================================================
# 2. It never goes backwards
# ===========================================================================
def test_it_never_downgrades_after_a_successful_upgrade():
    """A transient database hiccup must not quietly return the fleet to shared
    Store tokens. That would be a security regression triggered by a blip."""
    legacy, device = Recorder("legacy"), Recorder("device")
    clock = Clock()
    available = {"yes": True}

    mode = ReceiverAuthMode(build=lambda: device if available["yes"] else None,
                            legacy=legacy, clock=clock, recheck_seconds=1.0)
    assert mode.authenticate() == "device"

    available["yes"] = False
    clock.advance(10)

    assert mode.authenticate() == "device"
    assert mode.device_credentials_accepted is True


def test_a_probe_that_raises_leaves_receivers_connected():
    legacy = Recorder("legacy")
    clock = Clock()

    def exploding():
        raise RuntimeError("database is briefly unavailable")

    mode = ReceiverAuthMode(build=exploding, legacy=legacy, clock=clock)

    assert mode.authenticate() == "legacy"
    clock.advance(10)
    assert mode.authenticate() == "legacy", "a probe failure took Receivers offline"


# ===========================================================================
# 3. It is a drop-in for the real authenticator
# ===========================================================================
def test_unknown_attributes_are_forwarded():
    class Rich(Recorder):
        def describe_transport(self):
            return "device"

    device = Rich("device")
    mode = ReceiverAuthMode(build=lambda: device, legacy=Recorder("legacy"))

    assert mode.describe_transport() == "device"


def test_it_reports_which_mode_is_active():
    legacy, device = Recorder("legacy"), Recorder("device")
    degraded = ReceiverAuthMode(build=lambda: None, legacy=legacy)
    upgraded = ReceiverAuthMode(build=lambda: device, legacy=legacy)

    assert degraded.describe() == "legacy store tokens only"
    assert upgraded.describe() == "device credentials + legacy store tokens"


def test_arguments_reach_the_underlying_authenticator():
    seen = {}

    class Checking:
        def authenticate(self, presented_token=None, authenticated_at=None):
            seen["token"] = presented_token
            return "ok"

    mode = ReceiverAuthMode(build=lambda: Checking(), legacy=Recorder("legacy"))
    mode.authenticate(presented_token="x", authenticated_at="t")

    assert seen["token"] == "x"


# ===========================================================================
# 4. The server actually uses it
# ===========================================================================
def test_the_server_wires_the_re_evaluating_mode():
    """A fix that is merely available is not a fix - the previous defect in this
    project was a creator nobody called."""
    source = (BACKEND_ROOT / "server.py").read_text(encoding="utf-8")
    assert "ReceiverAuthMode" in source, (
        "server.py still freezes the authenticator choice at start-up"
    )
