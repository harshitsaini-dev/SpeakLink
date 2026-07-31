"""Safe, fixed reason codes for Receiver credential refusals.

WHY THIS EXISTS

``receiver_runtime_auth`` collapsed every distinct failure into one exception with
``from None``, discarding the cause. Six identical
``AUTHENTICATION_REFUSED attempts=1`` lines on a real Store PC carried no
information, and two successive root-cause reports were wrong because they were
inferred rather than measured. The true cause - a migration state that cannot
verify hashed credentials at all - was invisible from the outside.

WHAT A REASON CODE MAY AND MAY NOT SAY

These are FIXED strings. They never carry a credential, a token, a hash, a
signature, an HMAC key, an enrolment code, or a username. They identify a branch,
not a value, so knowing one tells an attacker which door is shut and nothing about
the key.

The split that matters: an UNAUTHENTICATED caller still gets the generic refusal,
because "device not found" versus "signature mismatch" is an enumeration oracle.
The reason code goes to the protected HQ log and to OWNER diagnostics, where the
person reading it is already trusted.
"""

from __future__ import annotations

from enum import Enum


class AuthReason(str, Enum):
    """One branch each. Never a value."""

    #: The migration state cannot verify hashed Device credentials at all, so the
    #: credential is never even compared. This is the one that cost four days.
    MIGRATION_STATE_BLOCKS_DEVICE_AUTH = "MIGRATION_STATE_BLOCKS_DEVICE_AUTH"
    #: The fleet has not been backfilled, so a hash-capable state cannot be entered.
    BACKFILL_REQUIRED = "BACKFILL_REQUIRED"

    DEVICE_NOT_FOUND = "DEVICE_NOT_FOUND"
    DEVICE_DISABLED = "DEVICE_DISABLED"
    STORE_INACTIVE = "STORE_INACTIVE"
    CREDENTIAL_NOT_FOUND = "CREDENTIAL_NOT_FOUND"
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
    KEY_VERSION_UNKNOWN = "KEY_VERSION_UNKNOWN"
    SIGNATURE_MISMATCH = "SIGNATURE_MISMATCH"
    TIMESTAMP_INVALID = "TIMESTAMP_INVALID"
    MALFORMED_PRESENTATION = "MALFORMED_PRESENTATION"
    UNKNOWN = "UNKNOWN"


#: What a Store operator is allowed to be told. Deliberately actionable and
#: deliberately free of anything that distinguishes "this Device is unknown" from
#: "this signature is wrong" - both read as "HQ rejected this Receiver".
OPERATOR_MESSAGES = {
    AuthReason.MIGRATION_STATE_BLOCKS_DEVICE_AUTH: (
        "HQ is not ready to accept Receiver credentials yet. Its credential "
        "migration has not been completed. Nothing is wrong with this computer - "
        "ask HQ to finish the Receiver credential migration."
    ),
    AuthReason.BACKFILL_REQUIRED: (
        "HQ is not ready to accept Receiver credentials yet. Ask HQ to finish the "
        "Receiver credential migration."
    ),
}

GENERIC_OPERATOR_MESSAGE = (
    "HQ rejected this Receiver credential. Ask HQ to check this Device."
)


def operator_message(reason: "AuthReason | None") -> str:
    """What the Store PC may display.

    Only the migration reasons get a specific message, and that is a deliberate
    asymmetry: they describe a fault in HQ that no action on the Store PC can fix,
    so telling the operator saves them from re-enrolling, replacing the identity
    or reinstalling - all of which were attempted on the real second PC.
    """
    if reason is None:
        return GENERIC_OPERATOR_MESSAGE
    return OPERATOR_MESSAGES.get(reason, GENERIC_OPERATOR_MESSAGE)


#: States in which the runtime verifier computes a hashed Device identity at all.
#: Mirrors receiver_auth_service._HASH_STATES; asserted equal by a test so the two
#: cannot drift, because the drift IS the defect this module documents.
HASH_CAPABLE_STATES = frozenset({"dual_verify", "hash_only", "raw_neutralized"})


def state_can_verify_device_credentials(state: object) -> bool:
    return isinstance(state, str) and state in HASH_CAPABLE_STATES


class DeviceEnrolmentBlocked(Exception):
    """Enrolment refused because HQ could not verify what it would issue.

    Carries a reason code and an operator-facing sentence. Raised BEFORE the
    one-time code is claimed, so a refusal costs nothing.
    """

    def __init__(self, reason: AuthReason, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail or operator_message(reason)
        super().__init__(self.detail)
