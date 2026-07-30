"""The logic behind SpeakLinkStoreSetup.exe. No GUI import here, on purpose.

A tkinter window cannot be driven headlessly, so every decision that matters -
is this URL safe, did enrolment succeed, which output was actually selected, is
the Receiver really connected - is a plain function, tested directly here.

Nothing here opens a socket or a real audio device; every seam (opener,
backend, popen, sleep/clock) is injected.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools import store_setup_core as core  # noqa: E402
from tools.receiver_credential_store import FakeCredentialProtector  # noqa: E402
from tools.windows_audio_devices import OutputDevice  # noqa: E402


# ===========================================================================
# Screen 1 - HQ connection
# ===========================================================================
class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_a_public_https_url_is_accepted_and_checked():
    def opener(url, timeout):
        assert url == "https://hq.example.com/api/"
        return _FakeResponse(200, {"service": "SpeakLink", "status": "ok"})

    result = core.test_hq_connection("https://hq.example.com", opener=opener)
    assert result.state is core.ConnectionState.CONNECTED_TO_HQ


def test_a_public_http_url_is_refused_before_any_network_call():
    calls = []
    result = core.test_hq_connection("http://speaklink.example.com",
                                      opener=lambda *a, **k: calls.append(1))
    assert result.state is core.ConnectionState.INSECURE_PUBLIC_URL_REFUSED
    assert calls == [], "a refused URL must never be reached"


def test_a_private_lan_http_url_needs_the_explicit_flag():
    result = core.test_hq_connection("http://192.168.4.134:8000",
                                      opener=lambda *a, **k: _FakeResponse(200, {"status": "ok"}))
    assert result.state is core.ConnectionState.INSECURE_PUBLIC_URL_REFUSED


def test_a_private_lan_http_url_with_the_flag_connects_but_warns():
    result = core.test_hq_connection(
        "http://192.168.4.134:8000",
        allow_insecure_private_lan=True, expected_hq_host="192.168.4.134",
        opener=lambda *a, **k: _FakeResponse(200, {"status": "ok"}),
    )
    assert result.state is core.ConnectionState.PRIVATE_LAN_WARNING


def test_a_backend_that_does_not_answer_is_connection_failed():
    import urllib.error

    def opener(url, timeout):
        raise urllib.error.URLError("refused")

    result = core.test_hq_connection("https://hq.example.com", opener=opener)
    assert result.state is core.ConnectionState.CONNECTION_FAILED


def test_a_reachable_but_wrong_service_is_connection_failed():
    result = core.test_hq_connection(
        "https://hq.example.com",
        opener=lambda *a, **k: _FakeResponse(200, {"status": "not-speaklink"}),
    )
    assert result.state is core.ConnectionState.CONNECTION_FAILED


# ===========================================================================
# Screen 2 - Enrolment
# ===========================================================================
def test_a_successful_enrolment_reports_enrolled(tmp_path):
    class _FakeTransport:
        def post_json(self, url, payload, *, timeout):
            return 201, {"device_public_id": "dev-123", "store_id": 7,
                        "credential": "speaklink_rcv_v1.11111111-1111-1111-1111-111111111111.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}

    result = core.redeem_enrollment(
        backend_url="https://hq.example.com",
        code="ECHO-TEST-CODE",
        device_name="till-1",
        hostname="TILL-1",
        credential_path=tmp_path / "cred.bin",
        protector=FakeCredentialProtector("test-computer"),
        transport=_FakeTransport(),
    )
    assert result.state is core.EnrolmentUiState.ENROLLED
    assert result.outcome.device_public_id == "dev-123"


def test_a_refused_code_is_generic_to_the_caller(tmp_path):
    class _FakeTransport:
        def post_json(self, url, payload, *, timeout):
            return 410, {"detail": "expired"}

    result = core.redeem_enrollment(
        backend_url="https://hq.example.com",
        code="ECHO-EXPIRED-CODE",
        device_name="till-1",
        hostname="TILL-1",
        credential_path=tmp_path / "cred.bin",
        protector=FakeCredentialProtector("test-computer"),
        transport=_FakeTransport(),
    )
    assert result.state is core.EnrolmentUiState.REFUSED
    assert result.detail == core.GENERIC_ENROLMENT_FAILURE
    assert "expired" not in result.detail.lower()


def test_an_already_enrolled_computer_is_refused_without_spending_the_code(tmp_path):
    credential_path = tmp_path / "cred.bin"
    credential_path.write_bytes(b"already here")

    called = []

    class _FakeTransport:
        def post_json(self, url, payload, *, timeout):
            called.append(1)
            return 201, {}

    result = core.redeem_enrollment(
        backend_url="https://hq.example.com", code="ECHO-A-CODE",
        device_name="till-1", hostname="TILL-1",
        credential_path=credential_path, protector=FakeCredentialProtector("test-computer"),
        transport=_FakeTransport(),
    )
    assert result.state is core.EnrolmentUiState.REFUSED
    assert called == [], "a code must never be spent against an already-enrolled computer"


# ===========================================================================
# Screen 3 - Audio output classification
# ===========================================================================
def _device(index, name, wireless_name=None):
    return OutputDevice(index=index, name=name, host_api="MME",
                       max_output_channels=2, default_samplerate=48000,
                       is_default=False)


def test_wired_devices_are_classified_wired():
    assert core.classify_output(_device(0, "Realtek(R) Audio")) is core.OutputKind.WIRED
    assert core.classify_output(_device(0, "USB Audio Device")) is core.OutputKind.WIRED


def test_bluetooth_devices_are_classified_bluetooth():
    assert core.classify_output(_device(0, "Headphones (Bluetooth Hands-Free AG)")) \
        is core.OutputKind.BLUETOOTH


def test_hdmi_devices_are_classified_hdmi():
    assert core.classify_output(_device(0, "NVIDIA High Definition Audio (HDMI)")) \
        is core.OutputKind.HDMI


def test_an_unrecognised_device_is_other_not_a_guess():
    assert core.classify_output(_device(0, "Weird Vendor Sound Card 9000")) \
        is core.OutputKind.OTHER


def test_listing_orders_wired_first_but_never_auto_selects():
    devices = [
        _device(0, "NVIDIA HDMI Output"),
        _device(1, "Headphones (Bluetooth)"),
        _device(2, "Realtek(R) Audio"),
    ]
    classified = core.list_classified_outputs(devices=devices)
    assert [c.kind for c in classified] == [
        core.OutputKind.WIRED, core.OutputKind.BLUETOOTH, core.OutputKind.HDMI,
    ]
    # Ordering is a suggestion. Nothing here marks one as "selected".
    assert all(not hasattr(c, "selected") for c in classified)


# ===========================================================================
# Screen 3 - Test Sound
# ===========================================================================
class _FakeStream:
    def __init__(self, *, fail_open=False, fail_write=False):
        self.fail_open = fail_open
        self.fail_write = fail_write
        self.written = 0

    def start(self):
        pass

    def stop(self):
        pass

    def close(self):
        pass

    def write(self, pcm):
        if self.fail_write:
            raise RuntimeError("device rejected the buffer")
        self.written += len(pcm)


class _FakeBackend:
    def __init__(self, *, fail_open=False, fail_write=False):
        self._fail_open = fail_open
        self._fail_write = fail_write

    def RawOutputStream(self, **kwargs):
        if self._fail_open:
            raise RuntimeError("could not open device")
        return _FakeStream(fail_write=self._fail_write)


class _FakeStdout:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def read(self, n):
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProcess:
    def __init__(self, chunks):
        self.stdout = _FakeStdout(chunks)
        self._waited = False

    def wait(self, timeout=None):
        self._waited = True

    def poll(self):
        return 0 if self._waited else None

    def kill(self):
        self._waited = True


def test_test_sound_reports_played_on_success(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    device = _device(0, "Realtek(R) Audio")
    result = core.play_test_tone(
        device, backend=_FakeBackend(),
        popen=lambda *a, **k: _FakeProcess([b"\x00\x01" * 100, b""]),
    )
    assert result.state is core.TestSoundState.PLAYED


def test_test_sound_reports_device_error_when_the_device_will_not_open(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    device = _device(0, "Realtek(R) Audio")
    result = core.play_test_tone(
        device, backend=_FakeBackend(fail_open=True),
        popen=lambda *a, **k: _FakeProcess([b"\x00\x01" * 100, b""]),
    )
    assert result.state is core.TestSoundState.DEVICE_ERROR


def test_test_sound_reports_playback_error_when_the_device_rejects_audio(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    device = _device(0, "Realtek(R) Audio")
    result = core.play_test_tone(
        device, backend=_FakeBackend(fail_write=True),
        popen=lambda *a, **k: _FakeProcess([b"\x00\x01" * 100, b""]),
    )
    assert result.state is core.TestSoundState.PLAYBACK_ERROR


def test_test_sound_reports_device_error_when_ffmpeg_is_missing(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: None)
    device = _device(0, "Realtek(R) Audio")
    result = core.play_test_tone(device, backend=_FakeBackend(),
                                 popen=lambda *a, **k: _FakeProcess([b""]))
    assert result.state is core.TestSoundState.DEVICE_ERROR


def test_test_sound_never_claims_speaker_verified(monkeypatch):
    """SPEAKER_VERIFIED is reserved for LinkGuard acoustic evidence."""
    for state in core.TestSoundState:
        assert state.value != "SPEAKER_VERIFIED"


def test_test_sound_uses_no_console_child(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/ffmpeg")
    captured = {}

    def fake_popen(command, **kwargs):
        captured.update(kwargs)
        return _FakeProcess([b""])

    core.play_test_tone(_device(0, "Realtek(R) Audio"), backend=_FakeBackend(),
                        popen=fake_popen)
    if sys.platform == "win32":
        import subprocess as _subprocess

        assert captured.get("creationflags", 0) & _subprocess.CREATE_NO_WINDOW


# ===========================================================================
# Screen 4 - waiting for CONNECTED, honestly
# ===========================================================================
def test_wait_for_connected_returns_connected_when_the_status_file_says_so(tmp_path):
    status = tmp_path / "receiver-status.json"
    status.write_text(json.dumps({"state": "CONNECTED"}), encoding="utf-8")
    result = core.wait_for_connected(status_path=status, timeout_seconds=1,
                                     poll_interval=0.01, sleep=lambda s: None)
    assert result.state is core.InstallState.CONNECTED


def test_wait_for_connected_times_out_rather_than_waiting_forever(tmp_path):
    status = tmp_path / "receiver-status.json"  # never written

    clock_values = iter([0, 0.5, 1.5, 100])  # exceeds a 1-second timeout

    result = core.wait_for_connected(
        status_path=status, timeout_seconds=1, poll_interval=0.01,
        sleep=lambda s: None, clock=lambda: next(clock_values),
    )
    assert result.state is core.InstallState.TIMED_OUT


def test_wait_for_connected_never_claims_connected_from_a_process_existing(tmp_path):
    """The whole point. A CONFIG_OK/STARTING file must not read as CONNECTED."""
    status = tmp_path / "receiver-status.json"
    status.write_text(json.dumps({"state": "DISCONNECTED"}), encoding="utf-8")
    result = core.wait_for_connected(status_path=status, timeout_seconds=0.05,
                                     poll_interval=0.01, sleep=lambda s: None)
    assert result.state is core.InstallState.TIMED_OUT


# ===========================================================================
# Rerun - never silently re-enrol
# ===========================================================================
def test_detect_existing_installation_when_none_exists(tmp_path):
    result = core.detect_existing_installation(
        credential_path=tmp_path / "cred.bin", protector=FakeCredentialProtector("test-computer"),
    )
    assert result.is_installed is False


def test_detect_existing_installation_when_enrolled(tmp_path, monkeypatch):
    from tools import receiver_agent

    credential_path = tmp_path / "cred.bin"
    protector = FakeCredentialProtector("test-computer")
    receiver_agent.enrol(
        backend_url="https://hq.example.com", code="ECHO-A-CODE",
        device_name="till-1", hostname="TILL-1",
        credential_path=credential_path, protector=protector,
        transport=_EnrolTransport(),
    )
    result = core.detect_existing_installation(credential_path=credential_path,
                                               protector=protector)
    assert result.is_installed is True
    assert result.device_public_id == "dev-1"


class _EnrolTransport:
    def post_json(self, url, payload, *, timeout):
        return 201, {"device_public_id": "dev-1", "store_id": 3,
                    "credential": "speaklink_rcv_v1.11111111-1111-1111-1111-111111111111.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"}
