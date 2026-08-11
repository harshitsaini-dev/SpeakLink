"""What a Store computer needs so nobody behind the counter has to think.

The pilot worked, but only because a person typed a long command with the right
audio selector in it. For a real Store the same settings have to survive a
reboot, a repair and an upgrade without anybody retyping them, and the process
has to run with no window for somebody to close.

Two pieces are tested here:

**A configuration file.** Non-secret settings - backend URL, audio selector,
log directory - written once by the installer and read at every start. The
Device credential stays where it is, sealed by DPAPI; nothing secret is ever
written here, and a config file that contained a credential would be a
credential in a plain file on a shop counter.

**A diagnostics command.** One read-only command a technician can run over the
phone, which answers "is it installed, is it running, can it reach HQ, which
speaker did it pick" without showing a single secret.

Nothing here opens an audio device, starts a process or reaches a network.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from tools.receiver_agent import (  # noqa: E402
    CONFIG_FILENAME,
    AgentError,
    ReceiverConfig,
    build_parser,
    default_config_path,
    diagnose_report,
    load_config,
    main,
    merge_config_into_arguments,
    save_config,
)


SELECTOR = "index:8@Speakers (Realtek(R) Audio)"
CREDENTIAL = "speaklink_rcv_v1.11111111-2222-4333-8444-555555555555." + "s" * 43


@pytest.fixture()
def config_path(tmp_path):
    return tmp_path / CONFIG_FILENAME


# ===========================================================================
# Where it lives
# ===========================================================================
def test_the_config_sits_beside_the_credential(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_config_path() == Path(
        r"C:\Users\someone\AppData\Local\SpeakLink\receiver") / CONFIG_FILENAME


def test_the_config_is_not_the_credential(monkeypatch):
    """Same folder, different file, and only one of them is sealed."""
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")
    assert default_config_path().name != "device-credential.bin"


# ===========================================================================
# Writing and reading
# ===========================================================================
def test_a_saved_config_reads_back_unchanged(config_path):
    saved = ReceiverConfig(
        backend_url="http://192.168.4.134:8000",
        expected_hq_host="192.168.4.134",
        allow_insecure_private_lan=True,
        audio_sink="windows",
        audio_output_device=SELECTOR,
        log_directory=r"C:\logs",
    )
    save_config(config_path, saved)
    assert load_config(config_path) == saved


def test_a_missing_config_is_not_an_error(tmp_path):
    """A machine that has never been installed must still be able to run by
    hand, exactly as the pilot did."""
    assert load_config(tmp_path / "nothing.json") is None


def test_a_corrupt_config_is_refused_rather_than_half_applied(config_path):
    """Half-applied settings are how a Store ends up pointed at the right HQ
    with the wrong speaker."""
    config_path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(AgentError):
        load_config(config_path)


def test_an_unknown_key_is_ignored_so_an_older_agent_still_starts(config_path):
    """Forward compatibility: a newer installer must not brick an older EXE
    that has not been upgraded yet."""
    config_path.write_text(json.dumps({
        "backend_url": "http://192.168.4.134:8000",
        "audio_sink": "windows",
        "audio_output_device": SELECTOR,
        "something_added_next_year": True,
    }), encoding="utf-8")
    loaded = load_config(config_path)
    assert loaded.audio_output_device == SELECTOR


def test_saving_is_atomic_enough_to_survive_a_power_cut(config_path):
    """A half-written config on a shop counter is a Store that will not start."""
    save_config(config_path, ReceiverConfig(backend_url="http://192.168.4.134:8000"))
    save_config(config_path, ReceiverConfig(backend_url="http://192.168.4.135:8000"))
    assert load_config(config_path).backend_url == "http://192.168.4.135:8000"
    assert not list(config_path.parent.glob("*.tmp*")), "a temporary file was left behind"


# ===========================================================================
# Nothing secret goes in it
# ===========================================================================
@pytest.mark.parametrize("field", ["credential", "password", "code", "token", "secret"])
def test_the_config_has_no_field_for_a_secret(field):
    assert not hasattr(ReceiverConfig(), field)


def test_a_secret_smuggled_into_a_config_file_is_refused(config_path):
    """Belt and braces: an installer bug, or somebody being helpful, must not
    turn this into a plaintext credential store."""
    config_path.write_text(json.dumps({
        "backend_url": "http://192.168.4.134:8000",
        "credential": CREDENTIAL,
    }), encoding="utf-8")
    with pytest.raises(AgentError) as refusal:
        load_config(config_path)
    assert CREDENTIAL not in str(refusal.value)


def test_a_written_config_contains_no_credential_shaped_value(config_path):
    save_config(config_path, ReceiverConfig(
        backend_url="http://192.168.4.134:8000", audio_output_device=SELECTOR))
    text = config_path.read_text(encoding="utf-8")
    assert "speaklink_rcv_v1" not in text
    assert "$2b$" not in text


# ===========================================================================
# The command line still wins
# ===========================================================================
def test_the_config_supplies_what_the_command_line_omits():
    arguments = build_parser().parse_args(["run", "--backend-url", "http://192.168.4.134:8000"])
    merge_config_into_arguments(arguments, ReceiverConfig(
        backend_url="http://ignored:8000", audio_sink="windows",
        audio_output_device=SELECTOR))
    assert arguments.audio_sink == "windows"
    assert arguments.audio_output_device == SELECTOR


def test_an_explicit_option_beats_the_saved_one():
    """A technician debugging on site must be able to override without editing
    a file, and without their override being remembered."""
    arguments = build_parser().parse_args([
        "run", "--backend-url", "http://192.168.4.134:8000",
        "--audio-sink", "null",
    ])
    merge_config_into_arguments(arguments, ReceiverConfig(
        audio_sink="windows", audio_output_device=SELECTOR))
    assert arguments.audio_sink == "null"


def test_the_backend_url_can_come_from_the_config():
    """So the scheduled task's command line can be short and boring."""
    arguments = build_parser().parse_args(["run"])
    merge_config_into_arguments(arguments, ReceiverConfig(
        backend_url="http://192.168.4.134:8000", expected_hq_host="192.168.4.134",
        allow_insecure_private_lan=True))
    assert arguments.backend_url == "http://192.168.4.134:8000"
    assert arguments.expected_hq_host == "192.168.4.134"
    assert arguments.allow_insecure_private_lan is True


def test_run_no_longer_demands_a_backend_url_on_the_command_line():
    """It has to come from somewhere, but a saved install should not have to
    repeat it in the task arguments."""
    arguments = build_parser().parse_args(["run"])
    assert arguments.backend_url is None


def test_a_run_with_neither_a_url_nor_a_config_is_refused(tmp_path, capsys):
    code = main(["run", "--config-path", str(tmp_path / "absent.json"),
                 "--credential-path", str(tmp_path / "device-credential.bin")])
    assert code != 0
    assert "backend" in (capsys.readouterr().err + capsys.readouterr().out).lower()


# ===========================================================================
# Diagnostics
# ===========================================================================
def test_diagnose_runs_without_a_config_or_a_credential(tmp_path):
    report = diagnose_report(config_path=tmp_path / "absent.json",
                             credential_path=tmp_path / "absent.bin")
    assert isinstance(report, str)
    assert report.strip()


def test_diagnose_says_whether_a_credential_exists_without_reading_it(tmp_path):
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(CREDENTIAL.encode())
    report = diagnose_report(config_path=tmp_path / "absent.json",
                            credential_path=credential)
    assert "present" in report.lower()
    assert CREDENTIAL not in report
    assert "s" * 43 not in report


def test_diagnose_reports_the_configured_audio_selector(tmp_path):
    config = tmp_path / CONFIG_FILENAME
    save_config(config, ReceiverConfig(
        backend_url="http://192.168.4.134:8000", audio_sink="windows",
        audio_output_device=SELECTOR))
    report = diagnose_report(config_path=config, credential_path=tmp_path / "absent.bin")
    assert SELECTOR in report
    assert "windows" in report


def test_diagnose_names_the_version(tmp_path):
    """Whatever version this build IS, diagnose has to say it.

    This pinned the literal "1.0.0", which passed for every build ever made
    precisely because AGENT_VERSION was never updated - the test agreed with
    the defect instead of catching it. Asserting against the constant means it
    keeps checking that the report names the version, and stops needing an edit
    every time the version legitimately changes.
    """
    from tools.receiver_agent import AGENT_VERSION

    report = diagnose_report(config_path=tmp_path / "absent.json",
                            credential_path=tmp_path / "absent.bin")
    assert AGENT_VERSION in report
    assert "agent version" in report


def test_diagnose_is_a_command(capsys, tmp_path):
    assert main(["diagnose", "--config-path", str(tmp_path / "absent.json"),
                 "--credential-path", str(tmp_path / "absent.bin")]) == 0
    assert capsys.readouterr().out.strip()


def test_diagnose_never_prints_a_secret(capsys, tmp_path):
    credential = tmp_path / "device-credential.bin"
    credential.write_bytes(CREDENTIAL.encode())
    main(["diagnose", "--config-path", str(tmp_path / "absent.json"),
          "--credential-path", str(credential)])
    printed = capsys.readouterr().out
    assert "speaklink_rcv_v1" not in printed
    assert "s" * 43 not in printed


# ===========================================================================
# The packaged build ships a windowed executable
# ===========================================================================
def test_the_spec_builds_a_windowed_background_executable():
    """Windows decides on a console from a flag in the executable header, not
    from how it was launched. Task Scheduler's "hidden" setting hides the task
    in its own UI - it does not stop a console application creating a window on
    the Store counter for a member of staff to close.
    """
    spec = (REPOSITORY_ROOT / "receiver_agent.spec").read_text(encoding="utf-8")
    assert "SpeakLinkReceiverBackground" in spec
    assert "console=False" in spec


def test_the_spec_still_builds_a_console_executable_for_people():
    """list-audio-devices prints a table and enrol reads a hidden prompt.
    Neither works in a process with no stdout."""
    spec = (REPOSITORY_ROOT / "receiver_agent.spec").read_text(encoding="utf-8")
    assert "console=True" in spec
    assert 'name="SpeakLinkReceiver"' in spec


def test_both_executables_are_collected_together():
    spec = (REPOSITORY_ROOT / "receiver_agent.spec").read_text(encoding="utf-8")
    collect = spec[spec.index("COLLECT("):]
    assert "console_executable" in collect and "background_executable" in collect


def test_the_windowed_build_does_not_pop_a_traceback_dialog():
    """An unattended counter is exactly where a modal error box sits unclosed
    for a week with the Receiver dead behind it."""
    spec = (REPOSITORY_ROOT / "receiver_agent.spec").read_text(encoding="utf-8")
    background = spec[spec.index("background_executable"):]
    assert "disable_windowed_traceback=True" in background
