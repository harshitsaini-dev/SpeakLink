"""StoreSetup, end to end, against a REAL backend and a real sealed credential.

WHAT MAKES THIS DIFFERENT FROM test_store_setup_core.py

That file proves each decision in isolation with a fake transport. This one
drives the whole chain - Test Connection, redeem a real code, seal a real
credential, select a real verified package, write the config, invoke the
installer, wait for CONNECTED - against the actual FastAPI routes, the actual
enrolment service, the actual credential store and the actual package verifier.
Nothing in the chain is stubbed except the two things that must not run in a
test: the PowerShell installer (injected ``run``) and the Receiver process
itself (its status file is written directly, which is exactly the seam a real
Receiver writes through).

ISOLATION, STATED EXPLICITLY

Temporary SQLite database, temporary key container with the fake protector,
temporary LOCALAPPDATA, temporary artifacts root, a synthetic package. No
network socket is opened, no port is bound, no Scheduled Task is created and
the protected database is never touched - the same module-reload fixture
pattern the existing enrolment endpoint tests use.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import secrets
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

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

from tools import store_setup_core as core  # noqa: E402
from tools.receiver_credential_store import FakeCredentialProtector  # noqa: E402

RUNTIME_MODULES = ("server", "db", "models", "schemas", "auth", "seed", "ws_manager")


class FakeRequest:
    def __init__(self, host: str = "203.0.113.9") -> None:
        self.client = SimpleNamespace(host=host)
        self.headers: dict = {}


@pytest.fixture(scope="module")
def hq(tmp_path_factory):
    """A real backend on a temporary database. Never the protected one."""
    root = tmp_path_factory.mktemp("storesetup-e2e")
    database = root / "e2e.db"
    container = root / "keys.bin"

    environment = {
        "ECHOCAST_DB_PATH": str(database),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"e2e-{secrets.token_hex(5)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
        "ECHOCAST_KEY_CONTAINER": str(container),
        "ECHOCAST_KEY_PROTECTOR": "fake",
    }
    previous = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    for name in RUNTIME_MODULES:
        sys.modules.pop(name, None)

    db_module = None
    try:
        db_module = importlib.import_module("db")
        server = importlib.import_module("server")
        models = importlib.import_module("models")
        assert Path(db_module.DB_PATH) == database.resolve(), (
            "the end-to-end test must never bind to the real database")
        server.startup_event()

        from key_custody import FakeProtector, create_key_container
        from migrations import run_receiver_credential_phase_one

        create_key_container(container, protector=FakeProtector())
        run_receiver_credential_phase_one(db_module.engine)

        with db_module.SessionLocal() as db:
            store = db.query(models.Store).order_by(models.Store.id).first()
            operator = db.query(models.HQUser).first()
            yield SimpleNamespace(server=server, db=db_module, models=models,
                                  store_id=store.id, store_code=store.store_code,
                                  operator=operator, database=database)
    finally:
        if db_module is not None:
            db_module.engine.dispose()
        for name in RUNTIME_MODULES:
            sys.modules.pop(name, None)
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture()
def fresh(hq):
    """A clean code budget and no active Devices, so real per-Store limits -
    which have their own tests and are doing their job - do not refuse the
    third scenario in this file."""
    from datetime import datetime, timezone

    from login_guard import LoginRateLimiter

    hq.server.enrollment_limiter = LoginRateLimiter(hq.server.ENROLLMENT_GUARD)
    with hq.db.SessionLocal() as db:
        db.query(hq.models.ReceiverEnrollmentCode).filter(
            hq.models.ReceiverEnrollmentCode.redeemed_at_epoch.is_(None)
        ).delete(synchronize_session=False)
        db.commit()
    now = datetime.now(timezone.utc).isoformat()
    with hq.db.engine.connect() as connection:
        connection.exec_driver_sql(
            "UPDATE receiver_devices SET status='retired', disabled_at=?, updated_at=? "
            "WHERE status='active'", (now, now))
        connection.commit()
    return hq


# ===========================================================================
# The seams: real routes reached through StoreSetup's own injection points
# ===========================================================================
def _create_code(hq, store_id=None) -> str:
    from schemas import EnrollmentCodeRequest

    with hq.db.SessionLocal() as db:
        response = hq.server.create_receiver_enrollment_code(
            EnrollmentCodeRequest(store_id=store_id or hq.store_id),
            db=db, user=hq.operator,
        )
    return response.code


class _RealBackendTransport:
    """StoreSetup's transport seam, wired to the real enrol route.

    Records every URL it is given, so a test can prove the code never travelled
    in one.
    """

    def __init__(self, hq):
        self.hq = hq
        self.urls: "list[str]" = []

    def post_json(self, url, payload, *, timeout):
        from fastapi import HTTPException
        from schemas import DeviceEnrollmentRequest

        self.urls.append(url)
        with self.hq.db.SessionLocal() as db:
            try:
                response = self.hq.server.enroll_receiver(
                    DeviceEnrollmentRequest(**payload), FakeRequest(), db=db)
            except HTTPException as refusal:
                return refusal.status_code, {"detail": refusal.detail}
        return 201, {
            "device_public_id": response.device_public_id,
            "credential": response.credential,
            "credential_version": response.credential_version,
            "store_id": response.store_id,
        }


def _real_root_opener(hq):
    """The connection-test seam, wired to the real GET /api/ route."""
    class _Response:
        def __init__(self, body):
            self.status = 200
            self._body = json.dumps(body).encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(url, timeout):
        return _Response(hq.server.root())
    return opener


def _fake_pe(path: Path, subsystem: int) -> None:
    blob = bytearray(512)
    blob[0:2] = b"MZ"
    header = 0x80
    blob[0x3C:0x40] = header.to_bytes(4, "little")
    blob[header:header + 4] = b"PE\x00\x00"
    at = header + 4 + 20 + 68
    blob[at:at + 2] = subsystem.to_bytes(2, "little")
    path.write_bytes(bytes(blob))


def _synthetic_package(artifacts_root: Path, *, background_subsystem=2) -> Path:
    package = artifacts_root / "EchoCastReceiver-9.9.9-e2e0000-20260101-000000"
    package.mkdir(parents=True)
    _fake_pe(package / "EchoCastReceiver.exe", 3)
    _fake_pe(package / "EchoCastReceiverBackground.exe", background_subsystem)
    (package / "ffmpeg.exe").write_bytes(b"not a real ffmpeg")
    (package / "manifest.json").write_text(json.dumps({"product": "EchoCastReceiver"}),
                                           encoding="utf-8")
    digests = []
    for item in sorted(package.rglob("*")):
        if item.is_file() and item.name not in ("SHA256SUMS.txt", "manifest.json"):
            digests.append(f"{hashlib.sha256(item.read_bytes()).hexdigest()}  "
                          f"{item.relative_to(package).as_posix()}")
    (package / "SHA256SUMS.txt").write_text("\n".join(digests) + "\n", encoding="utf-8")
    return package


class _RecordingInstaller:
    """Stands in for Install-EchoCastStoreReceiver.ps1. Records the whole
    command line, so a test can prove no secret was ever put on one."""

    def __init__(self, returncode=0):
        self.returncode = returncode
        self.commands: "list[list[str]]" = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        return SimpleNamespace(returncode=self.returncode, stdout="installed", stderr="")


# ===========================================================================
# The happy path, whole
# ===========================================================================
def test_the_full_first_run_chain_reaches_connected(fresh, tmp_path):
    """Connection -> code -> sealed credential -> verified package -> config ->
    installer -> CONNECTED. Every step against the real thing where it can be."""
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    package = _synthetic_package(artifacts)
    credential_path = tmp_path / "state" / "device-credential.bin"
    config_path = tmp_path / "state" / "config.json"
    status_path = tmp_path / "state" / "receiver-status.json"
    protector = FakeCredentialProtector("e2e-computer")

    # 1. Test Connection, through the real GET /api/ route.
    connection = core.test_hq_connection(
        "http://192.168.4.134:8000",
        allow_insecure_private_lan=True, expected_hq_host="192.168.4.134",
        opener=_real_root_opener(fresh),
    )
    assert connection.state is core.ConnectionState.PRIVATE_LAN_WARNING, connection.detail

    # 2 & 3. A real code, redeemed through the real enrol route.
    code = _create_code(fresh)
    transport = _RealBackendTransport(fresh)
    enrolment = core.redeem_enrollment(
        backend_url="http://192.168.4.134:8000", code=code,
        device_name="Counter PC", hostname="TILL-1",
        credential_path=credential_path, protector=protector,
        allow_insecure_private_lan=True, expected_hq_host="192.168.4.134",
        transport=transport,
    )
    assert enrolment.state is core.EnrolmentUiState.ENROLLED, enrolment.detail
    assert enrolment.outcome.store_id == fresh.store_id
    assert credential_path.exists(), "the credential must be sealed to this computer"

    # The code must never have travelled in a URL.
    for url in transport.urls:
        assert code not in url

    # 4. The package is chosen by verification, not by name.
    chosen = core.locate_verified_receiver_package(artifacts_root=artifacts)
    assert chosen == package

    # 5. Non-secret config.
    core.save_config(config_path, core.ReceiverConfig(
        backend_url="http://192.168.4.134:8000",
        expected_hq_host="192.168.4.134",
        allow_insecure_private_lan=True,
        audio_sink="windows",
        audio_output_device="index:3@Realtek(R) Audio",
    ))
    written = json.loads(config_path.read_text(encoding="utf-8"))
    assert written["audio_output_device"] == "index:3@Realtek(R) Audio"

    # 6. The installer, recorded rather than run.
    installer = _RecordingInstaller()
    result = core.run_receiver_installer(
        ["-PackagePath", str(chosen), "-BackendUrl", "http://192.168.4.134:8000",
         "-AudioOutputDevice", "index:3@Realtek(R) Audio"],
        run=installer,
    )
    assert result.returncode == 0
    assert installer.commands, "the installer was never invoked"

    # 7. The Receiver reports CONNECTED through its own status file.
    from tools.receiver_agent import write_status

    write_status(status_path, "CONNECTED", detail="the backend accepted this Device")
    outcome = core.wait_for_connected(status_path=status_path, timeout_seconds=2,
                                      poll_interval=0.01, sleep=lambda s: None)
    assert outcome.state is core.InstallState.CONNECTED

    # 8. Nothing secret anywhere a person or a log could see it.
    credential_bytes = credential_path.read_bytes()
    for command in installer.commands:
        joined = " ".join(command)
        assert code not in joined, "the enrolment code reached a command line"
        assert "echocast_rcv_v1" not in joined, "a credential reached a command line"
    config_text = config_path.read_text(encoding="utf-8")
    assert code not in config_text
    assert "echocast_rcv_v1" not in config_text
    status_text = status_path.read_text(encoding="utf-8")
    assert "echocast_rcv_v1" not in status_text
    assert credential_bytes not in config_text.encode("utf-8")


def test_the_same_code_cannot_be_redeemed_twice(fresh, tmp_path):
    """Use-once, proven against the real service rather than asserted."""
    protector = FakeCredentialProtector("e2e-computer")
    code = _create_code(fresh)

    first = core.redeem_enrollment(
        backend_url="https://hq.example.com", code=code, device_name="A", hostname="A",
        credential_path=tmp_path / "a.bin", protector=protector,
        transport=_RealBackendTransport(fresh),
    )
    assert first.state is core.EnrolmentUiState.ENROLLED

    second = core.redeem_enrollment(
        backend_url="https://hq.example.com", code=code, device_name="B", hostname="B",
        credential_path=tmp_path / "b.bin", protector=protector,
        transport=_RealBackendTransport(fresh),
    )
    assert second.state is core.EnrolmentUiState.REFUSED
    assert second.detail == core.GENERIC_ENROLMENT_FAILURE
    assert not (tmp_path / "b.bin").exists()


# ===========================================================================
# Negative scenarios
# ===========================================================================
def test_an_unreachable_hq_fails_the_connection_test_and_stops_there(fresh):
    import urllib.error

    def refusing_opener(url, timeout):
        raise urllib.error.URLError("connection refused")

    result = core.test_hq_connection("https://hq.example.com", opener=refusing_opener)
    assert result.state is core.ConnectionState.CONNECTION_FAILED


def test_an_invalid_code_is_refused_generically(fresh, tmp_path):
    result = core.redeem_enrollment(
        backend_url="https://hq.example.com", code="ECHO-NOPE-NOPE",
        device_name="A", hostname="A", credential_path=tmp_path / "a.bin",
        protector=FakeCredentialProtector("e2e-computer"),
        transport=_RealBackendTransport(fresh),
    )
    assert result.state is core.EnrolmentUiState.REFUSED
    assert result.detail == core.GENERIC_ENROLMENT_FAILURE
    assert not (tmp_path / "a.bin").exists()


def test_an_already_enrolled_computer_never_spends_a_code(fresh, tmp_path):
    credential_path = tmp_path / "already.bin"
    credential_path.write_bytes(b"an existing sealed credential")
    code = _create_code(fresh)
    transport = _RealBackendTransport(fresh)

    result = core.redeem_enrollment(
        backend_url="https://hq.example.com", code=code, device_name="A", hostname="A",
        credential_path=credential_path,
        protector=FakeCredentialProtector("e2e-computer"), transport=transport,
    )
    assert result.state is core.EnrolmentUiState.REFUSED
    assert transport.urls == [], "the code must not be sent at all"

    # And the code is still good, because it was never spent.
    fine = core.redeem_enrollment(
        backend_url="https://hq.example.com", code=code, device_name="B", hostname="B",
        credential_path=tmp_path / "fresh.bin",
        protector=FakeCredentialProtector("e2e-computer"),
        transport=_RealBackendTransport(fresh),
    )
    assert fine.state is core.EnrolmentUiState.ENROLLED


def test_a_package_with_a_console_background_exe_is_never_installed(fresh, tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    _synthetic_package(artifacts, background_subsystem=3)
    with pytest.raises(core.NoVerifiedReceiverPackage) as failure:
        core.locate_verified_receiver_package(artifacts_root=artifacts)
    assert "WINDOWS_GUI" in str(failure.value)


def test_a_tampered_package_is_never_installed(fresh, tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    package = _synthetic_package(artifacts)
    (package / "ffmpeg.exe").write_bytes(b"swapped after the build")
    with pytest.raises(core.NoVerifiedReceiverPackage):
        core.locate_verified_receiver_package(artifacts_root=artifacts)


def test_an_installer_failure_is_reported_not_swallowed(fresh, tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    package = _synthetic_package(artifacts)
    installer = _RecordingInstaller(returncode=1)
    result = core.run_receiver_installer(["-PackagePath", str(package)], run=installer)
    assert result.returncode == 1


def test_a_task_that_starts_but_never_connects_is_a_timeout_not_a_success(fresh, tmp_path):
    """The single most important negative case in this file. Files copied, task
    registered, process running - and the Store is still silent. That must never
    read as success."""
    from tools.receiver_agent import write_status

    status_path = tmp_path / "receiver-status.json"
    write_status(status_path, "DISCONNECTED", detail="the connection to HQ ended")
    outcome = core.wait_for_connected(status_path=status_path, timeout_seconds=0.05,
                                      poll_interval=0.01, sleep=lambda s: None)
    assert outcome.state is core.InstallState.TIMED_OUT


def test_no_status_file_at_all_is_a_timeout(fresh, tmp_path):
    outcome = core.wait_for_connected(status_path=tmp_path / "never-written.json",
                                      timeout_seconds=0.05, poll_interval=0.01,
                                      sleep=lambda s: None)
    assert outcome.state is core.InstallState.TIMED_OUT


def test_a_credential_that_cannot_be_written_is_reported_not_hidden(fresh, tmp_path):
    """The dangerous case: the code is SPENT, a Device now exists at HQ, and
    this computer cannot store what it was given. Reported, so an administrator
    knows to revoke that Device - never silently swallowed."""
    code = _create_code(fresh)

    class _RefusingProtector:
        def protect(self, payload: bytes) -> bytes:
            raise OSError("this computer cannot seal anything")

        def unprotect(self, payload: bytes) -> bytes:
            raise OSError("nor unseal it")

    result = core.redeem_enrollment(
        backend_url="https://hq.example.com", code=code, device_name="A", hostname="A",
        credential_path=tmp_path / "unwritable.bin", protector=_RefusingProtector(),
        transport=_RealBackendTransport(fresh),
    )
    assert result.state is core.EnrolmentUiState.REFUSED
    assert not (tmp_path / "unwritable.bin").exists()


def test_the_protected_database_was_never_opened(fresh):
    """A guard on this whole file: if any of the above bound to the real
    database, this is where it shows."""
    assert Path(fresh.db.DB_PATH) == fresh.database.resolve()
    assert "echocast_live" not in str(fresh.db.DB_PATH)
