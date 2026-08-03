"""The Settings Password protects CHANGES, and nothing else.

Three properties, each of which would be a release blocker if it broke.

THE GATE IS IN THE CORE, NOT THE WINDOW

Every mutating helper refuses without a SettingsAuthorization, so a caller
that never opened the GUI is refused too. These tests call the core functions
directly for exactly that reason: the UI is not the security boundary, and a
test that only clicked buttons would not know the difference.

THE RECEIVER NEVER CONSULTS IT

The Agent auto-starts at logon, authenticates with its own Device credential,
reconnects, heartbeats and plays with nobody present. A password prompt that
could block any of that would be a worse fault than the one this prevents - a
silent Store. Proven structurally: receiver_agent must not import the module
at all, so no future edit can quietly make playback depend on it.

READ-ONLY STAYS OPEN

Status, health and diagnostics answer while the Store Kit is locked. Somebody
diagnosing a silent shop at seven in the morning must not need a password to
find out what is wrong.
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
from tools import store_kit_settings_password as settings_password  # noqa: E402

PASSWORD = "a-store-settings-password"


def _authorized():
    from datetime import datetime, timezone

    return core.SettingsAuthorization(granted_at=datetime.now(timezone.utc))


@pytest.fixture()
def profile(tmp_path):
    """An isolated Store profile: config, credential, verifier. Never the real
    one - a test that reached the live profile could lock a technician out of
    a working Store."""
    root = tmp_path / "receiver"
    root.mkdir(parents=True)
    config = root / "config.json"
    config.write_text(json.dumps({
        "backend_url": "https://old.example",
        "expected_hq_host": "old.example",
        "allow_insecure_private_lan": False,
        "audio_sink": "windows",
        "audio_output_device": "Speakers (Original)",
        "log_directory": None,
        "installed_version": "0.1.0",
        "source_commit": "abc1234",
    }, indent=2) + "\n", encoding="utf-8")
    credential = root / "receiver-credential.bin"
    credential.write_bytes(b"not-a-real-sealed-credential")
    return root, config, credential


# ===========================================================================
# The public stop bypass is gone
# ===========================================================================
def test_the_public_stop_has_no_bypass_parameter():
    """`_internal=True` was a bypass an ordinary caller could pass. A renamed
    flag would be the same defect, so this checks the SIGNATURE."""
    import inspect

    parameters = inspect.signature(core.stop_receiver).parameters
    assert "_internal" not in parameters
    for name in parameters:
        assert "internal" not in name.lower()
        assert "bypass" not in name.lower()
        assert "force" not in name.lower()
    assert "authorization" in parameters


def test_stopping_without_authorization_is_refused():
    with pytest.raises(settings_password.SettingsPasswordRefused):
        core.stop_receiver(run=lambda *a, **k: None)


def test_the_private_primitive_is_what_restart_uses():
    """A restart must not ask for the password twice, and must not need a
    bypass on the public function to avoid it."""
    import inspect

    assert hasattr(core, "_stop_receiver_task")
    source = inspect.getsource(core.restart_receiver)
    assert "_stop_receiver_task(" in source
    assert "stop_receiver(" not in source.replace("_stop_receiver_task(", "")


# ===========================================================================
# Every mutating core helper refuses without proof
# ===========================================================================
@pytest.mark.parametrize("call", [
    pytest.param(lambda: core.repair_installation(package_path="x"), id="repair"),
    pytest.param(lambda: core.stop_receiver(), id="stop"),
    pytest.param(lambda: core.uninstall_receiver(), id="uninstall"),
    pytest.param(lambda: core.replace_device_identity(
        credential_path="x", confirmation_word=core.CONFIRMATION_WORD),
        id="replace-identity"),
    pytest.param(lambda: core.redeem_enrollment(
        backend_url="https://hq.example", code="ABC", device_name="till",
        hostname="till", credential_path="x", protector=None), id="enrol"),
])
def test_a_direct_core_call_without_authorization_is_refused(call):
    """Called directly, with no GUI anywhere. This is what a script, another
    tool, or a curious person with a Python prompt would do."""
    with pytest.raises(settings_password.SettingsPasswordRefused):
        call()


def test_replace_identity_needs_the_password_AND_the_typed_word(profile):
    """Neither substitutes for the other: the password says who, the typed
    word says they meant it."""
    _root, _config, credential = profile

    # Right word, no password.
    with pytest.raises(settings_password.SettingsPasswordRefused):
        core.replace_device_identity(credential_path=credential,
                                     confirmation_word=core.CONFIRMATION_WORD)
    assert credential.exists()

    # Password, wrong word - refused by returning False, credential intact.
    assert core.replace_device_identity(
        credential_path=credential, confirmation_word="nope",
        authorization=_authorized()) is False
    assert credential.exists()


# ===========================================================================
# Config is byte-identical after a refusal
# ===========================================================================
def test_an_unauthorized_audio_change_leaves_the_config_byte_identical(profile):
    _root, config, _credential = profile
    before = config.read_bytes()

    class Device:
        verified_selector = "Speakers (Evil)"

    with pytest.raises(settings_password.SettingsPasswordRefused):
        core.change_audio_output(device=Device(), config_path=config,
                                 run=lambda *a, **k: None)

    assert config.read_bytes() == before


def test_an_unauthorized_enrolment_leaves_the_credential_untouched(profile):
    """Enrolment is where backend_url and the Device identity are written, so
    this is the HQ-address protection too: there is no separate editor."""
    _root, config, credential = profile
    config_before = config.read_bytes()
    credential_before = credential.read_bytes()

    with pytest.raises(settings_password.SettingsPasswordRefused):
        core.redeem_enrollment(
            backend_url="https://evil.example", code="ABC",
            device_name="till", hostname="till",
            credential_path=credential, protector=None)

    assert config.read_bytes() == config_before
    assert credential.read_bytes() == credential_before


# ===========================================================================
# Read-only stays open
# ===========================================================================
def test_reading_status_needs_no_authorization(profile):
    """Whatever it reports, it must not RAISE for lack of a password."""
    root, config, credential = profile
    try:
        core.get_status_snapshot(credential_path=credential, protector=None,
                                 config_path=config,
                                 run=lambda *a, **k: None)
    except settings_password.SettingsPasswordError:
        pytest.fail("reading status demanded the Settings Password")
    except Exception:
        pass  # any other failure is this fixture's shape, not the gate


def test_reading_diagnostics_needs_no_authorization(profile):
    _root, config, credential = profile
    try:
        core.build_redacted_diagnostics(credential_path=credential,
                                        config_path=config, devices=[])
    except settings_password.SettingsPasswordError:
        pytest.fail("diagnostics demanded the Settings Password")
    except Exception:
        pass


def test_no_read_only_helper_mentions_authorization():
    """A viewing function that grew an authorization argument would be a
    viewing function somebody made you log in for."""
    import inspect

    for name in ("get_status_snapshot", "build_redacted_diagnostics",
                 "query_task_state", "export_diagnostics", "open_log_folder",
                 "detect_existing_installation", "list_classified_outputs"):
        function = getattr(core, name)
        assert "authorization" not in inspect.signature(function).parameters, (
            f"{name} is a read-only helper and must not require authorization")


# ===========================================================================
# The Receiver runtime is independent - release blocker
# ===========================================================================
def test_the_receiver_agent_does_not_import_the_settings_password_module():
    """Structural, so no future edit can quietly make playback depend on it.

    Checked by parsing the imports rather than scanning text: a mention in a
    docstring explaining that the Agent must NOT depend on this module would
    otherwise fail the test that protects the property.
    """
    import ast

    source = (REPOSITORY_ROOT / "tools" / "receiver_agent.py").read_text(
        encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    offenders = [name for name in imported if "settings_password" in name]
    assert not offenders, (
        f"receiver_agent imports {offenders} - Receiver playback must never "
        "depend on the Settings Password")


def test_a_corrupt_verifier_does_not_stop_the_receiver_reading_its_config(profile):
    """The Agent loads config.json. A broken settings-password.json beside it
    must be irrelevant to that - only settings CHANGES fail closed."""
    from tools.receiver_agent import load_config

    root, config, _credential = profile
    (root / settings_password.VERIFIER_FILENAME).write_text(
        "{broken", encoding="utf-8")

    loaded = load_config(config)
    assert loaded is not None
    assert loaded.backend_url == "https://old.example"


def test_a_corrupt_verifier_blocks_a_settings_change(profile):
    root, config, _credential = profile
    verifier = root / settings_password.VERIFIER_FILENAME
    verifier.write_text("{broken", encoding="utf-8")
    before = verifier.read_bytes()

    with pytest.raises(settings_password.SettingsPasswordCorrupt):
        core.authorize_settings("anything", verifier_path=verifier)

    # And it is neither repaired nor replaced.
    assert verifier.read_bytes() == before


# ===========================================================================
# Nothing leaks
# ===========================================================================
def test_no_store_kit_code_puts_the_password_on_a_command_line():
    """A command line is readable by every user on the machine through the
    process list."""
    for name in ("store_setup_core.py", "store_setup_gui.py",
                 "store_kit_settings_password.py"):
        source = (REPOSITORY_ROOT / "tools" / name).read_text(encoding="utf-8")
        lowered = source.lower()
        for forbidden in ("--settings-password", "-settingspassword",
                          "settings_password_plaintext"):
            assert forbidden not in lowered, f"{name} carries {forbidden}"


def test_the_verifier_module_is_never_reached_by_hq_facing_code():
    """Nothing that talks to HQ has any business importing it."""
    import ast

    for name in ("receiver_agent.py", "receiver_credential_store.py"):
        source = (REPOSITORY_ROOT / "tools" / name).read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source)):
            module = None
            if isinstance(node, ast.Import):
                module = " ".join(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
            if module and "settings_password" in module:
                pytest.fail(f"{name} imports the Settings Password module")


def test_there_is_no_in_app_forgot_or_reset_password():
    """A product decision, pinned. Recovery is an authorized Windows
    Administrator procedure precisely BECAUSE an administrator already owns
    the filesystem - so an in-app bypass would only weaken the protection
    against ordinary Store users without helping anybody else."""
    source = (REPOSITORY_ROOT / "tools" / "store_setup_gui.py").read_text(
        encoding="utf-8").lower()
    for forbidden in ("forgot password", "forgot settings password",
                      "reset password", "reset settings password",
                      "master password", "recovery code", "security question"):
        assert forbidden not in source, f"an in-app bypass appeared: {forbidden}"
