"""The production Windows Receiver Agent: enrol once, then run for months.

``audio_receiver_pilot.py`` proved a Store can receive and play audio. It is a
pilot: the operator exports a shared Store token and starts it by hand. This is
the thing that actually gets installed on 44 tills, so the questions are
different ones.

* **The code and the credential must never reach a command line.** ``tasklist``,
  Windows event logs, crash dumps and any screen-share show process arguments to
  anyone standing there. The code is typed into a hidden prompt or piped on
  stdin; the credential is read from a DPAPI-sealed file and only ever appears in
  an ``Authorization`` header.
* **A revoked Device must stop.** An Agent that reconnects forever after being
  revoked is a Store that cannot be taken off the air, and 44 of them retrying in
  a loop is a denial of service HQ inflicted on itself.
* **CONNECTED is not READY and READY is not PLAYBACK_CONFIRMED.** The whole
  status model rests on that, so the Agent must not shortcut it - which is why it
  reuses the pilot's own state methods rather than reimplementing them.

Nothing here opens a socket or reaches the network. The HTTP transport and the
WebSocket connector are both injected, so what is under test is the Agent's
decisions rather than someone else's I/O.
"""

from __future__ import annotations

import asyncio
import io
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
for candidate in (REPOSITORY_ROOT, REPOSITORY_ROOT / "backend"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from tools.audio_receiver_pilot import AudioReceiverPilot  # noqa: E402
from tools.receiver_credential_store import (  # noqa: E402
    CredentialStoreError,
    FakeCredentialProtector,
    load_credential,
    save_credential,
)
from tools.receiver_agent import (  # noqa: E402
    AgentError,
    BackoffPolicy,
    DeviceReceiverSession,
    EnrolmentAmbiguous,
    EnrolmentRefused,
    InsecureBackendError,
    SupervisorOutcome,
    TerminalAuthentication,
    build_parser,
    enrol,
    enrolment_endpoint,
    normalise_backend_url,
    read_enrolment_code,
    receiver_websocket_url,
    supervise,
)


CODE = "ECHO-4H7K-9QW2"
CREDENTIAL = "speaklink_rcv_v1.11111111-2222-4333-8444-555555555555." + "s" * 43
DEVICE_PUBLIC_ID = "11111111-2222-4333-8444-555555555555"
NOW = datetime(2026, 7, 27, 9, 0, tzinfo=timezone.utc)
HQ = "https://hq.example.internal"


@pytest.fixture()
def protector() -> FakeCredentialProtector:
    return FakeCredentialProtector("this-computer")


@pytest.fixture()
def credential_path(tmp_path: Path) -> Path:
    return tmp_path / "receiver" / "device-credential.bin"


class FakeTransport:
    """Records exactly what was sent where, and answers however a test says."""

    def __init__(self, responses=None) -> None:
        self.responses = list(responses or [(200, {
            "device_public_id": DEVICE_PUBLIC_ID,
            "credential": CREDENTIAL,
            "credential_version": 1,
            "store_id": 7,
        })])
        self.calls: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict, *, timeout: float):
        self.calls.append((url, dict(payload)))
        outcome = self.responses.pop(0) if self.responses else (500, {})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _enrol(credential_path, protector, transport=None, **overrides):
    parameters = dict(
        backend_url=HQ,
        code=CODE,
        device_name="Store UN till 1",
        hostname="UN-TILL-1",
        software_version="1.0.0",
        credential_path=credential_path,
        protector=protector,
        transport=transport or FakeTransport(),
        now=NOW,
        allow_insecure_loopback=False,
    )
    parameters.update(overrides)
    return enrol(**parameters)


# ===========================================================================
# 1 & 2. Neither secret can reach a command line
# ===========================================================================
def test_the_parser_has_no_option_that_takes_an_enrolment_code():
    """``tasklist`` shows process arguments to anyone standing at the till."""
    parser = build_parser()
    for forbidden in ("--code", "--enrolment-code", "--enrollment-code"):
        with pytest.raises(SystemExit):
            parser.parse_args(["enrol", "--backend-url", HQ, forbidden, CODE])


def test_the_parser_has_no_option_that_takes_a_device_credential():
    parser = build_parser()
    for forbidden in ("--credential", "--token", "--device-credential", "--bearer"):
        with pytest.raises(SystemExit):
            parser.parse_args(["run", "--backend-url", HQ, forbidden, CREDENTIAL])


def test_no_parser_option_accepts_a_secret_shaped_value():
    """Read from the parser itself rather than a list I remembered to update."""
    parser = build_parser()
    names = {
        option
        for action in parser._actions
        for option in action.option_strings
    }
    for subparser in [
        choice
        for action in parser._actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
        for choice in action.choices.values()
    ]:
        names |= {option for action in subparser._actions for option in action.option_strings}
    banned = {"code", "credential", "token", "secret", "password", "bearer", "key"}
    offenders = {name for name in names if any(word in name.lower() for word in banned)}
    # --credential-path names a FILE, not a secret. Anything else is a mistake.
    assert offenders <= {"--credential-path"}, f"secret-shaped options: {offenders}"


def test_the_agent_never_takes_a_store_id_from_the_operator():
    """The authenticated server identity is authoritative. A Store id typed on a
    command line is a way to be wrong about which Store is broadcasting."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--backend-url", HQ, "--store-id", "7"])


# ===========================================================================
# 3 & 4. The code travels in a body, and nothing prints either secret
# ===========================================================================
def test_the_code_is_submitted_in_the_request_body_only(credential_path, protector):
    transport = FakeTransport()
    _enrol(credential_path, protector, transport)
    url, payload = transport.calls[0]
    assert CODE not in url, "the code reached the URL, where every proxy logs it"
    assert payload["code"] == CODE
    assert url == enrolment_endpoint(HQ)


def test_enrolling_prints_neither_the_code_nor_the_credential(credential_path, protector, capsys):
    outcome = _enrol(credential_path, protector)
    captured = capsys.readouterr()
    printed = captured.out + captured.err
    assert CODE not in printed
    assert CREDENTIAL not in printed
    assert "s" * 43 not in printed
    # The safe identity is exactly what the operator needs to see.
    assert outcome.device_public_id == DEVICE_PUBLIC_ID
    assert outcome.store_id == 7


def test_the_enrolment_outcome_carries_no_credential(credential_path, protector):
    outcome = _enrol(credential_path, protector)
    assert not hasattr(outcome, "credential")
    assert CREDENTIAL not in repr(outcome)


def test_a_refusal_never_quotes_the_code_back(credential_path, protector):
    transport = FakeTransport([(400, {"detail": "That enrolment code cannot be used."})])
    with pytest.raises(EnrolmentRefused) as refusal:
        _enrol(credential_path, protector, transport)
    assert CODE not in str(refusal.value)


# ===========================================================================
# 5, 6 & 7. Transport security
# ===========================================================================
def test_plain_http_to_a_real_host_is_refused():
    for url in ("http://hq.example.internal", "http://10.0.0.5:8000", "http://192.168.1.9"):
        with pytest.raises(InsecureBackendError):
            normalise_backend_url(url, allow_insecure_loopback=True)


def test_https_is_accepted():
    assert normalise_backend_url(HQ, allow_insecure_loopback=False) == HQ


def test_loopback_http_needs_the_explicit_flag():
    for url in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"):
        with pytest.raises(InsecureBackendError):
            normalise_backend_url(url, allow_insecure_loopback=False)
        assert normalise_backend_url(url, allow_insecure_loopback=True)


def test_the_flag_does_not_open_a_hole_for_a_lookalike_host():
    """``127.0.0.1.evil.example`` is not loopback, whatever it resembles."""
    for url in ("http://127.0.0.1.evil.example", "http://localhost.evil.example"):
        with pytest.raises(InsecureBackendError):
            normalise_backend_url(url, allow_insecure_loopback=True)


def test_a_url_that_is_not_one_is_refused():
    for url in ("", "hq.example.internal", "ftp://hq", "ws://127.0.0.1:8000"):
        with pytest.raises(AgentError):
            normalise_backend_url(url, allow_insecure_loopback=True)


# ===========================================================================
# The private LAN pilot mode
# ===========================================================================
LAN_HQ = "http://192.168.4.134:8000"
LAN_HOST = "192.168.4.134"


def _lan(url=LAN_HQ, *, private_lan=True, expected=LAN_HOST, loopback=False):
    return normalise_backend_url(
        url,
        allow_insecure_loopback=loopback,
        allow_insecure_private_lan=private_lan,
        expected_hq_host=expected,
    )


def test_the_hq_private_address_is_accepted_with_the_explicit_flag():
    assert _lan() == LAN_HQ


def test_the_same_address_is_refused_without_the_flag():
    """The flag is the decision. Without it this is an ordinary plain-HTTP URL
    to a host that is not loopback, and it is refused like any other."""
    with pytest.raises(InsecureBackendError):
        _lan(private_lan=False)


def test_the_private_lan_flag_requires_an_expected_host():
    """"Any private address" is not the same as "the address the operator
    assigned and firewalled". A typo pointing at another machine on the same
    subnet would otherwise be accepted silently."""
    with pytest.raises(AgentError):
        _lan(expected=None)


def test_a_different_private_address_is_refused_even_with_the_flag():
    with pytest.raises(InsecureBackendError):
        _lan("http://192.168.4.200:8000")
    with pytest.raises(InsecureBackendError):
        _lan("http://10.0.0.5:8000")


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:8000",
        "http://1.1.1.1:8000",
        "http://203.0.113.10:8000",
    ],
)
def test_a_public_http_address_is_refused_even_with_the_flag(url: str):
    """The flag says "private LAN", and this is the half that makes that true."""
    with pytest.raises(InsecureBackendError):
        _lan(url, expected=url.split("//")[1].split(":")[0])


def test_a_hostname_is_refused_even_when_it_looks_internal():
    """A name resolves wherever its owner points it, and can be repointed
    tomorrow. Only a literal private address is checkable at the moment it is
    used."""
    for url in ("http://hq.example.internal:8000", "http://hq:8000",
                "http://192-168-4-134.example.com:8000"):
        with pytest.raises(InsecureBackendError):
            _lan(url, expected="hq.example.internal")


def test_link_local_and_multicast_are_refused():
    for url in ("http://169.254.4.134:8000", "http://224.0.0.1:8000"):
        with pytest.raises(InsecureBackendError):
            _lan(url, expected=url.split("//")[1].split(":")[0])


def test_the_private_lan_flag_does_not_authorise_loopback():
    """The two flags are separate decisions and neither implies the other."""
    with pytest.raises(InsecureBackendError):
        normalise_backend_url(
            "http://127.0.0.1:8000",
            allow_insecure_loopback=False,
            allow_insecure_private_lan=True,
            expected_hq_host="127.0.0.1",
        )


def test_the_loopback_flag_does_not_authorise_the_private_lan():
    with pytest.raises(InsecureBackendError):
        normalise_backend_url(
            LAN_HQ, allow_insecure_loopback=True, allow_insecure_private_lan=False
        )


def test_https_needs_neither_flag_and_is_unaffected():
    assert normalise_backend_url(HQ, allow_insecure_loopback=False) == HQ
    assert normalise_backend_url(
        "https://192.168.4.134:8000",
        allow_insecure_loopback=False, allow_insecure_private_lan=False,
    ) == "https://192.168.4.134:8000"


def test_the_websocket_url_for_the_lan_pilot_is_plain_ws_and_carries_no_credential():
    url = receiver_websocket_url(LAN_HQ)
    assert url == f"ws://{LAN_HOST}:8000/api/ws/receiver"
    assert "?" not in url and "token" not in url


def test_the_parser_exposes_both_flags_separately_and_neither_by_default():
    parser = build_parser()
    arguments = parser.parse_args(["enrol", "--backend-url", LAN_HQ, "--device-name", "x"])
    assert arguments.allow_insecure_loopback is False
    assert arguments.allow_insecure_private_lan is False
    assert arguments.expected_hq_host is None

    asked = parser.parse_args([
        "enrol", "--backend-url", LAN_HQ, "--device-name", "x",
        "--allow-insecure-private-lan", "--expected-hq-host", LAN_HOST,
    ])
    assert asked.allow_insecure_private_lan is True
    assert asked.expected_hq_host == LAN_HOST
    assert asked.allow_insecure_loopback is False


def test_neither_new_option_is_secret_shaped():
    """The same rule as every other option: nothing that could carry a secret."""
    parser = build_parser()
    for option in ("--allow-insecure-private-lan", "--expected-hq-host"):
        assert not any(word in option for word in ("code", "credential", "token", "password"))


def test_enrolling_over_the_private_lan_still_keeps_the_code_out_of_the_url(
    credential_path, protector
):
    transport = FakeTransport()
    _enrol(
        credential_path, protector, transport,
        backend_url=LAN_HQ, allow_insecure_loopback=False,
        allow_insecure_private_lan=True, expected_hq_host=LAN_HOST,
    )
    url, payload = transport.calls[0]
    assert CODE not in url
    assert url == f"{LAN_HQ}/api/receiver-devices/enroll"
    assert payload["code"] == CODE


# ===========================================================================
# 17. The credential is not in the WebSocket URL
# ===========================================================================
def test_the_websocket_url_carries_no_credential():
    url = receiver_websocket_url(HQ)
    assert url == "wss://hq.example.internal/api/ws/receiver"
    assert "?" not in url and "token" not in url


def test_loopback_http_becomes_plain_websocket():
    assert receiver_websocket_url("http://127.0.0.1:8000") == "ws://127.0.0.1:8000/api/ws/receiver"


def test_the_session_sends_the_credential_as_a_bearer_header(credential_path, protector):
    """The one place a credential is allowed to appear."""
    recorded = {}

    async def fake_connect(url, **kwargs):
        recorded["url"] = url
        recorded["headers"] = kwargs.get("additional_headers", {})
        raise _StopBeforeSession()

    session = DeviceReceiverSession(
        ws_url="wss://hq.example.internal/api/ws/receiver",
        credential=CREDENTIAL,
        connect=fake_connect,
    )
    with pytest.raises(_StopBeforeSession):
        asyncio.run(session.run())

    assert CREDENTIAL not in recorded["url"]
    assert recorded["headers"]["Authorization"] == f"Bearer {CREDENTIAL}"


class _StopBeforeSession(Exception):
    """Ends the test at the moment the socket would open."""


# ===========================================================================
# 8, 9 & 10. Storage and reconnecting without re-enrolling
# ===========================================================================
def test_enrolling_stores_the_credential_sealed(credential_path, protector):
    _enrol(credential_path, protector)
    assert credential_path.exists()
    assert CREDENTIAL.encode() not in credential_path.read_bytes()
    assert load_credential(credential_path, protector=protector).credential() == CREDENTIAL


def test_the_agent_reconnects_from_the_stored_credential_without_enrolling(
    credential_path, protector
):
    """The point of enrolling once: months of restarts with no operator, no code
    and no second visit to the Store."""
    _enrol(credential_path, protector)
    transport = FakeTransport([])  # any HTTP call at all would pop from an empty list

    record = load_credential(credential_path, protector=protector)
    assert record.credential() == CREDENTIAL
    assert record.store_id == 7
    assert transport.calls == [], "running must not talk to the enrolment endpoint"


def test_enrolling_twice_is_refused_before_the_code_is_spent(credential_path, protector):
    """Refuse locally *first*. Asking HQ would burn a good code and strand the
    Device this computer is already using."""
    _enrol(credential_path, protector)
    transport = FakeTransport()
    with pytest.raises(AgentError):
        _enrol(credential_path, protector, transport)
    assert transport.calls == [], "a code was spent on an enrolment that could not be stored"
    assert load_credential(credential_path, protector=protector).credential() == CREDENTIAL


def test_a_corrupt_credential_file_is_rejected_rather_than_guessed(credential_path, protector):
    _enrol(credential_path, protector)
    raw = bytearray(credential_path.read_bytes())
    raw[-1] ^= 0xFF
    credential_path.write_bytes(bytes(raw))
    with pytest.raises(CredentialStoreError):
        load_credential(credential_path, protector=protector)


def test_a_credential_the_server_malformed_is_never_stored(credential_path, protector):
    transport = FakeTransport([(200, {
        "device_public_id": DEVICE_PUBLIC_ID, "credential": "not-a-credential",
        "credential_version": 1, "store_id": 7,
    })])
    with pytest.raises(AgentError):
        _enrol(credential_path, protector, transport)
    assert not credential_path.exists()


# ===========================================================================
# Ambiguity after a successful response
# ===========================================================================
def test_a_successful_response_is_never_resubmitted(credential_path, protector, monkeypatch):
    """The code is spent and the Device exists at HQ. Sending it again cannot
    help and would look like an attacker replaying a code."""
    import tools.receiver_credential_store as store

    def refuse(*args, **kwargs):
        raise OSError("the disk is full")

    monkeypatch.setattr(store.os, "replace", refuse)
    transport = FakeTransport()
    with pytest.raises(EnrolmentAmbiguous) as failure:
        _enrol(credential_path, protector, transport)
    monkeypatch.undo()

    assert len(transport.calls) == 1, "the spent code was submitted again"
    message = str(failure.value)
    assert CREDENTIAL not in message
    assert DEVICE_PUBLIC_ID in message, "the operator must be told what to revoke"


def test_a_connection_failure_before_any_response_may_be_retried(credential_path, protector):
    """Safe: nothing was issued, so nothing can be double-issued."""
    transport = FakeTransport([
        ConnectionError("the network was not there yet"),
        (200, {
            "device_public_id": DEVICE_PUBLIC_ID, "credential": CREDENTIAL,
            "credential_version": 1, "store_id": 7,
        }),
    ])
    outcome = _enrol(credential_path, protector, transport, attempts=2, retry_sleep=lambda _: None)
    assert outcome.device_public_id == DEVICE_PUBLIC_ID
    assert len(transport.calls) == 2


def test_a_rejected_code_is_never_retried(credential_path, protector):
    """400 means the code is wrong, expired or already used. Retrying is just
    guessing at codes, which is what the rate limit exists to stop."""
    transport = FakeTransport([
        (400, {"detail": "That enrolment code cannot be used."}),
        (200, {"device_public_id": DEVICE_PUBLIC_ID, "credential": CREDENTIAL,
               "credential_version": 1, "store_id": 7}),
    ])
    with pytest.raises(EnrolmentRefused):
        _enrol(credential_path, protector, transport, attempts=3, retry_sleep=lambda _: None)
    assert len(transport.calls) == 1


# ===========================================================================
# Reading the code
# ===========================================================================
def test_the_code_can_be_piped_on_stdin():
    assert read_enrolment_code(stream=io.StringIO(f"{CODE}\n")) == CODE


def test_a_hidden_prompt_is_used_when_there_is_a_terminal():
    asked = []

    def fake_getpass(prompt):
        asked.append(prompt)
        return f"  {CODE}  "

    assert read_enrolment_code(stream=None, prompt=fake_getpass) == CODE
    assert asked, "the operator was never asked"
    assert CODE not in asked[0]


def test_an_empty_code_is_refused():
    with pytest.raises(AgentError):
        read_enrolment_code(stream=io.StringIO("\n"))


def test_a_byte_order_mark_from_powershell_is_stripped():
    """Windows PowerShell prepends a UTF-8 BOM when piping to a native command.

        "ECHO-XXXX-XXXX" | SpeakLinkReceiver.exe enrol --from-stdin

    arrives with one extra leading character, and ``str.strip()`` does not
    remove it because a BOM is not whitespace. Without this the operator is told
    their enrolment code cannot be used, and nothing points at the real cause.

    Measured rather than assumed: this exact pipeline turned a 33-character
    value into a 34-character one.
    """
    assert read_enrolment_code(stream=io.StringIO(f"\ufeff{CODE}\n")) == CODE
    assert read_enrolment_code(stream=io.StringIO(f"\ufeff  {CODE}  \n")) == CODE


def test_a_byte_order_mark_is_stripped_from_a_rotated_credential_too():
    """The same pipeline, the same trap, the same operator."""
    from tools.receiver_agent import rotate_local_credential

    protector = FakeCredentialProtector("this-computer")
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "device-credential.bin"
        save_credential(
            path, credential=CREDENTIAL, device_public_id=DEVICE_PUBLIC_ID,
            store_id=7, backend_origin=HQ, protector=protector, now=NOW,
        )
        rotated = "speaklink_rcv_v2.11111111-2222-4333-8444-555555555555." + "z" * 43
        rotate_local_credential(
            path, protector=protector, stream=io.StringIO(f"\ufeff{rotated}\n")
        )
        assert load_credential(path, protector=protector).credential() == rotated


# ===========================================================================
# 11, 12 & 13. Stopping when told to, retrying when it is only the network
# ===========================================================================
def _outcome(attempt_results, *, policy=None, stable_after=30.0):
    """Drive the supervisor with a scripted sequence of session results."""
    delays: list[float] = []
    durations = iter(attempt_results)

    async def attempt():
        result = next(durations)
        if isinstance(result, Exception):
            raise result
        return result

    async def sleep(seconds):
        delays.append(seconds)

    outcome = asyncio.run(
        supervise(
            attempt_session=attempt,
            policy=policy or BackoffPolicy(
                initial_seconds=1, maximum_seconds=8, multiplier=2, jitter_fraction=0
            ),
            sleep=sleep,
            random_value=lambda: 0.5,
            stable_seconds=stable_after,
            max_attempts=len(attempt_results),
        )
    )
    return outcome, delays


def test_a_revoked_device_stops_instead_of_reconnecting_forever():
    """44 Agents retrying a revoked credential in a loop is HQ attacking itself."""
    outcome, delays = _outcome([TerminalAuthentication("the server refused this credential")])
    assert outcome.state == "AUTHENTICATION_REFUSED"
    assert outcome.should_retry is False
    assert delays == [], "it slept before giving up on a credential that will never work"


def test_a_disabled_device_stops_the_same_way():
    """The backend refuses disabled and revoked identically, on purpose - telling
    them apart would leak whether a Store has enrolled Devices."""
    outcome, _ = _outcome([TerminalAuthentication("refused")])
    assert outcome.state == "AUTHENTICATION_REFUSED"


def test_a_network_failure_reconnects_with_a_bounded_backoff():
    outcome, delays = _outcome([
        ConnectionError("network down"), ConnectionError("still down"),
        ConnectionError("and again"), ConnectionError("and again"),
        ConnectionError("and again"),
    ])
    assert outcome.state == "NETWORK_ERROR"
    assert delays == sorted(delays), "the backoff went backwards"
    assert max(delays) <= 8, f"the backoff broke its own ceiling: {delays}"
    assert len(delays) == 4, "it did not keep trying"


def test_the_backoff_resets_after_a_stable_connection():
    """An Agent that has been up for hours must not treat one blip as failure
    number nine and wait a minute before coming back."""
    outcome, delays = _outcome(
        [ConnectionError("a"), ConnectionError("b"), ConnectionError("c"),
         {"seconds": 120}, ConnectionError("d")],
        stable_after=30.0,
    )
    assert delays[:3] == [1, 2, 4], f"the backoff did not grow: {delays}"
    assert delays[3] == delays[0], (
        f"after two minutes connected it still waited {delays[3]}s, as if it had "
        "been failing all along"
    )


def test_a_clean_stop_is_not_an_error():
    outcome, delays = _outcome([{"seconds": 5, "stopped": True}])
    assert outcome.state == "STOPPED"
    assert delays == []


# ===========================================================================
# The backoff itself
# ===========================================================================
def test_the_backoff_grows_and_is_bounded():
    policy = BackoffPolicy(initial_seconds=1, maximum_seconds=30, multiplier=2, jitter_fraction=0)
    delays = [policy.delay(attempt, random_value=0.5) for attempt in range(1, 12)]
    assert delays[:5] == [1, 2, 4, 8, 16]
    assert all(delay <= 30 for delay in delays)
    assert delays[-1] == 30


def test_the_backoff_is_jittered():
    """44 Stores whose network returns at the same moment must not all reconnect
    in the same millisecond."""
    policy = BackoffPolicy(initial_seconds=4, maximum_seconds=30, multiplier=2, jitter_fraction=0.5)
    assert policy.delay(1, random_value=0.0) < policy.delay(1, random_value=1.0)
    assert policy.delay(1, random_value=0.0) > 0


def test_the_backoff_is_never_zero_or_negative():
    policy = BackoffPolicy(initial_seconds=1, maximum_seconds=30, multiplier=2, jitter_fraction=1.0)
    assert all(policy.delay(a, random_value=r) > 0 for a in range(1, 8) for r in (0.0, 0.5, 1.0))


# ===========================================================================
# 15, 16 & 19. The state model is the pilot's, unchanged
# ===========================================================================
def test_the_agent_reuses_the_pilots_own_evidence_methods():
    """The amplifier evidence path is preserved by being *the same code*, not by
    being a careful copy of it. These are identity comparisons on purpose."""
    for name in ("_on_prepare", "_on_audio", "_on_stop",
                 "_heartbeat_loop", "_session_loop", "_shutdown"):
        assert getattr(DeviceReceiverSession, name) is getattr(AudioReceiverPilot, name), (
            f"{name} was reimplemented; the proven path is no longer the one that runs"
        )


def test_connected_does_not_imply_ready():
    session = DeviceReceiverSession(ws_url="wss://x/api/ws/receiver", credential=CREDENTIAL)
    session.report["connected"] = True
    session._record_state("CONNECTED")
    assert session.report["ready"] is False
    assert "READY" not in session.report["states"]


def test_ready_does_not_imply_playback_confirmed():
    session = DeviceReceiverSession(ws_url="wss://x/api/ws/receiver", credential=CREDENTIAL)
    session.report["ready"] = True
    session._record_state("READY")
    assert session.report["playback_confirmed"] is False
    assert session.report["audio_receiving"] is False


def test_the_agent_never_claims_speaker_verification():
    session = DeviceReceiverSession(ws_url="wss://x/api/ws/receiver", credential=CREDENTIAL)
    assert session.report["speaker_verified"] is False
    source = (REPOSITORY_ROOT / "tools" / "receiver_agent.py").read_text(encoding="utf-8")
    assert '"speaker_verified": True' not in source
    assert "speaker_verified=True" not in source
    # The Agent may only ever read that flag, never set it. LinkGuard sets it.
    assert 'report["speaker_verified"] =' not in source


def test_the_session_never_renders_the_credential():
    session = DeviceReceiverSession(ws_url="wss://x/api/ws/receiver", credential=CREDENTIAL)
    for rendering in (repr(session), str(session)):
        assert CREDENTIAL not in rendering


# ===========================================================================
# 18. Legacy mode is explicit and off by default
# ===========================================================================
def test_legacy_mode_is_off_by_default():
    arguments = build_parser().parse_args(["run", "--backend-url", HQ])
    assert arguments.legacy_pilot_mode is False


def test_legacy_mode_must_be_asked_for_by_name():
    arguments = build_parser().parse_args(["run", "--backend-url", HQ, "--legacy-pilot-mode"])
    assert arguments.legacy_pilot_mode is True


def test_legacy_mode_still_refuses_a_token_on_the_command_line():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--backend-url", HQ, "--legacy-pilot-mode",
                           "--receiver-token", "a" * 32])


# ===========================================================================
# The four commands exist and removal is deliberate
# ===========================================================================
def test_the_agent_has_exactly_the_intended_commands():
    """A closed set, so a future command has to be a decision rather than a drift.

    ``rotate-local-credential`` is the operator's half of a rotation: an
    administrator rotates at HQ, and this is how the new credential reaches the
    till without ever passing through a command argument.
    """
    parser = build_parser()
    commands = {
        name
        for action in parser._actions
        if hasattr(action, "choices") and isinstance(action.choices, dict)
        for name in action.choices
    }
    assert commands == {
        "enrol", "run", "status", "rotate-local-credential", "remove-local-credential",
        # Added deliberately, and this guard is why it had to be. The device
        # inventory existed only as `python tools/windows_audio_devices.py`,
        # which is unavailable on the machines that need it: a Store desktop has
        # no Python. Without a way to list devices an operator can only guess a
        # name, and one endpoint appears under MME, DirectSound, WASAPI and
        # WDM-KS, so a guess is usually ambiguous and refused.
        "list-audio-devices",
        # Read-only, and the one thing a technician can be asked to run over
        # the phone: it shows version, settings, resolved audio device and the
        # tail of the log, and no credential, code or password.
        "diagnose",
    }


def test_the_rotated_credential_is_never_a_command_argument():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["rotate-local-credential", "--credential", CREDENTIAL])
    # It is read the same way an enrolment code is: hidden prompt, or stdin.
    assert parser.parse_args(["rotate-local-credential", "--from-stdin"]).from_stdin is True


def test_rotating_the_local_credential_keeps_the_device_identity(credential_path, protector):
    from tools.receiver_agent import rotate_local_credential

    _enrol(credential_path, protector)
    rotated = "speaklink_rcv_v2.11111111-2222-4333-8444-555555555555." + "n" * 43
    rotate_local_credential(
        credential_path, protector=protector, stream=io.StringIO(f"{rotated}\n")
    )
    record = load_credential(credential_path, protector=protector)
    assert record.credential() == rotated
    assert record.device_public_id == DEVICE_PUBLIC_ID, "rotation changed which Device this is"


def test_rotating_to_something_that_is_not_a_credential_is_refused(credential_path, protector):
    from tools.receiver_agent import rotate_local_credential

    _enrol(credential_path, protector)
    with pytest.raises(CredentialStoreError):
        rotate_local_credential(
            credential_path, protector=protector, stream=io.StringIO("not-a-credential\n")
        )
    assert load_credential(credential_path, protector=protector).credential() == CREDENTIAL


def test_removing_the_local_credential_needs_typed_confirmation(credential_path, protector):
    from tools.receiver_agent import remove_local_credential

    _enrol(credential_path, protector)
    assert remove_local_credential(credential_path, confirm=lambda _: "no") is False
    assert credential_path.exists(), "a credential was destroyed without confirmation"

    assert remove_local_credential(credential_path, confirm=lambda _: "remove") is True
    assert not credential_path.exists()


def test_status_reports_identity_without_the_credential(credential_path, protector, capsys):
    from tools.receiver_agent import describe_status

    _enrol(credential_path, protector)
    described = describe_status(credential_path, protector=protector)
    rendered = str(described)
    assert DEVICE_PUBLIC_ID in rendered
    assert CREDENTIAL not in rendered
    assert "s" * 43 not in rendered


def test_status_without_a_credential_says_so_plainly(credential_path, protector):
    from tools.receiver_agent import describe_status

    described = describe_status(credential_path, protector=protector)
    assert described["enrolled"] is False
    assert "credential" not in str(described).lower() or CREDENTIAL not in str(described)


# ===========================================================================
# 20. The protected database is never involved
# ===========================================================================
def test_the_agent_never_touches_the_protected_database(credential_path, protector):
    protected = REPOSITORY_ROOT / "backend" / "speaklink_live.db"
    before = protected.stat().st_mtime_ns if protected.exists() else None
    _enrol(credential_path, protector)
    after = protected.stat().st_mtime_ns if protected.exists() else None
    assert before == after


def test_the_agent_imports_nothing_from_the_backend():
    """It runs on a till. It needs no schema, no engine and no FastAPI."""
    import ast

    source = (REPOSITORY_ROOT / "tools" / "receiver_agent.py").read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"sqlalchemy", "fastapi", "db", "models", "schemas", "server", "key_custody"}
    assert not (imported & forbidden), f"the Agent must not import {imported & forbidden}"
