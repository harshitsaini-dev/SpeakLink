"""The Rerun workflow's logic: Status, Repair, Restart, Stop, Diagnostics,
Uninstall, Replace Device Identity. Every task/installer action goes through a
named .ps1 script, injected here as a fake ``run`` - nothing shells out for
real in this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools import store_setup_core as core  # noqa: E402
from tools.receiver_credential_store import FakeCredentialProtector  # noqa: E402
from tools.windows_audio_devices import OutputDevice  # noqa: E402


#: Proof that the Settings Password was entered. These tests exercise the
#: MUTATION, not the gate - the gate has its own file - so they construct the
#: authorization directly rather than typing a password each time.
def _authorized():
    from datetime import datetime, timezone

    from tools.store_setup_core import SettingsAuthorization

    return SettingsAuthorization(granted_at=datetime.now(timezone.utc))


VALID_CREDENTIAL = "echocast_rcv_v1.11111111-1111-1111-1111-111111111111." + "A" * 43


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _enroll(tmp_path, protector):
    from tools import receiver_agent

    class _Transport:
        def post_json(self, url, payload, *, timeout):
            return 201, {"device_public_id": "dev-1", "store_id": 3,
                        "credential": VALID_CREDENTIAL}

    credential_path = tmp_path / "cred.bin"
    receiver_agent.enrol(
        backend_url="https://hq.example.com", code="ECHO-A-CODE",
        device_name="till-1", hostname="TILL-1",
        credential_path=credential_path, protector=protector, transport=_Transport(),
    )
    return credential_path


# ===========================================================================
# query_task_state
# ===========================================================================
def test_an_absent_task_is_reported_not_registered():
    result = core.query_task_state(run=lambda *a, **k: _FakeCompletedProcess(
        returncode=0, stdout="NOT_REGISTERED\n"))
    assert result.registered is False


def test_a_task_that_is_not_ours_is_reported_as_such():
    result = core.query_task_state(run=lambda *a, **k: _FakeCompletedProcess(
        returncode=0, stdout="NOT_OURS\n"))
    assert result.registered is True
    assert result.is_ours is False


def test_our_task_reports_state_and_process_count():
    result = core.query_task_state(run=lambda *a, **k: _FakeCompletedProcess(
        returncode=0, stdout="STATE=Ready\nPROCESS_COUNT=1\n"))
    assert result.is_ours is True
    assert result.state == "Ready"
    assert result.process_count == 1


# ===========================================================================
# get_status_snapshot - never a secret
# ===========================================================================
def test_status_on_a_never_enrolled_computer(tmp_path):
    snapshot = core.get_status_snapshot(
        credential_path=tmp_path / "cred.bin", protector=FakeCredentialProtector("t"))
    assert snapshot.is_installed is False


def test_status_on_an_enrolled_computer_shows_public_facts_only(tmp_path, monkeypatch):
    protector = FakeCredentialProtector("t")
    credential_path = _enroll(tmp_path, protector)
    monkeypatch.setattr(core, "query_task_state",
                        lambda **k: core.TaskState(registered=True, is_ours=True,
                                                   state="Ready", process_count=1))
    snapshot = core.get_status_snapshot(
        credential_path=credential_path, protector=protector,
        config_path=tmp_path / "no-config.json",
        status_path=tmp_path / "no-status.json",
    )
    assert snapshot.is_installed is True
    assert snapshot.device_public_id == "dev-1"
    assert snapshot.store_id == 3
    assert snapshot.task.state == "Ready"


def test_status_snapshot_never_carries_the_credential(tmp_path, monkeypatch):
    protector = FakeCredentialProtector("t")
    credential_path = _enroll(tmp_path, protector)
    monkeypatch.setattr(core, "query_task_state",
                        lambda **k: core.TaskState(registered=False))
    snapshot = core.get_status_snapshot(
        credential_path=credential_path, protector=protector,
        config_path=tmp_path / "no-config.json", status_path=tmp_path / "no-status.json",
    )
    import dataclasses

    text = json.dumps(dataclasses.asdict(snapshot), default=str)
    assert VALID_CREDENTIAL not in text
    assert "credential" not in snapshot.__dataclass_fields__


# ===========================================================================
# repair_installation
# ===========================================================================
def test_repair_reports_ok_on_success(tmp_path):
    result = core.repair_installation(authorization=_authorized(), 
        package_path=tmp_path / "pkg",
        run=lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout="repaired"))
    assert result.ok is True


def test_repair_reports_failure_without_raising(tmp_path):
    result = core.repair_installation(authorization=_authorized(), 
        package_path=tmp_path / "pkg",
        run=lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="", stderr="boom"))
    assert result.ok is False
    assert "boom" in result.detail


# ===========================================================================
# restart_receiver - CONNECTED required, timeout is honest
# ===========================================================================
def test_restart_waits_for_connected(tmp_path, monkeypatch):
    status_path = tmp_path / "receiver-status.json"
    status_path.write_text(json.dumps({"state": "CONNECTED"}), encoding="utf-8")
    monkeypatch.setattr(core, "receiver_status_path", lambda: status_path)

    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return _FakeCompletedProcess(returncode=0, stdout="STOPPED\n" if "Stop" in command
                                     else "STARTED\n")

    result = core.restart_receiver(run=fake_run, timeout_seconds=1, sleep=lambda s: None)
    assert result.state is core.InstallState.CONNECTED
    assert any("Stop" in c for c in calls)
    assert any("Start" in c for c in calls)


def test_restart_refuses_a_task_that_is_not_ours():
    result = core.restart_receiver(
        run=lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="",
                                                  stderr="this task is not ours"))
    assert result.state is core.InstallState.INSTALL_FAILED


def test_restart_times_out_honestly_rather_than_claiming_connected(tmp_path, monkeypatch):
    status_path = tmp_path / "receiver-status.json"  # never written
    monkeypatch.setattr(core, "receiver_status_path", lambda: status_path)

    result = core.restart_receiver(
        run=lambda command, **k: _FakeCompletedProcess(
            returncode=0, stdout="STOPPED\n" if "Stop" in command else "STARTED\n"),
        timeout_seconds=0.05, sleep=lambda s: None,
    )
    assert result.state is core.InstallState.TIMED_OUT


def test_a_spawned_process_alone_never_counts_as_restart_success(tmp_path, monkeypatch):
    """The exact regression this project keeps guarding against, one screen
    further along: the task started fine, but the Receiver never actually
    reported CONNECTED - that must not read as success."""
    status_path = tmp_path / "receiver-status.json"
    status_path.write_text(json.dumps({"state": "DISCONNECTED"}), encoding="utf-8")
    monkeypatch.setattr(core, "receiver_status_path", lambda: status_path)

    result = core.restart_receiver(
        run=lambda command, **k: _FakeCompletedProcess(
            returncode=0, stdout="STOPPED\n" if "Stop" in command else "STARTED\n"),
        timeout_seconds=0.05, sleep=lambda s: None,
    )
    assert result.state is core.InstallState.TIMED_OUT


# ===========================================================================
# stop_receiver
# ===========================================================================
def test_stop_reports_success():
    result = core.stop_receiver(authorization=_authorized(), 
        run=lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout="STOPPED\n"))
    assert result.ok is True


def test_stop_reports_failure_without_raising():
    result = core.stop_receiver(authorization=_authorized(), 
        run=lambda *a, **k: _FakeCompletedProcess(returncode=1, stdout="", stderr="not ours"))
    assert result.ok is False
    assert "not ours" in result.detail


# ===========================================================================
# change_audio_output
# ===========================================================================
def test_change_audio_output_saves_before_restarting(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    status_path = tmp_path / "receiver-status.json"
    status_path.write_text(json.dumps({"state": "CONNECTED"}), encoding="utf-8")
    monkeypatch.setattr(core, "receiver_status_path", lambda: status_path)

    device = OutputDevice(index=2, name="Realtek(R) Audio", host_api="MME",
                          max_output_channels=2, default_samplerate=48000, is_default=False)
    # Selecting an output now also resolves and stores the STABLE Core Audio
    # endpoint id, because HQ's per-Store volume drives the Windows master and
    # a PortAudio index is not a safe thing to drive it from. A fake backend
    # stands in for Core Audio so this test never touches the real mixer.
    endpoint_id = "{0.0.0.00000000}.{aaaaaaaa-1111-2222-3333-444444444444}"

    class FakeEndpointBackend:
        def list_endpoints(self):
            return [{"endpoint_id": endpoint_id, "name": "Realtek(R) Audio"}]

        def controller(self, requested):
            raise AssertionError("selecting an output must not mutate an endpoint")

    core.change_audio_output(authorization=_authorized(),
        device=device, config_path=config_path,
        run=lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout="STARTED\n"),
        timeout_seconds=1,
        endpoint_backend=FakeEndpointBackend(),
    )
    saved = core.load_config(config_path)
    assert saved.audio_output_device == device.verified_selector
    assert saved.audio_sink == "windows"
    assert saved.windows_endpoint_id == endpoint_id


def test_change_audio_output_refuses_an_endpoint_it_cannot_identify(tmp_path):
    """Better to refuse than to store a guess.

    A wrong endpoint id is permanent and silent: every later broadcast would
    move the master volume of hardware nobody selected. The technician is
    still standing there, so the error is useful now and impossible later.
    """
    from tools import windows_endpoint_volume

    config_path = tmp_path / "config.json"
    device = OutputDevice(index=2, name="Realtek(R) Audio", host_api="MME",
                          max_output_channels=2, default_samplerate=48000,
                          is_default=False)

    class TwoOfThem:
        def list_endpoints(self):
            return [{"endpoint_id": "{0.0.0.0}.{a}", "name": "Realtek(R) Audio"},
                    {"endpoint_id": "{0.0.0.0}.{b}", "name": "Realtek(R) Audio"}]

    with pytest.raises(windows_endpoint_volume.EndpointAmbiguous):
        core.change_audio_output(authorization=_authorized(),
            device=device, config_path=config_path,
            run=lambda *a, **k: _FakeCompletedProcess(returncode=0, stdout="STARTED\n"),
            timeout_seconds=1, endpoint_backend=TwoOfThem())
    assert not config_path.exists(), "a refusal must not leave a half-written config"


# ===========================================================================
# build_redacted_diagnostics
# ===========================================================================
def test_diagnostics_never_contains_the_credential(tmp_path, monkeypatch):
    protector = FakeCredentialProtector("t")
    credential_path = _enroll(tmp_path, protector)
    monkeypatch.setattr(core, "query_task_state",
                        lambda **k: core.TaskState(registered=True, is_ours=True,
                                                   state="Ready", process_count=1))
    text = core.build_redacted_diagnostics(
        credential_path=credential_path, config_path=tmp_path / "no-config.json")
    assert VALID_CREDENTIAL not in text
    assert "Ready" in text


# ===========================================================================
# export_diagnostics
# ===========================================================================
def test_export_writes_a_timestamped_file(tmp_path):
    from datetime import datetime, timezone

    target = core.export_diagnostics(
        "safe diagnostic text", export_directory=tmp_path,
        now=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
    )
    assert target.exists()
    assert "20260102-030405" in target.name


def test_export_refuses_text_containing_a_credential(tmp_path):
    with pytest.raises(core.UnsafeDiagnosticsExport):
        core.export_diagnostics(f"oops: {VALID_CREDENTIAL}", export_directory=tmp_path)
    assert list(tmp_path.iterdir()) == [], "nothing should have been written"


def test_export_refuses_a_bearer_header(tmp_path):
    with pytest.raises(core.UnsafeDiagnosticsExport):
        core.export_diagnostics("Authorization: Bearer abc123", export_directory=tmp_path)


# ===========================================================================
# open_log_folder
# ===========================================================================
def test_open_log_folder_opens_the_known_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "default_log_directory", lambda: tmp_path / "logs")
    opened = []
    result = core.open_log_folder(opener=lambda p: opened.append(p))
    assert result == tmp_path / "logs"
    assert opened == [str(tmp_path / "logs")]


def test_open_log_folder_refuses_an_unexpected_path(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "default_log_directory", lambda: tmp_path / "logs")
    with pytest.raises(core.UntrustedLogPath):
        core.open_log_folder(tmp_path / "somewhere-else", opener=lambda p: None)


# ===========================================================================
# replace_device_identity
# ===========================================================================
def test_replace_identity_requires_the_exact_confirmation_word(tmp_path):
    protector = FakeCredentialProtector("t")
    credential_path = _enroll(tmp_path, protector)
    assert core.replace_device_identity(authorization=_authorized(), credential_path=credential_path,
                                        confirmation_word="yes please") is False
    assert credential_path.exists(), "a wrong confirmation must not remove the credential"


def test_replace_identity_removes_the_credential_on_exact_confirmation(tmp_path):
    protector = FakeCredentialProtector("t")
    credential_path = _enroll(tmp_path, protector)
    assert core.replace_device_identity(authorization=_authorized(), 
        credential_path=credential_path, confirmation_word=core.CONFIRMATION_WORD) is True
    assert not credential_path.exists()
