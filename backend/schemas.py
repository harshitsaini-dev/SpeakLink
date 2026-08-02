"""Pydantic schemas for EchoCast Live API."""
from datetime import datetime, timezone
from typing import ClassVar, Optional, List
from pydantic import BaseModel, ConfigDict, Field, field_serializer

# One source of truth for how long a human password must be. Four copies of a
# number is four chances for them to disagree, and the disagreement shows up as
# a browser accepting what the server then rejects.
from password_policy import PASSWORD_MAX_LENGTH, PASSWORD_MIN_LENGTH


def _utc_iso(value: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as an unambiguous UTC ISO-8601 string.

    SQLite drops tzinfo on round-trip, so a value read back from the database
    is a naive datetime that is UTC by this project's storage convention (see
    ``_as_utc_text`` in server.py). Pydantic's default ``model_dump(mode="json")``
    serializes a naive datetime with NO trailing offset - e.g.
    "2026-08-01T05:30:00.123456" - which a browser's ``new Date(...)`` then
    parses as LOCAL time, not UTC. On an IST browser that silently shifts every
    timestamp by exactly UTC+05:30, which is the broadcast-timer defect this
    guards against. Every timestamp leaving this API must carry an explicit
    "Z" so a client can never guess wrong.
    """
    if value is None:
        return None
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: str
    is_active: bool


#: What an administrator is allowed to see about another account.
#:
#: Written out field by field on purpose. A response model built from the ORM
#: object with everything included would publish ``password_hash`` and
#: ``session_version`` the moment somebody added them - and those are exactly
#: the two fields most likely to be added. A hash in a response body is the
#: input to an offline attack no rate limit protects against; a session counter
#: tells an attacker how many times they need to be wrong.
class HQUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    display_name: str
    role: str
    is_active: bool
    lifecycle_state: str
    created_at: Optional[datetime] = None
    disabled_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None


class HQUserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    role: str
    # Long enough to matter, short enough to be typed accurately by somebody
    # reading it out. Complexity rules push people towards Passw0rd! and a
    # sticky note; length does not.
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class HQUserUpdate(BaseModel):
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    username: Optional[str] = Field(default=None, min_length=1, max_length=100)


class HQUserRoleUpdate(BaseModel):
    role: str


class PermissionOverrideChange(BaseModel):
    code: str
    #: "INHERIT" deletes the override row rather than storing a redundant one.
    effect: str = Field(pattern="^(INHERIT|ALLOW|DENY)$")


class PermissionOverridesUpdate(BaseModel):
    changes: List[PermissionOverrideChange] = Field(min_length=1)


class StoreScopeEntry(BaseModel):
    scope_type: str = Field(pattern="^(STORE|CITY|REGION)$")
    store_id: Optional[int] = None
    scope_value: Optional[str] = None


class StoreScopeUpdate(BaseModel):
    #: An empty list is meaningful: it clears every assignment for this
    #: account, returning it to unrestricted.
    entries: List[StoreScopeEntry] = Field(default_factory=list)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH)


class PasswordResetOut(BaseModel):
    """Confirmation only. Nothing here can be replayed or reused."""
    user_id: int
    sessions_ended: bool
    detail: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class WebSocketTicketRequest(BaseModel):
    """Which socket the caller intends to open.

    Required, with no default. A default audience would recreate the defect this
    field exists to close: whichever value it fell back to would be mintable by
    any authenticated account, and the microphone uplink is not something a
    read-only account may open.
    """

    audience: str = Field(..., pattern="^(hq|broadcaster)$")


class EnrollmentCodeRequest(BaseModel):
    store_id: int = Field(..., gt=0)


class EnrollmentCodeResponse(BaseModel):
    """The one and only time the raw code leaves the server."""

    code: str
    store_id: int
    expires_in_seconds: int


class EnrollmentCodeStatusOut(BaseModel):
    """What an authenticated HQ page may know about an enrollment record.

    Every field here is either an identifier the API already exposes or a
    timestamp. There is deliberately no ``code`` and no ``code_hash``: the raw
    code leaves the server exactly once, at creation, and the verifier is not
    something a page has any use for. A test asserts no field named like a
    secret can be added later.

    ``progress`` carries only stages backed by stored or current evidence -
    never one inferred from how much time has passed. A Device that was created
    is not a Device that ever connected, and this response must not blur them.
    """

    #: Kept as a class attribute rather than an Enum so a caller can check the
    #: closed set without importing the service layer. REVOKED is absent on
    #: purpose - nothing in this schema can revoke a code.
    ALLOWED_STATES: ClassVar[tuple] = ("UNUSED", "USED", "EXPIRED")

    id: int
    store_id: int
    state: str
    created_at: str
    expires_at: str
    used_at: Optional[str] = None
    device_public_id: Optional[str] = None
    progress: List[str] = []


class DeviceEnrollmentRequest(BaseModel):
    """Sent by a Receiver computer. The code is in the body, never a URL."""

    code: str = Field(..., min_length=1, max_length=200)
    device_name: str = Field(..., min_length=1, max_length=200)
    hostname: str = Field("", max_length=253)
    software_version: str = Field("", max_length=64)


class DeviceEnrollmentResponse(BaseModel):
    """The one and only time the raw credential leaves the server.

    No later read returns it: the database holds a verifier, not the value.
    """

    device_public_id: str
    credential: str
    credential_version: int
    store_id: int


class CredentialRotationResponse(BaseModel):
    """The one and only time a rotated credential leaves the server.

    It cannot be fetched again: the database holds a verifier, not the value. The
    previous credential stops working the instant this one is issued, so an
    operator who loses this has to rotate again rather than fall back.
    """

    device_public_id: str
    credential: str
    credential_version: int
    store_id: int
    previous_credential_retired: bool = True


class ReceiverDeviceOut(BaseModel):
    """Everything the dashboard may know about a Device - and nothing else.

    Deliberately carries no credential, no verifier and no key version.
    """

    public_id: str
    store_id: int
    display_name: str
    status: str
    enrolled_at: str
    disabled_at: str | None = None
    created_at: str
    updated_at: str
    archived_at: str | None = None


class StoreBase(BaseModel):
    store_code: str = Field(..., min_length=1, max_length=50)
    store_name: str = Field(..., min_length=1, max_length=200)
    city: str
    region: str
    is_online_store: bool = False


class StoreCreate(StoreBase):
    pass


class StoreUpdate(BaseModel):
    """Editable Store fields.

    ``is_active`` is deliberately absent. Turning a Store on and off is a
    lifecycle transition with its own rules - an archived Store must not become
    active because somebody PUT a boolean - so it has its own endpoints.
    """

    store_code: Optional[str] = None
    store_name: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    is_online_store: Optional[bool] = None


class StoreOut(BaseModel):
    """Everything the dashboard may know about a Store.

    ``receiver_token`` used to be here, which meant every authenticated caller -
    including, once roles exist, a read-only VIEWER - could read the shared
    Receiver credential of all 44 Stores straight out of ``GET /api/stores``.
    The page never rendered it, but it sat in the response body: browser memory,
    devtools, any HAR file, any proxy log. It is gone, and a test asserts it
    stays gone.
    """

    model_config = ConfigDict(from_attributes=True)
    id: int
    store_code: str
    store_name: str
    city: str
    region: str
    is_online_store: bool
    is_active: bool
    lifecycle_state: Optional[str] = None
    status: str
    last_seen: Optional[datetime] = None
    created_at: datetime


class StoresMetaOut(BaseModel):
    regions: List[str]
    cities: List[str]


class SessionCreate(BaseModel):
    campaign_name: str = Field(..., min_length=1, max_length=255)
    target_mode: str  # all|selected|region|city|online_only
    store_ids: Optional[List[int]] = None
    region: Optional[str] = None
    city: Optional[str] = None
    notes: Optional[str] = None


class TargetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_id: int
    play_status: str
    command_sent_at: Optional[datetime] = None
    started_playing_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    error_message: Optional[str] = None
    #: Populated by the endpoint from the Store row, which is NEVER removed
    #: even after a permanent delete (see store_deletion.py) - so history
    #: keeps showing a real name/code instead of a bare id or a blank field.
    store_code: Optional[str] = None
    store_name: Optional[str] = None
    store_deleted: bool = False

    @field_serializer("command_sent_at", "started_playing_at", "stopped_at", when_used="json")
    def _serialize_utc(self, value: Optional[datetime]) -> Optional[str]:
        return _utc_iso(value)


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    campaign_name: str
    started_by: int
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    status: str
    target_mode: str
    selected_store_count: int
    online_store_count: int
    offline_store_count: int
    notes: Optional[str]
    created_at: datetime
    # Carried on the row, not just implied by the filter: with include_archived
    # the list mixes both, and the operator has to see which is which.
    archived_at: Optional[datetime] = None

    @field_serializer("started_at", "ended_at", "created_at", "archived_at",
                      when_used="json")
    def _serialize_utc(self, value: Optional[datetime]) -> Optional[str]:
        return _utc_iso(value)


class SessionDetailOut(SessionOut):
    targets: List[TargetOut] = []


# ReceiverEventIn and ReceiverVerifyOut are deliberately gone.
#
# They served GET /api/receiver/verify?token=... and POST /api/receiver/event,
# both of which accepted a raw Store credential from an unauthenticated caller -
# one of them through a URL. A schema with a bare ``token`` field is an
# invitation to add such a route back, so it goes with the routes. See the note
# in server.py under RECEIVER.


class SystemLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    level: str
    message: str
    created_at: datetime

    @field_serializer("created_at", when_used="json")
    def _serialize_utc(self, value: datetime) -> Optional[str]:
        return _utc_iso(value)
