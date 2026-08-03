"""A local password that protects Store Kit CONFIGURATION, and nothing else.

WHAT IT IS FOR

A Store PC sits in a shop. Anybody who can reach the keyboard can currently
open the Store Kit and repoint the Receiver at a different HQ, change its
audio output, or replace its Device identity. This password stops an ordinary
unauthorized Store user doing that.

WHAT IT IS EMPHATICALLY NOT

It is not the HQ login, not the Receiver Device credential, not an enrolment
code, not a JWT, not the HMAC key, and not a Windows password. Above all it is
NOT required to receive announcements: the Receiver Agent must auto-start,
authenticate, reconnect, heartbeat and play with nobody present. A password
prompt that could block playback would be a worse fault than the one it
prevents - a silent Store is the failure this whole project exists to avoid.

THE HONEST LIMIT, STATED IN CODE AND IN THE DOCS

A Windows Administrator owns the filesystem and can delete the verifier. This
is app-level protection against ordinary Store staff, not a boundary against
the machine's administrator. Nothing here pretends otherwise.

WHY scrypt AND NOT bcrypt

bcrypt is what HQ uses for accounts and is already trusted here - but it is a
third-party package, and the Store Kit is frozen with PyInstaller onto Store
PCs where every dependency is another thing that can fail to bundle.
hashlib.scrypt is in the standard library, is memory-hard, and is on the
brief's list of acceptable choices. The algorithm and its parameters are
recorded in the verifier so a future change can migrate rather than guess.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.store_kit_settings_password import (  # noqa: E402
    SettingsPasswordCorrupt,
    SettingsPasswordNotSet,
    SettingsPasswordRefused,
    SettingsPasswordWeak,
    change_password,
    establish_password,
    is_configured,
    read_verifier,
    require_authorization,
    verify_password,
)

GOOD = "a-store-settings-password"
OTHER = "a-different-store-password"


@pytest.fixture()
def verifier(tmp_path):
    """An isolated profile root. Never the real Store Receiver profile."""
    return tmp_path / "SpeakLink" / "receiver" / "settings-password.json"


# ===========================================================================
# Fresh setup
# ===========================================================================
def test_a_fresh_profile_has_no_password(verifier):
    assert is_configured(verifier) is False


def test_establishing_requires_the_two_entries_to_match(verifier):
    with pytest.raises(SettingsPasswordRefused):
        establish_password(verifier, GOOD, "not-the-same")
    assert not verifier.exists(), "a mismatch wrote a verifier anyway"


def test_an_empty_password_is_refused(verifier):
    for empty in ("", "   "):
        with pytest.raises(SettingsPasswordWeak):
            establish_password(verifier, empty, empty)
    assert not verifier.exists()


def test_a_short_password_is_refused(verifier):
    with pytest.raises(SettingsPasswordWeak):
        establish_password(verifier, "short", "short")
    assert not verifier.exists()


def test_establishing_writes_a_verifier(verifier):
    establish_password(verifier, GOOD, GOOD)
    assert is_configured(verifier)
    assert verify_password(verifier, GOOD) is True


# ===========================================================================
# What is stored
# ===========================================================================
def test_the_plaintext_password_is_nowhere_in_the_file(verifier):
    establish_password(verifier, GOOD, GOOD)
    raw = verifier.read_bytes()
    assert GOOD.encode("utf-8") not in raw
    assert GOOD not in raw.decode("utf-8", "replace")


def test_the_verifier_records_its_algorithm_and_parameters(verifier):
    establish_password(verifier, GOOD, GOOD)
    stored = json.loads(verifier.read_text(encoding="utf-8"))
    assert stored["algorithm"] == "scrypt"
    assert stored["version"] >= 1
    for parameter in ("n", "r", "p"):
        assert isinstance(stored[parameter], int)
    assert stored["salt"] and stored["verifier"]


def test_two_stores_with_the_same_password_get_different_material(tmp_path):
    """Per-install salt. Without it, one leaked verifier would identify every
    Store using the same password - and Stores in one chain very often do."""
    first = tmp_path / "a" / "settings-password.json"
    second = tmp_path / "b" / "settings-password.json"
    establish_password(first, GOOD, GOOD)
    establish_password(second, GOOD, GOOD)

    a = json.loads(first.read_text(encoding="utf-8"))
    b = json.loads(second.read_text(encoding="utf-8"))
    assert a["salt"] != b["salt"]
    assert a["verifier"] != b["verifier"]


def test_the_verifier_is_not_a_plain_hash_of_the_password(verifier):
    """A bare digest would be trivially reversible from a wordlist."""
    import hashlib

    establish_password(verifier, GOOD, GOOD)
    stored = json.loads(verifier.read_text(encoding="utf-8"))
    for naive in (hashlib.sha256(GOOD.encode()).hexdigest(),
                  hashlib.md5(GOOD.encode()).hexdigest(),
                  hashlib.sha1(GOOD.encode()).hexdigest()):
        assert stored["verifier"] != naive


# ===========================================================================
# Verification
# ===========================================================================
def test_the_right_password_verifies(verifier):
    establish_password(verifier, GOOD, GOOD)
    assert verify_password(verifier, GOOD) is True


def test_the_wrong_password_does_not(verifier):
    establish_password(verifier, GOOD, GOOD)
    assert verify_password(verifier, OTHER) is False


def test_verification_of_an_unset_password_refuses_rather_than_allowing(verifier):
    """Fail closed. 'No password set' must never mean 'everything is
    permitted' - that is exactly the upgrade case, where an existing Store has
    no verifier yet."""
    with pytest.raises(SettingsPasswordNotSet):
        require_authorization(verifier, GOOD)


def test_require_authorization_passes_with_the_right_password(verifier):
    establish_password(verifier, GOOD, GOOD)
    assert require_authorization(verifier, GOOD) is True


def test_require_authorization_raises_on_the_wrong_password(verifier):
    establish_password(verifier, GOOD, GOOD)
    with pytest.raises(SettingsPasswordRefused):
        require_authorization(verifier, OTHER)


def test_a_refusal_message_never_contains_the_attempted_password(verifier):
    establish_password(verifier, GOOD, GOOD)
    try:
        require_authorization(verifier, "the-attempted-secret")
    except SettingsPasswordRefused as refusal:
        assert "the-attempted-secret" not in str(refusal)
        assert GOOD not in str(refusal)
    else:
        pytest.fail("a wrong password was accepted")


def test_repeated_wrong_attempts_do_not_damage_the_verifier(verifier):
    establish_password(verifier, GOOD, GOOD)
    before = verifier.read_bytes()
    for _ in range(25):
        assert verify_password(verifier, OTHER) is False
    assert verifier.read_bytes() == before
    # And the real password still works afterwards - a Store must never be
    # locked out of its own settings because somebody guessed badly.
    assert verify_password(verifier, GOOD) is True


# ===========================================================================
# Corruption
# ===========================================================================
def test_a_corrupt_verifier_blocks_settings_changes(verifier):
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text("{not json at all", encoding="utf-8")
    with pytest.raises(SettingsPasswordCorrupt):
        require_authorization(verifier, GOOD)


def test_a_verifier_missing_its_fields_is_corrupt_not_absent(verifier):
    """An empty object must not read as 'no password set', which would turn
    a truncated write into an open door."""
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text("{}", encoding="utf-8")
    with pytest.raises(SettingsPasswordCorrupt):
        require_authorization(verifier, GOOD)


def test_a_corrupt_verifier_is_never_deleted_or_rewritten(verifier):
    """Recovering it may be the only way back. Deleting it automatically
    would also be a free reset for anybody able to corrupt the file."""
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text("{broken", encoding="utf-8")
    before = verifier.read_bytes()
    for _ in range(3):
        with pytest.raises(SettingsPasswordCorrupt):
            require_authorization(verifier, GOOD)
    assert verifier.read_bytes() == before


def test_a_corrupt_verifier_cannot_be_overwritten_by_establishing(verifier):
    """Otherwise corrupting the file becomes the reset mechanism."""
    verifier.parent.mkdir(parents=True, exist_ok=True)
    verifier.write_text("{broken", encoding="utf-8")
    with pytest.raises(SettingsPasswordCorrupt):
        establish_password(verifier, OTHER, OTHER)


def test_establishing_twice_is_refused(verifier):
    """Establishing is for a Store that has none. Replacing a known password
    is change_password, which requires the current one."""
    establish_password(verifier, GOOD, GOOD)
    with pytest.raises(SettingsPasswordRefused):
        establish_password(verifier, OTHER, OTHER)
    assert verify_password(verifier, GOOD) is True


# ===========================================================================
# Changing it
# ===========================================================================
def test_changing_requires_the_current_password(verifier):
    establish_password(verifier, GOOD, GOOD)
    with pytest.raises(SettingsPasswordRefused):
        change_password(verifier, "wrong-current-password", OTHER, OTHER)
    assert verify_password(verifier, GOOD) is True, "the old password stopped working"


def test_changing_requires_the_new_entries_to_match(verifier):
    establish_password(verifier, GOOD, GOOD)
    with pytest.raises(SettingsPasswordRefused):
        change_password(verifier, GOOD, OTHER, "something-else-entirely")
    assert verify_password(verifier, GOOD) is True


def test_changing_rejects_a_weak_new_password(verifier):
    establish_password(verifier, GOOD, GOOD)
    with pytest.raises(SettingsPasswordWeak):
        change_password(verifier, GOOD, "tiny", "tiny")
    assert verify_password(verifier, GOOD) is True


def test_a_successful_change_swaps_which_password_works(verifier):
    establish_password(verifier, GOOD, GOOD)
    change_password(verifier, GOOD, OTHER, OTHER)
    assert verify_password(verifier, OTHER) is True
    assert verify_password(verifier, GOOD) is False


def test_a_change_rotates_the_salt(verifier):
    """A new password on the old salt would let anyone who captured the old
    file confirm a guess against the new one more cheaply."""
    establish_password(verifier, GOOD, GOOD)
    before = json.loads(verifier.read_text(encoding="utf-8"))["salt"]
    change_password(verifier, GOOD, OTHER, OTHER)
    after = json.loads(verifier.read_text(encoding="utf-8"))["salt"]
    assert before != after


def test_a_failed_change_leaves_the_file_byte_identical(verifier):
    establish_password(verifier, GOOD, GOOD)
    before = verifier.read_bytes()
    with pytest.raises(SettingsPasswordRefused):
        change_password(verifier, "wrong", OTHER, OTHER)
    assert verifier.read_bytes() == before


# ===========================================================================
# It stays local
# ===========================================================================
def test_the_verifier_carries_no_receiver_or_hq_material(verifier):
    """This file must be about one thing. A Store token or backend URL living
    beside the verifier is a file that gets copied for the wrong reason."""
    establish_password(verifier, GOOD, GOOD)
    stored = json.loads(verifier.read_text(encoding="utf-8"))
    for forbidden in ("token", "credential", "backend_url", "device", "jwt",
                      "hmac", "store_code", "enrolment", "enrollment"):
        assert forbidden not in json.dumps(stored).lower()


def test_the_password_never_reaches_the_process_environment(verifier):
    before = dict(os.environ)
    establish_password(verifier, GOOD, GOOD)
    require_authorization(verifier, GOOD)
    assert os.environ == before
    assert not any(GOOD in value for value in os.environ.values())


def test_reading_the_verifier_returns_no_secret_material(verifier):
    """read_verifier is what a status screen would call. It must be able to
    say 'a password is set' without handing anything back that helps guess
    it."""
    establish_password(verifier, GOOD, GOOD)
    described = read_verifier(verifier)
    body = json.dumps(described, default=str).lower()
    assert GOOD not in body
    assert "salt" not in body
    assert "verifier" not in body
    assert described["configured"] is True


def test_the_file_is_written_atomically(verifier, monkeypatch):
    """A half-written verifier is a Store that cannot change its settings and
    cannot prove why. The same tmp-then-replace convention the Receiver config
    already uses."""
    establish_password(verifier, GOOD, GOOD)
    leftovers = list(verifier.parent.glob("*.tmp"))
    assert leftovers == [], f"a temporary file survived: {leftovers}"
