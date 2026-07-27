"""Pydantic schemas for SpeakLink API."""
from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: str
    is_active: bool


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


class EnrollmentCodeRequest(BaseModel):
    store_id: int = Field(..., gt=0)


class EnrollmentCodeResponse(BaseModel):
    """The one and only time the raw code leaves the server."""

    code: str
    store_id: int
    expires_in_seconds: int


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


class StoreBase(BaseModel):
    store_code: str = Field(..., min_length=1, max_length=50)
    store_name: str = Field(..., min_length=1, max_length=200)
    city: str
    region: str
    is_online_store: bool = False


class StoreCreate(StoreBase):
    pass


class StoreUpdate(BaseModel):
    store_name: Optional[str] = None
    city: Optional[str] = None
    region: Optional[str] = None
    is_online_store: Optional[bool] = None
    is_active: Optional[bool] = None


class StoreOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    store_code: str
    store_name: str
    city: str
    region: str
    is_online_store: bool
    receiver_token: str
    is_active: bool
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


class SessionDetailOut(SessionOut):
    targets: List[TargetOut] = []


class ReceiverEventIn(BaseModel):
    token: str
    event_type: str
    details: Optional[str] = None


class ReceiverVerifyOut(BaseModel):
    ok: bool
    store: Optional[StoreOut] = None


class SystemLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    level: str
    message: str
    created_at: datetime
