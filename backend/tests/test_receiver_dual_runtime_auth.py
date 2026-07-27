"""Accepting Device credentials without breaking the Receivers already running.

The backend authenticates Receivers with LegacyStoreTokenRuntimeAuthenticator:
one raw token per Store, shared by every computer in it. Device credentials now
exist, but switching outright would disconnect the amplifier Receiver that
produced every piece of hardware evidence so far, at the moment it is switched.

So both are accepted, deliberately and temporarily, by trying the Device
credential first and falling back to the legacy token. Two properties matter
more than the convenience:

* a **revoked or disabled** Device must never fall through and be accepted as
  its Store's legacy token. If it could, revocation would be theatre.
* the caller must be able to see WHICH proved the identity, so the dashboard can
  show honestly that a Store is still on a shared token.

Nothing here opens a socket. Fake authenticators stand in for both sides so the
composition itself is what is tested.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone
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

from receiver_connection_inventory import ConnectionAuthenticationSource  # noqa: E402
from receiver_runtime_auth import (  # noqa: E402
    DualRuntimeAuthenticator,
    ReceiverRuntimeAuthenticationError,
    ReceiverRuntimeConfigurationError,
    ReceiverRuntimeIdentity,
)


NOW = datetime(2026, 7, 27, 10, 0, tzinfo=timezone.utc)
DEVICE_CREDENTIAL = "echocast_rcv_v1.11111111-2222-4333-8444-555555555555.aaaa"
LEGACY_TOKEN = "a" * 32


class FakeAuthenticator:
    """Accepts exactly one token and records every attempt."""

    def __init__(self, accepts: str | None, identity: ReceiverRuntimeIdentity | None = None) -> None:
        self._accepts = accepts
        self._identity = identity
        self.attempts: list[str] = []

    def authenticate(self, *, presented_token: str, authenticated_at: datetime):
        self.attempts.append(presented_token)
        if self._accepts is not None and presented_token == self._accepts:
            return self._identity
        raise ReceiverRuntimeAuthenticationError()


def _device_identity() -> ReceiverRuntimeIdentity:
    return ReceiverRuntimeIdentity(
        store_id=1, device_id=7, credential_id=9,
        authentication_source=ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL,
    )


def _legacy_identity() -> ReceiverRuntimeIdentity:
    return ReceiverRuntimeIdentity(
        store_id=1,
        authentication_source=ConnectionAuthenticationSource.LEGACY_STORE_TOKEN,
    )


def _dual(device_accepts=DEVICE_CREDENTIAL, legacy_accepts=LEGACY_TOKEN):
    device = FakeAuthenticator(device_accepts, _device_identity())
    legacy = FakeAuthenticator(legacy_accepts, _legacy_identity())
    return DualRuntimeAuthenticator(device=device, legacy=legacy), device, legacy


# ---------------------------------------------------------------------------
# Both transports work
# ---------------------------------------------------------------------------
def test_a_device_credential_is_accepted():
    dual, _, _ = _dual()
    identity = dual.authenticate(presented_token=DEVICE_CREDENTIAL, authenticated_at=NOW)
    assert identity.device_id == 7
    assert identity.authentication_source is ConnectionAuthenticationSource.HASHED_DEVICE_CREDENTIAL


def test_a_legacy_store_token_still_works():
    """The amplifier Receiver that produced every hardware result so far is on
    one of these. Breaking it to make a point would be its own kind of failure."""
    dual, _, _ = _dual()
    identity = dual.authenticate(presented_token=LEGACY_TOKEN, authenticated_at=NOW)
    assert identity.device_id is None
    assert identity.authentication_source is ConnectionAuthenticationSource.LEGACY_STORE_TOKEN


def test_the_device_credential_is_tried_first():
    """A Store that has enrolled Devices should stop depending on the shared
    token as soon as one is presented, without any configuration change."""
    dual, device, legacy = _dual()
    dual.authenticate(presented_token=DEVICE_CREDENTIAL, authenticated_at=NOW)
    assert device.attempts == [DEVICE_CREDENTIAL]
    assert legacy.attempts == [], "the legacy path was consulted unnecessarily"


# ---------------------------------------------------------------------------
# Revocation must actually revoke
# ---------------------------------------------------------------------------
def test_a_revoked_device_credential_is_not_rescued_by_the_legacy_path():
    """The whole point of per-Device revocation. If a revoked credential could
    fall through and be accepted as its Store's token, revocation would be
    theatre."""
    dual, _, legacy = _dual(device_accepts=None)  # the Device path now refuses it
    with pytest.raises(ReceiverRuntimeAuthenticationError):
        dual.authenticate(presented_token=DEVICE_CREDENTIAL, authenticated_at=NOW)
    assert legacy.attempts == [DEVICE_CREDENTIAL]  # it was tried, and it refused too


def test_an_unknown_token_is_refused_by_both():
    dual, device, legacy = _dual()
    with pytest.raises(ReceiverRuntimeAuthenticationError):
        dual.authenticate(presented_token="neither-of-them", authenticated_at=NOW)
    assert device.attempts == legacy.attempts == ["neither-of-them"]


def test_the_refusal_is_identical_whichever_path_failed():
    """Two different errors would tell a caller whether a Store has Devices."""
    dual, _, _ = _dual()
    refusals = []
    for token in ("neither-of-them", "echocast_rcv_v1.deadbeef", "b" * 32):
        try:
            dual.authenticate(presented_token=token, authenticated_at=NOW)
        except ReceiverRuntimeAuthenticationError as refusal:
            refusals.append(str(refusal))
    assert len(set(refusals)) == 1, f"the refusal varies by path: {set(refusals)}"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------
def test_both_authenticators_are_required():
    for kwargs in ({"device": None, "legacy": FakeAuthenticator(None)},
                   {"device": FakeAuthenticator(None), "legacy": None}):
        with pytest.raises(ReceiverRuntimeConfigurationError):
            DualRuntimeAuthenticator(**kwargs)


def test_it_never_prints_a_token():
    dual, _, _ = _dual()
    for rendering in (repr(dual), str(dual)):
        assert DEVICE_CREDENTIAL not in rendering
        assert LEGACY_TOKEN not in rendering


def test_a_failing_device_path_does_not_hide_a_configuration_error():
    """A missing key ring is the operator's problem and must not be silently
    swallowed into "bad credential"."""

    class Misconfigured:
        def authenticate(self, *, presented_token, authenticated_at):
            raise ReceiverRuntimeConfigurationError()

    dual = DualRuntimeAuthenticator(device=Misconfigured(), legacy=FakeAuthenticator(LEGACY_TOKEN))
    with pytest.raises(ReceiverRuntimeConfigurationError):
        dual.authenticate(presented_token=LEGACY_TOKEN, authenticated_at=NOW)


# ---------------------------------------------------------------------------
# The dashboard can tell the difference
# ---------------------------------------------------------------------------
def test_the_identity_says_which_transport_proved_it():
    dual, _, _ = _dual()
    device = dual.authenticate(presented_token=DEVICE_CREDENTIAL, authenticated_at=NOW)
    legacy = dual.authenticate(presented_token=LEGACY_TOKEN, authenticated_at=NOW)
    assert device.authentication_source != legacy.authentication_source
