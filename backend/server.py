"""SpeakLink - main FastAPI application.

This is a standalone module. It does NOT touch or share state with any
existing system. Uses its own SQLite DB (speaklink_live.db).
"""
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

import os
import time
import uuid
import logging
import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Set

from fastapi import (FastAPI, APIRouter, Depends, File, Form, Header, HTTPException, Query,
                     Request, Response, UploadFile, WebSocket, WebSocketDisconnect,
                     status)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.staticfiles import StaticFiles
from sqlalchemy import bindparam, func, or_, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, ValidationError

from db import engine, get_db, SessionLocal
from models import (
    Base, HQUser, Store, BroadcastSession, BroadcastTarget, ReceiverEvent, SystemLog
)
from schemas import (
    LiveTargetAddIn,
    ListenerJoin,
    ListenerRequestAccess,
    WebRoomAutoApproveUpdate,
    ChatMessageIn, ChatSettingsIn, ChatMuteIn,
    BulkTargetActionIn,
    LoginRequest, LoginResponse, UserOut,
    StoreCreate, StoreUpdate, StoreOut, StoresMetaOut,
    BroadcastTargetStoreOut, BroadcastTargetsOut,
    SessionCreate, SessionOut, SessionDetailOut, TargetOut,
    StoreAudioControlUpdate, StoreAudioStateOut, StoreAudioControlOut,
    RecordingOut,
    SystemLogOut,
    EnrollmentCodeRequest, EnrollmentCodeResponse, EnrollmentCodeStatusOut,
    DeviceEnrollmentRequest, DeviceEnrollmentResponse,
    ReceiverDeviceOut, CredentialRotationResponse,
    HQUserOut, HQUserCreate, HQUserUpdate, HQUserRoleUpdate,
    PasswordChangeIn, PasswordResetIn, PasswordResetOut,
    PermissionOverridesUpdate,
    StoreScopeUpdate,
    WebSocketTicketRequest,
)
from receiver_rotation_service import (
    CredentialNotFoundError,
    DeviceNotRotatableError,
    RotationPersistenceError,
    rotate_receiver_device_credential,
)
from rbac import (
    Permission,
    PermissionDenied,
    Role,
    effective_permissions,
    ensure_rbac_schema,
    has_permission,
    may_manage_role,
    migrate_legacy_roles,
    parse_role,
    require_permission,
)
import store_audio_control
from store_audio_control import (
    StoreAudioControlError,
    StoreNotInSessionError,
    UnknownSessionError,
    registry as store_audio_registry,
)
from permission_catalog import (
    OwnerOverrideRefused,
    RightsEscalationRefused,
    UnknownPermissionCode,
    describe_user_permissions,
    ensure_permission_schema,
    has_permission_code,
    require_permission_code,
    resolve_effective_permissions,
    set_permission_overrides,
)
from store_scope import (
    InvalidScopeEntry,
    ensure_store_scope_schema,
    list_user_scope,
    resolve_store_scope,
    set_user_scope,
)
from broadcast_reconciliation import reconcile_orphaned_broadcasts
import broadcast_target_lifecycle as lifecycle
from broadcast_target_lifecycle import ensure_target_lifecycle_schema
from broadcast_reservation import (
    StoreBusyError,
    StoreNotInScopeError,
    active_busy_store_ids,
    ensure_broadcast_lease_schema,
    release_store_lease,
    reserve_one_store_for_session,
    release_session_leases,
    reserve_stores_for_session,
)
import active_broadcast_management as abm
from enrolment_refusal import (RefusalCategory,
                               classify_enrolment_refusal,
                               describe_outstanding_codes,
                               fingerprint)
from deletion_safety import (
    DeletionRefused,
    delete_device_if_unused,
    delete_store_if_unused,
    delete_user_if_unused,
    device_dependencies,
    store_dependencies,
    user_dependencies,
)
from store_deletion import (
    StoreDeletionRefused,
    list_store_deletion_events,
)
from store_permanent_delete import (
    StorePermanentDeletionRefused,
    ensure_store_permanent_delete_schema,
    permanently_delete_store,
    purge_legacy_store_tombstones,
)
from admin_search import (
    value_list,
    matches_any,
    int_list,
    sort_rows,
    BulkSelectionError,
    DEFAULT_PAGE_SIZE,
    Page,
    apply_paging,
    like_term,
    normalize_paging,
    parse_date,
    resolve_bulk_selection,
)
from admin_records import (
    archive_logs,
    archive_sessions,
    delete_logs_permanently,
    delete_sessions_permanently,
    ensure_admin_records_schema,
    list_admin_deletion_events,
)
from device_deletion import (
    DeviceDeletionRefused,
    ensure_device_deletion_schema,
    list_device_deletion_events,
    permanently_delete_device_with_history,
)
from user_deletion import (
    UserDeletionRefused,
    ensure_user_deletion_schema,
    list_user_deletion_events,
)
from user_permanent_delete import (
    PermanentDeletionRefused,
    ensure_user_permanent_delete_schema,
    permanently_delete_user,
    purge_legacy_user_tombstones,
)
from user_schema import UserSchemaError, ensure_user_auth_schema
from user_lifecycle import (
    migrate_super_admin_to_owner,
    DuplicateUsernameError,
    LastSuperAdminError,
    RoleAssignmentRefused,
    SelfActionRefused,
    UserLifecycleError,
    UserNotFoundError,
    UserNotRestorableError,
    UserTransitionRefused,
    archive_user,
    assign_role,
    create_user,
    disable_user,
    enable_user,
    ensure_user_lifecycle_schema,
    list_users,
    read_user,
    restore_user,
    set_password_hash,
    update_user,
)
from store_lifecycle import (
    StoreLifecycleError,
    StoreNotFoundError,
    StoreNotRestorableError,
    StoreTransitionRefused,
    archive_store,
    disable_store,
    enable_store,
    ensure_store_lifecycle_schema,
    restore_store,
    validate_location,
    validate_store_code,
    validate_store_name,
)
from receiver_primary_device import (
    DeviceNotPromotableError,
    clear_primary_for_device,
    describe_store_devices,
    ensure_primary_device_schema,
    primary_device_id,
    promote_device,
)
from receiver_device_service import (
    MigrationNotReadyError,
    ReceiverDeviceServiceError,
)
from receiver_enrollment_codes import (
    CODE_TTL_SECONDS,
    ReceiverEnrollmentCode,
    describe_state,
    ensure_enrollment_device_link_schema,
)
from audio_protocol import (
    build_prepare_message,
    build_stop_message,
    build_stand_down_message,
    build_resume_message,
    build_set_audio_control_message,
)
from auth import verify_password, hash_password, create_access_token, get_current_user
from seed import seed_admin, seed_stores
from audio_streaming import DEFAULT_STORE_QUEUE_CAPACITY
from ws_manager import manager
from receiver_connection_inventory import ReceiverConnectionInventoryError
from receiver_runtime_auth import (
    DualRuntimeAuthenticator,
    LegacyStoreTokenRuntimeAuthenticator,
    MigrationAwareReceiverRuntimeAuthenticator,
)
import csv
import io
from urllib.parse import quote
import hashlib
import secrets
import broadcast_recording
import web_rooms
import web_chat
import announcements
import announcement_schedule
import announcement_service
import announcement_protocol
import receiver_output_device
import announcement_rooms
import broadcast_group
import store_kits
from receiver_index_repair import repair_receiver_indexes
from receiver_auth_reasons import DeviceEnrolmentBlocked
import chat_attachments
from web_participant_runtime import (
    HEARTBEAT_INTERVAL_SECONDS,
    PlaybackState,
    WebParticipantRegistry,
)
from receiver_contract import (
    AudioControlAcknowledgement,
    EndpointStateAcknowledgement,
    HEARTBEAT_INTERVAL_SECONDS,
    AudioReceivingAcknowledgement,
    ConnectionState,
    DeviceErrorAcknowledgement,
    DuplicateMessageError,
    HeartbeatAcknowledgement,
    NonMonotonicSequenceError,
    PlaybackConfirmedAcknowledgement,
    PlaybackErrorAcknowledgement,
    ReceiverContractError,
    StoppedAcknowledgement,
    WrongSessionError,
)
from ws_tickets import (
    AUDIENCE_BROADCASTER,
    AUDIENCE_HQ,
    TICKET_TTL_SECONDS,
    TicketRejected,
    WebSocketTicketStore,
)
from pathlib import Path

from key_custody import (
    DpapiProtector,
    FakeProtector,
    KeyCustodyError,
    ProtectionScope,
    load_key_ring,
)
from key_custody_acl import SERVICE_CONTAINER_PATH
from receiver_auth_mode import ReceiverAuthMode
from receiver_key_bootstrap import bootstrap_from_environment
from receiver_enrollment_api import (
    DeviceNotFound,
    DeviceNotRestorable,
    EnrollmentRefused,
    EnrollmentUnavailable,
    TooManyOutstandingCodes,
    archive_device,
    create_enrollment_code,
    disable_device,
    ensure_device_archive_schema,
    list_devices,
    read_device,
    redeem_and_enroll,
    restore_device,
    revoke_device,
)
from login_guard import (
    LoginGuardConfig,
    LoginRateLimiter,
    burn_password_comparison,
    clear_failed_logins,
    client_identifier,
    config_from_environment,
    lockout_retry_after,
    normalise_username,
    proxy_headers_trusted,
    register_failed_login,
)

# Validated at import, so an unusable value stops the process rather than
# quietly disabling the login defences.
LOGIN_GUARD = config_from_environment()
TRUST_PROXY_HEADERS = proxy_headers_trusted()
login_limiter = LoginRateLimiter(LOGIN_GUARD)

# ---- web audience runtime -------------------------------------------------
#: Live listener sockets, heartbeats and browser playback state. Memory only:
#: none of it means anything after a restart, and persisting it would be a
#: database write per heartbeat per listener.
web_participants = WebParticipantRegistry()

#: The one-time plaintext join password, held only for the lifetime of this
#: process so the console can offer Copy Password on the page that created the
#: Broadcast. It is NOT storage: a restart loses it, and the honest answer
#: afterwards is "password configured" plus rotation, not a fabricated value.
_ROOM_PASSWORD_ONCE: dict[int, str] = {}

#: Listener session tokens minted by an Approve, waiting to be collected by the
#: browser polling its own admission state. Memory only, and removed the moment
#: it is collected: it is that listener's credential, and it is never returned
#: to the broadcaster who approved them.
_PENDING_LISTENER_TOKENS: dict[int, str] = {}

#: Public join surfaces are unauthenticated, so they get the same treatment as
#: login. Deliberately separate limiters: exhausting password attempts must not
#: also block somebody looking a room up from a shared link.
WEB_ROOM_LOOKUP_GUARD = LoginGuardConfig(max_attempts=30, window_seconds=60,
                                         max_failures=30, lockout_seconds=60)
WEB_JOIN_GUARD = LoginGuardConfig(max_attempts=8, window_seconds=300,
                                  max_failures=8, lockout_seconds=300)
web_lookup_limiter = LoginRateLimiter(WEB_ROOM_LOOKUP_GUARD)
web_join_limiter = LoginRateLimiter(WEB_JOIN_GUARD)
#: The announcement listening link, which is the same shape of problem: a
#: public endpoint where a correct password IS the authorisation.
#:
#: It had none. The RBAC matrix lists /api/announce/join among the deliberately
#: unauthenticated routes and justifies that, in prose, with "each is rate
#: limited" - and the code that would have made the sentence true was never
#: written. An operator may choose a memorable ID (AN-DIWALI, by design) and a
#: four-character password, so without a budget both are guessable at leisure.
ANNOUNCE_JOIN_GUARD = LoginGuardConfig(max_attempts=8, window_seconds=300,
                                       max_failures=8, lockout_seconds=300)
announce_join_limiter = LoginRateLimiter(ANNOUNCE_JOIN_GUARD)

# The enrolment endpoint is unauthenticated by design - the code IS the
# credential - so it gets its own budget rather than sharing the login one.
ENROLLMENT_GUARD = LoginGuardConfig(max_attempts=10, window_seconds=300, max_failures=5,
                                    lockout_seconds=900, max_entries=1024)
enrollment_limiter = LoginRateLimiter(ENROLLMENT_GUARD)


def receiver_key_container_path() -> Path:
    """Where this process reads and writes the Receiver HMAC container.

    One resolver. The bootstrap and the reader must never be able to disagree
    about which file they mean.
    """
    configured = os.environ.get("SPEAKLINK_KEY_CONTAINER")
    return Path(configured) if configured else SERVICE_CONTAINER_PATH


def receiver_key_protector():
    """One protector choice, for the same reason."""
    if os.environ.get("SPEAKLINK_KEY_PROTECTOR") == "fake":
        # Local staging only. Never reachable unless explicitly configured.
        return FakeProtector()
    return DpapiProtector(scope=ProtectionScope.CURRENT_USER)


def receiver_key_ring():
    """Open the Receiver HMAC key container, or return None.

    None rather than an exception so the caller can answer 503 with a message
    about the server rather than one that looks like a bad enrolment code.

    The container is never created HERE - a key minted as a side effect of the
    first request is a key nobody decided to make. It is minted once at startup
    by ``bootstrap_receiver_key_container`` below, in this process, because DPAPI
    CURRENT_USER binds a container to the identity that sealed it and this is the
    identity that has to open it.
    """
    try:
        return load_key_ring(
            receiver_key_container_path(), protector=receiver_key_protector()
        )
    except KeyCustodyError:
        return None

# Browser WebSockets cannot send an Authorization header, so the HQ sockets
# authenticate with a single-use ticket instead of a reusable JWT in the URL.
# In memory is correct here: SpeakLink runs exactly one Uvicorn worker because
# Receiver connection state is already process-local.
ws_ticket_store = WebSocketTicketStore()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("speaklink")

RECEIVER_AUTH_FAILURE_CODE = 4401
RECEIVER_AUTH_FAILURE_REASON = "Receiver authentication failed"
RECEIVER_CONNECTION_FAILURE_CODE = 1013
RECEIVER_CONNECTION_FAILURE_REASON = "Receiver connection unavailable"
MAX_RECEIVER_TOKEN_LENGTH = 128


# ---- app + startup ----
app = FastAPI(title="SpeakLink", version="1.0.0")


def configure_receiver_runtime(
    application: FastAPI,
    *,
    authenticator,
    connection_manager,
) -> None:
    """Explicitly configure Receiver runtime dependencies for one app instance."""

    if not callable(getattr(authenticator, "authenticate", None)):
        raise TypeError("Receiver runtime authenticator is invalid")
    application.state.receiver_runtime_authenticator = authenticator
    application.state.receiver_connection_manager = connection_manager


default_receiver_runtime_authenticator = LegacyStoreTokenRuntimeAuthenticator(
    SessionLocal
)


def build_receiver_runtime_authenticator():
    """Prefer Device credentials; keep legacy Store tokens working meanwhile.

    Both are accepted during an explicit, temporary migration period. A Device
    credential is tried first, so a Store stops depending on its shared token
    the moment one of its computers presents a real credential - no flag, no
    restart.

    If the phase-one schema or the key container is not available, this returns
    the legacy authenticator alone rather than failing: a host that has not
    migrated yet must keep its Receivers connected. The dashboard can still tell
    the two apart, because the identity records which transport proved it.

    Removing this - and the legacy path with it - is the documented cutover.
    """
    key_ring = receiver_key_ring()
    if key_ring is None:
        return None
    try:
        # SQLAlchemy's Inspector, not sqlite_master. The sqlite_master form
        # raised on PostgreSQL, the except below swallowed it, and this
        # function returned None - which silently degrades the whole fleet to
        # legacy Store-token authentication and refuses every Device
        # credential. HQ then looks completely healthy while no Store can
        # connect, which is the same failure shape as the RC14 blocker and
        # just as invisible.
        from sqlalchemy import inspect

        present = set(inspect(engine).get_table_names())
        if not {"receiver_devices", "receiver_credentials"} <= present:
            return None
        return DualRuntimeAuthenticator(
            device=MigrationAwareReceiverRuntimeAuthenticator(
                engine, hash_keys=key_ring.as_mapping()
            ),
            legacy=default_receiver_runtime_authenticator,
        )
    except Exception:
        # Never let a probe failure take the Receivers offline. None means
        # "not device-capable yet"; ReceiverAuthMode keeps the legacy
        # authenticator serving and tries again shortly.
        return None


# Before the authenticator is built, never after. build_receiver_runtime_
# authenticator() runs once, here, at import: with no key ring it returns the
# legacy Store-token authenticator ALONE for the life of the process, so a
# container minted later is not used until a restart. That is the shape of the
# defect this fixes - the first installed HQ start came up "working" with every
# enrolled Device silently unable to use its own credential.
#
# A refusal is allowed to propagate. Failing to start is the correct outcome when
# the alternative is minting a key over credentials that are still in use: the
# supervisor records the exit and reports it, which is louder than a server that
# runs and quietly authenticates nobody.
#: Re-evaluated rather than frozen. The choice used to be made once, here, and
#: a backend that started before run_receiver_credential_phase_one created the
#: Device tables served legacy-only Store tokens for its whole life - refusing
#: every Device credential while enrolment kept succeeding. That is exactly what
#: refused Device 3b1ff11f on the second PC. See backend/receiver_auth_mode.py.
receiver_auth_mode = None

_key_bootstrap_outcome = bootstrap_from_environment(
    container_path=receiver_key_container_path(),
    protector=receiver_key_protector(),
)

receiver_auth_mode = ReceiverAuthMode(
    build=build_receiver_runtime_authenticator,
    legacy=default_receiver_runtime_authenticator,
)
configure_receiver_runtime(
    app,
    authenticator=receiver_auth_mode,
    connection_manager=manager,
)


#: Coarse role-matrix permissions that were never split into their own route
#: (broadcast controls, and the three read-only dashboards) map straight onto
#: one fine-grained code, so their existing `Depends(require(Permission.X))`
#: call sites keep working unmodified and still gain per-user override
#: support through the same resolver. The MANAGE_* fallbacks exist only as a
#: defensive net in case a future route reintroduces a coarse Depends() by
#: mistake - every real MANAGE_STORES/MANAGE_DEVICES/MANAGE_USERS call site was
#: converted to its specific fine-grained code (see permission_catalog.py).
_COARSE_TO_FINE: dict[Permission, str] = {
    Permission.VIEW_STATUS: "menu.broadcast.view",
    Permission.VIEW_HISTORY: "menu.history.view",
    Permission.VIEW_LOGS: "menu.logs.view",
    Permission.START_BROADCAST: "broadcast.start",
    Permission.STOP_BROADCAST: "broadcast.stop",
    Permission.EMERGENCY_STOP: "broadcast.emergency_stop",
    Permission.MANAGE_STORES: "stores.update",
    Permission.MANAGE_DEVICES: "devices.rotate",
    Permission.MANAGE_USERS: "users.update",
}


def require(permission):
    """A dependency that admits only accounts holding one permission.

    Accepts either a legacy coarse ``rbac.Permission`` (mapped onto its
    fine-grained code via ``_COARSE_TO_FINE``) or a fine-grained permission
    code string directly (``"stores.update"``, ``"devices.rotate"``, ...).
    Either way, the actual decision - role default plus this account's
    ALLOW/DENY overrides - is made in exactly one place:
    ``permission_catalog.resolve_effective_permissions``. No route compares a
    role string itself.

    Written as a factory so every route names its permission at the point of
    definition. A route with no `require(...)` is authenticated-only, and that
    now has to be a visible choice rather than an omission - a test walks the
    routing table and fails on any authenticated route without one.
    """
    code = _COARSE_TO_FINE[permission] if isinstance(permission, Permission) else permission

    def guard(user: HQUser = Depends(get_current_user)) -> HQUser:
        if not has_permission_code(engine, user, code):
            # 403, not 404: the caller is authenticated and the resource exists.
            # The message is identical for every permission, so a refusal never
            # maps out what the system can do.
            raise HTTPException(status_code=403, detail=RBAC_REFUSED)
        return user

    return guard


def _require_store_in_scope(user: HQUser, store_id: int) -> None:
    """Refuse a Store outside this account's assigned Store/City/Zone scope.

    A permission (``stores.update``) answers "may this account edit a Store
    at all"; scope answers "which one". Both are checked, independently -
    holding the permission never bypasses scope, and being in scope never
    substitutes for the permission. ``None`` means unrestricted (OWNER, or
    an account with no scope rows), so this is a silent no-op for the common
    unscoped case.
    """
    scope = resolve_store_scope(engine, user)
    if scope is not None and store_id not in scope:
        raise HTTPException(status_code=403, detail=RBAC_REFUSED)


#: One refusal message, taken from the exception itself so the two can never
#: drift apart. Identical for every permission and every role: a refusal that
#: named what was missing would map out the system for whoever probed it.
RBAC_REFUSED = str(PermissionDenied())


def require_super_admin(user: HQUser = Depends(get_current_user)) -> HQUser:
    """Reserved for the few actions an ADMIN must not reach.

    Restoring an archived Store is one: archiving is how a Store is retired, and
    un-retiring it should need the account that also owns security settings.
    """
    if parse_role(user.role) is not Role.OWNER or not user.is_active:
        raise HTTPException(
            status_code=403, detail="You do not have permission to perform this action."
        )
    return user


def _write_log(db: Session, level: str, message: str):
    try:
        db.add(SystemLog(level=level, message=message))
        db.commit()
    except Exception:
        db.rollback()


@app.on_event("startup")
def startup_event():
    Base.metadata.create_all(bind=engine)

    # The Receiver Credential Lifecycle tables - Devices, credentials, their
    # event log and the migration state row.
    #
    # These used to be created only by running migrations.py by hand, which was
    # right when every HQ was migrated once from an older database by a
    # documented runbook. It is wrong for a repository-native deployment, where
    # a brand-new machine creates its database on first start and there is no
    # earlier state to migrate FROM: without this, the database came up looking
    # healthy and complete, and the first attempt to enrol a Store Receiver
    # failed against tables that had never been created.
    #
    # Safe on every boot rather than only on a fresh one: the migration records
    # itself in schema_migrations and returns immediately when it is already
    # applied, so an existing HQ pays one SELECT for it.
    #
    # SQLite only, by design - the PostgreSQL schema comes from
    # postgres_schema.py instead - so a Postgres deployment skips it rather
    # than failing.
    if engine.dialect.name == "sqlite":
        try:
            from migrations import (
                ProtectedDatabaseError,
                run_receiver_credential_phase_one,
            )
            run_receiver_credential_phase_one(engine)
        except ProtectedDatabaseError:
            # The historical development database, which is migrated only under
            # an explicit maintenance opt-in. Not an error here.
            logger.info("Receiver credential schema: protected database, skipped")
        except Exception:
            # ERROR, not warning: enrolment cannot work without these tables,
            # and a quiet warning is how that reaches a Store instead of an
            # operator.
            logger.error("Receiver credential schema could not be prepared - "
                         "enrolling a Store Receiver will fail until this is "
                         "resolved", exc_info=True)

    # Additive and idempotent - a new table, no ALTER on anything that already
    # exists - so it is safe on every boot rather than needing a maintenance
    # window. A host without the phase-one schema simply has an empty one.
    try:
        ensure_primary_device_schema(engine)
        # Additive, and safe on every boot. A pending mixer change must
        # survive an HQ restart - outliving a disconnection is its whole
        # purpose - so unlike live readings it has to live in the database.
        broadcast_recording.ensure_recording_schema(engine)
        # An index is derivable, and its absence stopped every enrolment on a
        # live estate for a day. Recreated here rather than merely reported.
        repair_receiver_indexes(engine)
        web_rooms.ensure_web_room_schema(engine)
        # Chat hangs off the room, which hangs off the session, both
        # ON DELETE CASCADE - so a broadcast deleted from history takes
        # its chat with it and there is no second cleanup to forget.
        web_chat.ensure_chat_schema(engine)
        # Recorded announcements. Four additive tables; see announcements.py
        # for why they are not a broadcast with a file attached.
        announcements.ensure_announcement_schema(engine)
        # Shops left pointing at a template that no longer applies. Archiving
        # used to leave them playing; that is fixed, but the rows it already
        # produced are the ones an operator is looking at.
        try:
            stranded = announcement_service.reconcile_playback(engine)
            if stranded:
                logger.warning(
                    "Stopped %d Store(s) that were still pointed at a template "
                    "which is archived or gone: %s", len(stranded), stranded)
        except Exception:  # noqa: BLE001 - never block a boot over tidying
            logger.exception("Could not reconcile announcement playback")
        # Which speaker each Store plays through, and what it reported it has.
        receiver_output_device.ensure_output_device_schema(engine)
        # Listening links for announcements. Their own room with their own
        # life; see announcement_rooms.py for why they are not broadcast rooms.
        announcement_rooms.ensure_announcement_room_schema(engine)
        # More than one voice on one broadcast; see broadcast_group.py.
        broadcast_group.ensure_group_schema(engine)
        # Anything left mid-flight by a crash is resolved BEFORE the first
        # request can read it. An unfinished .part is never promoted to
        # AVAILABLE: HQ stopping mid-announcement is exactly when a recording
        # is least trustworthy.
        try:
            resolved = broadcast_recording.reconcile_recordings(
                engine, broadcast_recording.recordings_directory())
            for outcome in resolved:
                logger.info("Recording for session %s reconciled as %s",
                            outcome["session_id"], outcome["status"])
        except Exception as failure:
            logger.warning("Recording reconciliation failed: %s", failure)
    except Exception:
        logger.warning("Receiver primary-device table could not be prepared", exc_info=False)

    # One additive column plus a backfill from is_active. Cheap on SQLite - no
    # row rewrite, no index rebuild - so it belongs at startup, not in a window.
    try:
        ensure_store_lifecycle_schema(engine)
    except Exception:
        logger.warning("Store lifecycle column could not be prepared", exc_info=False)

    # TRUE permanent Store deletion: Store Code snapshots on the history rows,
    # non-reusable Store ids, and the nullability those historical references
    # need so a deleted Store's row can actually leave the table.
    #
    # ERROR rather than a warning: without this schema a permanent delete
    # cannot release the Store Code, which is the whole defect being fixed.
    try:
        ensure_store_permanent_delete_schema(engine)
    except Exception:
        logger.error("Store permanent-delete schema could not be prepared - "
                     "permanently deleting a Store will not release its Store "
                     "Code until this is resolved", exc_info=True)

    # Stores the OLD tombstone design marked deleted but left in the table,
    # still holding their Store Codes. Only lifecycle_state='deleted' is
    # touched - never an archived or active Store. Idempotent.
    try:
        purged_stores = purge_legacy_store_tombstones(engine)
        if purged_stores["purged"]:
            logger.info("Released %s Store Code(s) from legacy permanent-delete "
                        "tombstones: %s", purged_stores["purged"],
                        ", ".join(purged_stores["store_codes"]))
    except Exception:
        logger.error("Legacy Store tombstones could not be migrated", exc_info=True)

    # One nullable column recording which Device a redemption produced. Left
    # NULL for every code redeemed before it existed, and NULL means "not
    # recorded" rather than "no Device" - so an older row degrades the reported
    # setup progress instead of asserting something false about it.
    try:
        ensure_enrollment_device_link_schema(engine)
    except Exception:
        logger.warning("Enrollment device-link column could not be prepared", exc_info=False)

    # Additive tables plus a reseed of the derived permission catalog/role
    # matrix from code - never touches user_permission_overrides or
    # permission_audit_events, which hold operator decisions and history.
    try:
        ensure_permission_schema(engine)
    except Exception:
        logger.warning("Permission catalog could not be prepared", exc_info=False)

    # One nullable column so a Device can be archived/restored the same way a
    # Store or a User already is, without touching the fixed status CHECK.
    try:
        ensure_device_archive_schema(engine)
    except Exception:
        logger.warning("Receiver Device archive column could not be prepared", exc_info=False)

    # Which live Broadcast Session holds which Store. Additive, and created
    # before any broadcast route can run: the partial unique index on it is
    # what actually prevents one Store carrying two live broadcasts, so a
    # missing table would mean the rule is enforced by nothing at all.
    try:
        ensure_broadcast_lease_schema(engine)
        # Additive: two columns on broadcast_targets. Existing rows read
        # as ACTIVE generation 1, which is what they were.
        ensure_target_lifecycle_schema(engine)
    except Exception:
        logger.warning("Broadcast Store lease schema could not be prepared",
                       exc_info=False)

    # Additive table for per-user Store/City/Zone scope. An account with no
    # rows here is unrestricted, so this is safe to run on every boot even
    # before anybody has assigned a scope.
    try:
        ensure_store_scope_schema(engine)
    except Exception:
        logger.warning("Store scope table could not be prepared", exc_info=False)

    # Irreversible User deletion: two additive hq_users columns and one audit
    # table. Additive and idempotent, so it is safe on every boot.
    try:
        ensure_user_deletion_schema(engine)
    except Exception:
        logger.warning("User deletion table could not be prepared", exc_info=False)

    # TRUE permanent deletion: owner snapshots on broadcast_sessions, and the
    # nullability the historical references need so a deleted account's row can
    # actually leave the table. See user_permanent_delete.py.
    #
    # ERROR rather than a warning if it fails: without this schema a permanent
    # delete cannot release the username, which is the whole defect being
    # fixed, and a quiet warning is how that reaches an operator as "the
    # username is already in use" months later.
    try:
        ensure_user_permanent_delete_schema(engine)
    except Exception:
        logger.error("User permanent-delete schema could not be prepared - "
                     "permanently deleting an account will not release its "
                     "username until this is resolved", exc_info=True)

    # Accounts the OLD tombstone design marked deleted but left in the table,
    # still holding their usernames. Finishing that decision is a migration,
    # not a new one: only lifecycle_state='deleted' is touched, never an
    # archived or active account. Idempotent - once the rows are gone there is
    # nothing left to match.
    try:
        purged = purge_legacy_user_tombstones(engine)
        if purged["purged"]:
            logger.info("Released %s username(s) from legacy permanent-delete "
                        "tombstones: %s", purged["purged"],
                        ", ".join(purged["usernames"]))
    except Exception:
        logger.error("Legacy user tombstones could not be migrated", exc_info=True)

    try:
        ensure_device_deletion_schema(engine)
    except Exception:
        logger.warning("Device deletion table could not be prepared", exc_info=False)

    # Broadcast History / System Log archive + administrative deletion audit.
    try:
        ensure_admin_records_schema(engine)
    except Exception:
        logger.warning("Admin records schema could not be prepared", exc_info=False)

    # Every hq_users migration, in one call, in the one order that works.
    #
    # This used to be two try blocks that ran the steps in the wrong order:
    # migrate_legacy_roles queries through the HQUser model, which knows about
    # display_name and lifecycle_state, so on a database that still had the
    # original six-column table it failed with
    #
    #     (sqlite3.OperationalError) no such column: hq_users.display_name
    #
    # and the except turned that into one warning line. On a fresh database
    # create_all builds the full table so the step worked and nobody noticed;
    # on a legacy one it has been failing silently, leaving roles unnormalised.
    # user_schema.ensure_user_auth_schema owns the order now, and the CLI calls
    # the same function - the duplication is what allowed them to disagree.
    try:
        ensure_user_auth_schema(engine, session_factory=SessionLocal)
    except UserSchemaError as failure:
        logger.warning("User tables could not be prepared: %s", failure)
    except Exception:
        logger.warning("User tables could not be prepared", exc_info=False)

    # Decided here rather than at import: whether Device credentials can be
    # verified depends on the database and the key container, and neither is
    # guaranteed to exist when this module is first imported.
    # Refreshed, not rebuilt: the mode upgrades itself the moment the Device
    # tables and key container exist, including when a migration runs under a
    # backend that is already serving.
    receiver_auth_mode.refresh()
    configure_receiver_runtime(app, authenticator=receiver_auth_mode,
                               connection_manager=manager)
    logger.info("Receiver authentication: %s", receiver_auth_mode.describe())

    with SessionLocal() as db:
        seed_admin(db)
        seed_stores(db)
    # Both migrations run AGAIN, after seeding, and this is not belt-and-braces.
    #
    # On a brand-new database the migrations above run against an empty
    # hq_users table, and seed_admin then inserts the first administrator with
    # the legacy role string 'admin'. Measured on a fresh install: the only
    # account was {'username': 'founder', 'role': 'admin'} - not normalised to
    # ADMIN, and with no SUPER_ADMIN anywhere in the system. Every
    # require_super_admin endpoint was therefore unreachable by anybody:
    # restoring an archived Store, and now resetting a password. The recovery
    # would have been editing the database by hand.
    #
    # Idempotent, so on an existing install this finds nothing to do. Same
    # function and therefore the same order as above - two hand-written
    # sequences is exactly how the first one drifted out of step.
    try:
        ensure_user_auth_schema(engine, session_factory=SessionLocal)
    except Exception:
        logger.warning("Role or lifecycle backfill after seeding failed", exc_info=False)
        _write_log(db, "info", "SpeakLink server started")

    # Close broadcasts orphaned by a previous process, BEFORE this one can
    # accept a new start. The schema exists by now, and nothing has served a
    # request yet, so this is the only moment where "every live session in the
    # database is an orphan" is reliably true.
    #
    # Deliberately NOT wrapped in a warn-and-continue. An unreleased lease is
    # a Store that answers STORE_BUSY to every future broadcast for ever, and
    # a warning printed next to "startup complete" is exactly how that would
    # go unnoticed until somebody tried to broadcast to it. The runtime is
    # empty at this point by construction; passing it explicitly keeps the
    # reconciler's contract honest rather than assuming emptiness.
    reconciliation = reconcile_orphaned_broadcasts(
        engine, active_session_ids=manager.broadcasts.active_session_ids())
    if reconciliation.changed_anything:
        _write_log(
            db, "warn",
            f"Startup reconciliation closed "
            f"{len(reconciliation.orphaned_session_ids)} interrupted "
            f"broadcast(s) and released {reconciliation.released_leases} "
            f"Store lease(s)")

    logger.info("SpeakLink startup complete")


#: The scheduler task, held so shutdown can end it rather than leaving it
#: running against a closing event loop.
_schedule_task: "asyncio.Task | None" = None


@app.on_event("startup")
async def start_announcement_scheduler():
    """The clock that opens and closes daily windows.

    Started here rather than woken by a request: a shop's ten o'clock has to
    arrive whether or not anybody happens to be looking at HQ, which is the
    entire point of scheduling a campaign instead of pressing play.
    """
    global _schedule_task
    _schedule_task = asyncio.create_task(_announcement_schedule_loop())


@app.on_event("shutdown")
async def stop_announcement_scheduler():
    global _schedule_task
    task, _schedule_task = _schedule_task, None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):  # noqa: BLE001
        pass


api = APIRouter(prefix="/api")


@api.get("/")
def root():
    return {"service": "SpeakLink", "status": "ok"}


# ================ AUTH ================
INVALID_CREDENTIALS = "Invalid username or password"
TOO_MANY_ATTEMPTS = "Too many sign-in attempts. Please try again later."


def _too_many_attempts(retry_after: int) -> HTTPException:
    """One response for throttled and for locked.

    Telling them apart would say whether the account exists, which is exactly
    what the generic 401 already refuses to reveal. The body carries no counter,
    no threshold and no timestamp - only the header an honest client needs.
    """
    return HTTPException(
        status_code=429,
        detail=TOO_MANY_ATTEMPTS,
        headers={"Retry-After": str(retry_after)},
    )


@api.post("/auth/login", response_model=LoginResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)):
    client_key = client_identifier(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        trust_proxy=TRUST_PROXY_HEADERS,
    )
    username_key = f"user:{normalise_username(payload.username)}"

    # Burst defence first: refuse before spending a bcrypt comparison, so a
    # flood cannot be used to exhaust CPU either.
    for key in (client_key, username_key):
        retry_after = login_limiter.retry_after(key)
        if retry_after is not None:
            _write_log(db, "warn", f"login_rate_limited key_kind={key.split(':', 1)[0]}")
            raise _too_many_attempts(retry_after)

    # A locked account gets no token, whatever password is presented.
    locked_for = lockout_retry_after(db, payload.username, LOGIN_GUARD, now=time.time())
    if locked_for is not None:
        # Safe to name the account here: a lock exists only for one that really
        # exists, so this cannot be turned into arbitrary attacker-written log
        # content. The operator needs to know which account is locked.
        _write_log(
            db, "warn", f"account_temporarily_locked user={normalise_username(payload.username)}"
        )
        raise _too_many_attempts(locked_for)

    user = db.query(HQUser).filter(HQUser.username == payload.username).first()
    if not user or not user.is_active:
        # Spend the same time as a real comparison so the clock does not answer
        # a question the message refuses to.
        burn_password_comparison(payload.password)
        authenticated = False
    else:
        authenticated = verify_password(payload.password, user.password_hash)

    if not authenticated:
        for key in (client_key, username_key):
            login_limiter.record_attempt(key)
        register_failed_login(db, payload.username, LOGIN_GUARD, now=time.time())
        # Deliberately no username: it is attacker-supplied on this path, so
        # recording it would let anyone write arbitrary text into system_logs
        # and would publish every account name they guessed at.
        _write_log(db, "warn", "login_failed")
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS)

    for key in (client_key, username_key):
        login_limiter.forget(key)
    clear_failed_logins(db, user.username)
    token = create_access_token(
        user.id, user.username, getattr(user, "session_version", 1) or 1
    )
    _write_log(db, "info", f"login_succeeded user={user.username}")
    return LoginResponse(access_token=token, user=UserOut.model_validate(user))


@api.post("/auth/logout")
def logout(user: HQUser = Depends(get_current_user)):
    # JWT is stateless; frontend just discards the token
    return {"ok": True}


@api.get("/auth/me", response_model=UserOut)
def me(user: HQUser = Depends(get_current_user)):
    return UserOut.model_validate(user)


@api.get("/auth/permissions")
def my_permissions(user: HQUser = Depends(get_current_user)):
    """The authoritative source for what THIS signed-in account may do.

    Frontend menu hiding is never the security boundary - every protected
    operation is enforced again, independently, by the specific
    `require(...)` guard on that endpoint. This exists so the frontend has one
    place to ask "what can I actually do" instead of re-deriving it from the
    role string.
    """
    return {
        "role": parse_role(user.role).value if parse_role(user.role) else None,
        "permissions": sorted(resolve_effective_permissions(engine, user)),
    }


#: The permission each socket requires. A ticket is only ever as strong as the
#: right needed to mint it, so this table is the whole access-control decision.
_TICKET_PERMISSIONS = {
    AUDIENCE_HQ: Permission.VIEW_STATUS,
    AUDIENCE_BROADCASTER: Permission.START_BROADCAST,
}


@api.post("/auth/ws-ticket")
def issue_websocket_ticket(
    payload: WebSocketTicketRequest,
    user: HQUser = Depends(get_current_user),
):
    """Mint a single-use handshake ticket for ONE named HQ WebSocket.

    A browser cannot set an Authorization header on a WebSocket handshake, so
    something has to travel in the URL - and Uvicorn logs the URL in full. This
    endpoint is reached over the normal authenticated HTTP API, where the JWT
    stays in a header, and returns a credential that is worthless seconds later
    and after a single use.

    THE TICKET IS SCOPED, AND THAT IS THE POINT. It used to carry only a user id,
    so one ticket opened both the dashboard and the microphone uplink - and the
    uplink checked nothing at all. A read-only VIEWER could therefore mint a
    ticket here and push audio to the loudspeakers of every targeted Store, or
    simply occupy the single uplink slot and deny it to the operator who was
    allowed to use it.

    The permission is checked per audience, and checked AGAIN at the handshake
    against a freshly loaded account: a right verified only at mint time is a
    right verified once, and an account can be demoted or disabled in the seconds
    before it connects.
    """
    required = _TICKET_PERMISSIONS[payload.audience]
    require_permission(user, required)
    ticket = ws_ticket_store.issue(user_id=str(user.id), audience=payload.audience)
    return {"ticket": ticket, "expires_in": TICKET_TTL_SECONDS,
            "audience": payload.audience}


#: What User Management may be sorted by.
USER_SORTS = {
    "username": lambda row: row.get("username"),
    "display_name": lambda row: row.get("display_name"),
    "role": lambda row: row.get("role"),
    "state": lambda row: row.get("state"),
    "created_at": lambda row: row.get("created_at"),
    "last_login_at": lambda row: row.get("last_login_at"),
}

# ================ HQ USERS ================
#
# Every refusal below is enforced here, on the server. The frontend hides
# buttons an account may not use, but hiding a button is a courtesy to the
# person looking at the screen - it is not a control. Anybody can open the
# network tab and issue the request themselves.
def _user_or_404(user_id: int) -> dict:
    try:
        return read_user(engine, user_id=user_id)
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="No such HQ User")


def _require_may_manage(actor: HQUser, target_role: str) -> None:
    """The actor's role must be allowed to manage the target's role.

    ADMIN cannot manage another ADMIN - two administrators disabling each other
    is a support call nobody wins - and cannot touch a SUPER_ADMIN at all,
    because being able to promote yourself would make every other restriction
    here decorative.
    """
    try:
        actor_role = Role(actor.role.upper())
        subject_role = Role(str(target_role).upper())
    except ValueError:
        raise HTTPException(status_code=403, detail=RBAC_REFUSED)
    if not may_manage_role(actor_role, subject_role):
        raise HTTPException(status_code=403, detail=RBAC_REFUSED)


def _user_lifecycle_call(action, **kwargs) -> dict:
    try:
        return action(engine, **kwargs)
    except DuplicateUsernameError as clash:
        raise HTTPException(status_code=409, detail=str(clash))
    except (LastSuperAdminError, SelfActionRefused) as refusal:
        # 409, not 403: the caller has the right permission, and the request is
        # refused because of what it would do to the system, not who asked.
        raise HTTPException(status_code=409, detail=str(refusal))
    except (UserTransitionRefused, UserNotRestorableError, RoleAssignmentRefused) as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    except UserNotFoundError:
        raise HTTPException(status_code=404, detail="No such HQ User")
    except UserLifecycleError as invalid:
        raise HTTPException(status_code=400, detail=str(invalid))


@api.get("/users", response_model=List[HQUserOut])
def list_hq_users(user: HQUser = Depends(require("menu.users.view"))):
    return [HQUserOut(**record) for record in list_users(engine)]


# Declared BEFORE /users/{user_id}: FastAPI matches routes in definition
# order, so with the parameterised route first the literal path 'search'
# is parsed as a user_id and the request fails with a 422 about an
# unparseable integer rather than reaching this handler.
@api.get("/users/search")
def search_users(
    q: Optional[str] = None,
    role: Optional[str] = None,
    state: Optional[str] = None,
    # A string, like every other multi-select filter: the control offers
    # tick-boxes, so `scope_store_id=4,7` is an ordinary request. As an int it
    # answered with "Input should be a valid integer" the moment somebody
    # ticked a second shop.
    scope_store_id: Optional[str] = None,
    scope_city: Optional[str] = None,
    scope_region: Optional[str] = None,
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    user: HQUser = Depends(require("menu.users.view")),
):
    """Filtered, paginated User Management.

    There is no include_deleted parameter any more, and its absence is the
    point. Permanent deletion used to leave a tombstoned row that this flag
    could reveal; deletion is now real, so there is nothing left to reveal and
    a flag promising otherwise would be a control that can never do anything.
    Accounts that still exist - active and archived - are selected with
    `state`.
    """
    page, page_size = normalize_paging(page, page_size)
    records = list_users(engine, include_deleted=False)

    needle = (q or "").strip().lower()
    if needle:
        records = [r for r in records
                   if needle in (r.get("username") or "").lower()
                   or needle in (r.get("display_name") or "").lower()]
    if role:
        records = [r for r in records if (r.get("role") or "").upper() == role.upper()]
    if state:
        wanted = state.lower()
        records = [r for r in records
                   if (r.get("lifecycle_state") or "active").lower() == wanted]

    # Scope filters: "which accounts are assigned to this Store/City/Zone".
    # Deliberately matches the ASSIGNMENT rows rather than the resolved Store
    # set - an account scoped to a City is found by its City assignment, not
    # by every Store that City happens to contain. UNION semantics are
    # untouched: this filters the list of accounts, it does not change what
    # any account can see.
    wanted_scope_stores = _id_list(scope_store_id)
    wanted_cities = _text_list(scope_city)
    wanted_regions = _text_list(scope_region)
    if wanted_scope_stores or wanted_cities or wanted_regions:
        matching_users = set()
        with engine.connect() as connection:
            for store in wanted_scope_stores:
                matching_users |= {r[0] for r in connection.execute(text(
                    "SELECT user_id FROM user_store_scope "
                    "WHERE scope_type = 'STORE' AND store_id = :i"),
                    {"i": store})}
            for city in wanted_cities:
                matching_users |= {r[0] for r in connection.execute(text(
                    "SELECT user_id FROM user_store_scope "
                    "WHERE scope_type = 'CITY' AND scope_value = :v"),
                    {"v": city})}
            for region in wanted_regions:
                matching_users |= {r[0] for r in connection.execute(text(
                    "SELECT user_id FROM user_store_scope "
                    "WHERE scope_type = 'REGION' AND scope_value = :v"),
                    {"v": region})}
        records = [r for r in records if r["id"] in matching_users]

    total = len(records)
    records = sort_rows(records, sort, dir, USER_SORTS)
    offset = (page - 1) * page_size
    return Page(items=records[offset:offset + page_size], total=total,
                page=page, page_size=page_size).as_dict()


@api.get("/users/{user_id}", response_model=HQUserOut)
def read_hq_user(user_id: int, user: HQUser = Depends(require("menu.users.view"))):
    return HQUserOut(**_user_or_404(user_id))


@api.post("/users", response_model=HQUserOut, status_code=201)
def create_hq_user(
    payload: HQUserCreate,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("users.create")),
):
    _require_may_manage(user, payload.role)
    created = _user_lifecycle_call(
        create_user,
        username=payload.username,
        display_name=payload.display_name,
        role=payload.role,
        password_hash=hash_password(payload.password),
    )
    # The password is never logged, and neither is its hash.
    _write_log(db, "info", f"hq_user_created username={created['username']} "
                           f"role={created['role']} by={user.username}")
    return HQUserOut(**created)


@api.patch("/users/{user_id}", response_model=HQUserOut)
def update_hq_user(
    user_id: int,
    payload: HQUserUpdate,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("users.update")),
):
    existing = _user_or_404(user_id)
    _require_may_manage(user, existing["role"])
    updated = _user_lifecycle_call(
        update_user, user_id=user_id,
        display_name=payload.display_name, username=payload.username)
    _write_log(db, "info", f"hq_user_updated id={user_id} by={user.username}")
    return HQUserOut(**updated)


@api.post("/users/{user_id}/role", response_model=HQUserOut)
def set_hq_user_role(
    user_id: int,
    payload: HQUserRoleUpdate,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("users.update")),
):
    existing = _user_or_404(user_id)
    # Both ends: you may not manage the account as it is now, and you may not
    # hand out a role you could not manage afterwards.
    _require_may_manage(user, existing["role"])
    _require_may_manage(user, payload.role)
    updated = _user_lifecycle_call(assign_role, user_id=user_id, role=payload.role,
                                   actor_id=user.id)
    _write_log(db, "warn", f"hq_user_role_changed id={user_id} role={updated['role']} "
                           f"by={user.username}")
    return HQUserOut(**updated)


def _lifecycle_endpoint(action, verb: str, level: str = "warn"):
    def handler(user_id: int, db: Session, user: HQUser) -> HQUserOut:
        existing = _user_or_404(user_id)
        _require_may_manage(user, existing["role"])
        changed = _user_lifecycle_call(action, user_id=user_id, actor_id=user.id)
        _write_log(db, level, f"hq_user_{verb} id={user_id} by={user.username}")
        return HQUserOut(**changed)
    return handler


@api.post("/users/{user_id}/disable", response_model=HQUserOut)
def disable_hq_user(user_id: int, db: Session = Depends(get_db),
                    user: HQUser = Depends(require("users.disable"))):
    return _lifecycle_endpoint(disable_user, "disabled")(user_id, db, user)


@api.post("/users/{user_id}/enable", response_model=HQUserOut)
def enable_hq_user(user_id: int, db: Session = Depends(get_db),
                   user: HQUser = Depends(require("users.update"))):
    return _lifecycle_endpoint(enable_user, "enabled", level="info")(user_id, db, user)


@api.post("/users/{user_id}/archive", response_model=HQUserOut)
def archive_hq_user(user_id: int, db: Session = Depends(get_db),
                    user: HQUser = Depends(require("users.disable"))):
    return _lifecycle_endpoint(archive_user, "archived")(user_id, db, user)


@api.post("/users/{user_id}/restore", response_model=HQUserOut)
def restore_hq_user(user_id: int, db: Session = Depends(get_db),
                    user: HQUser = Depends(require("users.update"))):
    return _lifecycle_endpoint(restore_user, "restored")(user_id, db, user)


# ---- passwords ------------------------------------------------------------
@api.post("/auth/change-password")
def change_own_password(
    payload: PasswordChangeIn,
    db: Session = Depends(get_db),
    user: HQUser = Depends(get_current_user),
):
    """Change your own password. The current one is required.

    Requiring it is not paperwork: without it, an unattended signed-in desktop
    is a permanent account takeover rather than a session somebody can end.
    """
    if not verify_password(payload.current_password, user.password_hash):
        # Deliberately the same shape as any other refusal, and logged without
        # either password.
        _write_log(db, "warn", f"password_change_refused user={user.username}")
        raise HTTPException(status_code=403, detail="That is not your current password.")
    _user_lifecycle_call(set_password_hash, user_id=user.id,
                         password_hash=hash_password(payload.new_password))
    _write_log(db, "warn", f"password_changed user={user.username}")
    # Every token minted before this moment is now invalid, including the one
    # that made this request. The caller has to sign in again.
    return {"ok": True, "sessions_ended": True}


@api.post("/users/{user_id}/reset-password", response_model=PasswordResetOut)
def reset_hq_user_password(
    user_id: int,
    payload: PasswordResetIn,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require_super_admin),
):
    """Set somebody else's password to one the administrator has chosen.

    The response confirms that it worked and returns nothing reusable. It does
    not echo the new password, and there is no reset link or token: a value in
    a response body is a value in a browser's memory, in a proxy log and in the
    screenshot somebody pastes into a chat.

    How the new password reaches the person is deliberately outside this
    system - see the runbook. Said out loud, or typed by them while the
    administrator looks away. It is single-use in practice because their first
    action is to change it.
    """
    existing = _user_or_404(user_id)
    _require_may_manage(user, existing["role"])
    _user_lifecycle_call(set_password_hash, user_id=user_id,
                         password_hash=hash_password(payload.new_password))
    _write_log(db, "warn", f"password_reset_for id={user_id} by={user.username}")
    return PasswordResetOut(
        user_id=user_id,
        sessions_ended=True,
        detail="The password was set and every existing session for that account ended.",
    )


# ================ RECEIVER DEVICE ENROLMENT ================
ENROLMENT_REFUSED = "That enrolment code cannot be used."


@api.post("/receiver-devices/enrollment-codes", response_model=EnrollmentCodeResponse)
def create_receiver_enrollment_code(
    payload: EnrollmentCodeRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.enrollment.create")),
):
    """Mint a one-time code for one Store. Shown once, never stored raw."""
    _require_store_in_scope(user, payload.store_id)
    try:
        issued = create_enrollment_code(db, store_id=payload.store_id, actor_user_id=user.id)
    except TooManyOutstandingCodes as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    except EnrollmentRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    # The code itself is never logged - only that one was created, and for whom.
    _write_log(db, "info", f"enrollment_code_created store_id={payload.store_id} by={user.username}")
    return EnrollmentCodeResponse(
        code=issued.code,
        store_id=issued.store_id,
        expires_in_seconds=CODE_TTL_SECONDS,
    )


@api.post("/receiver-devices/enroll", response_model=DeviceEnrollmentResponse)
def enroll_receiver(
    payload: DeviceEnrollmentRequest,
    request: Request,
    db: Session = Depends(get_db),
):
    """Redeem a code and receive one Device credential, exactly once.

    Unauthenticated on purpose: a Receiver computer has no credential yet, and
    the code is what proves an administrator sent it. Rate-limited because that
    makes it the one endpoint worth guessing at.
    """
    client_key = f"enrol:{client_identifier(request.client.host if request.client else None, request.headers.get('x-forwarded-for'), trust_proxy=TRUST_PROXY_HEADERS)}"
    retry_after = enrollment_limiter.retry_after(client_key)
    if retry_after is not None:
        _write_log(db, "warn", "enrollment_rate_limited")
        raise HTTPException(
            status_code=429,
            detail="Too many enrolment attempts. Please try again later.",
            headers={"Retry-After": str(retry_after)},
        )

    # Derived here, once, so that nothing below this line can reach the raw
    # code. The fingerprint is 8 hex characters of a hash: enough to tell two
    # attempts apart in a log, not enough to reconstruct anything.
    presented_fingerprint = fingerprint(payload.code)
    presented_length = len(payload.code or "")

    key_ring = receiver_key_ring()
    try:
        result = redeem_and_enroll(
            db,
            engine,
            code=payload.code,
            device_name=payload.device_name,
            hostname=payload.hostname,
            software_version=payload.software_version,
            key_ring=key_ring,
        )
    except DeviceEnrolmentBlocked as blocked:
        # HQ cannot verify what it would issue - the credential migration state
        # says so - and it refused BEFORE claiming the code, so nothing was
        # spent. This escaped as a 500 until a test tripped it: an operator saw
        # "Internal Server Error" for a condition the exception was carrying a
        # written explanation of.
        _write_log(db, "warn",
                   f"enrollment_blocked reason={getattr(blocked.reason, 'value', blocked.reason)}")
        raise HTTPException(status_code=503, detail=str(blocked))
    except EnrollmentUnavailable as unavailable:
        # The operator's problem, not the caller's, and it must never look like
        # a bad code.
        _write_log(db, "warn", "enrollment_unavailable")
        raise HTTPException(status_code=503, detail=str(unavailable))
    except EnrollmentRefused as refusal:
        enrollment_limiter.record_attempt(client_key)
        # The category is recorded, the caller is not told it.
        #
        # An operator needs to know whether the code was unknown, expired,
        # already used or refused because the Store is archived - four different
        # actions. But this endpoint is unauthenticated, so telling the CALLER
        # which one applied turns it into an oracle: "expired" means the code
        # existed, "already used" means somebody enrolled with it. So the
        # category goes to the audit log, which needs a signed-in account to
        # read, and the wire response stays one generic sentence.
        #
        # The raw code is never part of either.
        category = classify_enrolment_refusal(str(refusal))
        # WHAT ELSE WAS TRUE AT THAT MOMENT, for the one category that is
        # otherwise undiagnosable from HQ. "That code matched nothing" leaves
        # an operator unable to tell a mistyped code from a stale one from a
        # code issued against a different HQ - so the log says how many usable
        # codes were outstanding, and how long ago the newest was issued.
        #
        # A code issued 40 seconds ago plus a presented code that matches
        # nothing is a copying problem, and the log now says so instead of
        # leaving somebody to generate a sixth code.
        #
        # No part of any code is recorded. Counting is not disclosure.
        context = ""
        if category is RefusalCategory.UNKNOWN_TOKEN:
            try:
                context = (f" presented_fingerprint={presented_fingerprint}"
                           f" presented_length={presented_length}"
                           " " + describe_outstanding_codes(db))
            except Exception:  # noqa: BLE001 - never fail a refusal over a hint
                context = ""
        _write_log(db, "warn",
                   f"enrollment_code_rejected category={category.value}{context}")
        raise HTTPException(status_code=400, detail=ENROLMENT_REFUSED)

    enrollment_limiter.forget(client_key)
    device = read_device(engine, public_id=result.device_public_id)
    _write_log(
        db, "info",
        f"receiver_device_enrolled device={result.device_public_id} store_id={device['store_id']}",
    )
    return DeviceEnrollmentResponse(
        device_public_id=result.device_public_id,
        credential=result.take_raw_credential(),
        credential_version=result.credential_version,
        store_id=device["store_id"],
    )


@api.get("/stores/{store_id}/receiver-devices", response_model=List[ReceiverDeviceOut])
def list_receiver_devices(
    store_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    _require_store_in_scope(user, store_id)
    try:
        return [ReceiverDeviceOut(**row) for row in list_devices(engine, store_id=store_id)]
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))


# Declared BEFORE /receiver-devices/{public_id}: FastAPI matches routes in
# definition order, so with the parameterised route first the literal path
# 'search' is taken as a public_id and the request 404s. The same trap
# already caught /users/search - see docs/learning-guide.md.
@api.get("/receiver-devices/search")
def search_receiver_devices(
    q: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    # A string, not an int: a multi-select filter legitimately sends
    # `store_id=4,7`, and an int parameter answers that with a 422 whose
    # detail is a LIST - which is how choosing two Stores took a page white.
    store_id: Optional[str] = None,
    status_f: Optional[str] = Query(None, alias="status"),
    # Both boxes ticked means "either", which is the unfiltered list. Said
    # here rather than built into `A AND NOT A`, which returns nothing.
    is_primary: Optional[str] = None,
    lifecycle: Optional[str] = None,
    include_archived: bool = True,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """Filtered, paginated Receiver Devices ACROSS Stores.

    archived and deleted are different states and are reported separately in
    ``lifecycle``: an archived Device can be restored, a permanently deleted
    one never can.

    A permanently deleted Device is NEVER returned here, under any query
    parameter. There is deliberately no include_deleted: an opt-in is a thing
    that can be left on, mistyped into a URL, or remembered by a stale
    bookmark, and this endpoint feeds the operational screens. Read tombstones
    through /receiver-devices/{public_id}/deletion-events instead.
    """
    page, page_size = normalize_paging(page, page_size)
    where = ["1=1"]
    params = {}
    term = like_term(q)
    if term:
        params["term"] = term
        where.append("(d.public_id LIKE :term OR d.display_name LIKE :term "
                     "OR s.store_code LIKE :term OR s.store_name LIKE :term)")
    # Every one of these is multi-value. The controls above them offer
    # tick-boxes, so `city=DELHI,JAIPUR` is an ordinary request rather than an
    # error - and an `=` comparison would answer a two-city question with an
    # empty table.
    _in_clause(where, params, "s.city", "city", _text_list(city))
    _in_clause(where, params, "s.region", "region", _text_list(region))
    _in_clause(where, params, "d.store_id", "store", _id_list(store_id))
    _in_clause(where, params, "d.status", "status", _text_list(status_f))
    # UNCONDITIONAL, and the only unconditional clause here. This endpoint is
    # operational, a permanently deleted Device is not, and there is therefore
    # no parameter that may bring one back. Making it opt-out was the defect:
    # RC18 removed the UI control that sent include_deleted, but the parameter
    # survived and a hand-crafted ?include_deleted=true still returned every
    # tombstone on the live system. A capability nobody can reach through the
    # product is still a capability.
    #
    # The row is untouched - see /receiver-devices/{public_id}/deletion-events
    # for the audit trail, which is where a tombstone is supposed to be read.
    where.append("d.deleted_at IS NULL")
    if not include_archived:
        where.append("d.archived_at IS NULL")
    if lifecycle in (None, "", "all_current"):
        # Every Device that still exists. 'all_current' is named rather than
        # implied by an empty value, so the control has no meaning that
        # nobody chose.
        pass
    elif lifecycle == "deleted":
        # Not an error, and deliberately not a 400: 'deleted' is a state this
        # endpoint knows about and simply never has anything to say about.
        # Returning an empty page says that without pretending the caller
        # asked something meaningless.
        where.append("1 = 0")
    elif lifecycle == "archived":
        where.append("d.archived_at IS NOT NULL AND d.deleted_at IS NULL")
    elif lifecycle == "active":
        where.append("d.archived_at IS NULL AND d.deleted_at IS NULL")
    want_primary = _tri_state(is_primary)
    if want_primary is not None:
        exists = ("EXISTS (SELECT 1 FROM receiver_store_primary_device p "
                  "WHERE p.device_id = d.id)")
        where.append(exists if want_primary else f"NOT {exists}")

    clause = " AND ".join(where) + _receiver_scope_clause(user, params)
    base = f"FROM receiver_devices d JOIN stores s ON s.id = d.store_id WHERE {clause}"

    total = db.execute(text(f"SELECT COUNT(*) {base}"), params).scalar_one()
    rows = db.execute(text(
        "SELECT d.id, d.public_id, d.display_name, d.status, d.archived_at, "
        "  d.deleted_at, s.id AS store_id, s.store_code, s.store_name, s.city, s.region, "
        "  EXISTS (SELECT 1 FROM receiver_store_primary_device p WHERE p.device_id = d.id) "
        f"    AS is_primary {base} ORDER BY d.id LIMIT :limit OFFSET :offset"),
        {**params, "limit": page_size, "offset": (page - 1) * page_size}).all()

    items = [{
        "public_id": r.public_id, "display_name": r.display_name, "status": r.status,
        "lifecycle": ("deleted" if r.deleted_at else
                      "archived" if r.archived_at else "active"),
        "archived_at": r.archived_at, "deleted_at": r.deleted_at,
        "is_primary": bool(r.is_primary),
        "store_id": r.store_id, "store_code": r.store_code,
        "store_name": r.store_name, "city": r.city, "region": r.region,
    } for r in rows]
    return Page(items=items, total=total, page=page, page_size=page_size).as_dict()


@api.get("/receiver-devices/{public_id}", response_model=ReceiverDeviceOut)
def read_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    try:
        return ReceiverDeviceOut(**read_device(engine, public_id=public_id))
    except DeviceNotFound:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))



def _device_internal_id(public_id: str) -> int | None:
    """Resolve a Device's internal row id from its public id.

    Done here rather than by widening the Device response shape: `public_id` is
    what the product exposes, and adding the internal key to every Device
    payload just to reach the runtime would publish it to every caller for
    ever.
    """
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT id FROM receiver_devices WHERE public_id = :p"),
                {"p": public_id},
            ).first()
        return int(row[0]) if row else None
    except Exception:
        # A runtime disconnect must never turn a successful lifecycle change
        # into a 500. The database change has already happened and is what
        # blocks the next reconnect.
        return None


async def _disconnect_device_runtime(public_id: str, device_id: int | None,
                                     *, action: str) -> bool:
    """Close a Device's live socket after its access has been withdrawn.

    Called AFTER the database change, never instead of it. The database is the
    authority on whether the Device may connect; this is what stops the socket
    that already did.

    Without it, disabling, revoking, archiving or permanently deleting a Device
    changed rows and nothing else: the socket had authenticated once at connect
    and was never re-checked, so it stayed registered and kept receiving
    broadcast audio. A Store whose only Device an operator had just deleted
    went READY, AUDIO_RECEIVING and PLAYBACK_CONFIRMED fifty seconds
    afterwards, while the Receiver Devices page correctly showed nothing.

    Deliberately keyed on the DEVICE id. Closing "the Store's connection"
    would silence a shop that had already failed over to a different, entirely
    valid Device.
    """
    resolved = device_id if device_id is not None else _device_internal_id(public_id)
    if resolved is None:
        return False
    closed = await manager.disconnect_device(resolved)
    if closed:
        logger.info(
            "Receiver Device %s disconnected from runtime after %s", public_id, action)
    return closed


@api.post("/receiver-devices/{public_id}/disable", response_model=ReceiverDeviceOut)
async def disable_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.disable")),
):
    """Stop this one computer. Its Store and every other Device keep working.

    "Stop" now means stop: the Device's live socket is closed as well as its
    row updated, so a disabled computer stops playing immediately rather than
    at whatever future moment it happens to reconnect.
    """
    try:
        device = disable_device(engine, public_id=public_id)
    except DeviceNotFound:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))
    await _disconnect_device_runtime(public_id, None, action="disable")
    _write_log(db, "warn", f"receiver_device_disabled device={public_id} by={user.username}")
    return ReceiverDeviceOut(**device)


@api.post("/receiver-devices/{public_id}/archive", response_model=ReceiverDeviceOut)
async def archive_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.archive")),
):
    """Retire this Device from the active list. Reversible - its credential
    history and every enrolment/audit event are untouched."""
    try:
        device = archive_device(engine, public_id=public_id)
    except DeviceNotFound:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))
    # Archiving retires the Device from the active list, and _set_status also
    # clears its primary assignment - so leaving its socket open would keep a
    # retired computer in the fanout.
    await _disconnect_device_runtime(public_id, None, action="archive")
    _write_log(db, "warn", f"receiver_device_archived device={public_id} by={user.username}")
    return ReceiverDeviceOut(**device)


@api.post("/receiver-devices/{public_id}/restore", response_model=ReceiverDeviceOut)
def restore_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.archive")),
):
    """Bring an archived Device back to disabled, never straight to active."""
    try:
        device = restore_device(engine, public_id=public_id)
    except DeviceNotRestorable as refusal:
        # 409, not 404: the Device is right there, and the refusal is about
        # what it has become rather than whether it exists.
        raise HTTPException(status_code=409, detail=str(refusal))
    except DeviceNotFound:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))
    _write_log(db, "info", f"receiver_device_restored device={public_id} by={user.username}")
    return ReceiverDeviceOut(**device)


@api.get("/receiver-devices/{public_id}/dependencies")
def read_receiver_device_dependencies(
    public_id: str,
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """What still refers to this Device, so the UI can show it before offering
    a permanent delete that would be refused anyway."""
    summary = device_dependencies(engine, public_id=public_id)
    if not summary.exists:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    return {"counts": summary.counts, "unchecked": summary.unchecked,
            "total": summary.total, "deletable": summary.deletable,
            "explanation": summary.explain()}


class DeviceTombstoneRequest(BaseModel):
    confirm: str
    acknowledged: bool = False


@api.post("/receiver-devices/{public_id}/delete-permanently")
async def tombstone_receiver_device(public_id: str, payload: DeviceTombstoneRequest,
                              db: Session = Depends(get_db),
                              user: HQUser = Depends(require("devices.delete_permanently"))):
    """Permanently remove a Receiver Device from operational SpeakLink even
    though it has credential and event history.

    The row is never deleted - see device_deletion.py - so every credential,
    credential event and Receiver event that refers to it stays readable.
    Its credentials are revoked, its primary assignment cleared, and its
    status becomes 'retired', which the Receiver authentication path already
    refuses. It can never be restored and its public_id can never be reused.
    """
    if not payload.acknowledged:
        raise HTTPException(
            status_code=400,
            detail="The 'this Device cannot be restored' acknowledgement is required.",
        )
    try:
        result = permanently_delete_device_with_history(
            engine, public_id=public_id, typed_confirmation=payload.confirm,
            actor_user_id=user.id,
        )
    except DeviceDeletionRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    # The row is a tombstone and its credentials are revoked, but a socket that
    # authenticated before either happened is still open until it is closed.
    await _disconnect_device_runtime(
        result.public_id, getattr(result, "device_id", None),
        action="permanent delete")
    _write_log(
        db, "warn",
        f"DEVICE_PERMANENTLY_DELETED device={result.public_id} "
        f"store_id={result.store_id} credentials_revoked={result.credentials_revoked} "
        f"was_primary={result.was_primary} by={user.username}",
    )
    return {
        "ok": True,
        "public_id": result.public_id,
        "store_id": result.store_id,
        "display_name": result.display_name,
        "deleted_at": result.deleted_at,
        "credentials_revoked": result.credentials_revoked,
        "was_primary": result.was_primary,
    }


@api.get("/receiver-devices/{public_id}/deletion-events")
def read_device_deletion_events(public_id: str,
                                user: HQUser = Depends(require("menu.receivers.view"))):
    """Audit trail for a tombstoned Device. Never a credential or a hash."""
    return {"events": list_device_deletion_events(engine, public_id=public_id)}


@api.delete("/receiver-devices/{public_id}/permanently")
def hard_delete_receiver_device(
    public_id: str, confirm: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.delete_permanently")),
):
    """Remove a never-enrolled-into Device for real. Refuses anything with an
    issued credential or a recorded event - which is every real Device, since
    enrolment is what creates one. Separate from /disable, /archive and
    /revoke, which all keep the row and its history."""
    try:
        removed = delete_device_if_unused(engine, public_id=public_id, typed_confirmation=confirm)
    except DeletionRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    _write_log(db, "warn",
               f"RECEIVER_DEVICE_DELETED device={removed['public_id']} by={user.username}")
    return {"ok": True, "deleted": removed}


@api.post("/receiver-devices/{public_id}/promote", response_model=List[ReceiverDeviceOut])
def promote_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.primary.assign")),
):
    """Make this one computer the Store's primary - the one that plays audio.

    Explicit on purpose. Nothing promotes a standby automatically, because that
    would move the announcement onto a computer nobody has confirmed is connected
    to the amplifier, and do it silently.
    """
    try:
        promoted = promote_device(engine, device_public_id=public_id, actor_user_id=user.id)
    except DeviceNotPromotableError:
        raise HTTPException(status_code=404, detail="Receiver Device not found or not active")
    except Exception:
        raise HTTPException(status_code=503, detail="The promotion could not be recorded")

    _write_log(
        db, "warn",
        f"receiver_primary_promoted device={public_id} store_id={promoted['store_id']} by={user.username}",
    )
    return [ReceiverDeviceOut(**row) for row in list_devices(engine, store_id=promoted["store_id"])]


@api.get("/stores/{store_id}/receiver-devices/roles")
def read_receiver_device_roles(
    store_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """Every Device of one Store with its primary/standby role. No credentials."""
    _require_store_in_scope(user, store_id)
    try:
        return describe_store_devices(engine, store_id=store_id)
    except Exception:
        raise HTTPException(status_code=503, detail="Receiver Device roles are unavailable")


@api.get("/stores/{store_id}/enrollment-codes",
         response_model=List[EnrollmentCodeStatusOut])
def list_receiver_enrollment_codes(
    store_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """Enrollment records for one Store: state, timestamps, and what they proved.

    Authenticated on purpose. The unauthenticated redemption endpoint stays a
    single generic sentence precisely so it cannot be used as an oracle; this
    is where an operator, signed in, gets the detail they need instead.

    Carries no raw code and no verifier. The raw code left the server once, at
    creation, and cannot be retrieved here or anywhere else - the database
    holds a SHA-256 of it, and that is not something a page has any use for.
    """
    _require_store_in_scope(user, store_id)
    codes = (
        db.query(ReceiverEnrollmentCode)
        .filter(ReceiverEnrollmentCode.store_id == store_id)
        .order_by(ReceiverEnrollmentCode.id.desc())
        .all()
    )
    if not codes:
        return []

    # Resolved once for the whole list rather than per row: a page showing ten
    # codes must not issue ten role queries.
    try:
        roles = describe_store_devices(engine, store_id=store_id)
    except Exception:  # noqa: BLE001 - progress degrades, the list still renders
        roles = []
    # DeviceRole is a str Enum whose value is "PRIMARY". Comparing against
    # "primary" silently matched nothing - the same case-mismatch that once made
    # a role check in this repository quietly always-false.
    primary_public_ids = {
        row["public_id"] for row in roles
        if str(row.get("role", "")).upper().endswith("PRIMARY")
    }
    # describe_store_devices deliberately publishes no internal row id, so the
    # public id is mapped to a device id here instead. Reading a "device_id" key
    # off that dict returned None for every Device, which made DEVICE_CONNECTED
    # unreachable - and made the test asserting its absence pass for the wrong
    # reason.
    device_ids_by_public_id = _device_ids_by_public_id(store_id)
    connected_device_ids = manager.connected_device_ids()

    return [
        EnrollmentCodeStatusOut(
            id=row.id,
            store_id=row.store_id,
            state=describe_state(row),
            created_at=_as_utc_text(row.created_at),
            expires_at=_epoch_to_utc_text(row.expires_at_epoch),
            used_at=(_epoch_to_utc_text(row.redeemed_at_epoch)
                     if row.redeemed_at_epoch is not None else None),
            device_public_id=row.device_public_id,
            progress=_enrollment_progress(
                row,
                primary_public_ids=primary_public_ids,
                device_ids_by_public_id=device_ids_by_public_id,
                connected_device_ids=connected_device_ids,
            ),
        )
        for row in codes
    ]


def _device_ids_by_public_id(store_id: int) -> dict:
    """Public id -> internal Device id, for one Store.

    Needed because connection state is keyed by the internal id while every API
    surface speaks the public one. Read-only, and it returns an empty mapping
    rather than raising: a failure here degrades reported setup progress and
    must not stop the enrollment list from rendering.
    """
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text("SELECT public_id, id FROM receiver_devices WHERE store_id = :store_id"),
                {"store_id": store_id},
            ).all()
        return {row.public_id: row.id for row in rows}
    except SQLAlchemyError:
        # Deliberately NOT `except Exception`. A broad catch here swallowed a
        # NameError from a missing `text` import and silently returned {}, which
        # made DEVICE_CONNECTED unreachable and let a test asserting its absence
        # pass for entirely the wrong reason. Only a real database fault degrades
        # progress; a bug in this function must surface.
        return {}


def _as_utc_text(value) -> str:
    """A stored naive datetime is UTC by this project's convention; say so
    explicitly rather than shipping an ambiguous timestamp to 44 Stores."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _epoch_to_utc_text(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _enrollment_progress(
    row,
    *,
    primary_public_ids,
    device_ids_by_public_id,
    connected_device_ids,
) -> List[str]:
    """Stages this record can actually prove, each checked on its own evidence.

    Every stage is backed by something stored or currently true. None is
    inferred from elapsed time, and none is inferred from silence:

    CODE_CREATED      the row exists
    CODE_REDEEMED     redeemed_at_epoch is set
    DEVICE_CREATED    device_public_id was recorded at redemption
    DEVICE_CONNECTED  that Device holds a live socket RIGHT NOW
    PRIMARY_ASSIGNED  that Device is this Store's primary (a stored fact)

    NOT a pipeline that stops at the first gap, and a test caught me writing it
    as one. DEVICE_CONNECTED is a LIVE fact and PRIMARY_ASSIGNED is a STORED
    one, so they are independent: a Device can be this Store's primary and be
    switched off right now. Gating the stored fact behind the live one hid a
    promotion that had definitely happened, which is the same class of error as
    inferring a stage that had not - reporting something other than what the
    evidence says.

    The two genuine dependencies are kept, because they are real: there is no
    DEVICE_CREATED without a redemption, and no redemption without a code.

    DEVICE_CONNECTED is deliberately about the present. ``receiver_events``
    records a store_id and no device id, so "has this Device ever connected?"
    cannot be answered from stored data - and a stage that cannot be proved is
    one this list must not contain.
    """
    stages = ["CODE_CREATED"]
    if row.redeemed_at_epoch is None:
        return stages
    stages.append("CODE_REDEEMED")

    public_id = row.device_public_id
    if not public_id:
        # Redeemed before the link column existed, or the link write failed.
        # Absent is absent; it is not evidence that no Device was created.
        return stages
    stages.append("DEVICE_CREATED")

    device_id = device_ids_by_public_id.get(public_id)
    if device_id is not None and device_id in connected_device_ids:
        stages.append("DEVICE_CONNECTED")
    if public_id in primary_public_ids:
        stages.append("PRIMARY_ASSIGNED")
    return stages


@api.post("/receiver-devices/{public_id}/rotate-credential",
          response_model=CredentialRotationResponse)
def rotate_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.rotate")),
):
    """Issue one new credential for one Device and retire the old one at once.

    There is no overlap window. You rotate because somebody may hold a copy of the
    old credential, and a grace period is a period in which that copy still works.
    The Device is offline until an operator carries the new credential to it.
    """
    key_ring = receiver_key_ring()
    if key_ring is None:
        raise HTTPException(
            status_code=503,
            detail="Receiver credential rotation is not available on this server.",
        )
    signing_version, signing_key = key_ring.signing_key()
    try:
        rotated = rotate_receiver_device_credential(
            engine,
            device_public_id=public_id,
            actor_user_id=user.id,
            hash_key=signing_key,
            hash_key_version=signing_version,
        )
    except DeviceNotRotatableError:
        raise HTTPException(status_code=404, detail="Receiver Device not found or not active")
    except CredentialNotFoundError:
        raise HTTPException(status_code=409, detail="That Device has no active credential")
    except (MigrationNotReadyError, RotationPersistenceError) as unavailable:
        # The operator's problem, and it must never look like a missing Device.
        _write_log(db, "warn", "receiver_credential_rotation_unavailable")
        raise HTTPException(status_code=503, detail=str(unavailable))
    except ReceiverDeviceServiceError:
        raise HTTPException(status_code=409, detail="That Device cannot be rotated right now")

    device = read_device(engine, public_id=public_id)
    # The credential is never logged - only that a rotation happened, and by whom.
    _write_log(
        db, "warn",
        f"receiver_credential_rotated device={public_id} store_id={device['store_id']} by={user.username}",
    )
    return CredentialRotationResponse(
        device_public_id=rotated.device_public_id,
        credential=rotated.take_raw_credential(),
        credential_version=rotated.credential_version,
        store_id=device["store_id"],
    )


@api.post("/receiver-devices/{public_id}/revoke", response_model=ReceiverDeviceOut)
async def revoke_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.revoke")),
):
    """Retire this one computer permanently, including any live connection."""
    try:
        device = revoke_device(engine, public_id=public_id)
    except DeviceNotFound:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))
    await _disconnect_device_runtime(public_id, None, action="revoke")
    _write_log(db, "warn", f"receiver_device_revoked device={public_id} by={user.username}")
    return ReceiverDeviceOut(**device)


# ================ STORES ================
@api.get("/stores", response_model=List[StoreOut])
def list_stores(
    city: Optional[str] = None,
    region: Optional[str] = None,
    status_f: Optional[str] = Query(None, alias="status"),
    q: Optional[str] = None,
    include_inactive: bool = False,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.stores.view")),
):
    query = db.query(Store)
    if not include_inactive:
        query = query.filter(Store.is_active.is_(True))
    if not include_archived:
        # Archived Stores are retired, not hidden: include_archived shows them so
        # their history stays reachable, but they never appear in the ordinary
        # list an operator picks broadcast targets from.
        query = query.filter(
            (Store.lifecycle_state.is_(None)) | (Store.lifecycle_state != "archived")
        )
    # A permanently deleted Store never comes back, unconditionally - unlike
    # archived, there is no flag that reveals it here. Its history stays
    # readable through the rows that still reference it (Broadcast History,
    # Receiver events), never through this operational list.
    query = query.filter(
        (Store.lifecycle_state.is_(None)) | (Store.lifecycle_state != "deleted")
    )
    if city:
        query = query.filter(Store.city == city)
    if region:
        query = query.filter(Store.region == region)
    if status_f:
        query = query.filter(Store.status == status_f)
    if q:
        like = f"%{q}%"
        query = query.filter((Store.store_name.ilike(like)) | (Store.store_code.ilike(like)))
    # Per-user Store/City/Zone scope. None means unrestricted (OWNER, or an
    # account with no scope rows) - the common case, so this is a no-op then.
    scope = resolve_store_scope(engine, user)
    if scope is not None:
        query = query.filter(Store.id.in_(scope) if scope else Store.id.in_([-1]))
    # Reflect actual online status from live WS state
    stores = query.order_by(Store.store_code).all()
    online_ids = manager.online_store_ids()
    for s in stores:
        if s.status not in ("playing", "error"):
            s.status = "online" if s.id in online_ids else "offline"
    return stores


@api.post("/stores", response_model=StoreOut, status_code=201)
def create_store(payload: StoreCreate, db: Session = Depends(get_db), user: HQUser = Depends(require("stores.create"))):
    if db.query(Store).filter(Store.store_code == payload.store_code).first():
        raise HTTPException(status_code=409, detail="store_code already exists")
    s = Store(**payload.model_dump(), receiver_token=uuid.uuid4().hex)
    db.add(s)
    db.commit()
    db.refresh(s)
    _write_log(db, "info", f"Store created: {s.store_code} by {user.username}")
    return s


@api.put("/stores/{store_id}", response_model=StoreOut)
def update_store(store_id: int, payload: StoreUpdate, db: Session = Depends(get_db), user: HQUser = Depends(require("stores.update"))):
    """Edit a Store's details. Never its state, and never its credentials."""
    s = db.query(Store).filter(Store.id == store_id).first()
    if not s or s.lifecycle_state == "deleted":
        raise HTTPException(status_code=404, detail="Store not found")
    _require_store_in_scope(user, store_id)

    fields = payload.model_dump(exclude_unset=True)
    validators = {
        "store_code": validate_store_code,
        "store_name": validate_store_name,
        "city": lambda value: validate_location(value, field="city"),
        "region": lambda value: validate_location(value, field="region"),
    }
    try:
        for name, validate in validators.items():
            if name in fields and fields[name] is not None:
                fields[name] = validate(fields[name])
    except StoreLifecycleError as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    new_code = fields.get("store_code")
    if new_code and new_code != s.store_code:
        clash = db.query(Store).filter(Store.store_code == new_code, Store.id != store_id).first()
        if clash:
            raise HTTPException(status_code=409, detail="store_code already exists")

    before = {"code": s.store_code, "name": s.store_name}
    for key, value in fields.items():
        if value is not None:
            setattr(s, key, value)
    db.commit()
    db.refresh(s)
    _write_log(
        db, "info",
        f"store_edited store_id={store_id} code={before['code']}->{s.store_code} by={user.username}",
    )
    return s


def _live_store_ids() -> set[int]:
    """Stores currently receiving a live broadcast, so one cannot be pulled out
    from under an announcement the people in it are listening to."""
    return manager.live_store_ids()


def _lifecycle_action(transition, store_id: int, user: HQUser, *, use_live_guard: bool = True):
    try:
        transition(
            SessionLocal, store_id=store_id, actor_user_id=user.id,
            **({"live_store_ids": _live_store_ids()} if use_live_guard else {}),
        )
    except StoreNotFoundError:
        raise HTTPException(status_code=404, detail="Store not found")
    except StoreNotRestorableError as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    except StoreTransitionRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    except StoreLifecycleError as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    store = SessionLocal().query(Store).filter(Store.id == store_id).first()
    return StoreOut.model_validate(store)


@api.post("/stores/{store_id}/disable", response_model=StoreOut)
def disable_store_endpoint(store_id: int, user: HQUser = Depends(require("stores.archive"))):
    """Switch a Store off. Reversible; its history is untouched."""
    _require_store_in_scope(user, store_id)
    return _lifecycle_action(disable_store, store_id, user)


@api.post("/stores/{store_id}/enable", response_model=StoreOut)
def enable_store_endpoint(store_id: int, user: HQUser = Depends(require("stores.update"))):
    """Switch a Store back on. An archived Store is not reachable from here."""
    _require_store_in_scope(user, store_id)
    return _lifecycle_action(enable_store, store_id, user, use_live_guard=False)


@api.post("/stores/{store_id}/archive", response_model=StoreOut)
def archive_store_endpoint(store_id: int, user: HQUser = Depends(require("stores.archive"))):
    """Retire a Store. Nothing is deleted - the row, its Devices, its sessions
    and its events all stay readable."""
    _require_store_in_scope(user, store_id)
    return _lifecycle_action(archive_store, store_id, user)


@api.post("/stores/{store_id}/restore", response_model=StoreOut)
def restore_store_endpoint(store_id: int, user: HQUser = Depends(require_super_admin)):
    """Bring an archived Store back to DISABLED, never straight to active."""
    return _lifecycle_action(restore_store, store_id, user, use_live_guard=False)


@api.delete("/stores/{store_id}")
def delete_store(store_id: int, db: Session = Depends(get_db), user: HQUser = Depends(require("stores.archive"))):
    """Delete means archive. A Store owns Devices, sessions, targets and events;
    removing the row would destroy the only record of what was announced where."""
    _require_store_in_scope(user, store_id)
    _lifecycle_action(archive_store, store_id, user)
    return {"ok": True, "archived": True}


@api.get("/stores/{store_id}/dependencies")
def read_store_dependencies(store_id: int,
                            user: HQUser = Depends(require("menu.stores.view"))):
    """What still refers to this Store, so the UI can show it before offering
    a delete that would be refused anyway."""
    _require_store_in_scope(user, store_id)
    summary = store_dependencies(engine, store_id=store_id)
    if not summary.exists:
        raise HTTPException(status_code=404, detail="Store not found")
    return {"counts": summary.counts, "unchecked": summary.unchecked,
            "total": summary.total, "deletable": summary.deletable,
            "explanation": summary.explain()}


@api.delete("/stores/{store_id}/permanently")
def hard_delete_store(store_id: int, confirm: str,
                      db: Session = Depends(get_db),
                      user: HQUser = Depends(require("stores.archive"))):
    """Remove a never-used Store for real. Refuses anything with history.

    Separate from DELETE /stores/{id}, which archives. Two verbs that do very
    different things must not be the same endpoint with a flag - somebody
    eventually passes the flag by accident.
    """
    _require_store_in_scope(user, store_id)
    try:
        removed = delete_store_if_unused(engine, store_id=store_id,
                                         typed_confirmation=confirm)
    except DeletionRefused as refusal:
        # 409, not 403: the caller has the permission, and the refusal is about
        # what the deletion would destroy.
        raise HTTPException(status_code=409, detail=str(refusal))
    _write_log(db, "warn",
               f"STORE_DELETED store_code={removed['store_code']} by={user.username}")
    return {"ok": True, "deleted": removed}


class StoreTombstoneRequest(BaseModel):
    confirm: str
    acknowledged: bool = False


@api.post("/stores/{store_id}/delete-permanently")
def tombstone_store(store_id: int, payload: StoreTombstoneRequest,
                    db: Session = Depends(get_db),
                    user: HQUser = Depends(require("stores.delete_permanently"))):
    """Permanently delete a Store. The row really goes, and the code is freed.

    This used to tombstone: the row stayed, marked deleted, so every history
    row referring to it stayed valid. It kept history readable and it kept the
    Store Code reserved for ever - an operator who permanently deleted TESTSTORE
    could never create TESTSTORE again, and store_deletion.py said so in its own
    docstring.

    Now the row is deleted and history is made independent of it first: each
    Broadcast Target, Receiver event and Device keeps a snapshot of the Store
    Code, and every historical pointer is nulled. NULL matters rather than
    being tidy - stores has no AUTOINCREMENT, so a dangling id would be handed
    to the next Store created and that Store would silently inherit somebody
    else's history.

    The old Store's Receiver identity is neutralised in the same transaction:
    credentials revoked, Devices retired and detached, and the legacy
    receiver_token dies with the row. A Receiver holding the old Store's
    credential cannot authenticate as the new Store that reuses its code.

    Refused while a broadcast is on air on that Store - deleting it would
    silence somebody else's announcement as a side effect.

    stores.delete_permanently defaults to SUPER ADMIN/OWNER only.
    """
    if not payload.acknowledged:
        raise HTTPException(
            status_code=400,
            detail="The 'this Store cannot be restored' acknowledgement is required.",
        )
    try:
        result = permanently_delete_store(
            engine, store_id=store_id, typed_confirmation=payload.confirm,
            actor_user_id=user.id, live_store_ids=_live_store_ids(),
        )
    except (StorePermanentDeletionRefused, StoreDeletionRefused) as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    _write_log(
        db, "warn",
        f"STORE_PERMANENTLY_DELETED store_id={result.store_id} "
        f"code={result.store_code} devices_detached={result.devices_detached} "
        f"credentials_revoked={result.credentials_revoked} "
        f"live_removed={result.live_rows_removed} "
        f"history_detached={result.history_rows_detached} "
        f"by={user.username}",
    )
    return {
        "ok": True,
        "store_id": result.store_id,
        "store_code": result.store_code,
        "store_name": result.store_name,
        "deleted_at": result.deleted_at,
        "row_deleted": True,
        "store_code_released": True,
        "devices_detached": result.devices_detached,
        "credentials_revoked": result.credentials_revoked,
        "live_removed": result.live_rows_removed,
        "history_detached": result.history_rows_detached,
    }


@api.get("/stores/{store_id}/deletion-events")
def read_store_deletion_events(store_id: int,
                               user: HQUser = Depends(require("menu.stores.view"))):
    """Audit trail for a tombstoned Store - who deleted it, when, and what it
    affected. Reachable on a deleted Store's history even though the Store
    itself is gone from every operational list."""
    return {"events": list_store_deletion_events(engine, store_id=store_id)}


@api.get("/users/{user_id}/dependencies")
def read_user_dependencies(user_id: int,
                           user: HQUser = Depends(require("menu.users.view"))):
    summary = user_dependencies(engine, user_id=user_id)
    if not summary.exists:
        raise HTTPException(status_code=404, detail="No such HQ User")
    return {"counts": summary.counts, "unchecked": summary.unchecked,
            "total": summary.total, "deletable": summary.deletable,
            "explanation": summary.explain()}


# ---- per-user permission overrides -----------------------------------------
#
# Gated on `users.permissions.manage` - the permission whose UI label is
# "Manage User Rights".
#
# These two routes used to require `require_super_admin`, a literal "is this
# account OWNER" test. That made the permission inert: an OWNER could grant
# Manage User Rights to an ADMIN, the ADMIN's effective permission set really
# did contain it, and the ADMIN still got 403 here - a granted right with no
# effect anywhere in the product.
#
# The original reasoning was sound about the risk and wrong about the remedy.
# The risk is that whoever edits rights can raise their own; the remedy is to
# forbid raising your own, not to forbid everyone but OWNER. So the role test
# is replaced by the capability, and the escalation guards it was standing in
# for are now explicit and enforced in `set_permission_overrides`:
#
#   * an OWNER target is never overridden        (OwnerOverrideRefused)
#   * nobody edits their own rights              (SelfRightsEditRefused)
#   * nobody grants what they do not hold        (GrantBeyondActorRefused)
#   * the role hierarchy decides who is a valid target (below)
#
# Together those are strictly stronger than the old check, because they also
# constrain an OWNER-granted ADMIN rather than assuming no such account exists.
def _require_may_manage_rights_of(actor: HQUser, target_role: Role) -> None:
    """Server-side target hierarchy for rights management.

    Reuses `rbac.may_manage_role` - the same matrix that already decides who
    may create, edit, disable or archive whom - rather than inventing a second
    hierarchy that could drift from it. ADMIN may therefore manage the rights
    of a BROADCASTER or VIEWER, and not of another ADMIN or an OWNER, which is
    exactly the existing policy for every other User Management action.
    """
    actor_role = parse_role(actor.role)
    if actor_role is None or not may_manage_role(actor_role, target_role):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to manage this account's rights.",
        )


@api.get("/users/{user_id}/permissions")
def read_user_permission_overrides(
    user_id: int,
    user: HQUser = Depends(require("users.permissions.manage")),
):
    existing = _user_or_404(user_id)
    role = parse_role(existing["role"])
    if role is None:
        raise HTTPException(status_code=400, detail="That account has no recognised role.")
    _require_may_manage_rights_of(user, role)
    return {
        "user_id": user_id,
        "role": role.value,
        "permissions": describe_user_permissions(engine, user_id=user_id, role=role),
    }


@api.put("/users/{user_id}/permissions")
def write_user_permission_overrides(
    user_id: int,
    payload: PermissionOverridesUpdate,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("users.permissions.manage")),
):
    existing = _user_or_404(user_id)
    role = parse_role(existing["role"])
    if role is None:
        raise HTTPException(status_code=400, detail="That account has no recognised role.")
    _require_may_manage_rights_of(user, role)
    try:
        audit_rows = set_permission_overrides(
            engine,
            actor=user,
            target_user_id=user_id,
            target_role=role,
            changes=[change.model_dump() for change in payload.changes],
        )
    except OwnerOverrideRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    # 403, not 400: these are authorisation refusals, not malformed input. The
    # request was well formed and this actor may not make it.
    except RightsEscalationRefused as refusal:
        raise HTTPException(status_code=403, detail=str(refusal))
    except UnknownPermissionCode as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    # The audit table already has the durable, queryable record; this log line
    # only carries the count and actor, never a permission-by-permission diff,
    # so scanning system_logs cannot substitute for querying the audit table.
    _write_log(
        db, "warn",
        f"user_permissions_changed target_id={user_id} changes={len(audit_rows)} by={user.username}",
    )
    return {
        "user_id": user_id,
        "role": role.value,
        "permissions": describe_user_permissions(engine, user_id=user_id, role=role),
    }


# ---- per-user Store/City/Zone scope -----------------------------------------
#
# Same reservation as the permission overrides above, and for the same
# reason: require_super_admin, not a permission check, so an override or a
# scope assignment can never grant an ADMIN a path to grant themselves more.
@api.get("/users/{user_id}/store-scope")
def read_user_store_scope(user_id: int, user: HQUser = Depends(require_super_admin)):
    _user_or_404(user_id)
    return {"user_id": user_id, "entries": list_user_scope(engine, user_id=user_id)}


@api.put("/users/{user_id}/store-scope")
def write_user_store_scope(
    user_id: int,
    payload: StoreScopeUpdate,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require_super_admin),
):
    _user_or_404(user_id)
    try:
        audit_rows = set_user_scope(
            engine, user_id=user_id,
            entries=[entry.model_dump() for entry in payload.entries],
            actor_id=user.id,
        )
    except InvalidScopeEntry as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    # The audit table has the durable per-entry record; this line only
    # carries the change count, never a scope-by-scope diff.
    _write_log(
        db, "warn",
        f"user_store_scope_changed target_id={user_id} changes={len(audit_rows)} "
        f"by={user.username}",
    )
    return {"user_id": user_id, "entries": list_user_scope(engine, user_id=user_id)}


class UserTombstoneRequest(BaseModel):
    confirm: str
    acknowledged: bool = False


@api.post("/users/{user_id}/delete-permanently")
def tombstone_user(user_id: int, payload: UserTombstoneRequest,
                   db: Session = Depends(get_db),
                   user: HQUser = Depends(require("users.delete_permanently"))):
    """Permanently delete an account. The row really goes.

    This used to tombstone: the hq_users row stayed, marked deleted, so that
    broadcast and audit references remained valid. It kept history readable
    and it kept the username reserved for ever - which meant an account an
    operator had permanently deleted still occupied the namespace, still
    appeared in User Management, and still offered Rights, Scope and Reset
    Password. That is a hidden account, not a deleted one.

    Now the row is deleted and history is made independent of it first: each
    broadcast keeps an immutable snapshot of who ran it, and every historical
    pointer is set to NULL. NULL matters rather than merely being tidy -
    hq_users has no AUTOINCREMENT, so a dangling id would be handed to the
    next account created and that person would silently inherit somebody
    else's broadcasts. See user_permanent_delete.py.

    The account's live security state - permission overrides and Store Scope -
    is deleted with it, so a later account reusing the username inherits
    nothing.

    Distinct from DELETE /users/{id}/permanently, which only ever removes an
    account nothing has ever referenced.
    """
    if not payload.acknowledged:
        raise HTTPException(
            status_code=400,
            detail="The 'this account cannot be restored' acknowledgement is required.",
        )
    existing = _user_or_404(user_id)
    try:
        result = permanently_delete_user(
            engine, user_id=user_id, typed_confirmation=payload.confirm,
            actor_user_id=user.id,
        )
    except (PermanentDeletionRefused, UserDeletionRefused) as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    _write_log(
        db, "warn",
        f"USER_PERMANENTLY_DELETED user_id={result.user_id} "
        f"username={result.username} role={result.role} "
        f"security_removed={result.security_rows_removed} "
        f"history_detached={result.history_rows_detached} by={user.username}",
    )
    return {
        "ok": True,
        "user_id": result.user_id,
        "username": result.username,
        "role": result.role,
        "deleted_at": result.deleted_at,
        "row_deleted": True,
        "username_released": True,
        "security_removed": result.security_rows_removed,
        "history_detached": result.history_rows_detached,
    }


@api.get("/users/{user_id}/deletion-events")
def read_user_deletion_events(user_id: int,
                              user: HQUser = Depends(require("menu.users.view"))):
    """Audit trail for a tombstoned account - who deleted it, when, and how
    much history still names it. Never a password or a hash."""
    return {"events": list_user_deletion_events(engine, user_id=user_id)}


@api.delete("/users/{user_id}/permanently")
def hard_delete_user(user_id: int, confirm: str,
                     db: Session = Depends(get_db),
                     user: HQUser = Depends(require("users.disable"))):
    """Remove an account that never did anything. Archive is still the default.

    The role check inside delete_user_if_unused refuses an OWNER outright, and
    _require_may_manage stops an ADMIN reaching one in the first place.
    """
    existing = _user_or_404(user_id)
    _require_may_manage(user, existing["role"])
    try:
        removed = delete_user_if_unused(engine, user_id=user_id,
                                        typed_confirmation=confirm, actor_id=user.id)
    except DeletionRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    _write_log(db, "warn",
               f"USER_DELETED username={removed['username']} by={user.username}")
    return {"ok": True, "deleted": removed}


@api.post("/stores/{store_id}/regenerate-token", response_model=StoreOut)
def regenerate_token(store_id: int, db: Session = Depends(get_db), user: HQUser = Depends(require("stores.update"))):
    s = db.query(Store).filter(Store.id == store_id).first()
    if not s or s.lifecycle_state == "deleted":
        raise HTTPException(status_code=404, detail="Store not found")
    _require_store_in_scope(user, store_id)
    s.receiver_token = uuid.uuid4().hex
    db.commit()
    db.refresh(s)
    _write_log(db, "info", f"Regenerated token for store {s.store_code}")
    return s


@api.get("/stores/meta/regions-cities", response_model=StoresMetaOut)
def stores_meta(db: Session = Depends(get_db), user: HQUser = Depends(require("menu.stores.view"))):
    not_deleted = (Store.lifecycle_state.is_(None)) | (Store.lifecycle_state != "deleted")
    regions = [r[0] for r in db.query(Store.region).filter(not_deleted)
              .distinct().order_by(Store.region).all() if r[0]]
    cities = [c[0] for c in db.query(Store.city).filter(not_deleted)
             .distinct().order_by(Store.city).all() if c[0]]
    return StoresMetaOut(regions=regions, cities=cities)


# ================ BROADCAST ================
@api.get("/broadcast/target-stores", response_model=BroadcastTargetsOut)
def list_broadcast_target_stores(
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.store_delivery")),
):
    """The Stores this account may point a broadcast at.

    WHY THIS EXISTS AS ITS OWN ENDPOINT

    Broadcast Console used to build its target list from ``GET /api/stores``,
    which is guarded by ``menu.stores.view`` - "View Store Management". That
    made one administrative permission decide an operational capability: an
    operator with Start Broadcast but without Store Management opened the
    Console and found an empty table, because the Store fetch behind it had
    already returned 403. Nothing in the Console said so.

    The two are genuinely different questions. Managing Stores is about the
    records; targeting Stores is about the estate. Someone can reasonably be
    trusted to broadcast to the shops in their region without also being
    trusted to rename, archive or delete them.

    The fix is not to widen ``menu.stores.view``, and not to drop the guard on
    ``/api/stores`` - both would hand the full administrative Store
    representation to every broadcaster. It is this: a separate catalog, gated
    on the permission that already governs the Console, returning only the
    fields the Console draws.

    WHAT IT DOES NOT CHANGE

    Store Scope. It is applied here exactly as ``GET /api/stores`` applies it,
    through the same ``resolve_store_scope`` - including the distinction
    between ``None`` (unrestricted) and an empty set (scoped to nothing, which
    stays nothing). A scoped broadcaster sees no more here than they did
    before, and an out-of-scope Store is absent from the response no matter
    how the request is crafted, because the filter is in the query rather than
    in the client.

    Targeting eligibility also stays as it was: active Stores only, archived
    excluded, permanently deleted excluded unconditionally. This endpoint
    decides who may ask, not which Stores are eligible.
    """
    query = db.query(Store).filter(Store.is_active.is_(True))
    # Archived is retired-but-recoverable and has never been targetable;
    # deleted is unconditional. Both conditions are written the same way as in
    # list_stores so the two lists cannot drift apart.
    query = query.filter(
        (Store.lifecycle_state.is_(None)) | (Store.lifecycle_state != "archived")
    )
    query = query.filter(
        (Store.lifecycle_state.is_(None)) | (Store.lifecycle_state != "deleted")
    )
    scope = resolve_store_scope(engine, user)
    if scope is not None:
        query = query.filter(Store.id.in_(scope) if scope else Store.id.in_([-1]))

    stores = query.order_by(Store.store_code).all()
    # Same live-status reflection the Console already relied on: the stored
    # column is only authoritative for playing/error.
    online_ids = manager.online_store_ids()
    for store in stores:
        if store.status not in ("playing", "error"):
            store.status = "online" if store.id in online_ids else "offline"

    return BroadcastTargetsOut(
        stores=[BroadcastTargetStoreOut.model_validate(s) for s in stores],
        regions=sorted({s.region for s in stores if s.region}),
        cities=sorted({s.city for s in stores if s.city}),
    )


def _require_physical_delivery(user: HQUser) -> None:
    """Refuse every physical Store target unless this account may deliver to one.

    Checked here rather than only on the routes because this is the single
    function through which every physical target is resolved. A guard on the
    endpoints would have to be repeated, and the one that was forgotten would be
    the one that mattered.

    Deliberately independent of Store Scope. Scope answers WHICH Stores and
    treats blank as unrestricted, so an account with no Scope and no physical
    permission must still be refused - the absence of a restriction is not a
    grant.
    """
    if not has_permission_code(engine, user, "broadcast.store_delivery"):
        raise HTTPException(status_code=403, detail=RBAC_REFUSED)


ONLY_WITH_LINK = "only_with_link"


def _resolve_targets(db: Session, payload: SessionCreate, user: HQUser) -> List[Store]:
    # Link-only is the one mode that asks for no physical delivery, so it is
    # answered before the physical-delivery check rather than being refused by
    # it. Zero Stores is the CORRECT result here, not an empty selection: this
    # broadcast has a web audience and no shop.
    if payload.target_mode == ONLY_WITH_LINK:
        # Contradictory physical intent is REFUSED, not quietly dropped.
        #
        # A request that says "no physical destination" while naming Stores is
        # two different instructions, and silently honouring the first would
        # hide the second from whoever sent it. Refusing says which one the
        # server acted on.
        if payload.store_ids or payload.region or payload.city:
            raise HTTPException(
                status_code=400,
                detail="Only With Link reaches no Stores, so it cannot name any.")
        return []

    _require_physical_delivery(user)
    q = db.query(Store).filter(Store.is_active.is_(True))
    mode = payload.target_mode
    if mode == "all":
        targets = q.all()
    elif mode == "selected":
        if not payload.store_ids:
            raise HTTPException(status_code=400, detail="store_ids required for target_mode=selected")
        targets = q.filter(Store.id.in_(payload.store_ids)).all()
    elif mode == "region":
        # Several zones, comma-separated. A campaign is almost never one zone,
        # and "the North and the South" used to mean either two broadcasts -
        # two microphones, two sets of leases, two things to remember to stop -
        # or picking every shop by hand. One value is a list of one, so an
        # existing single-zone request is unchanged.
        zones = value_list(payload.region)
        if not zones:
            raise HTTPException(status_code=400, detail="region required")
        targets = q.filter(Store.region.in_(zones)).all()
    elif mode == "city":
        cities = value_list(payload.city)
        if not cities:
            raise HTTPException(status_code=400, detail="city required")
        targets = q.filter(Store.city.in_(cities)).all()
    elif mode == "online_only":
        # CONNECTIVITY, not the Store's business classification.
        #
        # This used to filter on Store.is_online_store, which is the column
        # Store Management edits with a checkbox labelled Online / Physical -
        # an e-commerce flag that defaults to False and has nothing to do with
        # whether a Receiver is reachable. So "Online Stores Only" targeted the
        # e-commerce stores and excluded every physical shop whose Receiver was
        # connected, which read as zero targets on a console showing BP ONLINE.
        #
        # The authority is the live Receiver connection inventory, the same one
        # the target inventory endpoint already uses to paint each row's status.
        connected = manager.online_store_ids()
        targets = [store for store in q.all() if store.id in connected]
    else:
        raise HTTPException(status_code=400, detail="Invalid target_mode")

    # Per-user Store/City/Zone scope. None means unrestricted. For an
    # explicit "selected" list, a Store outside scope is refused rather than
    # silently dropped - the caller asked for it by id, so a silent narrowing
    # would broadcast to fewer Stores than the confirmation dialog showed.
    # Every other mode narrows silently, because narrowing IS what scope means
    # for "all my Stores"/"my Zone"/"my city".
    scope = resolve_store_scope(engine, user)
    if scope is not None:
        if mode == "selected":
            out_of_scope = [t.id for t in targets if t.id not in scope]
            if out_of_scope:
                raise HTTPException(status_code=403, detail=RBAC_REFUSED)
        else:
            targets = [t for t in targets if t.id in scope]
    return targets


@api.post("/broadcast/sessions", response_model=SessionOut, status_code=201)
def create_session(payload: SessionCreate, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.START_BROADCAST))):
    targets = _resolve_targets(db, payload, user)
    if not targets and payload.target_mode == "online_only":
        # Its own answer: "no Stores match" would send an operator hunting
        # through filters for a selection they never made.
        raise HTTPException(status_code=409,
                            detail="No authorized Stores are currently online.")
    if not targets and payload.target_mode != ONLY_WITH_LINK:
        raise HTTPException(status_code=400, detail="No stores match the selection criteria")
    online_ids = manager.online_store_ids()
    online_count = sum(1 for t in targets if t.id in online_ids)
    session = BroadcastSession(
        campaign_name=payload.campaign_name,
        started_by=user.id,
        status="pending",
        target_mode=payload.target_mode,
        selected_store_count=len(targets),
        online_store_count=online_count,
        offline_store_count=len(targets) - online_count,
        notes=payload.notes,
    )
    db.add(session)
    db.flush()
    for t in targets:
        db.add(BroadcastTarget(session_id=session.id, store_id=t.id, play_status="pending"))
    db.commit()
    db.refresh(session)

    # One web room per Broadcast, created with the session rather than at start
    # so the operator can copy and share the link before going live. Listeners
    # can be admitted early; they simply hear nothing until the Broadcast is
    # live, because the relay has no audio to give them yet.
    #
    # The generated password is returned ONCE, here. There is no column it
    # could be read back from, so the console keeps it for this page lifetime
    # and offers rotation afterwards.
    room, password = web_rooms.create_room(engine, session_id=session.id)
    _ROOM_PASSWORD_ONCE[session.id] = password
    _write_log(db, "info", f"Session created #{session.id} '{session.campaign_name}' targets={len(targets)}")
    return session


def _store_busy_refusal(db: Session, busy_store_ids, scope) -> HTTPException:
    """The one STORE_BUSY answer, built in one place.

    WHAT IT DELIBERATELY OMITS: the owning user, their display name, the other
    campaign's name, and the other session's id. A Broadcaster learns which of
    THEIR OWN selected Stores is unavailable and nothing more - who is using it
    is somebody else's business, and an operator who can enumerate other
    people's campaigns by trying to start broadcasts has been given a directory
    nobody meant to publish.

    Store codes rather than ids, because "BP" is what the operator selected and
    what they must deselect. Ids are included too for the UI to match rows
    without re-resolving codes.
    """
    visible = sorted(s for s in busy_store_ids if scope is None or s in scope)
    codes = sorted(row.store_code for row in
                   db.query(Store).filter(Store.id.in_(visible)).all()) \
        if visible else []
    return HTTPException(
        status_code=409,
        detail={
            "code": "STORE_BUSY",
            "message": (
                f"{', '.join(codes)} currently in use by another broadcast. "
                "Remove the busy Store and try again."
                if codes else
                "One or more selected Stores are currently in use by another "
                "broadcast."
            ),
            "busy_store_ids": visible,
            "busy_store_codes": codes,
        },
    )


def _refuse_busy_stores(db: Session, user: HQUser, target_store_ids) -> None:
    """Read-only conflict check. Claims nothing, so it can run before the
    single-broadcast gate without leaving a lease behind on any other
    refusal."""
    if not target_store_ids:
        return
    scope = resolve_store_scope(engine, user)
    busy = active_busy_store_ids(engine) & set(target_store_ids)
    if busy:
        raise _store_busy_refusal(db, busy, scope)


def _revalidate_online_targets(db: Session, session: BroadcastSession,
                               user: HQUser, connected: set) -> list:
    """Re-resolve an Online Stores Only session against live connectivity.

    Rewrites this session's target rows to exactly the Stores that are
    authorised AND connected right now, and refuses rather than starting a
    physical broadcast with nothing on the other end.
    """
    _require_physical_delivery(user)
    scope = resolve_store_scope(engine, user)

    eligible = db.query(Store).filter(Store.is_active.is_(True)).all()
    resolved = [store for store in eligible
                if store.id in connected
                and (scope is None or store.id in scope)]

    if not resolved:
        # Truthful refusal. Silently falling back to every Store, or to the
        # offline ones, or to a link-only broadcast, would each put a campaign
        # somewhere the operator did not ask for.
        raise HTTPException(
            status_code=409,
            detail="No authorized Stores are currently online.")

    # Replace the preview rows with what was just resolved, so the target rows,
    # the leases and the PREPARE commands below all describe the same set.
    keep = {store.id for store in resolved}
    for row in db.query(BroadcastTarget).filter(
            BroadcastTarget.session_id == session.id).all():
        if row.store_id not in keep:
            db.delete(row)
    existing = {row.store_id for row in db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == session.id).all()}
    for store_id in keep - existing:
        db.add(BroadcastTarget(session_id=session.id, store_id=store_id,
                               play_status="pending"))
    session.selected_store_count = len(keep)
    # Committed, not merely flushed: the reservation and lease work below opens
    # its own connection on the same SQLite file, and pending writes here would
    # hold the write lock against it.
    db.commit()
    return db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == session.id).all()


@api.post("/broadcast/sessions/{sid}/start", response_model=SessionOut)
async def start_session(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.START_BROADCAST))):
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "pending":
        raise HTTPException(status_code=400, detail=f"Session cannot start (status={session.status})")

    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == sid).all()
    online_ids = manager.online_store_ids()

    # ONLINE STORES ONLY is resolved AGAIN, here, against connectivity as it is
    # at this instant.
    #
    # The set stored at session creation is a preview: a Store may have dropped
    # or reconnected in the seconds since, and the browser's copy is older
    # still. Re-resolving means a stale page cannot start a broadcast to a
    # Receiver that is no longer there, and a Store that came back is not
    # excluded for having been offline a moment ago.
    #
    # What this deliberately does NOT do is keep tracking. Once Start succeeds
    # the set is frozen for the life of the broadcast: a Store connecting later
    # is not added, because joining an announcement half way through is a
    # decision an operator makes, not something a heartbeat does. Adding and
    # removing Stores mid-broadcast is its own milestone.
    if session.target_mode == "online_only":
        targets = _revalidate_online_targets(db, session, user, online_ids)

    target_store_ids = {t.store_id for t in targets}

    # A Store conflict is reported BEFORE the one-broadcast-at-a-time gate
    # below, because it is the more specific and more actionable answer: "BP is
    # in use" tells an operator what to change, where "a broadcast is already
    # live" tells them only to wait. This is a read-only look - the actual
    # claim happens after the gate, so a start refused for any other reason
    # cannot leave a lease behind.
    _refuse_busy_stores(db, user, target_store_ids)

    # The old single-broadcast gate stood here. It is gone: several sessions
    # may now be live at once, each owning its own queues and microphone
    # socket. What prevents two broadcasts reaching one Store is the
    # broadcast_store_leases unique index claimed below - a database
    # invariant rather than a process-local flag, so it survives a restart and
    # cannot be raced.

    # Claim every target Store, or none of them, BEFORE anything is marked
    # live or any Receiver is told to play. All-or-nothing is the operationally
    # important half: starting on "the Stores that happened to be free" would
    # put a campaign on air half-targeted, and the operator would have no
    # reason to suspect it - they asked for all of them.
    #
    # The refusal names only Stores this account can already see. Who holds a
    # busy Store, and for what campaign, is deliberately not in the answer.
    scope = resolve_store_scope(engine, user)
    try:
        reserve_stores_for_session(engine, session_id=session.id,
                                   store_ids=target_store_ids, scope=scope)
    except StoreNotInScopeError:
        raise HTTPException(
            status_code=403,
            detail="This broadcast targets Stores that are not available to "
                   "this account.",
        )
    except StoreBusyError as busy:
        # Lost the race between the read-only pre-check above and this claim.
        # Same answer, same shape - the caller cannot tell which layer refused,
        # and does not need to.
        raise _store_busy_refusal(db, busy.busy_store_ids, scope)

    session.status = "live"
    session.started_at = datetime.now(timezone.utc)
    session.online_store_count = sum(1 for sid_ in target_store_ids if sid_ in online_ids)
    session.offline_store_count = len(target_store_ids) - session.online_store_count
    now = datetime.now(timezone.utc)
    for t in targets:
        t.command_sent_at = now
        if t.store_id in online_ids:
            t.play_status = "pending"
            t.started_playing_at = None
            t.error_message = None
        else:
            t.play_status = "failed"
            t.error_message = "Receiver offline at broadcast start"
    db.commit()
    db.refresh(session)

    # The announcements in these Stores step aside. DUCKED, not PAUSED: see
    # announcements.py - a broadcast ending must resume only what the broadcast
    # itself moved, and a Store somebody deliberately silenced must stay
    # silent.
    #
    # Safe to duck the whole target list because a Store can only be in one
    # live broadcast at a time - the broadcast_store_leases unique index
    # claimed above is what guarantees it - so nothing here is standing aside
    # for somebody else's session.
    try:
        for ducked_store in announcement_service.duck_stores(engine, target_store_ids):
            await manager.send_to_receiver(
                ducked_store,
                announcement_protocol.pause_command(reason="broadcast"))
    except Exception:  # noqa: BLE001
        # A broadcast must go live even if the announcement bookkeeping fails.
        # The worst case is a jingle playing under a broadcast, which is
        # audible and fixable; refusing to broadcast is neither.
        logger.exception("Could not duck announcements for session %s", session.id)

    await manager.start_live_session(session.id, target_store_ids,
                                     owner_user_id=session.started_by)
    # Live output-volume state for this session's Stores, at the product
    # defaults (100%, unmuted). In memory only - see store_audio_control.
    store_audio_registry.start_session(
        session_id=session.id,
        owner_user_id=session.started_by,
        store_ids=target_store_ids,
    )
    # Send PREPARE then PLAY to online targets. PREPARE carries the negotiated
    # audio format so the Receiver can run its real FFmpeg/codec checks before
    # reporting READY. Audio is only meaningful after that acknowledgement.
    for sid_ in target_store_ids:
        if sid_ in online_ids:
            await manager.send_to_receiver(
                sid_,
                build_prepare_message(session_id=session.id, store_id=sid_),
            )
            await manager.send_to_receiver(sid_, {"type": "play", "session_id": session.id, "campaign": session.campaign_name})
    # One recording per broadcast, begun as the broadcast begins. It records
    # the bytes arriving on the operator's microphone socket - which is HQ's
    # OUTGOING audio, after the accepted gain and mute path - and can never
    # contain Store ambient sound, because none of that travels on that socket.
    _start_recording(session.id)
    await manager.notify_dashboards({"type": "session_started", "session_id": session.id})
    _write_log(db, "info", f"Session #{session.id} started; {session.online_store_count}/{session.selected_store_count} online")
    return session


async def _close_listener_socket(socket, payload: dict) -> None:
    """Tell one listener why, then close. Never raises: cleanup runs regardless."""
    try:
        await socket.send_text(json.dumps(payload))
    except Exception:
        pass
    try:
        await socket.close(code=1000)
    except Exception:
        # A socket that is already gone is the ordinary case here.
        pass


async def _end_web_room(session_id: int, *, reason: str) -> None:
    """End this Broadcast's room and disconnect its listeners. Idempotent."""
    try:
        web_rooms.end_room(engine, session_id=session_id)
    except Exception as failure:
        logger.warning("web room for session %s could not be ended: %s",
                       session_id, type(failure).__name__)
    room = None
    try:
        room = web_rooms.get_room_for_session(engine, session_id=session_id)
    except Exception:
        room = None
    if room is None:
        return
    for socket in web_participants.drop_room(room.id):
        await _close_listener_socket(
            socket, {"type": "room_ended", "reason": reason})


async def _end_session(db: Session, session: BroadcastSession, final_status: str, reason: str = "", broadcast_to_all: bool = False):
    # Every way a broadcast can end arrives here - normal stop, emergency stop,
    # a dropped microphone, server cleanup - which is exactly why finalization
    # lives here rather than in the stop route. Idempotent, and it never raises.
    await _finish_recording(session.id)
    # The room ends with the Broadcast, wherever the end came from - normal
    # stop, emergency stop, a dropped microphone, cleanup. Marking it ENDED
    # also clears every listener session token, so no admitted listener can
    # reconnect to a Broadcast that is over.
    await _end_web_room(session.id, reason=reason or "broadcast_ended")
    now = datetime.now(timezone.utc)
    session.status = final_status
    session.ended_at = now
    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == session.id).all()
    for t in targets:
        if t.play_status == "playing":
            t.play_status = "stopped"
            t.stopped_at = now
    db.commit()
    # STOP goes to THIS session's own targets, read from its own rows. It used
    # to be read from the singleton live_target_store_ids, which with
    # concurrent sessions would mean stopping whichever Stores the most recent
    # broadcast happened to list - silencing somebody else's announcement.
    stop_ids = {t.store_id for t in targets}
    # The announcements come back - in THIS session's Stores only.
    #
    # Deliberately computed before the emergency widening below. An emergency
    # stop tells every connected Receiver to stop whatever it is doing, and
    # resuming announcements across that wider set would start a jingle in a
    # shop that is standing aside for a DIFFERENT broadcast still on air.
    # Only what this broadcast ducked is restored, and only if it is still
    # DUCKED - a Store an operator paused during the broadcast stays paused.
    try:
        for resumed_store in announcement_service.unduck_stores(engine, stop_ids):
            await _dispatch_announcement(
                resumed_store,
                announcement_service.get_playback(engine, store_id=resumed_store))
    except Exception:  # noqa: BLE001
        logger.exception("Could not resume announcements after session %s",
                         session.id)
    if broadcast_to_all:
        # Emergency safety net: tell every connected Receiver to stop, whatever
        # it was doing. Deliberately wider than this session.
        stop_ids |= set(manager.receivers.keys())
    for sid_ in stop_ids:
        await manager.send_to_receiver(sid_, {"type": "stop", "session_id": session.id, "reason": reason})
    # Closes only this session's queues and pump tasks, and closes only its own
    # broadcaster socket - every other live session keeps its operator and its
    # audio.
    await manager.stop_live_session(session.id)
    # Forget this session's output-volume state. The Receiver restores the
    # Store's pre-broadcast output itself on `stop`, so this is only HQ's
    # bookkeeping - but leaving it behind would let a later request name a
    # finished session and be answered.
    store_audio_registry.end_session(session.id)
    # Release ONLY this session's Stores. Scoped by session id rather than by
    # store id: releasing by Store could free one another session is
    # legitimately broadcasting to, and the symptom would be a second campaign
    # arriving on speakers that were already busy.
    release_session_leases(engine, session_id=session.id)
    await manager.notify_dashboards({"type": "session_ended", "session_id": session.id, "status": final_status})


def _audio_control_state_rows(sid: int) -> List[StoreAudioStateOut]:
    """Control state joined with what HQ knows about each Receiver.

    ``supported`` and ``online`` are computed here rather than stored, because
    both are properties of the live connection: a Store that reconnects with an
    older Receiver must stop being controllable immediately, and control state
    that remembered "supported" from an earlier connection would keep offering
    a control that silently does nothing.
    """
    rows: List[StoreAudioStateOut] = []
    for entry in store_audio_registry.describe(sid):
        store_id = entry["store_id"]
        snapshot = manager.get_receiver_snapshot(store_id)
        capabilities = getattr(snapshot, "capabilities", None) if snapshot else None
        # The Receiver's own answer, never inferred here. A Store that has not
        # reported capabilities at all - an older build, or one that has not
        # yet sent receiver_ready this session - stays "unknown", which the
        # Console renders as genuinely unsupported rather than guessing at a
        # friendlier reason.
        status = getattr(capabilities, "output_control_status", "unknown")             if capabilities else "unknown"
        rows.append(StoreAudioStateOut(
            **entry,
            online=manager.is_receiver_online(store_id),
            supported=bool(capabilities and capabilities.output_volume),
            control_status=status,
        ))
    return rows


@api.get("/broadcast/sessions/{sid}/audio-control",
         response_model=StoreAudioControlOut)
def read_store_audio_control(
    sid: int,
    user: HQUser = Depends(require("store_audio.control")),
):
    """Current per-Store output state for a broadcast you own.

    Reading needs no permission of its own beyond the one that allows
    controlling output at all: this returns nothing about a session the caller
    does not own, and ownership is the gate. A separate ``store_audio.view``
    would be a second code that could only ever say yes wherever this one
    already does.
    """
    _require_audio_control_owner(sid, user)
    return StoreAudioControlOut(session_id=sid, stores=_audio_control_state_rows(sid))


def _require_audio_control_owner(sid: int, user: HQUser) -> None:
    """Only the operator running a broadcast may steer its output.

    Deliberately NOT satisfied by broadcast.stop_any or broadcast.active_view.
    A supervisor entitled to END somebody's broadcast is not thereby entitled
    to sit inside it changing how loud individual shops are - that is an
    invisible, continuous intervention rather than a single accountable act,
    and the operator on the other end would have no way to tell it was
    happening. Ending a broadcast is loud; quietly remixing one is not.
    """
    try:
        owner_id = store_audio_registry.session_owner(sid)
    except UnknownSessionError as refusal:
        # 409 rather than 404: the session may well exist in history. What is
        # gone is the LIVE state, and "no longer active" is the honest reason.
        raise HTTPException(status_code=409, detail=str(refusal))
    if owner_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="You can only change output volume for your own broadcast.",
        )


@api.post("/broadcast/sessions/{sid}/audio-control",
          response_model=StoreAudioControlOut)
async def set_store_audio_control(
    sid: int,
    payload: StoreAudioControlUpdate,
    user: HQUser = Depends(require("store_audio.control")),
):
    """Set one Store's SpeakLink output level for the rest of this broadcast.

    Also requires physical delivery. Steering a Store's loudspeaker is physical
    delivery by any reasonable reading, so an account that may not target a
    Store must not be able to reach one through its volume either.

    This controls the SpeakLink audio OUTPUT on the Store PC. The amplifier's
    physical volume control is separate, and nothing here can observe or change
    it - a Store reporting "applied 60" means its software output is at 60% of
    the decoded signal, not that the room is at 60% of anything.

    Returns immediately after handing the command to the Receiver, with the
    Store still marked pending. The applied value arrives later on the
    Receiver's own acknowledgement; a 200 here means "sent", never "applied".
    """
    _require_physical_delivery(user)
    _require_audio_control_owner(sid, user)

    # Store Scope, enforced server-side and before anything is sent. A scoped
    # operator can be broadcasting to a Store through a target mode that
    # selected it - scope governs which Stores they may act on individually,
    # and turning the volume down in a shop is exactly such an act.
    scope = resolve_store_scope(engine, user)
    if scope is not None and payload.store_id not in scope:
        raise HTTPException(
            status_code=403,
            detail="That Store is not in your Store Scope.",
        )

    try:
        state = store_audio_registry.request(
            session_id=sid,
            store_id=payload.store_id,
            volume_percent=payload.volume_percent,
            muted=payload.muted,
        )
    except StoreNotInSessionError as refusal:
        raise HTTPException(status_code=404, detail=str(refusal))
    except UnknownSessionError as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    except StoreAudioControlError as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    # Sent outside any lock, and only to a Receiver that said it understands
    # this command. An older Receiver is left alone entirely rather than sent
    # a message it would have to ignore.
    snapshot = manager.get_receiver_snapshot(payload.store_id)
    capabilities = getattr(snapshot, "capabilities", None) if snapshot else None
    if capabilities and capabilities.output_volume:
        await manager.send_to_receiver(
            payload.store_id,
            build_set_audio_control_message(
                session_id=sid,
                command_id=state.last_command_id,
                volume_percent=state.requested_volume_percent,
                muted=state.requested_muted,
            ),
        )

    return StoreAudioControlOut(session_id=sid, stores=_audio_control_state_rows(sid))



# ---------------------------------------------------------------------------
# Master Volume - the estate's mixers, independent of any broadcast
# ---------------------------------------------------------------------------
#: One recording writer per LIVE broadcast. Runtime only - the metadata row is
#: the durable record, and a writer is a file handle plus a queue.
_RECORDING_WRITERS: dict[int, "broadcast_recording.RecordingWriter"] = {}


def _start_recording(session_id: int) -> None:
    """Begin recording one broadcast. Never raises into the broadcast path.

    A recording that cannot start must not stop an announcement going out, so
    every failure here is recorded against the row and swallowed.
    """
    try:
        directory = broadcast_recording.recordings_directory()
        writer = broadcast_recording.RecordingWriter(
            session_id=session_id, directory=directory)
        broadcast_recording.start_record(
            engine, session_id=session_id, file_name=writer.file_name)
        writer.start()
        if writer.failed:
            broadcast_recording.finish_record(
                engine, session_id=session_id,
                status=broadcast_recording.STATUS_FAILED, error=writer.error)
            return
        _RECORDING_WRITERS[session_id] = writer
    except Exception as failure:
        logger.warning("Recording could not start for session %s: %s",
                       session_id, failure)


async def _finish_recording(session_id: int) -> None:
    """Finalize and publish. Called from EVERY path that ends a broadcast.

    Normal stop, emergency stop, a dropped microphone and server cleanup all
    arrive here, which is why it is idempotent and never raises.
    """
    writer = _RECORDING_WRITERS.pop(session_id, None)
    if writer is None:
        return
    try:
        status = await writer.close()
        probed = {}
        if status in (broadcast_recording.STATUS_AVAILABLE,
                      broadcast_recording.STATUS_PARTIAL):
            probed = broadcast_recording.probe_recording(writer.final_path)
            # ffprobe present and refusing the file is real evidence it cannot
            # be played. ffprobe ABSENT is evidence of nothing, so the file is
            # kept rather than condemned by a missing tool.
            if probed.get("error") or probed.get("has_audio") is False:
                status = broadcast_recording.STATUS_FAILED
        size = None
        if writer.final_path.exists():
            size = writer.final_path.stat().st_size
        broadcast_recording.finish_record(
            engine, session_id=session_id, status=status,
            container=probed.get("container"), codec=probed.get("codec"),
            byte_size=size, duration_seconds=probed.get("duration_seconds"),
            chunks_written=writer.chunks_written,
            chunks_dropped=writer.chunks_dropped,
            error=writer.error or (
                "some audio was dropped while writing to disk"
                if writer.chunks_dropped else None))
    except Exception as failure:
        logger.warning("Recording could not be finalized for session %s: %s",
                       session_id, failure)
        broadcast_recording.finish_record(
            engine, session_id=session_id,
            status=broadcast_recording.STATUS_FAILED, error=str(failure))


@api.post("/broadcast/sessions/{sid}/stop", response_model=SessionOut)
async def stop_session(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.STOP_BROADCAST))):
    """Stop YOUR OWN broadcast. Never anybody else's.

    Ownership is checked for EVERY role, including OWNER. Until now this route
    checked only that the caller held STOP_BROADCAST, so with one global
    broadcast it was harmless - there was only one thing to stop, and usually
    your own. With concurrent sessions the same code is a cross-user kill: any
    holder of STOP_BROADCAST could silence another operator mid-announcement
    by passing their session id, which is a small guessable integer.

    Stopping somebody else's broadcast is a real operational need, but it is a
    deliberate, audited, separately-permissioned act - Emergency Stop - not a
    side effect of knowing a number.

    The refusal is 404, identical to an unknown session. A 403 would confirm
    that a session with that id exists and belongs to somebody else, which
    turns this route into an oracle for enumerating other people's broadcasts.
    """
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session or session.started_by != user.id:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "live":
        raise HTTPException(status_code=400, detail=f"Session not live (status={session.status})")
    await _end_session(db, session, "ended", reason="normal_stop")
    db.refresh(session)
    _write_log(db, "info", f"Session #{session.id} stopped by {user.username}")
    return session


@api.get("/broadcast/audio-metrics")
def read_audio_metrics(user: HQUser = Depends(require(Permission.VIEW_STATUS))):
    """Per-Store bounded-queue counters, so somebody can actually read them.

    ``WSManager.audio_metrics()`` has existed since the bounded queues were
    built and was reachable from nowhere - no route, no CLI, no log line. The
    numbers that say "Store 12 is nearly dropping audio" were computed on every
    broadcast and discarded. A metric nobody can read is a metric that does not
    operationally exist.

    ``max_depth`` is the one worth watching: ``depth`` is sampled, so a Store
    that filled its queue and drained a moment earlier reads as zero, which is
    indistinguishable from a Store that queued nothing at all.

    VIEW_STATUS rather than MANAGE_*: this is operational health an on-call
    person needs at 7am, and it carries integers only. No payload, no Store
    token, no Device credential, no connection id - and a test asserts that
    rather than trusting this docstring.
    """
    per_session = manager.audio_metrics()
    rows = []
    for session_id, per_store in sorted(per_session.items()):
        for _store_id, metrics in sorted(per_store.items()):
            # session_id is carried on every row: with concurrent broadcasts,
            # "store 12 is dropping audio" is not answerable without knowing
            # which broadcast it was dropping.
            rows.append({"session_id": session_id, **dict(metrics)})
    return {
        "capacity": DEFAULT_STORE_QUEUE_CAPACITY,
        "session_count": len(per_session),
        "store_count": len(rows),
        "stores": rows,
    }


@api.post("/broadcast/emergency-stop")
async def emergency_stop(db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.EMERGENCY_STOP))):
    """EMERGENCY STOP ALL - every live broadcast, whoever owns it.

    Not "stop the current session" and not "stop mine". This is the one
    operation that reaches across owners, which is why it has its own
    permission rather than travelling with ordinary broadcast rights, and why
    ordinary Stop remains own-session-only for every role including OWNER.

    The session ids are SNAPSHOTTED before the loop. Ending a session mutates
    the registry it is read from, so iterating it live would skip sessions -
    the exact failure mode where "all stopped" is reported and one broadcast
    is still on air.

    Each session is ended through the same _end_session used by an ordinary
    stop, so each gets STOP sent to ITS OWN targets carrying ITS OWN
    session_id, its own queues closed, its own microphone socket closed and
    its own leases released. There is no global target set anywhere in this
    path.

    A failure on one session must not abandon the others. Each is attempted
    independently and failures are collected, because leaving four broadcasts
    live because the fifth misbehaved is worse than the original emergency.
    """
    snapshot = list(manager.broadcasts.active_session_ids())
    stopped: list[int] = []
    failed: list[int] = []
    silenced_stores: list[int] = []
    for session_id in snapshot:
        try:
            session = db.query(BroadcastSession).filter(
                BroadcastSession.id == session_id).first()
            if session and session.status == "live":
                await _end_session(db, session, "emergency_stopped",
                                   reason="emergency", broadcast_to_all=True)
                stopped.append(session.id)
        except Exception:
            # Bounded: the id, never the exception text, which could carry a
            # path or a connection detail into an operator-facing summary.
            db.rollback()
            failed.append(session_id)
            logger.exception("Emergency Stop could not end session %s", session_id)

    # ...and every recorded announcement, in every shop.
    #
    # This button is pressed when something must come out of the speakers NOW.
    # A version that stopped live microphones and left a jingle playing would
    # be answering the question nobody asked: whoever hits Emergency Stop is
    # not distinguishing between the two kinds of audio, they are stopping the
    # sound in their shops.
    #
    # Attempted per Store and never allowed to fail the whole action, for the
    # same reason each session is: one unreachable shop must not leave the
    # rest playing.
    for row in announcement_service.live_status(engine):
        if row["state"] == announcements.STATE_STOPPED:
            continue
        try:
            updated = announcement_service.stop(
                engine, store_id=row["store_id"], actor_id=user.id)
            await _dispatch_announcement(row["store_id"], updated)
            silenced_stores.append(row["store_id"])
        except Exception:  # noqa: BLE001
            logger.exception("Emergency Stop could not silence Store %s",
                             row["store_id"])

    if silenced_stores:
        _write_log(db, "warn",
                   f"EMERGENCY STOP by {user.username}: announcements silenced "
                   f"in {len(silenced_stores)} Stores")

    if failed:
        _write_log(db, "error",
                   f"EMERGENCY STOP by {user.username}: stopped {stopped}, "
                   f"FAILED to stop {failed}")
        # Honest rather than convenient: the caller is told exactly which
        # broadcasts are still live, so they can act on them.
        raise HTTPException(
            status_code=500,
            detail={
                "code": "EMERGENCY_STOP_INCOMPLETE",
                "message": "Some broadcasts could not be stopped and may still "
                           "be live.",
                "stopped_session_ids": stopped,
                "failed_session_ids": failed,
            },
        )
    if stopped:
        _write_log(db, "error", f"EMERGENCY STOP triggered by {user.username} "
                                f"on session(s) {stopped}")
        return {"ok": True, "session_ids": stopped,
                "session_id": stopped[0]}
    # Nothing live - still tell every connected Receiver to stop, as a safety
    # net against a Store left playing by a session this process has no record
    # of (a restart, for instance).
    for sid_ in list(manager.receivers.keys()):
        await manager.send_to_receiver(sid_, {"type": "stop", "reason": "emergency"})
    _write_log(db, "warn", f"Emergency stop invoked with no live session by {user.username}")
    return {"ok": True, "session_ids": [], "session_id": None}


@api.get("/broadcast/active")
def active_broadcasts(db: Session = Depends(get_db),
                      user: HQUser = Depends(require("menu.broadcast.view"))):
    """Every live broadcast, redacted to what THIS account may know.

    THREE TIERS, DECIDED HERE AND NOT IN REACT

    * ``mine`` - your own live broadcast, in full. It is yours.
    * ``busy_store_ids`` - Stores held by anybody's broadcast, intersected
      with your Store Scope. A Broadcaster needs this to pick different
      Stores; it says a Store is unavailable and nothing about who has it.
    * ``sessions`` - other people's broadcasts with owner and campaign. EMPTY
      unless this account holds broadcast.view_ownership.

    The redaction is applied by NOT SERIALISING the hidden fields, rather than
    by sending them and hiding them client-side. A field that reaches the
    browser has been disclosed, whatever the interface does with it
    afterwards - it is in the network tab, the response cache and any log
    that records bodies.

    SCOPE AND COUNTS

    Target lists are intersected with the viewer's Store Scope, and
    ``target_store_count`` counts what SURVIVED that intersection. Reporting
    the real total while showing fewer rows would let a scoped Admin infer
    exactly how many Stores they are not allowed to see - a count is a
    disclosure like any other.

    Nothing here reports SPEAKER_VERIFIED, and nothing infers playback from
    the fact that a broadcast is live.
    """
    scope = resolve_store_scope(engine, user)
    may_see_owners = has_permission_code(engine, user, "broadcast.view_ownership")
    may_see_targets = has_permission_code(engine, user, abm.TARGETS_CODE)
    may_manage = has_permission_code(engine, user, abm.PAGE_CODE)

    busy = active_busy_store_ids(engine, scope=scope)

    mine = None
    others = []
    for session_id in manager.broadcasts.active_session_ids():
        live = manager.broadcasts.get(session_id)
        if live is None:
            continue
        session = db.query(BroadcastSession).filter(
            BroadcastSession.id == session_id).first()
        if session is None:
            continue

        visible_targets = sorted(
            store_id for store_id in live.target_store_ids
            if scope is None or store_id in scope)

        if live.owner_user_id == user.id:
            mine = {
                "session_id": session.id,
                "campaign_name": session.campaign_name,
                "started_at": session.started_at.isoformat()
                if session.started_at else None,
                "target_store_ids": visible_targets,
                "target_store_count": len(visible_targets),
            }
            continue

        if not may_see_owners:
            # Deliberately nothing at all - not a redacted stub. An entry with
            # a null owner still discloses how many other broadcasts exist.
            continue

        owner = db.query(HQUser).filter(HQUser.id == live.owner_user_id).first()
        entry = {
            "session_id": session.id,
            "campaign_name": session.campaign_name,
            "owner_user_id": live.owner_user_id,
            "owner_username": owner.username if owner else None,
            "owner_display_name": owner.display_name if owner else None,
            "started_at": session.started_at.isoformat()
            if session.started_at else None,
            "target_store_count": len(visible_targets),
        }
        # The EXACT Stores of somebody else's broadcast are a separate
        # disclosure from who owns it, and now have their own permission.
        # This route used to send target_store_ids to every view_ownership
        # holder, which made ownership visibility a back door to target
        # visibility - the exact leak broadcast.view_targets exists to
        # prevent. The count survives because a Broadcaster already learns
        # occupancy from busy_store_ids.
        if may_see_targets:
            entry["target_store_ids"] = visible_targets
        others.append(entry)

    return {
        "mine": mine,
        "sessions": others,
        "busy_store_ids": sorted(busy),
        "may_view_ownership": may_see_owners,
        "may_view_targets": may_see_targets,
        # A compact count for the console badge, so Broadcast Console can say
        # "Active Broadcasts: 17 - View" without rendering 17 rows. Present
        # only for accounts that may open the supervision page at all;
        # otherwise the number itself would disclose how many broadcasts
        # exist to somebody with no right to know.
        "active_count": (len(manager.broadcasts.active_session_ids())
                         if may_manage else None),
        "may_manage_active": may_manage,
    }


def _active_management_rows(db: Session, user: HQUser, visibility) -> list:
    """The shared row build. One active-truth source, one scope intersection.

    Both the list and the per-session routes go through here so a session can
    never be visible on one and absent from the other.
    """
    scope = resolve_store_scope(engine, user)
    store_cache: dict[int, object] = {}

    def store_lookup(store_id: int):
        if store_id not in store_cache:
            store_cache[store_id] = db.query(Store).filter(Store.id == store_id).first()
        return store_cache[store_id]

    def web_room_lookup(session_id: int):
        """The compact room summary for one live Broadcast, or None.

        Built here rather than in the module so the redaction module stays free
        of database and runtime imports. Whether the caller may SEE any of it is
        decided later, in ActiveRow.serialize.
        """
        room = web_rooms.get_room_for_session(engine, session_id=session_id)
        if room is None:
            return None
        participants = web_rooms.list_participants(engine, room_id=room.id)
        counts = web_participants.counts_for_room(room.id)
        return abm.WebRoomSummary(
            public_code=room.public_code,
            status=room.status,
            auto_approve=room.auto_approve,
            # Only if THIS process still holds the plaintext it generated.
            # Nothing reads it back from storage, because nothing stores it.
            password=_ROOM_PASSWORD_ONCE.get(session_id) or None,
            waiting_count=sum(
                1 for row in participants
                if row.admission_status == web_rooms.AdmissionStatus.REQUESTED),
            connected_count=counts["connected"],
            listening_count=counts["listening"],
        )

    return abm.collect_active_rows(
        runtime=manager.broadcasts,
        session_lookup=lambda sid: db.query(BroadcastSession).filter(
            BroadcastSession.id == sid).first(),
        owner_lookup=lambda uid: db.query(HQUser).filter(HQUser.id == uid).first(),
        store_lookup=store_lookup,
        scope=scope,
        viewer_user_id=user.id,
        web_room_lookup=web_room_lookup,
    )


@api.get("/broadcast/active-management")
def active_management_list(
    q: str | None = None,
    owner: str = "all",
    # Strings, because the controls above them are multi-select: `store_id=4,7`
    # is an ordinary request. An int parameter answered it with "Input should
    # be a valid integer", which is a sentence about types shown to somebody
    # who ticked a second Store.
    owner_user_id: str | None = None,
    store_id: str | None = None,
    sort: str = abm.SORT_NEWEST,
    dir: str = "asc",
    page: int = 1,
    page_size: int = abm.DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(abm.PAGE_CODE)),
):
    """The Active Broadcasts supervision list. Metadata only, never targets.

    Deliberately does NOT return each session's Stores even to a caller
    holding view_targets: 50 sessions x every target is the payload the
    operator specifically asked us not to send, and the exact Stores have
    their own route. What view_targets buys here is the right to ASK for that
    detail, and to search and filter by Store.

    Unauthorized filters are refused rather than ignored. A store_id filter
    silently dropped for somebody without view_targets would return the
    unfiltered list, which they would reasonably read as "these are the
    broadcasts on that Store".
    """
    visibility = abm.resolve_visibility(engine, user)
    rows = _active_management_rows(db, user, visibility)
    try:
        rows = abm.filter_and_sort(
            rows,
            visibility=visibility,
            search=q,
            owner_filter=owner,
            owner_user_id=owner_user_id,
            store_id=store_id,
            sort=sort,
        )
    except abm.OwnershipVisibilityDenied:
        raise HTTPException(status_code=403,
                            detail="You do not have permission to filter by broadcaster.")
    except abm.TargetVisibilityDenied:
        raise HTTPException(status_code=403,
                            detail="You do not have permission to filter by Store.")

    # Column sorting, applied to the whole filtered set before pagination.
    # abm.filter_and_sort already ordered by newest/oldest; a named column
    # REPLACES that rather than adding to it, so there is always a way back to
    # the reading the page opens with.
    serialized = [row.serialize(visibility) for row in rows]
    if sort not in abm.SORTS:
        serialized = sort_rows(serialized, sort, dir, ACTIVE_BROADCAST_SORTS)
    window, total, resolved_page, resolved_size = abm.paginate(
        serialized, page=page, page_size=page_size)
    pages = (total + resolved_size - 1) // resolved_size if resolved_size else 0
    return {
        "items": window,
        "total": total,
        "page": resolved_page,
        "page_size": resolved_size,
        "pages": pages,
        "has_more": resolved_page * resolved_size < total,
        "meta": visibility.as_dict(),
    }


@api.get("/broadcast/active-management/{sid}/stores")
def active_management_stores(sid: int, db: Session = Depends(get_db),
                             user: HQUser = Depends(require(abm.PAGE_CODE))):
    """The EXACT Stores of one live broadcast.

    Separate route, separate permission, and a hard refusal rather than an
    empty list - an empty list would be indistinguishable from a broadcast
    with no in-scope Stores, and would teach the caller that the session
    exists either way.

    Store names come from the Store rows the session actually targets, never
    from Receiver Device names: a Device is named by whoever enrolled it, may
    be renamed, tombstoned or shared, and is not the authority on which Store
    a broadcast reached.
    """
    visibility = abm.resolve_visibility(engine, user)
    if not visibility.may_view_targets:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to view the Stores of a broadcast.")

    rows = _active_management_rows(db, user, visibility)
    row = next((r for r in rows if r.session_id == sid), None)
    if row is None:
        # 404 for "not live" and for "not visible to you" alike, so this
        # cannot be used to probe which session ids exist.
        raise HTTPException(status_code=404, detail="No such active broadcast")

    return {
        "session_id": row.session_id,
        "campaign_name": row.campaign_name,
        "started_at": row.started_at,
        # Scope-intersected, like every other Store list in this application.
        "stores": [target.as_dict() for target in row.visible_targets],
        "target_store_count": len(row.visible_targets),
        **({"owner_user_id": row.owner_user_id,
            "owner_username": row.owner_username,
            "owner_display_name": row.owner_display_name}
           if visibility.may_view_ownership or row.is_mine else {}),
    }


@api.post("/broadcast/sessions/{sid}/targets")
async def add_store_to_live_broadcast(
    sid: int, payload: LiveTargetAddIn, db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.store_delivery")),
):
    """Add ONE Store to a Broadcast that is already on air.

    Every gate that guards starting a Broadcast guards this too, because this
    IS starting one - on a single shop, while the rest keep playing. The gates
    run before anything is written, so a refusal leaves the running Broadcast
    exactly as it was.

    WHAT MAKES THIS DIFFERENT FROM START

    A Store joining now has missed the beginning of the stream, and a WebM
    stream cannot be joined at an arbitrary byte: the decoder needs the header
    and then whole Clusters. So this hands the Store the cached initialization
    segment and starts it on a Cluster boundary, which is what
    join_store_at_live_edge does. It joins the LIVE EDGE - no backlog, because
    a shop playing an announcement that already finished is worse than a shop
    that joined a moment late.

    ACTIVE here means audio is being delivered. It is not a claim that anything
    is audible; play_status carries what the Receiver reports, and neither is
    acoustic verification.
    """
    # The operator running this broadcast, and nobody else - the same rule the
    # output-volume control already applies, for the same reason. A supervisor
    # entitled to END somebody's broadcast is not thereby entitled to sit
    # inside it adding and removing shops: that is an invisible, continuous
    # intervention rather than one accountable act, and the operator on the
    # other end would have no way to tell it was happening.
    #
    # It is also why this lives under /broadcast/sessions and not under
    # active-management. Reaching it through the supervision page meant
    # requiring broadcast.active_view, which an ordinary BROADCASTER does not
    # have - so a Broadcaster could not add a Store to their OWN broadcast.
    _require_audio_control_owner(sid, user)

    # Physical delivery, at the single choke point every physical target passes
    # through. Deliberately separate from Store Scope: one says whether you may
    # reach Stores at all, the other says which.
    _require_physical_delivery(user)

    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None or session.status != "live":
        raise HTTPException(status_code=409,
                            detail="This broadcast is no longer live.")

    # Only With Link means zero physical Stores, and it means it. Refused in
    # the backend rather than by hiding a button, because a hidden button is a
    # suggestion and this is a rule.
    if session.target_mode == ONLY_WITH_LINK:
        raise HTTPException(
            status_code=409,
            detail=("This is an Only With Link broadcast, which reaches web "
                    "listeners only. Adding a Store would change what it is."))

    store = db.query(Store).filter(Store.id == payload.store_id).first()
    if store is None or not store.is_active:
        raise HTTPException(status_code=404, detail="No such Store.")

    scope = resolve_store_scope(engine, user)
    if scope is not None and store.id not in scope:
        # Same answer as a Store that does not exist. A caller outside scope is
        # not entitled to learn that this one does.
        raise HTTPException(status_code=404, detail="No such Store.")

    existing = db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == sid,
        BroadcastTarget.store_id == store.id).first()
    if existing is not None and existing.lifecycle_state in (
            lifecycle.ADDING, lifecycle.PREPARING,
            lifecycle.ACTIVE, lifecycle.PAUSED):
        # Idempotent rather than an error: a double click should not be a
        # second lease and a second queue.
        return {"session_id": sid, "store_id": store.id,
                "lifecycle_state": existing.lifecycle_state,
                "generation": existing.current_generation,
                "already_participating": True}

    if store.id not in manager.online_store_ids():
        raise HTTPException(
            status_code=409,
            detail=(f"{store.store_code} has no Receiver connected, so it "
                    "cannot join this broadcast."))

    # ---- from here on something has been claimed, so every failure cleans up
    try:
        reserve_one_store_for_session(engine, session_id=sid, store_id=store.id)
    except StoreBusyError:
        raise HTTPException(
            status_code=409,
            detail=(f"{store.store_code} is already live in another broadcast. "
                    "It was not added, and that broadcast was not disturbed."))

    generation = (existing.current_generation + 1) if existing else 1
    if existing is None:
        target = BroadcastTarget(
            session_id=sid, store_id=store.id,
            store_code_snapshot=store.store_code,
            store_name_snapshot=store.store_name,
            play_status="pending",
            lifecycle_state=lifecycle.PREPARING,
            current_generation=generation)
        db.add(target)
    else:
        target = existing
        target.lifecycle_state = lifecycle.PREPARING
        target.current_generation = generation
        target.play_status = "pending"
        target.error_message = None
        target.stopped_at = None
    db.commit()

    async def fail(detail: str, status: int = 409):
        """Undo everything this request claimed, then say why."""
        release_store_lease(engine, session_id=sid, store_id=store.id)
        target.lifecycle_state = lifecycle.FAILED
        target.error_message = detail[:500]
        db.commit()
        # The Receiver may already have been prepared, which on this protocol
        # means it has taken over the Windows output. Stop is terminal and puts
        # it back - the only safe instruction available before the stand-down
        # primitive exists.
        await manager.send_to_receiver(store.id, build_stop_message(session_id=sid))
        raise HTTPException(status_code=status, detail=detail)

    store_audio_registry.add_store(session_id=sid, store_id=store.id)
    await manager.send_to_receiver(
        store.id, build_prepare_message(session_id=sid, store_id=store.id))

    ready = await manager.wait_for_store_ready(
        store.id, timeout=ADD_STORE_READY_TIMEOUT_SECONDS)
    if not ready:
        await fail(f"{store.store_code} did not report ready in time. "
                   "It was not added and nothing else changed.")

    joined = await manager.broadcasts.join_store_at_live_edge(sid, store.id)
    if joined is None or not joined.get("joined"):
        await fail(f"{store.store_code} could not be started at the live edge: "
                   f"{(joined or {}).get('reason', 'no audio to join yet')}.")

    target.lifecycle_state = lifecycle.ACTIVE
    target.command_sent_at = datetime.utcnow()
    db.commit()

    await manager.send_to_receiver(
        store.id, {"type": "play", "session_id": sid,
                   "campaign": session.campaign_name})
    _write_log(db, "info",
               f"STORE_ADD_ACTIVE session_id={sid} store_id={store.id} "
               f"generation={generation} actor_user_id={user.id}")
    await manager.notify_dashboards(
        {"type": "target_added", "session_id": sid, "store_id": store.id})
    return {"session_id": sid, "store_id": store.id,
            "store_code": store.store_code,
            "lifecycle_state": target.lifecycle_state,
            "generation": generation,
            "bootstrap": joined}


#: What a bulk request may ask for, mapped to the single-Store handler that
#: already knows how to do it. Reusing the handlers rather than reimplementing
#: them is deliberate: a second copy of "add a Store" would eventually disagree
#: with the first about leases, generations or the live edge, and the
#: disagreement would only show up in a shop.
_BULK_ACTIONS = ("add", "remove", "pause", "resume")


@api.post("/broadcast/sessions/{sid}/targets/bulk")
async def bulk_target_action(
    sid: int, payload: BulkTargetActionIn, db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.store_delivery")),
):
    """Add, remove, pause or resume every Store in a Zone, City or list.

    ONE ACTION, MANY SHOPS, AND AN HONEST ANSWER FOR EACH. The response is a
    per-Store outcome list rather than a single ok/failed, because a Zone
    action almost never succeeds uniformly: some shops are offline, one is in
    somebody else's broadcast, another is already paused. Collapsing that into
    "12 of 17 succeeded" would leave the operator to guess which five.

    SEQUENTIAL, deliberately. Adding a Store waits for its Receiver to report
    ready, so a Zone of twenty could be twenty waits - but the alternative is
    concurrent database work on one Session, and a corrupted target list is a
    worse outcome than a slow one. Pause and remove are fast enough that this
    does not matter; add and resume are the slow pair and are documented as
    such in the UI.

    A refusal for one Store never stops the rest. That is the whole reason for
    doing this in a loop that catches: an operator silencing a Zone because
    something is wrong needs the other sixteen shops silenced even if one is
    unreachable.
    """
    _require_audio_control_owner(sid, user)
    _require_physical_delivery(user)

    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None or session.status != "live":
        raise HTTPException(status_code=409,
                            detail="This broadcast is no longer live.")

    scope = resolve_store_scope(engine, user)
    store_ids = _resolve_bulk_target_stores(db, payload, scope)
    if not store_ids:
        # Not an error. A Zone with no Stores this account may reach is a
        # truthful answer to a truthful question, and pretending otherwise
        # would send an operator hunting for a fault that is not there.
        return {"session_id": sid, "action": payload.action,
                "requested": 0, "succeeded": 0, "results": []}

    handler = {
        "add": lambda store_id: add_store_to_live_broadcast(
            sid, LiveTargetAddIn(store_id=store_id), db=db, user=user),
        "remove": lambda store_id: remove_store_from_live_broadcast(
            sid, store_id, db=db, user=user),
        "pause": lambda store_id: pause_store_in_live_broadcast(
            sid, store_id, db=db, user=user),
        "resume": lambda store_id: resume_store_in_live_broadcast(
            sid, store_id, db=db, user=user),
    }[payload.action]

    results = []
    for store_id in store_ids:
        try:
            outcome = await handler(store_id)
            results.append({"store_id": store_id, "ok": True,
                            "lifecycle_state": outcome.get("lifecycle_state"),
                            "detail": None})
        except HTTPException as refusal:
            # Recorded and carried on with. The refusal text is the same
            # sentence the single-Store route would have shown, so an operator
            # reads one vocabulary whichever way they acted.
            results.append({"store_id": store_id, "ok": False,
                            "lifecycle_state": None,
                            "detail": str(refusal.detail)})
        except Exception as failure:  # pragma: no cover - defence in depth
            logger.warning("Bulk %s failed for store %s: %s",
                           payload.action, store_id, failure)
            results.append({"store_id": store_id, "ok": False,
                            "lifecycle_state": None,
                            "detail": "That Store could not be changed."})

    succeeded = sum(1 for row in results if row["ok"])
    _write_log(db, "info",
               f"BULK_TARGET_ACTION session_id={sid} action={payload.action} "
               f"requested={len(store_ids)} succeeded={succeeded} "
               f"actor_user_id={user.id}")
    return {"session_id": sid, "action": payload.action,
            "requested": len(store_ids), "succeeded": succeeded,
            "results": results}


def _resolve_bulk_target_stores(db: Session, payload, scope) -> list[int]:
    """Which Stores a bulk request actually names, within the caller's scope.

    Scope is applied HERE rather than left to the per-Store handlers, so a
    scoped operator asking for a whole Zone gets their own Stores acted on
    instead of a list of refusals for shops they are not allowed to know about.
    """
    query = db.query(Store).filter(Store.is_active.is_(True))
    if payload.region:
        query = query.filter(Store.region == payload.region)
    if payload.city:
        query = query.filter(Store.city == payload.city)
    if payload.store_ids:
        query = query.filter(Store.id.in_(payload.store_ids))
    rows = query.order_by(Store.store_code.asc()).all()
    return [row.id for row in rows if scope is None or row.id in scope]


@api.post("/broadcast/sessions/{sid}/targets/{store_id}/pause")
async def pause_store_in_live_broadcast(
    sid: int, store_id: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.store_delivery")),
):
    """Silence ONE Store without taking it out of the Broadcast.

    PAUSE IS NOT REMOVE, and the difference is the lease. A removed Store is
    released, so another Broadcast may claim it; a paused Store is still this
    Broadcast's, waiting to come back. Releasing the lease here would let a
    colleague's announcement take the shop over during a pause the operator
    intends to end in thirty seconds.

    The Receiver is told to STAND DOWN rather than to stop: it closes its
    decoder and output device and hands the shop's own volume back, but keeps
    the session, so Resume costs a device open instead of a whole
    re-negotiation of readiness.
    """
    _require_audio_control_owner(sid, user)
    _require_physical_delivery(user)

    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None or session.status != "live":
        raise HTTPException(status_code=409,
                            detail="This broadcast is no longer live.")

    scope = resolve_store_scope(engine, user)
    if scope is not None and store_id not in scope:
        raise HTTPException(status_code=404,
                            detail="That Store is not in this broadcast.")

    target = db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == sid,
        BroadcastTarget.store_id == store_id).first()
    if target is None:
        raise HTTPException(status_code=404,
                            detail="That Store is not in this broadcast.")
    if target.lifecycle_state in (lifecycle.PAUSED, lifecycle.PAUSING):
        # Idempotent: a second click is the same intent, already true.
        return {"session_id": sid, "store_id": store_id,
                "lifecycle_state": target.lifecycle_state,
                "generation": target.current_generation,
                "already_paused": True}
    if target.lifecycle_state != lifecycle.ACTIVE:
        raise HTTPException(
            status_code=409,
            detail="Only a Store that is currently receiving can be paused.")

    target.lifecycle_state = lifecycle.PAUSING
    db.commit()

    # Delivery stops FIRST. Telling a Receiver to stand down while still
    # pushing audio at it would queue chunks for a decoder that is closing.
    store_audio_registry.drop_store(session_id=sid, store_id=store_id)
    await manager.broadcasts.drop_store(sid, store_id)

    delivered = await manager.send_to_receiver(
        store_id, build_stand_down_message(session_id=sid))

    # The lease is deliberately NOT released. See the docstring.
    target.lifecycle_state = lifecycle.PAUSED
    db.commit()

    _write_log(db, "info",
               f"STORE_PAUSED session_id={sid} store_id={store_id} "
               f"generation={target.current_generation} "
               f"stand_down_delivered={bool(delivered)} actor_user_id={user.id}")
    await manager.notify_dashboards(
        {"type": "target_paused", "session_id": sid, "store_id": store_id})
    return {"session_id": sid, "store_id": store_id,
            "lifecycle_state": target.lifecycle_state,
            "generation": target.current_generation,
            "stand_down_delivered": bool(delivered)}


@api.post("/broadcast/sessions/{sid}/targets/{store_id}/resume")
async def resume_store_in_live_broadcast(
    sid: int, store_id: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.store_delivery")),
):
    """Bring a paused Store back into the Broadcast it never left.

    A NEW GENERATION, not a continuation. The Receiver re-opens its output and
    takes the Windows endpoint over again from the shop's own level, so the
    volume baseline belongs to this participation rather than to the one before
    the pause - a shop coming back at the previous announcement's level is
    exactly the surprise this avoids. The generation is what lets a late
    acknowledgement from before the pause be recognised and dropped.

    It rejoins at the LIVE EDGE, like any other late join: a shop replaying the
    part of the announcement it missed, out of step with every other shop, is
    worse than a shop that missed it.
    """
    _require_audio_control_owner(sid, user)
    _require_physical_delivery(user)

    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None or session.status != "live":
        raise HTTPException(status_code=409,
                            detail="This broadcast is no longer live.")

    scope = resolve_store_scope(engine, user)
    if scope is not None and store_id not in scope:
        raise HTTPException(status_code=404,
                            detail="That Store is not in this broadcast.")

    target = db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == sid,
        BroadcastTarget.store_id == store_id).first()
    if target is None:
        raise HTTPException(status_code=404,
                            detail="That Store is not in this broadcast.")
    if target.lifecycle_state == lifecycle.ACTIVE:
        return {"session_id": sid, "store_id": store_id,
                "lifecycle_state": target.lifecycle_state,
                "generation": target.current_generation,
                "already_active": True}
    if target.lifecycle_state != lifecycle.PAUSED:
        raise HTTPException(status_code=409,
                            detail="Only a paused Store can be resumed.")

    store = db.query(Store).filter(Store.id == store_id).first()
    code = store.store_code if store else str(store_id)

    if store_id not in manager.online_store_ids():
        raise HTTPException(
            status_code=409,
            detail=(f"{code} has no Receiver connected, so it cannot be "
                    "resumed. It is still paused."))

    generation = (target.current_generation or 1) + 1
    target.current_generation = generation
    db.commit()

    async def fail(detail: str):
        """Leave the Store PAUSED rather than in a state nobody can act on."""
        target.lifecycle_state = lifecycle.PAUSED
        target.error_message = detail[:500]
        db.commit()
        raise HTTPException(status_code=409, detail=detail)

    store_audio_registry.add_store(session_id=sid, store_id=store_id)
    await manager.send_to_receiver(store_id, build_resume_message(
        session_id=sid, store_id=store_id, generation=generation))

    ready = await manager.wait_for_store_ready(
        store_id, timeout=ADD_STORE_READY_TIMEOUT_SECONDS)
    if not ready:
        await fail(f"{code} did not report ready after resuming. It is still "
                   "paused and nothing else changed.")

    joined = await manager.broadcasts.join_store_at_live_edge(sid, store_id)
    if joined is None or not joined.get("joined"):
        await fail(f"{code} could not be started at the live edge: "
                   f"{(joined or {}).get('reason', 'no audio to join yet')}.")

    target.lifecycle_state = lifecycle.ACTIVE
    target.error_message = None
    target.command_sent_at = datetime.utcnow()
    db.commit()

    _write_log(db, "info",
               f"STORE_RESUMED session_id={sid} store_id={store_id} "
               f"generation={generation} actor_user_id={user.id}")
    await manager.notify_dashboards(
        {"type": "target_resumed", "session_id": sid, "store_id": store_id})
    return {"session_id": sid, "store_id": store_id,
            "lifecycle_state": target.lifecycle_state,
            "generation": generation,
            "bootstrap": joined}


@api.delete("/broadcast/sessions/{sid}/targets/{store_id}")
async def remove_store_from_live_broadcast(
    sid: int, store_id: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.store_delivery")),
):
    """Take ONE Store out of a Broadcast that is still on air.

    The mirror of add_store_to_live_broadcast, and gated identically: the
    operator running this broadcast, holding physical delivery, within their
    Store Scope.

    WHY THIS IS A STOP AND NOT A PAUSE

    Stop is terminal on this protocol: the Receiver hands the Windows audio
    output back and forgets the session. That is exactly what removal means -
    the shop is out, and a shop that is out must not be holding the speakers.
    Pausing (leaving the Receiver attached but silent) needs a stand-down
    instruction the Receiver does not have yet, which is why Pause is a later
    milestone and not a variation of this one.

    ORDER MATTERS. Delivery is cut first, then the Receiver is told, then the
    lease is released. Releasing the lease first would let another broadcast
    claim the Store while this one was still pushing audio at it.

    The Store can be added back afterwards; that is a fresh generation, not a
    resumption, and it rejoins at the live edge like any other late join.
    """
    _require_audio_control_owner(sid, user)
    _require_physical_delivery(user)

    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None or session.status != "live":
        raise HTTPException(status_code=409,
                            detail="This broadcast is no longer live.")

    scope = resolve_store_scope(engine, user)
    if scope is not None and store_id not in scope:
        # Same answer as a Store that is not in this broadcast. Out of scope is
        # not entitled to learn which shops a broadcast is reaching.
        raise HTTPException(status_code=404,
                            detail="That Store is not in this broadcast.")

    target = db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == sid,
        BroadcastTarget.store_id == store_id).first()
    if target is None:
        raise HTTPException(status_code=404,
                            detail="That Store is not in this broadcast.")

    participating = (lifecycle.ADDING, lifecycle.PREPARING,
                     lifecycle.ACTIVE, lifecycle.PAUSING, lifecycle.PAUSED)
    if target.lifecycle_state not in participating:
        # Idempotent. A second click, or a Store that already dropped out on
        # its own, is not an error - the caller's intent is already true.
        return {"session_id": sid, "store_id": store_id,
                "lifecycle_state": target.lifecycle_state,
                "generation": target.current_generation,
                "already_removed": True}

    # Removing the last Store is ALLOWED, and deliberately so. It leaves a
    # broadcast that is live and reaching no shop, which sounds like something
    # to refuse - but every Broadcast has a web room, so it is still reaching
    # its web audience, and refusing would break the ordinary way an operator
    # moves an announcement from one shop to another: remove this one, add
    # that one. Forcing the opposite order is a rule that exists only to be
    # worked around.
    #
    # What is owed instead is honesty, so the count of Stores still receiving
    # is returned and the console can say "no Stores" rather than implying the
    # announcement is still going somewhere.
    remaining = db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == sid,
        BroadcastTarget.store_id != store_id,
        BroadcastTarget.lifecycle_state.in_(participating)).count()

    target.lifecycle_state = lifecycle.REMOVING
    db.commit()

    # 1. Cut delivery, so nothing more is queued for a Store on its way out.
    store_audio_registry.drop_store(session_id=sid, store_id=store_id)
    await manager.broadcasts.drop_store(sid, store_id)

    # 2. Tell the Receiver, which gives the audio output back. Best effort: a
    #    Receiver that has already gone offline cannot be told, and that must
    #    not strand the row in REMOVING or hold the lease.
    delivered = await manager.send_to_receiver(
        store_id, build_stop_message(session_id=sid))

    # 3. Only now is the Store free for another broadcast to claim.
    release_store_lease(engine, session_id=sid, store_id=store_id)

    target.lifecycle_state = lifecycle.REMOVED
    target.stopped_at = datetime.utcnow()
    # play_status carries what the Receiver last reported and stays that way.
    # Overwriting it here would be HQ inventing an acoustic fact.
    db.commit()

    _write_log(db, "info",
               f"STORE_REMOVED session_id={sid} store_id={store_id} "
               f"generation={target.current_generation} "
               f"stop_delivered={bool(delivered)} actor_user_id={user.id}")
    await manager.notify_dashboards(
        {"type": "target_removed", "session_id": sid, "store_id": store_id})
    return {"session_id": sid, "store_id": store_id,
            "lifecycle_state": target.lifecycle_state,
            "generation": target.current_generation,
            "stop_delivered": bool(delivered),
            "stores_remaining": remaining}


@api.post("/broadcast/active-management/{sid}/stop")
async def active_management_stop(sid: int, db: Session = Depends(get_db),
                                 user: HQUser = Depends(require(abm.PAGE_CODE))):
    """Stop ONE named broadcast, which may belong to somebody else.

    NOT Emergency Stop. Emergency Stop ends every broadcast estate-wide and
    keeps its own permission; this ends exactly the session named in the URL
    and leaves every other one on air. They are different operations with
    different blast radii, and conflating them is how an operator intending
    to silence one Store silences forty-four.

    Three independent gates for a cross-owner stop:

      broadcast.active_view  - already enforced by the dependency
      broadcast.stop_any     - the power to reach across owners
      Store Scope            - covering EVERY target, not merely the visible
                               ones, because Stop ends the whole session

    Deliberately does NOT require view_targets. Ending a broadcast on Stores
    you administer is a legitimate act for a supervisor who is not entitled
    to know which campaign or which colleague it belonged to - so the ACTION
    is permitted while the DETAIL stays hidden, and the confirmation the
    client renders is built from whatever it was allowed to read.

    Own sessions keep the ordinary rule and need only broadcast.stop, so a
    Broadcaster with no supervision rights at all can still stop their own
    broadcast from the Console.
    """
    visibility = abm.resolve_visibility(engine, user)
    rows = _active_management_rows(db, user, visibility)
    row = next((r for r in rows if r.session_id == sid), None)
    if row is None:
        raise HTTPException(status_code=404, detail="No such active broadcast")

    if row.is_mine:
        # Your own broadcast: the existing permission, unchanged. stop_any is
        # not required and must not become required, or this page would
        # regress own-stop for every ordinary Broadcaster.
        if not has_permission_code(engine, user, "broadcast.stop"):
            raise HTTPException(status_code=403,
                                detail="You do not have permission to stop a broadcast.")
    else:
        if not visibility.may_stop_any:
            raise HTTPException(
                status_code=403,
                detail="You do not have permission to stop another operator's broadcast.")
        scope = resolve_store_scope(engine, user)
        outside = abm.stop_scope_refusal(row, scope)
        if outside:
            # The COUNT of out-of-scope Stores, never their ids or names: the
            # caller is not entitled to learn which Stores they cannot see.
            _write_log(db, "warn",
                       f"Cross-owner stop REFUSED (out of scope): actor_user_id={user.id} "
                       f"session_id={sid} outside_store_count={len(outside)}")
            raise HTTPException(
                status_code=403,
                detail=(f"This broadcast reaches {len(outside)} Store(s) outside your "
                        "Store Scope. Stopping it would end the broadcast on all of "
                        "them, so it was refused."))

    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None or session.status != "live":
        raise HTTPException(status_code=404, detail="No such active broadcast")

    target_count = len(row.all_target_store_ids)
    try:
        await _end_session(db, session, "ended", reason="stopped_by_supervisor")
    except Exception:
        # Never report STOPPED because a command was sent. Cleanup is what
        # releases the leases and closes the audio path; if it failed the
        # broadcast may still be live, and saying otherwise would leave an
        # operator believing a Store is silent when it is not.
        db.rollback()
        logger.exception("Selected stop failed for session %s", sid)
        _write_log(db, "error",
                   f"Cross-owner stop FAILED: actor_user_id={user.id} "
                   f"session_id={sid} target_owner_user_id={row.owner_user_id}")
        raise HTTPException(
            status_code=500,
            detail={"code": "STOP_FAILED",
                    "message": "This broadcast could not be stopped and may still be "
                               "live. Refresh the list and try again.",
                    "session_id": sid})

    db.refresh(session)
    # Audited through the existing system-log infrastructure. Ids and counts
    # only - no password, no token, no Device credential, no audio.
    if row.is_mine:
        _write_log(db, "info", f"Session #{sid} stopped by owner {user.username}")
    else:
        _write_log(db, "warn",
                   f"CROSS-OWNER STOP: actor_user_id={user.id} actor={user.username} "
                   f"stopped session_id={sid} owner_user_id={row.owner_user_id} "
                   f"target_store_count={target_count} result=ended")
    return {"ok": True, "session_id": sid, "status": session.status}


@api.get("/broadcast/current")
def current_broadcast(db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.VIEW_STATUS))):
    """The caller's OWN live broadcast.

    Owner-scoped now that several can be live at once. Returning "the" live
    session would hand every viewer another operator's campaign name and
    target list, which is exactly the disclosure the ownership-visibility
    permission exists to gate - and that permission is a later checkpoint, so
    until it lands this answers only about your own.
    """
    own = [sid for sid in manager.broadcasts.active_session_ids()
           if manager.broadcasts.owner_of(sid) == user.id]
    if not own:
        return {"live": False}
    session = db.query(BroadcastSession).filter(
        BroadcastSession.id == own[0]).first()
    if not session:
        return {"live": False}
    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == session.id).all()
    # A scoped user must not learn about targets outside their assigned
    # Stores - not even that a live session is reaching them. This never
    # changes what IS live, only what THIS response reveals about it.
    scope = resolve_store_scope(engine, user)
    if scope is not None:
        targets = [t for t in targets if t.store_id in scope]
    online_ids = manager.online_store_ids()
    ready_ids = manager.ready_store_ids()
    if scope is not None:
        online_ids = {i for i in online_ids if i in scope}
        ready_ids = {i for i in ready_ids if i in scope}
    return {
        "live": True,
        "session": SessionOut.model_validate(session).model_dump(mode="json"),
        "targets": [TargetOut.model_validate(t).model_dump(mode="json") for t in targets],
        "online_receivers": list(online_ids),
        # READY comes only from an explicit receiver_ready acknowledgement.
        # Being connected is never enough, so these two lists are separate.
        "ready_receivers": list(ready_ids),
    }


def _scoped_session_target_counts(db: Session, session: BroadcastSession, scope) -> dict:
    """Recompute selected/online/offline counts from only the in-scope
    targets of one session, so a scoped user's history view cannot infer how
    many Stores outside their scope a campaign actually reached."""
    if scope is None:
        return {
            "selected_store_count": session.selected_store_count,
            "online_store_count": session.online_store_count,
            "offline_store_count": session.offline_store_count,
        }
    targets = db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id == session.id, BroadcastTarget.store_id.in_(scope)
    ).all()
    online_ids = manager.online_store_ids()
    online = sum(1 for t in targets if t.store_id in online_ids)
    return {
        "selected_store_count": len(targets),
        "online_store_count": online,
        "offline_store_count": len(targets) - online,
    }


@api.get("/broadcast/history", response_model=List[SessionOut])
def broadcast_history(limit: int = 50, include_archived: bool = False,
                      db: Session = Depends(get_db),
                      user: HQUser = Depends(require(Permission.VIEW_HISTORY))):
    query = db.query(BroadcastSession)
    if not include_archived:
        query = query.filter(BroadcastSession.archived_at.is_(None))
    sessions = query.order_by(BroadcastSession.id.desc()).limit(limit).all()
    scope = resolve_store_scope(engine, user)
    if scope is None:
        return _with_recordings(sessions)
    # A scoped user sees only campaigns that reached at least one of their
    # Stores, and the counts on it are recomputed to that subset - never the
    # real totals, which would leak how many out-of-scope Stores were also
    # targeted.
    visible = []
    for session in sessions:
        in_scope_targets = db.query(BroadcastTarget.id).filter(
            BroadcastTarget.session_id == session.id, BroadcastTarget.store_id.in_(scope)
        ).first()
        if not in_scope_targets:
            continue
        counts = _scoped_session_target_counts(db, session, scope)
        for key, value in counts.items():
            setattr(session, f"_scoped_{key}", value)
            setattr(session, key, value)
        visible.append(session)
    return _with_recordings(visible)



def _with_recordings(sessions):
    """Attach each broadcast's recording metadata to its history row.

    One query for the whole page rather than one per row: History is the screen
    most likely to be opened with hundreds of sessions behind it.
    """
    rows = [SessionOut.model_validate(session) for session in sessions]
    try:
        recordings = broadcast_recording.all_recordings(engine)
    except Exception:
        # A recording metadata problem must not take Broadcast History down.
        # The page is how an operator finds out what happened at all.
        return rows
    for row in rows:
        record = recordings.get(row.id)
        if record is not None:
            row.recording = RecordingOut(**{
                key: value for key, value in record.as_dict().items()
                if key != "session_id"
            })
    return rows


@api.get("/broadcast/sessions/{sid}/recording")
def read_broadcast_recording(
    sid: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.VIEW_HISTORY)),
):
    """Recording metadata for one broadcast.

    Gated on the SAME permission as Broadcast History itself. A recording is
    the audio of a broadcast this account is already entitled to read about,
    so inventing a second permission would only create a way to see that a
    recording exists and never be allowed to hear it.
    """
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_history_store_scope(db, session, user)
    record = broadcast_recording.get_recording(engine, session_id=sid)
    if record is None:
        raise HTTPException(status_code=404, detail="No recording for that broadcast")
    return RecordingOut(**{key: value for key, value in record.as_dict().items()
                           if key != "session_id"})


def _require_history_store_scope(db: Session, session, user: HQUser) -> None:
    """A scoped operator may only reach broadcasts touching their own Stores.

    Broadcast History already applies Store Scope; a recording is the audio of
    one of those broadcasts and must not be a way around it.
    """
    scope = resolve_store_scope(engine, user)
    if scope is None:
        return
    target_ids = {
        row.store_id for row in
        db.query(BroadcastTarget).filter(BroadcastTarget.session_id == session.id).all()
    }
    if not target_ids & set(scope):
        # 404, matching the rest of History: a 403 would confirm the broadcast
        # exists and let an out-of-scope account enumerate the estate.
        raise HTTPException(status_code=404, detail="Session not found")


@api.get("/broadcast/sessions/{sid}/recording/download")
def download_broadcast_recording(
    sid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.VIEW_HISTORY)),
):
    """The same bytes as playback, offered as a file.

    Deliberately the same permission, the same Store Scope and the same route
    body as playback - downloading is not a different entitlement from
    listening, and a second implementation would be a second place for the
    authorization to drift.

    The only difference is Content-Disposition: attachment rather than inline,
    with a name built from the session id alone. No campaign name, no username,
    nothing that could carry something private into a downloads folder.
    """
    return _stream_recording(sid, request, db, user, disposition="attachment")


@api.get("/broadcast/sessions/{sid}/recording/audio")
def stream_broadcast_recording(
    sid: int,
    request: Request,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.VIEW_HISTORY)),
):
    return _stream_recording(sid, request, db, user, disposition="inline")


def _stream_recording(sid: int, request: Request, db: Session, user: HQUser,
                      *, disposition: str):
    """The audio itself, authenticated, with byte-range support.

    NOT a static mount. The recordings directory is never served as a public
    folder: every byte leaves through this route, which checks the same
    permission and the same Store Scope as History.

    Range support exists because a browser audio element asks for one - seeking
    in a WebM without it means refetching the whole file, and some browsers
    simply refuse to seek at all.
    """
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    _require_history_store_scope(db, session, user)

    record = broadcast_recording.get_recording(engine, session_id=sid)
    if record is None or record.status not in (
            broadcast_recording.STATUS_AVAILABLE,
            broadcast_recording.STATUS_PARTIAL):
        # PARTIAL is playable and deliberately allowed: a recording with a gap
        # is still the best evidence of what went out.
        raise HTTPException(status_code=404,
                            detail="No playable recording for that broadcast")

    directory = broadcast_recording.recordings_directory()
    path = (directory / record.file_name).resolve()
    if path.parent != directory.resolve() or not path.exists():
        # The row says there is audio and there is not. Recorded honestly so
        # the next History read stops offering a Play button.
        broadcast_recording.finish_record(
            engine, session_id=sid, status=broadcast_recording.STATUS_MISSING,
            error="the recording file was not found")
        raise HTTPException(status_code=404, detail="The recording file is missing")

    total = path.stat().st_size
    range_header = request.headers.get("range")
    start, end = 0, total - 1
    status_code = 200
    if range_header and range_header.startswith("bytes="):
        piece = range_header.split("=", 1)[1].split(",")[0]
        first, _, last = piece.partition("-")
        try:
            if first:
                start = int(first)
                end = int(last) if last else total - 1
            elif last:
                start = max(0, total - int(last))
        except ValueError:
            start, end = 0, total - 1
        else:
            status_code = 206
        start = max(0, min(start, total - 1))
        end = max(start, min(end, total - 1))

    length = end - start + 1

    def chunks():
        # Streamed in pieces rather than read whole: an hour of announcements
        # should not have to fit in memory to be played back.
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(65536, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    headers = {
        "Content-Length": str(length),
        "Accept-Ranges": "bytes",
        # An attachment name derived from the session id alone - no campaign
        # name, no username, nothing that could carry something private into a
        # downloads folder.
        "Content-Disposition":
            f'{disposition}; filename="broadcast-{sid:06d}.webm"',
    }
    if status_code == 206:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    return StreamingResponse(chunks(), status_code=status_code,
                             media_type="audio/webm", headers=headers)


@api.get("/broadcast/sessions/{sid}", response_model=SessionDetailOut)
def session_detail(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.VIEW_HISTORY))):
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    scope = resolve_store_scope(engine, user)
    targets_query = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == sid)
    if scope is not None:
        # No in-scope targets means this campaign never reached this
        # account's Stores at all - treated the same as "not found", so a
        # scoped user cannot distinguish "no session" from "a session that
        # only targeted Stores I cannot see".
        if not targets_query.filter(BroadcastTarget.store_id.in_(scope)).first():
            raise HTTPException(status_code=404, detail="Session not found")
        targets_query = targets_query.filter(BroadcastTarget.store_id.in_(scope))
        counts = _scoped_session_target_counts(db, session, scope)
        for key, value in counts.items():
            setattr(session, key, value)
    targets = targets_query.all()
    out = SessionDetailOut.model_validate(session)
    enriched = []
    for t in targets:
        target_out = TargetOut.model_validate(t)
        if t.store is not None:
            target_out.store_code = t.store.store_code
            target_out.store_name = t.store.store_name
            target_out.store_deleted = t.store.lifecycle_state == "deleted"
        else:
            # The Store was permanently deleted. Its identity was snapshotted
            # onto this row at that moment, which is what keeps history
            # readable - and deliberately what is read here rather than a
            # lookup by code, because a DIFFERENT Store may now be using that
            # code and this target has nothing to do with it.
            target_out.store_code = getattr(t, "store_code_snapshot", None)
            target_out.store_name = getattr(t, "store_name_snapshot", None)
            target_out.store_deleted = True
        enriched.append(target_out)
    out.targets = enriched
    return out


# ================ RECEIVER ================
# GET /api/receiver/verify and POST /api/receiver/event are deliberately gone.
#
# The first took a raw Store credential in a query string - the least private
# part of a request, reaching access logs, reverse-proxy logs, browser history,
# copied links, monitoring tools, screenshots and Referer headers. Anybody who
# could read one log line could connect a Receiver as that Store. The second
# took the same credential in a JSON body: better than a URL, and still an
# unauthenticated route that accepted a long-lived shared secret and wrote a row
# against whichever Store it named.
#
# They were removed rather than moved to Authorization: Bearer, because nothing
# that ships called either one. receiver_agent.py and audio_receiver_pilot.py
# both authenticate over /api/ws/receiver with a header; the only caller was
# frontend/src/pages/Receiver.jsx, which has been unrouted since the
# query-token work and is not even bundled. Re-plumbing an endpoint no client
# uses would have kept the attack surface and delivered nothing.
#
# A Receiver reports its state over its authenticated WebSocket, where the
# server already knows which Store and which Device is speaking - so a caller
# cannot name a Store it does not hold a credential for.


# ================ LOGS ================
@api.get("/logs", response_model=List[SystemLogOut])
def list_logs(
    level: Optional[str] = None,
    limit: int = 200,
    include_archived: bool = False,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.VIEW_LOGS)),
):
    q = db.query(SystemLog)
    if level:
        q = q.filter(SystemLog.level == level)
    if not include_archived:
        # Archived log lines are hidden, not removed - Show Archived brings
        # them back. Permanent deletion is a separate, irreversible action.
        q = q.filter(SystemLog.archived_at.is_(None))
    return q.order_by(SystemLog.id.desc()).limit(limit).all()


class BulkIdsRequest(BaseModel):
    #: "ids" (explicit rows - a single row action, or Select Page) or
    #: "filtered" (every server-side match of `filters`, resolved inside the
    #: caller's own scope). See admin_search.resolve_bulk_selection for why
    #: the second mode exists rather than having React enumerate ids.
    mode: str = "ids"
    ids: List[int] = Field(default_factory=list)
    #: Present only for permanent deletion. Bulk destruction requires the
    #: operator to type it, exactly like the single-row deletes elsewhere.
    confirm: Optional[str] = None
    acknowledged: bool = False
    #: What the operator had on screen when they chose "all filtered". Recorded
    #: in the audit so a bulk delete can be explained months later.
    filters: Optional[dict] = None


def _require_bulk_confirmation(payload: BulkIdsRequest, expected: str) -> None:
    if not payload.acknowledged:
        raise HTTPException(
            status_code=400,
            detail="The 'this cannot be undone' acknowledgement is required.")
    if (payload.confirm or "").strip() != expected:
        raise HTTPException(
            status_code=409,
            detail=f"The typed confirmation did not match. Type exactly: {expected}")


def _history_ids_matching(filters: dict, user: HQUser, db: Session) -> list:
    """Every broadcast session id matching `filters`, narrowed to the caller's
    Store scope. Runs the SAME query the search endpoint runs, so a bulk
    action can never select a row the operator could not have seen."""
    query = db.query(BroadcastSession.id)
    term = like_term(filters.get("q"))
    if term:
        query = query.filter(BroadcastSession.campaign_name.ilike(term))
    if filters.get("status"):
        query = query.filter(BroadcastSession.status == filters["status"])
    try:
        start_at = parse_date(filters.get("date_from"))
        end_at = parse_date(filters.get("date_to"), end_of_day=True)
    except ValueError as bad:
        raise HTTPException(status_code=400, detail=str(bad))
    if start_at:
        query = query.filter(BroadcastSession.created_at >= start_at)
    if end_at:
        query = query.filter(BroadcastSession.created_at <= end_at)
    # The SAME shapes the list route accepts. If this resolved filters
    # differently, "Select all filtered" would act on a different set from the
    # one on screen - which for a permanent delete is the worst bug this file
    # could have.
    bulk_owners = _id_list(filters.get("started_by"))
    if bulk_owners:
        query = query.filter(BroadcastSession.started_by.in_(bulk_owners))

    target_conditions = []
    bulk_stores = _id_list(filters.get("store_id"))
    if bulk_stores:
        target_conditions.append(BroadcastTarget.store_id.in_(bulk_stores))
    if filters.get("city") or filters.get("region"):
        store_match = []
        if filters.get("city"):
            store_match.append(Store.city.in_(_text_list(filters["city"])))
        if filters.get("region"):
            store_match.append(Store.region.in_(_text_list(filters["region"])))
        target_conditions.append(BroadcastTarget.store_id.in_(
            db.query(Store.id).filter(*store_match).scalar_subquery()))
    if target_conditions:
        query = query.filter(
            db.query(BroadcastTarget.id).filter(
                BroadcastTarget.session_id == BroadcastSession.id,
                *target_conditions).exists())

    if filters.get("archived_only"):
        query = query.filter(BroadcastSession.archived_at.isnot(None))
    elif not filters.get("include_archived"):
        query = query.filter(BroadcastSession.archived_at.is_(None))

    scope = resolve_store_scope(engine, user)
    if scope is not None:
        query = query.filter(
            db.query(BroadcastTarget.id).filter(
                BroadcastTarget.session_id == BroadcastSession.id,
                BroadcastTarget.store_id.in_(scope) if scope
                else BroadcastTarget.store_id.in_([-1])).exists())
    return [row[0] for row in query.all()]


def _log_ids_matching(filters: dict, db: Session) -> list:
    query = db.query(SystemLog.id)
    term = like_term(filters.get("q"))
    if term:
        query = query.filter(SystemLog.message.ilike(term))
    if filters.get("level"):
        query = query.filter(SystemLog.level == filters["level"])
    try:
        start_at = parse_date(filters.get("date_from"))
        end_at = parse_date(filters.get("date_to"), end_of_day=True)
    except ValueError as bad:
        raise HTTPException(status_code=400, detail=str(bad))
    if start_at:
        query = query.filter(SystemLog.created_at >= start_at)
    if end_at:
        query = query.filter(SystemLog.created_at <= end_at)
    for field_name, column in (("actor_user_id", SystemLog.actor_user_id),
                               ("store_id", SystemLog.store_id),
                               ("device_public_id", SystemLog.device_public_id)):
        if filters.get(field_name) is not None:
            query = query.filter(column == filters[field_name])
    if filters.get("archived_only"):
        query = query.filter(SystemLog.archived_at.isnot(None))
    elif not filters.get("include_archived"):
        query = query.filter(SystemLog.archived_at.is_(None))
    return [row[0] for row in query.all()]


def _resolve_bulk(payload: BulkIdsRequest, resolver):
    try:
        return resolve_bulk_selection(payload.mode, payload.ids, payload.filters,
                                      resolver=resolver)
    except BulkSelectionError as bad:
        raise HTTPException(status_code=400, detail=str(bad))


@api.post("/broadcast/history/archive")
def archive_broadcast_sessions(payload: BulkIdsRequest,
                               db: Session = Depends(get_db),
                               user: HQUser = Depends(require("broadcast_history.archive"))):
    """Hide sessions from the normal History list. Reversible - nothing is
    removed, and Show Archived brings them back into view."""
    ids, matched = _resolve_bulk(payload, lambda f: _history_ids_matching(f, user, db))
    result = archive_sessions(engine, session_ids=ids,
                              actor_user_id=user.id, archived=True)
    _write_log(db, "warn",
               f"BROADCAST_HISTORY_ARCHIVED count={result.affected} by={user.username}")
    return {**result.as_dict(), "matched": matched}


@api.post("/broadcast/history/unarchive")
def unarchive_broadcast_sessions(payload: BulkIdsRequest,
                                 db: Session = Depends(get_db),
                                 user: HQUser = Depends(require("broadcast_history.archive"))):
    ids, matched = _resolve_bulk(payload, lambda f: _history_ids_matching(f, user, db))
    result = archive_sessions(engine, session_ids=ids,
                              actor_user_id=user.id, archived=False)
    _write_log(db, "info",
               f"BROADCAST_HISTORY_UNARCHIVED count={result.affected} by={user.username}")
    return {**result.as_dict(), "matched": matched}


@api.get("/broadcast/history/{sid}/chat")
def read_broadcast_history_chat(
    sid: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.history.view")),
):
    """The chat transcript of a finished Broadcast.

    Readable by anyone who may read the history it belongs to, and for exactly
    as long: the messages cascade from the room, which cascades from the
    session, so deleting the broadcast from history takes this with it.

    The HOST view, deliberately. Private messages were addressed to whoever
    hosted the Broadcast, and this page is the record of what happened - a
    transcript with the private half silently missing would be a transcript
    that lies by omission to the very people entitled to audit it. Deleted
    messages appear as tombstones for the same reason.

    Store Scope is not consulted. A chat message belongs to a Broadcast and to
    a person who typed it, not to a shop; there is nothing here to scope by,
    and inventing one would only hide half a conversation at random.
    """
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None:
        raise HTTPException(status_code=404, detail="No such broadcast.")
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    if room is None:
        # A Broadcast from before web rooms existed. Empty is the truth, and it
        # is a different answer from "this broadcast does not exist".
        return {"session_id": sid, "campaign_name": session.campaign_name,
                "chat_enabled": False, "chat_mode": web_chat.PUBLIC,
                "messages": []}
    settings = web_chat.get_settings(engine, room_id=room.id)
    messages = web_chat.history_for_host(engine, room_id=room.id, limit=1000)
    # The transcript is the record. Whether the removed half of it is readable
    # is the same question here as in the live panel, and gets the same answer.
    reveal = _may_see_removed_chat(user)
    return {"session_id": sid, "campaign_name": session.campaign_name,
            **settings, "may_see_removed": reveal,
            "messages": [m.public_dict(reveal_removed=reveal) for m in messages]}


@api.post("/store-kits")
async def upload_store_kit(
    file: UploadFile = File(...),
    user: HQUser = Depends(require("store_kit.manage")),
):
    """Put a new installer on HQ, from the Console.

    A build happens on somebody's machine, and getting it onto HQ used to mean
    a file copy that nobody could audit. This is the same act with a name
    against it: the upload is logged, the checksum is computed here rather than
    taken from the uploader, and the stored filename is built by HQ rather than
    accepted from the request.

    HQ HOLDS EXACTLY ONE KIT. Whatever this upload is called, it replaces
    whatever was here - a list of builds means somebody eventually installs the
    wrong one, and "which build is that Store on?" stops having one answer. The
    names that were removed come back in the response.

    HQ does not validate that the file IS the SpeakLink installer, and does not
    pretend to - it checks the extension, the size and the magic bytes, which
    catches the ordinary mistakes. Trusting a build is a human act; the account
    that uploaded it is recorded so that trust has somewhere to point.
    """
    raw = await file.read()
    try:
        kit, superseded = store_kits.store_uploaded_kit(
            raw, filename=file.filename or "")
    except store_kits.KitRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    with SessionLocal() as db:
        _write_log(db, "warn",
                   f"STORE_KIT_UPLOADED name={kit.name} bytes={kit.size_bytes} "
                   f"sha256={kit.sha256} by={user.username}"
                   + (f" superseded={','.join(superseded)}" if superseded else ""))
    # The names that went are returned, not swallowed: an operator uploading a
    # new build is entitled to be told which one it replaced.
    return {**kit.public_dict(), "superseded": superseded}


@api.delete("/store-kits/{name}")
def delete_store_kit(
    name: str, user: HQUser = Depends(require("store_kit.manage")),
):
    """Remove a kit HQ is offering.

    Deliberately not gated on it being an old one: an installer uploaded by
    mistake is exactly the one that needs removing quickly, and it will be the
    newest.
    """
    if not store_kits.delete_kit(name):
        raise HTTPException(status_code=404, detail="No such Store Kit.")
    with SessionLocal() as db:
        _write_log(db, "warn", f"STORE_KIT_DELETED name={name} by={user.username}")
    return {"deleted": name}


@api.get("/store-kits")
def list_store_kits(user: HQUser = Depends(require("store_kit.download"))):
    """Which Store Kits this HQ can hand out.

    Newest first, by the file's own modification time rather than by version
    string - 1.10.0 sorts before 1.9.0, and a name is a claim while a timestamp
    is something the machine observed.
    """
    kits = store_kits.list_kits()
    return {
        "kits": [kit.public_dict() for kit in kits],
        "latest": kits[0].public_dict() if kits else None,
        # Said explicitly, because "no kits" and "the feature is broken" look
        # identical otherwise, and the fix for the first one is a build.
        "directory_exists": store_kits.kits_directory().exists(),
    }


@api.get("/store-kits/latest/download")
def download_latest_store_kit(
    user: HQUser = Depends(require("store_kit.download")),
):
    """The newest kit, as a file.

    A convenience over naming it: the machine at the till wants "the current
    one", and a person reading a version number off a screen and typing it into
    another machine is a step that can go wrong.
    """
    kit = store_kits.latest_kit()
    if kit is None:
        raise HTTPException(
            status_code=404,
            detail=("This HQ has no Store Kit to hand out yet. Build one and "
                    "put it in the store-kits folder."))
    return _kit_file_response(kit)


@api.get("/store-kits/{name}/download")
def download_store_kit(
    name: str, user: HQUser = Depends(require("store_kit.download")),
):
    """A named kit - for installing a shop deliberately on an older build."""
    path = store_kits.resolve_kit_path(name)
    if path is None:
        # The name is matched against the listing rather than joined onto a
        # path, so an attempt at traversal is simply a name that is not there.
        raise HTTPException(status_code=404, detail="No such Store Kit.")
    kit = next(k for k in store_kits.list_kits() if k.name == name)
    return _kit_file_response(kit)


def _kit_file_response(kit):
    path = store_kits.kits_directory() / kit.name
    with SessionLocal() as db:
        _write_log(db, "info", f"STORE_KIT_DOWNLOADED name={kit.name}")
    return FileResponse(
        path=str(path), filename=kit.name, media_type="application/zip",
        headers={
            # The checksum travels with the file, so a Store can check what it
            # received against what HQ holds without a second request.
            "X-SpeakLink-Kit-SHA256": kit.sha256,
            "Cache-Control": "no-store",
        })


@api.post("/broadcast/history/delete-permanently")
def delete_broadcast_sessions(payload: BulkIdsRequest,
                              db: Session = Depends(get_db),
                              user: HQUser = Depends(
                                  require("broadcast_history.delete_permanently"))):
    """Really remove broadcast sessions and their targets. Irreversible.

    Unlike a Store/User/Device, history IS the record - there is nothing
    downstream for a tombstone to protect - so this genuinely deletes.
    Nothing else is touched: never a Store, a User or a Receiver Device.
    """
    _require_bulk_confirmation(payload, "DELETE")
    ids, matched = _resolve_bulk(payload, lambda f: _history_ids_matching(f, user, db))

    # The audio goes FIRST, deliberately. Deleting the rows first and then
    # discovering the file could not be removed would leave an orphan with
    # nothing left pointing at it - unfindable, and never cleaned up. This
    # order can at worst leave a row whose audio is gone, which the next read
    # reports honestly as MISSING.
    #
    # Only these sessions' own files are touched. The recordings directory
    # itself is never removed.
    recordings_directory = broadcast_recording.recordings_directory()
    known = broadcast_recording.all_recordings(engine)
    for session_id in ids:
        record = known.get(session_id)
        if record is None:
            continue
        try:
            broadcast_recording.remove_recording_file(
                recordings_directory, record.file_name)
        except Exception as failure:
            logger.warning("Recording file for session %s could not be removed: %s",
                           session_id, failure)

    # Chat images go with the audio, and for the same reason: a photograph
    # somebody sent during an announcement must not outlive the record of the
    # announcement. The message ROWS cascade from the room; these are files, so
    # nothing cascades them and they are removed here explicitly.
    for session_id in ids:
        try:
            chat_attachments.delete_session_images(session_id)
        except Exception as failure:
            logger.warning("Chat images for session %s could not be removed: %s",
                           session_id, failure)

    result = delete_sessions_permanently(
        engine, session_ids=ids, actor_user_id=user.id,
        filters=payload.filters)
    # The metadata rows follow their sessions. A FOREIGN KEY with ON DELETE
    # CASCADE covers this when SQLite has foreign keys enabled, but that is a
    # per-connection PRAGMA and this must not depend on it.
    for session_id in ids:
        try:
            broadcast_recording.delete_record(engine, session_id=session_id)
        except Exception:
            pass
    _write_log(db, "warn",
               f"BROADCAST_HISTORY_DELETED count={result.affected} "
               f"requested={result.requested} by={user.username}")
    return {**result.as_dict(), "matched": matched}


@api.post("/logs/archive")
def archive_system_logs(payload: BulkIdsRequest, db: Session = Depends(get_db),
                        user: HQUser = Depends(require("system_logs.archive"))):
    ids, matched = _resolve_bulk(payload, lambda f: _log_ids_matching(f, db))
    result = archive_logs(engine, log_ids=ids, actor_user_id=user.id,
                          archived=True)
    _write_log(db, "warn", f"SYSTEM_LOGS_ARCHIVED count={result.affected} by={user.username}")
    return {**result.as_dict(), "matched": matched}


@api.post("/logs/unarchive")
def unarchive_system_logs(payload: BulkIdsRequest, db: Session = Depends(get_db),
                          user: HQUser = Depends(require("system_logs.archive"))):
    ids, matched = _resolve_bulk(payload, lambda f: _log_ids_matching(f, db))
    result = archive_logs(engine, log_ids=ids, actor_user_id=user.id,
                          archived=False)
    _write_log(db, "info", f"SYSTEM_LOGS_UNARCHIVED count={result.affected} by={user.username}")
    return {**result.as_dict(), "matched": matched}


@api.post("/logs/delete-permanently")
def delete_system_logs(payload: BulkIdsRequest, db: Session = Depends(get_db),
                       user: HQUser = Depends(require("system_logs.delete_permanently"))):
    """Really remove system_logs rows. Irreversible.

    Only system_logs is touched. The administrative deletion audit lives in
    its own table precisely so a log purge can never erase the record of the
    purge.
    """
    _require_bulk_confirmation(payload, "DELETE")
    ids, matched = _resolve_bulk(payload, lambda f: _log_ids_matching(f, db))
    result = delete_logs_permanently(engine, log_ids=ids,
                                     actor_user_id=user.id, filters=payload.filters)
    _write_log(db, "warn",
               f"SYSTEM_LOGS_DELETED count={result.affected} "
               f"requested={result.requested} by={user.username}")
    return {**result.as_dict(), "matched": matched}


@api.get("/admin/deletion-events")
def read_admin_deletion_events(record_type: Optional[str] = None, limit: int = 200,
                               user: HQUser = Depends(require("menu.logs.view"))):
    """The immutable administrative deletion audit. Records who removed how
    many rows and by what filter - never the deleted content, which would
    defeat the deletion the operator asked for."""
    return {"events": list_admin_deletion_events(engine, record_type=record_type,
                                                 limit=limit)}


# ================ RECEIVER STATUS / DEVICE SEARCH ================
# Receiver Status is Store-shaped (one row per Store plus the health of its
# Receiver). Receiver Devices is Device-shaped and spans Stores - the
# existing /stores/{id}/receiver-devices only ever shows one Store, which is
# not a search surface at all.
#
# receiver_devices and receiver_store_primary_device are raw-SQL tables
# (migrations.py / receiver_primary_device.py), not ORM models, so these
# queries are built as parameterised SQL exactly like every other reader of
# those tables. Every value is bound, never interpolated.


def _receiver_scope_clause(user, params):
    """Returns a SQL fragment narrowing to the caller's Store scope, or ''.

    The empty frozenset case matters: scope rows exist but resolve to no
    Stores, which is a real 'nothing' and must not be read as 'everything'."""
    scope = resolve_store_scope(engine, user)
    if scope is None:
        return ""
    if not scope:
        return " AND 1=0"
    keys = []
    for index, store_id in enumerate(sorted(scope)):
        key = f"scope_{index}"
        params[key] = store_id
        keys.append(f":{key}")
    return f" AND s.id IN ({', '.join(keys)})"


def _text_list(value) -> list[str]:
    """`ACTIVE`, `ACTIVE,OFFLINE`, or nothing - as a list."""
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _in_clause(where: list, params: dict, column: str, name: str,
               values: list) -> None:
    """An IN over however many values were chosen, one numbered bind each.

    Numbered binds rather than an expanding bindparam because these queries
    are assembled as text and executed in several places; a parameter that
    only works if every call site remembers to declare it expanding is a trap
    the next filter falls into.
    """
    if not values:
        return
    keys = []
    for index, value in enumerate(values):
        key = f"{name}_{index}"
        params[key] = value
        keys.append(f":{key}")
    where.append(f"{column} IN ({', '.join(keys)})")


def _id_list(value) -> list[int]:
    """`4`, `4,7`, or nothing - as a list of ints.

    One helper for every filter that names rows by id. A filter that accepts
    one value where the control offers several is not a smaller feature; it is
    a 422 in front of somebody who ticked a second box.
    """
    out: list[int] = []
    for part in str(value or "").split(","):
        text_part = part.strip()
        if text_part:
            try:
                out.append(int(text_part))
            except ValueError:
                continue
    return out


def _tri_state(value) -> bool | None:
    """Yes, no, or either. Both ticked is "either", not "nothing"."""
    picked = {part.strip().lower()
              for part in str(value or "").split(",") if part.strip()}
    yes = bool(picked & {"true", "1", "yes"})
    no = bool(picked & {"false", "0", "no"})
    if yes and not no:
        return True
    if no and not yes:
        return False
    return None


def _receiver_status_where(user, *, q, city, region, store_id, status_f):
    """One narrowing used by BOTH the search and its filter options, so an
    option list can never offer a Zone whose Stores the caller cannot see."""
    where = ["s.is_active = 1" if engine.dialect.name == "sqlite" else "s.is_active = true",
             "(s.lifecycle_state IS NULL OR s.lifecycle_state <> 'deleted')"]
    params = {}
    term = like_term(q)
    if term:
        params["term"] = term
        where.append(
            "(s.store_code LIKE :term OR s.store_name LIKE :term OR s.id IN "
            "(SELECT d.store_id FROM receiver_devices d "
            " WHERE d.public_id LIKE :term OR d.display_name LIKE :term))")
    # IN rather than =, because a filter may name several values. One value is
    # a list of one, so every existing link and bookmark still means what it
    # meant: `?region=NORTH` is unchanged.
    cities = value_list(city)
    if cities:
        params["cities"] = cities; where.append("s.city IN :cities")
    regions = value_list(region)
    if regions:
        params["regions"] = regions; where.append("s.region IN :regions")
    store_ids = int_list(store_id)
    if store_ids:
        params["store_ids"] = store_ids; where.append("s.id IN :store_ids")
    statuses = value_list(status_f)
    if statuses:
        params["statuses"] = statuses; where.append("s.status IN :statuses")
    clause = " AND ".join(where)
    clause += _receiver_scope_clause(user, params)
    return clause, params


@api.get("/receivers/search")
def search_receiver_status(
    q: Optional[str] = None,
    # Comma-separated, because a filter may name several values. Typed as a
    # string rather than an int for exactly that reason - `store_id=4,7` is a
    # legitimate request and an int parameter would reject it with a 422 the
    # operator cannot act on.
    city: Optional[str] = None,
    region: Optional[str] = None,
    store_id: Optional[str] = None,
    status_f: Optional[str] = Query(None, alias="status"),
    # A string, for the same reason as the others: the control offers two
    # boxes and somebody may tick both. `true,false` is a legitimate request
    # meaning "either", and a bool parameter answers it with a 422 the
    # operator can neither read nor act on - which is how this filter took a
    # whole page white.
    has_primary: Optional[str] = None,
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """Filtered, paginated Receiver Status.

    Reports only what SpeakLink can actually prove. ``speaker_verified`` is
    NEVER derived from connected/ready/audio-receiving/playback-confirmed:
    acoustic verification arrives on a separate trusted path, and inferring
    it from software liveness is exactly the claim this project refuses to
    make. It is reported as null until that proof exists.
    """
    page, page_size = normalize_paging(page, page_size)
    where, params = _receiver_status_where(
        user, q=q, city=city, region=region, store_id=store_id, status_f=status_f)

    want = _tri_state(has_primary)
    if want is not None:
        exists = ("EXISTS (SELECT 1 FROM receiver_store_primary_device p "
                  "WHERE p.store_id = s.id)")
        where += f" AND {'' if want else 'NOT '}{exists}"

    def prepared(sql: str):
        """Bind the list parameters as expanding, so IN gets a real list."""
        statement = text(sql)
        for name in ("cities", "regions", "store_ids", "statuses"):
            if name in params:
                statement = statement.bindparams(bindparam(name, expanding=True))
        return statement

    total = db.execute(
        prepared(f"SELECT COUNT(*) FROM stores s WHERE {where}"), params).scalar_one()
    # ORDER BY built from an ALLOWLIST, never from the parameter itself: a
    # sort column interpolated into SQL is an injection, and the safe version
    # is also the one that cannot fail a page over a typo.
    orderable = {"store_code": "s.store_code", "store_name": "s.store_name",
                 "city": "s.city", "region": "s.region", "status": "s.status",
                 "device_count": "device_count", "has_primary": "has_primary"}
    column = orderable.get(sort or "", "s.store_code")
    # LOWER() for the text columns: sorted by raw bytes, "NIT Faridabad" lands
    # before "Nangal Raya" and the list looks broken to the person reading it.
    if column in ("s.store_code", "s.store_name", "s.city", "s.region", "s.status"):
        column = f"LOWER({column})"
    descending = "DESC" if str(dir).lower() == "desc" else "ASC"

    rows = db.execute(prepared(
        "SELECT s.id, s.store_code, s.store_name, s.city, s.region, s.status, "
        "  EXISTS (SELECT 1 FROM receiver_store_primary_device p WHERE p.store_id = s.id) "
        "    AS has_primary, "
        "  (SELECT COUNT(*) FROM receiver_devices d WHERE d.store_id = s.id "
        "     AND d.deleted_at IS NULL) AS device_count "
        f"FROM stores s WHERE {where} ORDER BY {column} {descending}, s.store_code "
        "LIMIT :limit OFFSET :offset"),
        {**params, "limit": page_size, "offset": (page - 1) * page_size}).all()

    online_ids = manager.online_store_ids()
    ready_ids = manager.ready_store_ids()
    items = [{
        "id": r.id, "store_code": r.store_code, "store_name": r.store_name,
        "city": r.city, "region": r.region,
        "status": "online" if r.id in online_ids else "offline",
        "connected": r.id in online_ids,
        "ready": r.id in ready_ids,
        "has_primary": bool(r.has_primary),
        "device_count": r.device_count,
        # Acoustic proof only - never inferred from the flags above.
        "speaker_verified": None,
    } for r in rows]
    return Page(items=items, total=total, page=page, page_size=page_size).as_dict()


@api.get("/receivers/filter-options")
def receiver_filter_options(db: Session = Depends(get_db),
                            user: HQUser = Depends(require("menu.receivers.view"))):
    """Zone/City/Store options drawn from the SAME scoped narrowing the search
    uses. A scoped account must not discover an out-of-scope Zone merely by
    opening a dropdown."""
    where, params = _receiver_status_where(
        user, q=None, city=None, region=None, store_id=None, status_f=None)
    rows = db.execute(text(
        f"SELECT s.id, s.store_code, s.store_name, s.city, s.region "
        f"FROM stores s WHERE {where} ORDER BY s.store_code"), params).all()
    return {
        "regions": sorted({r.region for r in rows if r.region}),
        "cities": sorted({r.city for r in rows if r.city}),
        "stores": [{"id": r.id, "store_code": r.store_code,
                    "store_name": r.store_name} for r in rows],
    }


# ================ SERVER-SIDE SEARCH / FILTER ================
# Deliberately separate paths from the existing list endpoints: those return
# bare arrays that the frontend, the Playwright mocks and the tooling all
# depend on, and adding a second response shape behind a flag would be two
# contracts wearing one name. See admin_search.py.


#: Store Management's lifecycle selections, and the ONE control that decides
#: which Stores it shows.
#:
#: Deliberately a single parameter rather than the older
#: include_inactive/include_archived pair. Two independent switches for one
#: concern produce combinations nobody designed - and worse, a UI built on
#: them can latch one switch on while changing the other, which is exactly how
#: selecting one lifecycle left the previous one still on screen.
#:
#: 'deleted' is absent on purpose and is refused below. A permanently deleted
#: Store is reachable only through the deletion-event and history surfaces.
STORE_LIFECYCLE_SELECTIONS = ("all_current", "active", "disabled", "archived")


def _store_admin_query(db: Session, user: HQUser, *, q=None, city=None, region=None,
                       lifecycle: str = "active"):
    """The one narrowing Store Management search AND its filter options use.

    Shared deliberately. When the list and the dropdown are built by two
    different queries they drift, and the way they drift is that a scoped
    account discovers an out-of-scope Zone by opening a menu.
    """
    query = db.query(Store)

    # A permanently deleted Store never appears here, under any selection. Its
    # history stays reachable through the rows that reference it, never
    # through this operational list.
    query = query.filter(
        (Store.lifecycle_state.is_(None)) | (Store.lifecycle_state != "deleted"))

    # A selection may now name SEVERAL states - "active and archived" is a real
    # question, and answering it used to mean running the search twice.
    #
    # The old comment here said each selection REPLACES the last, and that was
    # about a bug: the previous choice used to stay in effect invisibly. With
    # checkboxes the choice is visible, so widening is something the reader
    # asked for rather than something that happened to them.
    wanted_states = value_list(lifecycle)
    if "all_current" in wanted_states:
        # Named rather than implied by an empty value, so the control has no
        # meaning nobody chose. It widens to everything not deleted, so any
        # other value alongside it adds nothing.
        wanted_states = []
    if wanted_states:
        conditions = []
        if "active" in wanted_states:
            # A legacy row with no lifecycle_state is active exactly when
            # is_active says so - the pairing store_lifecycle keeps.
            conditions.append(
                (Store.lifecycle_state == "active")
                | (Store.lifecycle_state.is_(None) & Store.is_active.is_(True)))
        if "disabled" in wanted_states:
            conditions.append(
                (Store.lifecycle_state == "disabled")
                | (Store.lifecycle_state.is_(None) & Store.is_active.is_(False)))
        if "archived" in wanted_states:
            conditions.append(Store.lifecycle_state == "archived")
        if conditions:
            query = query.filter(or_(*conditions))

    cities = value_list(city)
    if cities:
        query = query.filter(Store.city.in_(cities))
    regions = value_list(region)
    if regions:
        query = query.filter(Store.region.in_(regions))

    term = like_term(q)
    if term:
        # like_term escapes % and _ so a literal one searches for itself
        # instead of quietly matching the whole catalog.
        query = query.filter(
            Store.store_code.ilike(term, escape="\\")
            | Store.store_name.ilike(term, escape="\\"))

    # Per-user Store/City/Zone scope, exactly as GET /stores applies it. None
    # means unrestricted; an empty frozenset is a real "nothing", not "all".
    scope = resolve_store_scope(engine, user)
    if scope is not None:
        query = query.filter(Store.id.in_(scope) if scope else Store.id.in_([-1]))
    return query


@api.get("/stores/search")
def search_stores(
    q: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    lifecycle: str = "active",
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.stores.view")),
):
    """Filtered, paginated Store Management.

    Deliberately a separate path from ``GET /api/stores``: that endpoint
    returns a bare array which the Broadcast Console, the Playwright mocks and
    the tooling all depend on, and adding a second response shape behind a
    flag would be two contracts wearing one name.

    Lifecycle only. Nothing here reports a Receiver as online or offline -
    that is Receiver Status's job, and it comes from live WebSocket state
    rather than from a Store row.
    """
    # Validated VALUE BY VALUE, because the filter may name several.
    #
    # This checked the whole parameter against the allowed set, and adding
    # multi-value filters broke it silently for the one caller that matters:
    # the page sends "active,archived" the moment somebody ticks two boxes,
    # and the whole list came back as a 400 the operator could do nothing
    # about.
    selections = value_list(lifecycle)
    if "deleted" in selections:
        # Refused rather than quietly returning nothing, so the caller learns
        # that this is not the surface for tombstones.
        raise HTTPException(
            status_code=400,
            detail="Permanently deleted Stores are not available here. Their "
                   "history is in the deletion-event records.")
    unknown = [value for value in selections
               if value not in STORE_LIFECYCLE_SELECTIONS]
    if unknown:
        # Ignoring an unknown value would silently return the default set,
        # which reads as a filter that works and does nothing.
        raise HTTPException(
            status_code=400,
            detail=f"Unknown lifecycle {unknown[0]!r}. Expected one of "
                   f"{', '.join(STORE_LIFECYCLE_SELECTIONS)}.")

    page, page_size = normalize_paging(page, page_size)
    query = _store_admin_query(db, user, q=q, city=city, region=region,
                               lifecycle=lifecycle)
    total = query.count()
    # ORDER BY from an allowlist, never from the parameter.
    orderable = {"store_code": Store.store_code, "store_name": Store.store_name,
                 "city": Store.city, "region": Store.region,
                 "status": Store.status, "lifecycle": Store.lifecycle_state,
                 "type": Store.is_online_store}
    column = orderable.get(sort or "", Store.store_code)
    # Case-insensitive, because a reader does not think in ASCII. Sorted by
    # raw bytes, "NIT Faridabad" lands before "Nangal Raya" and the list looks
    # broken - which is the only thing a sort order has to avoid.
    #
    # lower() rather than COLLATE NOCASE: the same expression works on SQLite
    # and PostgreSQL, and a collation that exists on one and not the other is
    # a page that sorts differently depending on where it is deployed.
    key = (column if column in (Store.status, Store.is_online_store)
           else func.lower(column))
    ordered = key.desc() if str(dir).lower() == "desc" else key.asc()
    rows = apply_paging(query.order_by(ordered, Store.store_code),
                        page, page_size).all()
    return Page(items=rows, total=total, page=page, page_size=page_size).as_dict(
        lambda row: {
            "id": row.id,
            "store_code": row.store_code,
            "store_name": row.store_name,
            "city": row.city,
            "region": row.region,
            "is_online_store": bool(row.is_online_store),
            "is_active": bool(row.is_active),
            "lifecycle_state": row.lifecycle_state or "active",
        })


@api.get("/stores/filter-options")
def store_filter_options(
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.stores.view")),
):
    """Zone/City options drawn from the SAME scoped narrowing the search uses.

    Built from the shared query rather than from the visible page, so the
    dropdown offers every Zone this account may reach and not merely the ones
    that happen to be on screen - and never one it may not.
    """
    # all_current: every Store this account may reach that still exists, so a
    # Zone is offered even when its only Store is archived.
    rows = _store_admin_query(db, user, lifecycle="all_current").all()
    return {
        "regions": sorted({row.region for row in rows if row.region}),
        "cities": sorted({row.city for row in rows if row.city}),
    }


@api.get("/logs/search")
def search_logs(
    q: Optional[str] = None,
    level: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    actor_user_id: Optional[str] = None,
    store_id: Optional[str] = None,
    device_public_id: Optional[str] = None,
    include_archived: bool = False,
    archived_only: bool = False,
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.logs.view")),
):
    """Filtered, paginated System Logs.

    The entity filters (actor/store/device) apply only to log lines recorded
    since those columns existed - the response says so in
    meta.entity_filter_coverage rather than letting a filter that silently
    matches nothing look like "no results".
    """
    page, page_size = normalize_paging(page, page_size)
    query = db.query(SystemLog)

    term = like_term(q)
    if term:
        query = query.filter(SystemLog.message.ilike(term))
    if level:
        query = query.filter(SystemLog.level == level)
    try:
        start_at = parse_date(date_from)
        end_at = parse_date(date_to, end_of_day=True)
    except ValueError as bad:
        raise HTTPException(status_code=400, detail=str(bad))
    if start_at:
        query = query.filter(SystemLog.created_at >= start_at)
    if end_at:
        query = query.filter(SystemLog.created_at <= end_at)
    wanted_actors = _id_list(actor_user_id)
    if wanted_actors:
        query = query.filter(SystemLog.actor_user_id.in_(wanted_actors))
    wanted_log_stores = _id_list(store_id)
    if wanted_log_stores:
        query = query.filter(SystemLog.store_id.in_(wanted_log_stores))
    if device_public_id:
        query = query.filter(SystemLog.device_public_id == device_public_id)

    if archived_only:
        query = query.filter(SystemLog.archived_at.isnot(None))
    elif not include_archived:
        query = query.filter(SystemLog.archived_at.is_(None))

    total = query.count()
    rows = apply_paging(query.order_by(SystemLog.id.desc()), page, page_size).all()

    structured = db.query(SystemLog).filter(
        (SystemLog.actor_user_id.isnot(None)) | (SystemLog.store_id.isnot(None))).count()
    result = Page(items=rows, total=total, page=page, page_size=page_size,
                  meta={"entity_filter_coverage": {
                      "rows_with_structured_entities": structured,
                      "note": "Actor/Store/Device filters cover log lines recorded "
                              "since those fields existed. Older lines are searchable "
                              "by text, level and date only.",
                  }})
    return result.as_dict(
        lambda row: SystemLogOut.model_validate(row).model_dump(mode="json"))


@api.get("/broadcast/history/search")
def search_broadcast_history(
    q: Optional[str] = None,
    status_f: Optional[str] = Query(None, alias="status"),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    # Strings, because the controls are multi-select. `store_id=4,7` and
    # `started_by=1,2` are ordinary requests; as ints they answered with
    # "Input should be a valid integer" the moment a second box was ticked.
    started_by: Optional[str] = None,
    store_id: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    include_archived: bool = False,
    archived_only: bool = False,
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.history.view")),
):
    """Filtered, paginated Broadcast History.

    A multi-target session matches a Store/City/Zone filter when AT LEAST ONE
    of its targets does, and is returned once rather than once per matching
    target - the join is expressed as an EXISTS subquery for exactly that
    reason.
    """
    page, page_size = normalize_paging(page, page_size)
    query = db.query(BroadcastSession)

    term = like_term(q)
    if term:
        query = query.filter(BroadcastSession.campaign_name.ilike(term))
    if status_f:
        query = query.filter(BroadcastSession.status == status_f)
    try:
        start_at = parse_date(date_from)
        end_at = parse_date(date_to, end_of_day=True)
    except ValueError as bad:
        raise HTTPException(status_code=400, detail=str(bad))
    if start_at:
        query = query.filter(BroadcastSession.created_at >= start_at)
    if end_at:
        query = query.filter(BroadcastSession.created_at <= end_at)
    wanted_owners = _id_list(started_by)
    if wanted_owners:
        query = query.filter(BroadcastSession.started_by.in_(wanted_owners))

    target_conditions = []
    wanted_history_stores = _id_list(store_id)
    if wanted_history_stores:
        target_conditions.append(
            BroadcastTarget.store_id.in_(wanted_history_stores))
    if city or region:
        store_match = []
        if city:
            store_match.append(Store.city.in_(_text_list(city)))
        if region:
            store_match.append(Store.region.in_(_text_list(region)))
        target_conditions.append(
            BroadcastTarget.store_id.in_(
                db.query(Store.id).filter(*store_match).scalar_subquery()))
    if target_conditions:
        query = query.filter(
            db.query(BroadcastTarget.id)
              .filter(BroadcastTarget.session_id == BroadcastSession.id,
                      *target_conditions)
              .exists())

    if archived_only:
        query = query.filter(BroadcastSession.archived_at.isnot(None))
    elif not include_archived:
        query = query.filter(BroadcastSession.archived_at.is_(None))

    scope = resolve_store_scope(engine, user)
    if scope is not None:
        query = query.filter(
            db.query(BroadcastTarget.id)
              .filter(BroadcastTarget.session_id == BroadcastSession.id,
                      BroadcastTarget.store_id.in_(scope) if scope
                      else BroadcastTarget.store_id.in_([-1]))
              .exists())

    total = query.count()
    # ORDER BY from an allowlist. The default stays newest-first, which is what
    # a history is for; a named column replaces it rather than adding to it.
    orderable = {
        "id": BroadcastSession.id,
        "campaign_name": BroadcastSession.campaign_name,
        "target_mode": BroadcastSession.target_mode,
        # "Targets" is the count the row displays, and "Duration" is derived
        # from the two timestamps - so both are sorted by what they are made
        # of rather than by a column that does not exist.
        "targets": BroadcastSession.online_store_count,
        "duration": BroadcastSession.ended_at,
        "started_at": BroadcastSession.started_at,
        "ended_at": BroadcastSession.ended_at,
        "status": BroadcastSession.status,
        "started_by": BroadcastSession.started_by_display_name,
    }
    if sort in orderable:
        column = orderable[sort]
        # Case-insensitive on the text columns, for the same reason as
        # everywhere else: sorted by raw bytes a list looks broken to the
        # person reading it.
        key = (func.lower(column)
               if sort in ("campaign_name", "status", "started_by",
                           "target_mode") else column)
        ordering = key.desc() if str(dir).lower() == "desc" else key.asc()
    else:
        ordering = BroadcastSession.id.desc()
    rows = apply_paging(query.order_by(ordering), page, page_size).all()
    # The SAME recording attachment the unpaginated /broadcast/history applies.
    #
    # This is the bug this route had: History in the browser reads THIS
    # endpoint, not that one, so every row arrived with recording=null and the
    # page said "No recording" for seven broadcasts whose audio was on disk and
    # whose metadata said AVAILABLE. Two endpoints returning the same shape had
    # drifted, and only one of them was ever looked at.
    #
    # Attached through the shared helper rather than repeated here, so the two
    # cannot drift again.
    # The SAME recording attachment the unpaginated /broadcast/history applies.
    #
    # This is the bug this route had: History in the browser reads THIS
    # endpoint, not that one, so every row arrived with recording=null and the
    # page said "No recording" for seven broadcasts whose audio was on disk and
    # whose metadata said AVAILABLE. Two endpoints returning the same shape had
    # drifted, and only one of them was ever looked at.
    #
    # Attached through the shared helper rather than repeated here, so the two
    # cannot drift again.
    attached = {row.id: row for row in _with_recordings(rows)}
    return Page(items=rows, total=total, page=page, page_size=page_size).as_dict(
        lambda row: attached[row.id].model_dump(mode="json"))


# ================ WEBSOCKETS ================
def _persist_receiver_ack(store_id: int, acknowledgement, received_at: datetime) -> None:
    """Persist meaningful transitions only; heartbeat freshness remains in memory."""
    if isinstance(acknowledgement, HeartbeatAcknowledgement):
        return

    event_type = acknowledgement.type
    details = None
    if isinstance(acknowledgement, (PlaybackErrorAcknowledgement, DeviceErrorAcknowledgement)):
        # Persist the bounded code, not receiver-supplied free text that could contain secrets.
        details = acknowledgement.error_code

    with SessionLocal() as db:
        db.add(ReceiverEvent(store_id=store_id, event_type=event_type, details=details))
        if isinstance(
            acknowledgement,
            (AudioReceivingAcknowledgement, PlaybackConfirmedAcknowledgement,
             PlaybackErrorAcknowledgement, StoppedAcknowledgement),
        ):
            target = db.query(BroadcastTarget).filter(
                BroadcastTarget.store_id == store_id,
                BroadcastTarget.session_id == acknowledgement.session_id,
            ).first()
            if target is not None:
                if isinstance(acknowledgement, AudioReceivingAcknowledgement):
                    target.play_status = "audio_receiving"
                    target.started_playing_at = None
                elif isinstance(acknowledgement, PlaybackConfirmedAcknowledgement):
                    target.play_status = "playback_confirmed"
                    target.started_playing_at = received_at
                    target.error_message = None
                elif isinstance(acknowledgement, PlaybackErrorAcknowledgement):
                    target.play_status = "playback_error"
                    target.error_message = details
                elif isinstance(acknowledgement, StoppedAcknowledgement):
                    target.play_status = "stopped"
                    target.stopped_at = received_at
        db.commit()


def _receiver_rejection_code(error: Exception) -> str:
    if isinstance(error, DuplicateMessageError):
        return "DUPLICATE_MESSAGE"
    if isinstance(error, NonMonotonicSequenceError):
        return "NON_MONOTONIC_SEQUENCE"
    if isinstance(error, WrongSessionError):
        return "WRONG_SESSION"
    if isinstance(error, (ValidationError, ValueError)):
        return "INVALID_ACKNOWLEDGEMENT"
    return "INVALID_TRANSITION"


def _receiver_bearer_token(websocket: WebSocket) -> str | None:
    headers = websocket.headers
    getlist = getattr(headers, "getlist", None)
    values = getlist("authorization") if getlist is not None else []
    if values and len(values) != 1:
        return None
    authorization = values[0] if values else headers.get("authorization")
    if not isinstance(authorization, str):
        return None
    parts = authorization.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    candidate = parts[1]
    if len(candidate) > MAX_RECEIVER_TOKEN_LENGTH:
        return None
    try:
        candidate.encode("ascii")
    except UnicodeEncodeError:
        return None
    return candidate


async def _reject_receiver_authentication(websocket: WebSocket) -> None:
    await websocket.close(
        code=RECEIVER_AUTH_FAILURE_CODE,
        reason=RECEIVER_AUTH_FAILURE_REASON,
    )


def _receiver_runtime_dependencies(websocket: WebSocket):
    runtime_app = getattr(websocket, "app", None)
    if runtime_app is None:
        scope = getattr(websocket, "scope", None)
        if isinstance(scope, dict):
            runtime_app = scope.get("app")
    state = getattr(runtime_app, "state", None)
    authenticator = getattr(
        state,
        "receiver_runtime_authenticator",
        default_receiver_runtime_authenticator,
    )
    connection_manager = getattr(
        state,
        "receiver_connection_manager",
        manager,
    )
    return authenticator, connection_manager


@app.websocket("/api/ws/receiver")
async def ws_receiver(websocket: WebSocket):
    candidate = _receiver_bearer_token(websocket)
    if candidate is None:
        await _reject_receiver_authentication(websocket)
        return

    authenticated_at = datetime.now(timezone.utc)
    authenticator, connection_manager = _receiver_runtime_dependencies(websocket)
    try:
        identity = authenticator.authenticate(
            presented_token=candidate,
            authenticated_at=authenticated_at,
        )
    except Exception:
        await _reject_receiver_authentication(websocket)
        return

    try:
        db = SessionLocal()
        try:
            store = (
                db.query(Store)
                .filter(Store.id == identity.store_id, Store.is_active.is_(True))
                .first()
            )
            if not store:
                raise LookupError
            store_id = store.id
        finally:
            db.close()
    except Exception:
        await _reject_receiver_authentication(websocket)
        return

    connection_id = uuid.uuid4().hex

    try:
        # Which computer in this Store actually plays the announcement.
        #
        # A legacy Receiver has no Device identity, so it keeps the old
        # one-connection-per-Store behaviour unchanged. An enrolled Device is
        # primary only if an administrator promoted it: a Store with no primary
        # receives no audio, rather than the announcement landing on whichever
        # computer happened to connect first.
        # The policy engages only once an administrator has designated a primary.
        # Until then the Store behaves exactly as it always has: one connection,
        # and it receives audio. Any other reading would mean that enrolling a
        # Device took its Store off the air until somebody clicked Promote, which
        # is not an upgrade anybody would survive across 44 Stores.
        #
        # Nothing here writes a primary row. A Store without one stays without
        # one; it is simply not yet under the policy.
        is_primary = True
        demote_superseded_device = False
        if identity.device_id is not None:
            primary_id = primary_device_id(engine, store_id=store_id)
            if primary_id is not None:
                is_primary = primary_id == identity.device_id
                # Only a genuine promotion demotes the outgoing socket. Everything
                # else - including a reconnect that maps to the backfilled Device -
                # is an ordinary replacement.
                demote_superseded_device = is_primary

        await connection_manager.connect_receiver(
            store_id,
            websocket,
            connection_id,
            authenticated_at,
            authentication_source=identity.authentication_source,
            device_id=identity.device_id,
            credential_id=identity.credential_id,
            is_primary=is_primary,
            demote_superseded_device=demote_superseded_device,
        )

        # Preserve the existing runtime health write, but only after the exact
        # accepted connection has been registered successfully.
        #
        # A standby is deliberately excluded. "Store online" means the computer
        # carrying this Store's audio is answering, and a standby carries none of
        # it: letting a spare machine set status='online' and refresh last_seen is
        # how HQ ends up showing a green Store with silent speakers.
        if is_primary:
            db = SessionLocal()
            try:
                connected_store = db.query(Store).filter(Store.id == store_id).first()
                if connected_store:
                    connected_store.last_seen = authenticated_at
                    connected_store.status = "online"
                    db.add(ReceiverEvent(store_id=store_id, event_type="connected"))
                    db.commit()
            finally:
                db.close()

        # If a broadcast is reaching this Store, send PLAY immediately so a
        # Receiver that reconnected mid-announcement rejoins it.
        #
        # Which broadcast is asked for by Store rather than assumed: with
        # concurrent sessions there is no such thing as "the" live session, and
        # telling a Receiver to rejoin the wrong one would have it play - and
        # acknowledge against - a campaign that was never targeting it. At most
        # one session can match, because the Store lease makes that true.
        rejoining_session_id = connection_manager.broadcasts.session_id_for_store(store_id)
        if rejoining_session_id is not None:
            connection_manager.prepare_receiver_session(store_id, rejoining_session_id)
            await connection_manager.send_to_receiver(
                store_id,
                {
                    "type": "play",
                    "session_id": rejoining_session_id,
                },
            )

        while True:
            if not connection_manager.is_current_receiver_connection(
                store_id,
                websocket,
                connection_id,
                device_id=identity.device_id,
            ):
                break
            try:
                msg = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=HEARTBEAT_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                if not connection_manager.is_current_receiver_connection(
                    store_id,
                    websocket,
                    connection_id,
                    device_id=identity.device_id,
                ):
                    break
                # A standby's silence is its own. Sweeping the Store's snapshot from
                # a standby's idle timer would let the spare machine's liveness
                # decide what HQ believes about the primary.
                if connection_manager.is_registered_standby(identity.device_id):
                    standby_state = connection_manager.evaluate_standby_freshness(
                        identity.device_id
                    )
                    if standby_state.connection is ConnectionState.OFFLINE:
                        await websocket.close(code=4408)
                        break
                    continue
                before = connection_manager.get_receiver_snapshot(store_id)
                after = connection_manager.evaluate_receiver_freshness(store_id)
                if before is not None and after.connection is not before.connection:
                    await connection_manager.notify_dashboards({
                        "type": "receiver_status",
                        "store_id": store_id,
                        "status": after.connection.value.lower(),
                    })
                if after.connection is ConnectionState.OFFLINE:
                    await websocket.close(code=4408)
                    break
                continue

            if not connection_manager.is_current_receiver_connection(
                store_id,
                websocket,
                connection_id,
                device_id=identity.device_id,
            ):
                break
            received_at = datetime.now(timezone.utc)
            try:
                data = json.loads(msg)
                if not isinstance(data, dict):
                    raise ValueError("receiver acknowledgement must be an object")

                # Speaker reports are handled BEFORE the session contract
                # parser, deliberately.
                #
                # They are not acknowledgements of anything: they carry no
                # session, no command id and no sequence, and they arrive when
                # no broadcast is running at all - which is most of the time.
                # Pushing them through a parser built for session
                # acknowledgements would mean either inventing a fake session
                # for them or loosening a validator that exists to be strict.
                if data.get("type") == "store_volume":
                    # What the shop's own speaker is set to, reported by the
                    # Receiver whenever somebody at the till changes it. It
                    # carries no session because there need not be one: a shop
                    # plays recorded announcements all day without ever
                    # preparing a broadcast, and its volume is still a fact.
                    try:
                        STORE_MASTER_VOLUME[store_id] = {
                            "volume_percent": int(data.get("volume_percent")),
                            "muted": bool(data.get("muted")),
                        }
                    except (TypeError, ValueError):
                        pass
                    continue

                if data.get("type") == "receiver_version":
                    # Kept in memory beside the connection: it describes the
                    # process on the other end of THIS socket, and a restart
                    # of either side makes it a fresh question.
                    RECEIVER_VERSIONS[store_id] = str(
                        data.get("version") or "unknown")[:40]
                    continue

                if data.get("type") in ("output_devices", "output_device_result"):
                    _handle_output_device_report(store_id, data)
                    continue

                # Announcement answers, for the same reason and in the same
                # place. They carry no session, no command id and no sequence
                # - an announcement runs for days with no broadcast anywhere
                # near it - so the session-contract parser below would reject
                # them, which is exactly what it was doing: the Store said
                # "announcement_failed" and HQ went on showing PLAYING.
                if data.get("type") in announcement_protocol.ACKNOWLEDGEMENTS:
                    try:
                        announcement_service.record_acknowledgement(
                            engine, store_id=store_id, kind=data["type"],
                            error=str(data.get("error") or ""))
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "Could not record an announcement acknowledgement "
                            "from Store %s", store_id)
                    if data.get("type") == announcement_protocol.ACK_FAILED:
                        # Logged at warn, because it is the shop telling us it
                        # is silent - and that is not a debug detail.
                        logger.warning(
                            "Store %s could not play the announcement: %s",
                            store_id, str(data.get("error") or "")[:300])
                    continue
                is_standby_ack = connection_manager.is_registered_standby(
                    identity.device_id
                )
                acknowledgement, _ = connection_manager.apply_receiver_payload(
                    store_id,
                    data,
                    received_at,
                    device_id=identity.device_id,
                )
            except (ReceiverContractError, ValidationError, ValueError, json.JSONDecodeError) as error:
                code = _receiver_rejection_code(error)
                await websocket.send_text(json.dumps({"type": "ack_rejected", "code": code}))
                continue

            # A standby's acknowledgement is not the Store's history. Persisting it
            # would refresh the Store's last_seen and file the standby's readiness
            # and playback claims against the Store, which is the same
            # misattribution the in-memory routing above exists to prevent - and
            # the persisted rows are what an operator reads afterwards.
            if is_standby_ack:
                continue

            # What the Store's Windows output is actually doing. Telemetry,
            # so it updates the ACTUAL fields only and never a command id: a
            # person at the till moving the slider does not retract the
            # operator's request and must not look like a reply to it.
            if isinstance(acknowledgement, EndpointStateAcknowledgement):
                # What the Store's Windows output is actually doing, while a
                # broadcast is running. Telemetry, so it updates the ACTUAL
                # fields only and never a command id: a person at the till
                # moving the slider does not retract the operator's request and
                # must not look like a reply to it.
                # ONE PLACE FOR "what is this shop's speaker set to".
                #
                # A broadcast reports it inside its session, and a Receiver
                # reports it outside one - and the Announcements console and
                # the Broadcast Console must never disagree about it. Both
                # readings land here.
                STORE_MASTER_VOLUME[store_id] = {
                    "volume_percent": acknowledgement.volume_percent,
                    "muted": bool(acknowledgement.muted),
                }
                observed = store_audio_registry.observe_endpoint_state(
                    session_id=acknowledgement.session_id,
                    store_id=store_id,
                    state_sequence=acknowledgement.state_sequence,
                    volume_percent=acknowledgement.volume_percent,
                    muted=acknowledgement.muted,
                )
                # None means stale or not ours; dashboards hear nothing, so a
                # delayed reading cannot drag a Console backwards.
                if observed is not None:
                    await connection_manager.notify_dashboards({
                        "type": "store_audio_state",
                        "session_id": acknowledgement.session_id,
                        "store_id": store_id,
                        **observed.as_dict(),
                    })
                continue

            # Output-volume acknowledgements are live control state, not
            # Store history: they say how loud a shop is for the next few
            # minutes. Recording them in the registry and NOT in the database
            # is what keeps a slider drag from writing rows.
            if isinstance(acknowledgement, AudioControlAcknowledgement):
                updated = store_audio_registry.acknowledge(
                    session_id=acknowledgement.session_id,
                    store_id=store_id,
                    command_id=acknowledgement.command_id,
                    result=acknowledgement.result,
                    applied_volume_percent=acknowledgement.applied_volume_percent,
                    applied_muted=acknowledgement.applied_muted,
                    output_device=acknowledgement.output_device,
                    error_code=acknowledgement.error_code,
                    error_message=acknowledgement.details,
                )
                # None means the acknowledgement was stale or unknown - an
                # answer to a question a newer command has already replaced.
                # Dashboards are told nothing, so a late ACK cannot walk a
                # slider backwards.
                if updated is not None:
                    await connection_manager.notify_dashboards({
                        "type": "store_audio_control",
                        "session_id": acknowledgement.session_id,
                        "store_id": store_id,
                        **updated.as_dict(),
                    })
                continue

            _persist_receiver_ack(store_id, acknowledgement, received_at)
            if isinstance(acknowledgement, (PlaybackErrorAcknowledgement, DeviceErrorAcknowledgement)):
                await connection_manager.notify_dashboards({
                    "type": "receiver_error",
                    "store_id": store_id,
                    "code": acknowledgement.error_code,
                })
    except ReceiverConnectionInventoryError:
        await websocket.close(
            code=RECEIVER_CONNECTION_FAILURE_CODE,
            reason=RECEIVER_CONNECTION_FAILURE_REASON,
        )
    except WebSocketDisconnect:
        pass
    except Exception as error:
        logger.warning(
            "Receiver WS closed after %s for store=%s",
            type(error).__name__,
            store_id,
        )
    finally:
        disconnected_current = connection_manager.disconnect_receiver(
            store_id,
            websocket,
            connection_id,
            device_id=identity.device_id if identity is not None else None,
        )
        if disconnected_current:
            db = SessionLocal()
            try:
                s = db.query(Store).filter(Store.id == store_id).first()
                if s:
                    s.status = "offline"
                    db.add(ReceiverEvent(store_id=store_id, event_type="disconnected"))
                    db.commit()
            finally:
                db.close()
            await connection_manager.notify_dashboards({
                "type": "receiver_status",
                "store_id": store_id,
                "status": "offline",
            })


@app.websocket("/api/ws/hq")
async def ws_hq(websocket: WebSocket, ticket: str = Query(...)):
    # A single-use ticket, not a reusable JWT: Uvicorn logs this URL in full.
    # Pinned to this socket's audience, so an uplink ticket cannot open it.
    try:
        user_id = ws_ticket_store.redeem(ticket, audience=AUDIENCE_HQ)
    except TicketRejected:
        await websocket.close(code=4401)
        return

    hq_id = f"{user_id}:{uuid.uuid4().hex[:8]}"
    await manager.connect_hq(hq_id, websocket)
    try:
        while True:
            # HQ dashboard doesn't send data (yet). This WS is for server->client push.
            msg = await websocket.receive_text()
            _ = msg  # ignored
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"HQ dashboard WS error: {e}")
    finally:
        manager.disconnect_hq(hq_id, websocket)


@app.websocket("/api/ws/broadcaster")
async def ws_broadcaster(websocket: WebSocket, ticket: str = Query(...),
                         session_id: int = Query(...)):
    """HQ mic audio uplink, bound to exactly ONE broadcast session.

    THE MOST PRIVILEGED SOCKET IN THE SYSTEM, and it used to be the least
    guarded: it redeemed a ticket, THREW THE USER ID AWAY, and accepted audio.
    No permission, no role lookup, no re-read. Any authenticated account -
    including a read-only VIEWER refused by every broadcast HTTP route - could
    push arbitrary audio to the loudspeakers of every targeted Store, or occupy
    this single slot and deny it to whoever was allowed to use it.

    Three checks now, deliberately all three: the ticket must have been minted
    FOR this socket (so a dashboard ticket is refused), the account it was
    minted for must STILL hold START_BROADCAST when the handshake arrives (a
    permission verified only at mint time is verified once, and an operator can
    be demoted in the seconds between minting and connecting), and the session
    named in the URL must be LIVE AND OWNED BY THAT ACCOUNT.

    The third is what stops the URL-editing attack. The socket used to discard
    every trace of which broadcast it belonged to and stream into whatever the
    singleton currently targeted, so with concurrent sessions any valid ticket
    holder could have fed audio into somebody else's announcement by changing a
    number. The session id is supplied by the browser and is therefore never
    trusted for anything except lookup: ownership is re-read from the database
    and compared against the authenticated account.
    """
    # A single-use ticket, not a reusable JWT: Uvicorn logs this URL in full.
    try:
        user_id = ws_ticket_store.redeem(ticket, audience=AUDIENCE_BROADCASTER)
    except TicketRejected:
        await websocket.close(code=4401)
        return

    # Re-read the account rather than trusting the ticket. Closing with 4403
    # rather than 4401 so an operator can tell "not allowed" from "bad ticket".
    with SessionLocal() as db:
        account = db.query(HQUser).filter(HQUser.id == int(user_id)).first() \
            if str(user_id).isdigit() else None
        if account is None or not has_permission(account, Permission.START_BROADCAST):
            await websocket.close(code=4403)
            return

    # Ownership, re-read rather than taken from the URL. 4404 for "not yours
    # or not live" without distinguishing the two: telling a caller which one
    # it was would confirm that somebody else's session exists.
    with SessionLocal() as db:
        session = db.query(BroadcastSession).filter(
            BroadcastSession.id == session_id).first()
        # The host, OR somebody this broadcast has admitted as a second voice.
        #
        # JOINED and nothing else - re-read here, not taken from anything the
        # browser sent. This single line is the whole of group broadcasting:
        # the request, the approval and the participant list are bookkeeping
        # around it, and a REQUESTED or DENIED account that could still push
        # audio would make every one of them decoration.
        admitted = (session is not None
                    and (session.started_by == int(user_id)
                         or broadcast_group.is_on_air(engine, session_id=session_id,
                                                      user_id=int(user_id))))
        if session is None or session.status != "live" or not admitted:
            await websocket.close(code=4404)
            return

    await websocket.accept()
    ok = await manager.broadcasts.attach_broadcaster(
        session_id, websocket, owner_user_id=int(user_id))
    if not ok:
        # This session already has a microphone. The first socket keeps it:
        # replacing it would let a second tab evict the operator who is
        # mid-announcement.
        await websocket.send_text(
            '{"type":"error","message":"This broadcast already has an active microphone"}')
        await websocket.close(code=4409)
        return

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                # Routed by THIS socket's session, never by a global target
                # set - which is what kept one operator's audio out of
                # another's Stores.
                await manager.fanout_audio(session_id, data)
                # AFTER the fan-out, and never awaited on the disk. offer()
                # only puts the chunk in a bounded queue; a background task
                # does the file work. A slow disk costs a truthful recording
                # status, never a delayed announcement.
                recorder = _RECORDING_WRITERS.get(session_id)
                if recorder is not None:
                    recorder.offer(data)
                # The web audience is the third sibling sink, and the same rule
                # applies: offer() frames the stream and fills bounded
                # per-listener queues, and never awaits a listener socket. A
                # stream it cannot frame costs web listeners only - the Stores
                # above and the recording beside it are already served.
                relay = manager.broadcasts.web_relay(session_id)
                if relay is not None:
                    relay.offer(data)
            # text messages ignored for now
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"Broadcaster WS error: {e}")
    finally:
        # Detach FIRST, and only if this socket still holds the slot, so a late
        # cleanup from a superseded socket cannot evict its replacement.
        detached = await manager.broadcasts.detach_broadcaster(session_id, websocket)
        # Safety: if THIS session is still live when its microphone drops, stop
        # it. Only this one - every other broadcast keeps running.
        if detached and manager.is_live(session_id):
            db = SessionLocal()
            try:
                session = db.query(BroadcastSession).filter(
                    BroadcastSession.id == session_id).first()
                if session and session.status == "live":
                    await _end_session(db, session, "ended", reason="broadcaster_disconnected")
                    _write_log(db, "warn", f"Session #{session.id} auto-stopped: broadcaster disconnected")
            finally:
                db.close()


# ================ WEB AUDIENCE (HQ side) ================
#
# Managing a room is managing YOUR OWN Broadcast's audience. Ownership is the
# gate rather than a new permission: approving, denying and removing listeners
# are as much a part of running your announcement as stopping it is, and a
# second permission would only ever say yes wherever broadcast.start already
# does.
#
# Deliberately NOT satisfied by broadcast.stop_any or broadcast.active_view, for
# the same reason store volume is not: a supervisor entitled to END somebody's
# broadcast is not thereby entitled to sit inside it deciding who may listen.


def _require_web_room_owner(sid: int, user: HQUser) -> None:
    """The signed-in account must own this Broadcast."""
    with SessionLocal() as db:
        session = db.query(BroadcastSession).filter(
            BroadcastSession.id == sid).first()
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        if session.started_by != user.id:
            # 403 rather than 404: the caller is authenticated and the session
            # exists. It is simply not theirs.
            raise HTTPException(
                status_code=403,
                detail="You can only manage the web audience of your own Broadcast.")


def _room_or_404(sid: int):
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    if room is None:
        raise HTTPException(status_code=404, detail="This Broadcast has no web room")
    return room


def _room_state(sid: int, *, room) -> dict:
    """Everything the console needs to draw the Web Audience panel."""
    participants = web_rooms.list_participants(engine, room_id=room.id)
    waiting = [row for row in participants
               if row.admission_status == web_rooms.AdmissionStatus.REQUESTED]
    admitted = [row for row in participants if row.is_admitted]
    counts = web_participants.counts_for_room(room.id)

    relay = manager.broadcasts.web_relay(sid)
    # A stream the framer cannot read costs web listeners and nothing else, so
    # the panel says so rather than the Broadcast pretending it is fine.
    delivery = "unavailable" if (relay is not None and relay.degraded_reason) else "ok"

    def runtime_for(participant_id: int) -> dict:
        runtime = web_participants.get(participant_id)
        if runtime is None:
            # Admitted but never connected, or connected and gone. Both are
            # real states and neither is "listening".
            return {"connected": False,
                    "playback_state": PlaybackState.DISCONNECTED,
                    "seconds_since_seen": None, "stale": False}
        return runtime.public_dict()

    return {
        "public_code": room.public_code,
        "status": room.status,
        "auto_approve": room.auto_approve,
        "delivery": delivery,
        # Present only on the page lifetime that generated or rotated it. There
        # is no column it could be read back from, and a masked placeholder
        # would imply the application knows something it does not.
        "password": _ROOM_PASSWORD_ONCE.get(sid) or None,
        "password_configured": True,
        "password_rotated_at": room.password_rotated_at,
        "counts": {
            "waiting": len(waiting),
            "admitted": len(admitted),
            "connected": counts["connected"],
            "listening": counts["listening"],
            "buffering": counts["buffering"],
            "paused": counts["paused"],
        },
        "waiting": [row.public_dict() for row in waiting],
        "listeners": [{**row.public_dict(), **runtime_for(row.id)}
                      for row in admitted],
    }


@api.get("/broadcast/sessions/{sid}/web-room")
def read_web_room(sid: int, user: HQUser = Depends(require(Permission.START_BROADCAST))):
    _require_web_room_owner(sid, user)
    return _room_state(sid, room=_room_or_404(sid))


@api.post("/broadcast/sessions/{sid}/web-room/password/rotate")
def rotate_web_room_password(
    sid: int, user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """Replace the join password and return the new plaintext, once.

    Already-admitted listeners are untouched: the password governs who may
    still come in, and removing the audience is what Kick is for.
    """
    _require_web_room_owner(sid, user)
    _room_or_404(sid)
    try:
        password = web_rooms.rotate_password(engine, session_id=sid)
    except web_rooms.RoomNotOpenError:
        raise HTTPException(status_code=409, detail="This Broadcast has ended")
    _ROOM_PASSWORD_ONCE[sid] = password
    with SessionLocal() as db:
        # The EVENT is recorded. The password never is.
        _write_log(db, "info", f"Web room password rotated for session #{sid}")
    return _room_state(sid, room=_room_or_404(sid))


@api.put("/broadcast/sessions/{sid}/web-room/auto-approve")
def set_web_room_auto_approve(
    sid: int, payload: WebRoomAutoApproveUpdate,
    user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    _require_web_room_owner(sid, user)
    _room_or_404(sid)
    try:
        web_rooms.set_auto_approve(engine, session_id=sid,
                                   enabled=payload.auto_approve)
    except web_rooms.RoomNotOpenError:
        raise HTTPException(status_code=409, detail="This Broadcast has ended")
    return _room_state(sid, room=_room_or_404(sid))


@api.get("/broadcast/sessions/{sid}/web-participants")
def list_web_participants(sid: int,
                          user: HQUser = Depends(require(Permission.START_BROADCAST))):
    _require_web_room_owner(sid, user)
    return _room_state(sid, room=_room_or_404(sid))


@api.post("/broadcast/sessions/{sid}/web-participants/{pid}/approve")
async def approve_web_participant(
    sid: int, pid: int, user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    _require_web_room_owner(sid, user)
    room = _room_or_404(sid)
    try:
        participant, token = web_rooms.approve_participant(
            engine, room_id=room.id, participant_id=pid)
    except web_rooms.ParticipantNotAdmissibleError as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    # The waiting browser is polling its own admission state, so it collects the
    # token without a refresh. The token is NEVER returned to the broadcaster -
    # it is that listener's credential, not theirs.
    if token is not None:
        _PENDING_LISTENER_TOKENS[participant.id] = token
    return _room_state(sid, room=room)


@api.post("/broadcast/sessions/{sid}/web-participants/{pid}/deny")
async def deny_web_participant(
    sid: int, pid: int, user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    _require_web_room_owner(sid, user)
    room = _room_or_404(sid)
    try:
        web_rooms.deny_participant(engine, room_id=room.id, participant_id=pid)
    except web_rooms.ParticipantNotAdmissibleError as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    _PENDING_LISTENER_TOKENS.pop(pid, None)
    return _room_state(sid, room=room)


@api.post("/broadcast/sessions/{sid}/web-participants/{pid}/kick")
async def kick_web_participant(
    sid: int, pid: int, user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """Remove one listener: invalidate the session, THEN close the socket.

    In that order. Marking the row first means that even if the close races a
    reconnect, the reconnect has nothing valid left to present.
    """
    _require_web_room_owner(sid, user)
    room = _room_or_404(sid)
    try:
        web_rooms.kick_participant(engine, room_id=room.id, participant_id=pid)
    except web_rooms.ParticipantNotAdmissibleError as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))

    _PENDING_LISTENER_TOKENS.pop(pid, None)
    socket = web_participants.socket_for(pid)
    web_participants.detach(participant_id=pid)
    relay = manager.broadcasts.web_relay(sid)
    if relay is not None:
        await relay.remove_listener(str(pid))
    if socket is not None:
        await _close_listener_socket(socket, {"type": "kicked"})
    return _room_state(sid, room=room)


# ================ PUBLIC LISTENER SURFACE ================
#
# Everything below is reachable WITHOUT an HQ account. That is the whole point,
# and it is also why each route is deliberately narrow.
#
# The listener credential these routes issue is scoped to one participant in one
# room. It is not an HQ session, it carries no permission code, and no route
# outside this section accepts it - so a listener holding one cannot read a
# Store, a Device, a User or another Broadcast. It travels in an HttpOnly
# cookie, never in a URL: Uvicorn logs request lines in full, and a credential
# in a query string is a credential in a log file and in browser history.

#: How long an operator waits for a Store added mid-broadcast to report READY.
#:
#: Bounded because somebody is standing at a console. Long enough for a
#: Receiver to run its real FFmpeg and output-device checks, short enough that
#: a wedged Store is reported rather than left spinning - and on timeout the
#: lease is released, so a slow Store never silently holds itself hostage.
ADD_STORE_READY_TIMEOUT_SECONDS = 20.0

#: A heartbeat is a few dozen bytes. Anything larger is not one.
MAX_LISTENER_FRAME_BYTES = 1024

#: Signs the pending-claim handle. Process-local on purpose: a claim is
#: only meaningful while the Broadcast that issued it is running, and a
#: restart ends the Broadcast's runtime anyway.
PENDING_CLAIM_SECRET = secrets.token_urlsafe(32)

LISTENER_COOKIE = "speaklink_listener"
#: A listener session lives as long as the Broadcast, and a Broadcast is an
#: announcement rather than a working day. Ending the room clears the token hash
#: anyway, so this is a backstop rather than the primary control.
LISTENER_COOKIE_MAX_AGE = 6 * 60 * 60

#: Kept only so an operator who set it does not get an error; the cookie policy
#: is now decided per request from the scheme the browser actually used, which
#: needs no configuration and cannot be forgotten. See _listener_cookie_kwargs.
LISTENER_COOKIE_INSECURE = os.getenv("SPEAKLINK_LAN_HTTP_LISTENERS", "") == "1"


def _request_is_https(request: Request) -> bool:
    """Whether the BROWSER reached us over HTTPS.

    A forwarded protocol header is honoured only when a proxy is explicitly
    trusted, for the same reason X-Forwarded-For is: otherwise any caller could
    assert it.
    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-proto", "")
        if forwarded:
            return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


def _listener_cookie_kwargs(request: Request) -> dict:
    """Cookie flags for the listener session, decided per request.

    ``secure`` follows the SCHEME THE BROWSER ACTUALLY USED, and this is the fix
    for a release-blocking defect rather than a preference.

    It used to be a global env flag, defaulting to Secure. On the LAN pilot HQ
    is reached over plain HTTP at 192.168.x.x, and Chromium refuses to store a
    Secure cookie from an untrustworthy origin - so the listener session was
    dropped by the browser and every request arrived anonymous. A password join
    then failed its WebSocket handshake and buffered for ever, and an approved
    listener polling its own state got 401 and was shown "Broadcast ended".

    None of that reproduced in the test suite, because http://localhost IS a
    trustworthy origin and keeps Secure cookies. The tests were right about the
    code and wrong about the world.

    Deciding per request keeps production strict without a global switch: served
    over HTTPS the cookie is Secure, served over plain HTTP on a LAN it is not,
    and no deployment has to remember to set anything. HttpOnly, SameSite and
    the /api/listen path are unchanged and unconditional.
    """
    return {
        "httponly": True,          # never readable by page script
        "secure": _request_is_https(request),
        # Lax, not None: the listener app is same-origin with HQ, so nothing
        # needs to send this cookie cross-site, and SameSite=None would hand it
        # to every third-party page that can make the browser issue a request.
        "samesite": "lax",
        "max_age": LISTENER_COOKIE_MAX_AGE,
        "path": "/api/listen",
    }


def _client_key(request: Request) -> str:
    return "listen:" + client_identifier(
        request.client.host if request.client else None,
        request.headers.get("x-forwarded-for"),
        trust_proxy=TRUST_PROXY_HEADERS,
    )


def _refuse_if_limited(limiter, key: str) -> None:
    wait = limiter.retry_after(key)
    if wait is not None:
        raise HTTPException(status_code=429,
                            detail="Too many attempts. Please wait and try again.",
                            headers={"Retry-After": str(wait)})


def _public_room_or_404(public_code: str):
    """Resolve a public code, revealing nothing about what does not exist.

    An unknown code and an ended room deliberately answer the same way, because
    a caller able to tell them apart can probe for which Broadcasts have
    existed. Nothing here returns the internal session id.
    """
    room = web_rooms.find_room_by_public_code(engine, public_code=public_code)
    if room is None or not room.is_open:
        raise HTTPException(status_code=404,
                            detail="No live Broadcast with that ID.")
    return room


def _session_is_live(session_id: int) -> bool:
    with SessionLocal() as db:
        session = db.query(BroadcastSession).filter(
            BroadcastSession.id == session_id).first()
        return bool(session and session.status == "live")


def _listener_view(room, participant, *, live: bool) -> dict:
    """What a listener may know. Never a Store, a session id or another person."""
    return {
        "public_code": room.public_code,
        "display_name": participant.display_name,
        "admission_status": participant.admission_status,
        "admitted": participant.is_admitted,
        "broadcast_live": live,
        "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
    }


@api.get("/listen/rooms/{public_code}")
def public_room_lookup(public_code: str, request: Request):
    """The minimum a join form needs: that this Broadcast exists."""
    key = _client_key(request)
    _refuse_if_limited(web_lookup_limiter, key)
    web_lookup_limiter.record_attempt(key)
    room = _public_room_or_404(public_code)
    # Deliberately NOT included: the session id, the campaign name, the
    # broadcaster, the Store count, or anything about physical targets.
    return {
        "public_code": room.public_code,
        "broadcast_live": _session_is_live(room.session_id),
        "auto_approve": room.auto_approve,
    }


@api.post("/listen/rooms/{public_code}/join")
def public_room_join(public_code: str, payload: ListenerJoin, request: Request,
                     response: Response):
    """Join with the password. Correct password IS the authorisation.

    A wrong password is refused as a wrong password. It is never quietly
    converted into a join request: the listener asked to use a credential they
    believe they have, and turning that into "waiting for approval" hides a
    typo behind an unrelated outcome.
    """
    key = _client_key(request)
    _refuse_if_limited(web_join_limiter, key)
    web_join_limiter.record_attempt(key)

    room = _public_room_or_404(public_code)
    try:
        name = web_rooms.normalise_display_name(payload.display_name)
    except web_rooms.InvalidDisplayNameError as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    # Already admitted to THIS room in this browser? Resume rather than admit
    # a second time.
    #
    # A refresh is not a new person. Creating another participant would leave
    # the console showing one row Listening and a duplicate row not connected,
    # for one listener - which is exactly what manual testing saw. Identity is
    # the session, never the display name, so a different browser with the same
    # name is still a different participant.
    existing = web_rooms.authenticate_listener(
        engine, token=request.cookies.get(LISTENER_COOKIE))
    if existing is not None and existing[0].id == room.id:
        return _listener_view(existing[0], existing[1],
                              live=_session_is_live(room.session_id))

    # The submitted password is never logged, and never echoed back.
    if not web_rooms.verify_join_password(engine, room=room,
                                          password=payload.password or ""):
        raise HTTPException(status_code=401, detail="Incorrect password.")

    try:
        participant, token = web_rooms.admit_with_password(
            engine, room=room, display_name=name)
    except web_rooms.RoomNotOpenError:
        raise HTTPException(status_code=404, detail="No live Broadcast with that ID.")

    web_join_limiter.forget(key)
    response.set_cookie(LISTENER_COOKIE, token, **_listener_cookie_kwargs(request))
    return _listener_view(room, participant, live=_session_is_live(room.session_id))


@api.post("/listen/rooms/{public_code}/request-access")
def public_room_request_access(public_code: str, payload: ListenerRequestAccess,
                               request: Request, response: Response):
    """Ask to be let in without a password.

    Auto Approve is resolved inside the row-creating transaction, so a toggle
    racing this request produces one participant in one state.
    """
    key = _client_key(request)
    _refuse_if_limited(web_join_limiter, key)
    web_join_limiter.record_attempt(key)

    room = _public_room_or_404(public_code)

    # Same reasoning as the password join: a browser already admitted to this
    # room is resumed, not admitted twice.
    existing = web_rooms.authenticate_listener(
        engine, token=request.cookies.get(LISTENER_COOKIE))
    if existing is not None and existing[0].id == room.id:
        return _listener_view(existing[0], existing[1],
                              live=_session_is_live(room.session_id))

    try:
        name = web_rooms.normalise_display_name(payload.display_name)
    except web_rooms.InvalidDisplayNameError as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    try:
        participant, token = web_rooms.request_access(
            engine, room=room, display_name=name)
    except web_rooms.RoomNotOpenError:
        raise HTTPException(status_code=404, detail="No live Broadcast with that ID.")

    if token is not None:
        # Auto Approve admitted them at once.
        response.set_cookie(LISTENER_COOKIE, token, **_listener_cookie_kwargs(request))
    else:
        # A pending participant still needs an identity to poll with, but it
        # must not be an admitted listener session. A short-lived claim ticket
        # naming only the participant serves that, and carries no authority.
        response.set_cookie(f"{LISTENER_COOKIE}_pending",
                            _pending_claim(participant.id),
                            **_listener_cookie_kwargs(request))
    return _listener_view(room, participant, live=_session_is_live(room.session_id))


def _pending_claim(participant_id: int) -> str:
    """An unguessable handle for a WAITING participant, signed by the server.

    Not a listener session: it authorises nothing and is accepted only by the
    admission-state route below. It exists so a waiting browser can be told it
    has been approved without polling by participant id, which anyone could
    guess.
    """
    digest = hashlib.sha256(
        f"{PENDING_CLAIM_SECRET}:{participant_id}".encode("utf-8")).hexdigest()
    return f"{participant_id}.{digest[:32]}"


def _participant_from_claim(claim: str) -> int | None:
    if not isinstance(claim, str) or "." not in claim:
        return None
    raw_id, _, provided = claim.partition(".")
    if not raw_id.isdigit():
        return None
    expected = _pending_claim(int(raw_id))
    # Constant-time: this is a signature check on an unauthenticated surface.
    if not secrets.compare_digest(claim, expected):
        return None
    return int(raw_id)


def _forget_listener_cookies(request: Request, response: Response) -> None:
    """Drop both listener cookies. They are HttpOnly, so only we can."""
    kwargs = _listener_cookie_kwargs(request)
    for name in (LISTENER_COOKIE, f"{LISTENER_COOKIE}_pending"):
        response.delete_cookie(name, path=kwargs["path"],
                               samesite=kwargs["samesite"],
                               secure=kwargs["secure"], httponly=True)


@api.get("/listen/me")
def listener_admission_state(request: Request, response: Response,
                             public_code: str | None = None):
    """A listener's own state IN ONE ROOM, and nothing about anybody else.

    Serves two callers: an admitted listener holding a session cookie, and a
    waiting one holding a pending claim. The second is how Approve reaches a
    browser without a page refresh.

    ``public_code`` names the Broadcast the browser is actually looking at, and
    state belonging to any OTHER Broadcast answers exactly as if there were no
    session at all.

    Without that this endpoint answered "whatever room this browser last
    touched", which is how a Kick became global: a kicked listener's pending
    claim still resolved, so opening a completely different Broadcast's link
    returned the OLD room's KICKED row and the page said "You were removed from
    this Broadcast" about a Broadcast they had never joined. A Kick removes a
    participant from one room; it is not a ban, and it must not answer for a
    room it was never about.
    """
    def belongs_here(room) -> bool:
        return public_code is None or room.public_code == public_code

    token = request.cookies.get(LISTENER_COOKIE)
    resolved = web_rooms.authenticate_listener(engine, token=token) if token else None
    if resolved is not None and belongs_here(resolved[0]):
        room, participant = resolved
        return _listener_view(room, participant,
                              live=_session_is_live(room.session_id))

    claim = request.cookies.get(f"{LISTENER_COOKIE}_pending")
    participant_id = _participant_from_claim(claim) if claim else None
    if participant_id is None:
        raise HTTPException(status_code=401, detail="Not admitted to a Broadcast.")

    participant = web_rooms.get_participant(engine, participant_id=participant_id)
    if participant is None:
        raise HTTPException(status_code=401, detail="Not admitted to a Broadcast.")
    room = web_rooms.get_room_by_id(engine, room_id=participant.room_id)
    if room is None or not belongs_here(room):
        raise HTTPException(status_code=401, detail="Not admitted to a Broadcast.")

    # Approved while waiting: hand over the listener session now, once. The
    # claim has done its job and is cleared, so it cannot later answer for a
    # room the browser has moved on from.
    if participant.is_admitted:
        pending = _PENDING_LISTENER_TOKENS.pop(participant.id, None)
        if pending is not None:
            response.set_cookie(LISTENER_COOKIE, pending,
                                **_listener_cookie_kwargs(request))
            response.delete_cookie(
                f"{LISTENER_COOKIE}_pending",
                path=_listener_cookie_kwargs(request)["path"],
                samesite="lax", secure=_request_is_https(request), httponly=True)
    return _listener_view(room, participant, live=_session_is_live(room.session_id))


# ================ CHAT: THE WEB AUDIENCE'S ONLY WAY TO ANSWER BACK ================
#
# A listener can hear the announcement and, until now, had no way to say "we
# cannot hear you". These routes are that channel and nothing more.
#
# The host half and the listener half authenticate completely differently - an
# HQ bearer token against a session they own, versus a listener cookie against
# one room - so every rule about who may READ what lives in web_chat.py and is
# shared by both. Two independent filters would eventually disagree, and what
# they would disagree about is somebody's private message.


def _chat_room_or_404(sid: int):
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    if room is None:
        raise HTTPException(status_code=404, detail="This Broadcast has no web room.")
    return room


def _may_see_removed_chat(user: HQUser) -> bool:
    """Whether THIS account may read messages an operator removed.

    Deliberately not "is this your Broadcast". Removing a message is a
    moderation act, and the person who moderates is not automatically the
    person entitled to keep reading what they took down - so the broadcaster
    sees the same tombstone their audience sees, and an account holding
    chat.view_deleted sees the words.
    """
    return has_permission_code(engine, user, "chat.view_deleted")


def _chat_state_for_host(sid: int, room, *, reveal_removed: bool = False) -> dict:
    settings = web_chat.get_settings(engine, room_id=room.id)
    messages = web_chat.history_for_host(engine, room_id=room.id)
    return {"session_id": sid, **settings,
            "may_see_removed": reveal_removed,
            "messages": [m.public_dict(reveal_removed=reveal_removed)
                         for m in messages]}


@api.get("/broadcast/sessions/{sid}/chat")
def read_broadcast_chat(sid: int,
                        user: HQUser = Depends(require(Permission.START_BROADCAST))):
    """Everything said in this room. The host is the one person a private
    message was addressed TO, so private messages are theirs to read."""
    _require_web_room_owner(sid, user)
    return _chat_state_for_host(sid, _chat_room_or_404(sid),
                                reveal_removed=_may_see_removed_chat(user))


@api.post("/broadcast/sessions/{sid}/chat")
def post_broadcast_chat_message(
    sid: int, payload: ChatMessageIn,
    user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """The host speaking to the room.

    Not gated on chat_enabled: turning chat off stops the AUDIENCE typing, and
    an operator may still need to answer the last question before the room
    goes quiet.
    """
    _require_web_room_owner(sid, user)
    room = _chat_room_or_404(sid)
    try:
        message = web_chat.post_host_message(
            engine, room_id=room.id,
            display_name=user.display_name or user.username, body=payload.body)
    except web_chat.ChatRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    return message.public_dict()


@api.put("/broadcast/sessions/{sid}/chat/settings")
def update_broadcast_chat_settings(
    sid: int, payload: ChatSettingsIn,
    user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """Chat on or off, and public or private.

    Changing the mode never rewrites messages already sent. A message sent
    while the room was private was sent in confidence and must not be
    published after the fact; hiding what was already said in public fools
    nobody who was in the room.
    """
    _require_web_room_owner(sid, user)
    room = _chat_room_or_404(sid)
    try:
        if payload.chat_enabled is not None:
            web_chat.set_chat_enabled(engine, room_id=room.id,
                                      enabled=payload.chat_enabled)
        if payload.chat_mode is not None:
            web_chat.set_chat_mode(engine, room_id=room.id, mode=payload.chat_mode)
    except web_chat.ChatRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    with SessionLocal() as db:
        _write_log(db, "info",
                   f"CHAT_SETTINGS session_id={sid} enabled={payload.chat_enabled} "
                   f"mode={payload.chat_mode} by={user.username}")
    return _chat_state_for_host(sid, room,
                                reveal_removed=_may_see_removed_chat(user))


@api.post("/broadcast/sessions/{sid}/chat/messages/{mid}/delete")
def delete_broadcast_chat_message(
    sid: int, mid: int,
    user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """Tombstone one message: the row and the author stay, the words go.

    Everyone in the room already saw it. Removing the row would make the
    transcript claim a conversation that did not happen.
    """
    _require_web_room_owner(sid, user)
    room = _chat_room_or_404(sid)
    if not web_chat.delete_message(engine, message_id=mid, room_id=room.id,
                                   actor_user_id=user.id):
        raise HTTPException(status_code=404,
                            detail="No such message in this Broadcast's chat.")
    # The image FILE stays. Removal takes a message out of the room; it does
    # not erase it, and the host has to be able to say what they removed. It
    # goes when the Broadcast is deleted from history, like everything else
    # here.
    return _chat_state_for_host(sid, room,
                                reveal_removed=_may_see_removed_chat(user))


@api.post("/broadcast/sessions/{sid}/web-participants/{pid}/chat-mute")
def set_web_participant_chat_mute(
    sid: int, pid: int, payload: ChatMuteIn,
    user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """Silence ONE listener without removing them.

    Deliberately not a Kick: somebody being disruptive in chat may still be a
    shop that needs to hear the announcement.
    """
    _require_web_room_owner(sid, user)
    room = _chat_room_or_404(sid)
    participant = web_rooms.get_participant(engine, participant_id=pid)
    if participant is None or participant.room_id != room.id:
        raise HTTPException(status_code=404,
                            detail="That listener is not in this Broadcast.")
    web_chat.set_participant_muted(engine, participant_id=pid, muted=payload.muted)
    return _chat_state_for_host(sid, room,
                                reveal_removed=_may_see_removed_chat(user))


# ---- the listener half -----------------------------------------------------

def _listener_or_401(request: Request):
    """Resolve the browser's listener cookie to (room, participant).

    Every failure - no cookie, unknown token, kicked, denied, room ended -
    answers the same way. A caller learning WHICH is a caller learning
    something about a room they are not in.
    """
    token = request.cookies.get(LISTENER_COOKIE)
    resolved = web_rooms.authenticate_listener(engine, token=token) if token else None
    if resolved is None:
        raise HTTPException(status_code=401, detail="Not admitted to a Broadcast.")
    return resolved


@api.get("/listen/chat")
def read_listener_chat(request: Request):
    """What THIS listener may see: public messages, plus their own private
    ones. The filter is in the query rather than applied afterwards - a filter
    is one early return away from being skipped, and what it would leak is
    somebody else's private message."""
    room, participant = _listener_or_401(request)
    settings = web_chat.get_settings(engine, room_id=room.id)
    messages = web_chat.history_for_listener(
        engine, room_id=room.id, participant_id=participant.id)
    return {
        "public_code": room.public_code,
        **settings,
        "muted": web_chat.is_participant_muted(engine, participant_id=participant.id),
        "me": participant.id,
        "messages": [m.public_dict() for m in messages],
    }


@api.post("/listen/chat")
def post_listener_chat(request: Request, payload: ChatMessageIn):
    """One message from one admitted listener.

    Every refusal here is one the page already knows about and should have
    prevented. It is repeated anyway, because a control that only exists in a
    browser is a suggestion.
    """
    room, participant = _listener_or_401(request)
    try:
        message = web_chat.post_listener_message(
            engine, room_id=room.id, participant_id=participant.id,
            display_name=participant.display_name, body=payload.body)
    except web_chat.ChatRefused as refusal:
        # 429 for the rate limit specifically, so a client can tell "slow down"
        # apart from "you are not allowed to".
        status = 429 if "Too many messages" in str(refusal) else 403
        raise HTTPException(status_code=status, detail=str(refusal))
    return message.public_dict()


def _image_response(message, session_id: int, *, reveal_removed: bool = False):
    """Serve one stored image, or 404. Never a path from the request.

    The filename comes from the database row, not from the URL, so there is no
    caller-supplied path to traverse with. Cache-Control is private: this is a
    photograph from somebody's shop, and a shared proxy has no business
    keeping a copy.
    """
    if not message.attachment_name or (message.deleted_at and not reveal_removed):
        # A listener asking for a removed image gets the same answer as one
        # asking for a message that never had a picture. The host gets the
        # picture, because they are the person who has to account for removing
        # it.
        raise HTTPException(status_code=404, detail="No image on that message.")
    payload = chat_attachments.read_image(session_id, message.attachment_name)
    if payload is None:
        # The row says there is an image and the disk disagrees. Honest 404
        # rather than a broken stream - and the row is left alone, because a
        # transcript recording that a picture WAS sent is still true.
        raise HTTPException(status_code=404, detail="That image is no longer stored.")
    return Response(
        content=payload,
        media_type=message.attachment_mime or "application/octet-stream",
        headers={
            "Cache-Control": "private, max-age=300",
            # Rendered inline, never executed: a browser must treat this as
            # the image its type says it is and nothing else.
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        })


@api.post("/broadcast/sessions/{sid}/chat/image")
async def post_broadcast_chat_image(
    sid: int, file: UploadFile = File(...), body: str = Form(""),
    user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """The host sending a picture - a photo of the right speaker setting, say.

    The caption is optional: with an image, the picture IS the message.
    """
    _require_web_room_owner(sid, user)
    room = _chat_room_or_404(sid)
    raw = await file.read()
    try:
        attachment = chat_attachments.store_image(raw, session_id=sid)
    except chat_attachments.AttachmentRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    try:
        message = web_chat.post_host_message(
            engine, room_id=room.id,
            display_name=user.display_name or user.username,
            body=body, attachment=attachment)
    except web_chat.ChatRefused as refusal:
        # The row was refused, so the file it would have belonged to must not
        # be left on disk. An orphaned upload is a file nothing points at and
        # nothing will ever clean up.
        chat_attachments.delete_image(sid, attachment["attachment_name"])
        raise HTTPException(status_code=400, detail=str(refusal))
    return message.public_dict()


@api.get("/broadcast/sessions/{sid}/chat/messages/{mid}/image")
def read_broadcast_chat_image(
    sid: int, mid: int, user: HQUser = Depends(require(Permission.START_BROADCAST)),
):
    """The host reading any image in their own room, private ones included."""
    _require_web_room_owner(sid, user)
    room = _chat_room_or_404(sid)
    message = web_chat.get_message(engine, message_id=mid, room_id=room.id)
    if message is None:
        raise HTTPException(status_code=404, detail="No such message.")
    return _image_response(message, sid,
                           reveal_removed=_may_see_removed_chat(user))


@api.get("/broadcast/history/{sid}/chat/messages/{mid}/image")
def read_history_chat_image(
    sid: int, mid: int, user: HQUser = Depends(require("menu.history.view")),
):
    """The same image, from the transcript of a finished Broadcast."""
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    if room is None:
        raise HTTPException(status_code=404, detail="No such message.")
    message = web_chat.get_message(engine, message_id=mid, room_id=room.id)
    if message is None:
        raise HTTPException(status_code=404, detail="No such message.")
    return _image_response(message, sid,
                           reveal_removed=_may_see_removed_chat(user))


@api.post("/listen/chat/image")
async def post_listener_chat_image(request: Request,
                                   file: UploadFile = File(...),
                                   body: str = Form("")):
    """A listener sending a picture - most usefully, a photo of what is wrong.

    Every gate a text message passes applies here too: chat must be on, the
    listener must not be muted, and the rate limit counts an image exactly as
    it counts a sentence. An upload is a message.
    """
    room, participant = _listener_or_401(request)
    raw = await file.read()
    try:
        attachment = chat_attachments.store_image(raw, session_id=room.session_id)
    except chat_attachments.AttachmentRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    try:
        message = web_chat.post_listener_message(
            engine, room_id=room.id, participant_id=participant.id,
            display_name=participant.display_name, body=body,
            attachment=attachment)
    except web_chat.ChatRefused as refusal:
        chat_attachments.delete_image(room.session_id,
                                      attachment["attachment_name"])
        status = 429 if "Too many messages" in str(refusal) else 403
        raise HTTPException(status_code=status, detail=str(refusal))
    return message.public_dict()


@api.get("/listen/chat/messages/{mid}/image")
def read_listener_chat_image(mid: int, request: Request):
    """An image THIS listener is entitled to see.

    The same rule as the transcript, applied to the bytes: public messages, or
    their own private one. Without this check a private photograph would be
    readable by anybody in the room who could guess a message id - which is
    exactly the kind of hole a URL that "nobody would find" leaves open.
    """
    room, participant = _listener_or_401(request)
    message = web_chat.get_message(engine, message_id=mid, room_id=room.id)
    if message is None:
        raise HTTPException(status_code=404, detail="No such message.")
    if message.visibility == web_chat.PRIVATE \
            and message.participant_id != participant.id:
        # 404, not 403: a listener is not entitled to learn that somebody
        # else's private message exists.
        raise HTTPException(status_code=404, detail="No such message.")
    return _image_response(message, room.session_id)


@api.post("/listen/forget")
def listener_forget(request: Request, response: Response):
    """Discard this browser's listener session so it can start over.

    This is what "Join Again" after a Kick calls. It grants nothing: it only
    throws away cookies the browser cannot reach itself, returning the page to
    the ordinary join form. Rejoining still requires the current password or a
    fresh Request Access, and the broadcaster still decides - which is why Kick
    stays meaningful rather than becoming a button that undoes itself.
    """
    _forget_listener_cookies(request, response)
    return {"forgotten": True}


# ================ LISTENER AUDIO SOCKET ================


@app.websocket("/api/listen/ws")
async def ws_listener(websocket: WebSocket):
    """One admitted browser listener, receiving live audio and nothing else.

    Authenticated by the listener cookie only. There is no ticket in the URL
    and no query parameter, because this URL is logged in full.

    The listener may SEND only a heartbeat carrying its browser playback state.
    Audio, Store commands, Broadcast commands and everything else are refused -
    this socket has no path to any of them, and unknown frames close it rather
    than being ignored, so a client cannot probe for one that is tolerated.
    """
    async def refuse(reason: str, code: int) -> None:
        """Accept, say why, then close.

        Closing BEFORE the handshake completes cannot deliver an application
        close code: the browser only ever sees 1006, indistinguishable from a
        dropped network. That is what made a refused listener retry for ever
        showing Buffering instead of reporting the refusal. Accepting first
        costs one frame and lets the page tell the truth.
        """
        try:
            await websocket.accept()
            await websocket.send_text(json.dumps({"type": "refused", "reason": reason}))
        except Exception:
            pass
        try:
            await websocket.close(code=code)
        except Exception:
            pass

    token = websocket.cookies.get(LISTENER_COOKIE)
    resolved = web_rooms.authenticate_listener(engine, token=token) if token else None
    if resolved is None:
        # One reason for every cause - unknown, kicked, denied, room ended.
        # Telling them which is telling them about a room they are not in.
        await refuse("not_admitted", 4401)
        return
    room, participant = resolved

    if not _session_is_live(room.session_id):
        # Admitted early, before the microphone is on. Not an error, and
        # explicitly NOT "ended" - the page should wait, not give up.
        await refuse("not_started", 4409)
        return

    relay = manager.broadcasts.web_relay(room.session_id)
    if relay is None or not relay.ready:
        await refuse("not_started", 4409)
        return

    await websocket.accept()
    listener_key = str(participant.id)
    runtime = web_participants.attach(
        participant_id=participant.id, room_id=room.id,
        session_id=room.session_id, socket=websocket)

    async def send_cluster(frame: bytes) -> None:
        await websocket.send_bytes(frame)

    try:
        # Attach is atomic in the relay and anchored to a Cluster index, so no
        # Cluster is lost or repeated between the bootstrap and the live feed.
        bootstrap = await relay.add_listener(listener_key, send_cluster)
        if bootstrap is None:
            await websocket.close(code=4409)
            return

        await websocket.send_text(json.dumps({
            "type": "bootstrap",
            "mime": "audio/webm;codecs=opus",
            "clusters": len(bootstrap.clusters),
            "heartbeat_seconds": HEARTBEAT_INTERVAL_SECONDS,
        }))
        await websocket.send_bytes(bootstrap.init_segment)
        for cluster in bootstrap.clusters:
            await websocket.send_bytes(cluster)

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                # A listener has no microphone here and never will. Publishing
                # is not merely ignored, it ends the connection.
                await websocket.close(code=4403)
                return
            text_frame = message.get("text")
            if text_frame is None:
                continue
            if len(text_frame) > MAX_LISTENER_FRAME_BYTES:
                await websocket.close(code=4403)
                return
            try:
                payload = json.loads(text_frame)
            except Exception:
                await websocket.close(code=4403)
                return
            if not isinstance(payload, dict) or payload.get("type") != "heartbeat":
                # Exactly one inbound message type. Anything else closes.
                await websocket.close(code=4403)
                return
            web_participants.heartbeat(
                participant_id=participant.id,
                playback_state=payload.get("playback_state"))
    except WebSocketDisconnect:
        pass
    except Exception as failure:
        logger.info("listener socket ended after %s", type(failure).__name__)
    finally:
        # Only if THIS socket still holds the slot, so a late cleanup from a
        # superseded socket cannot evict its replacement. Removing the runtime
        # IS the disconnect: _room_state reports a participant with no runtime
        # as not connected, so a closed tab stops claiming to be Listening as
        # soon as the close is observed rather than after a timeout.
        web_participants.detach(participant_id=participant.id, socket=websocket)
        live_relay = manager.broadcasts.web_relay(room.session_id)
        if live_relay is not None:
            await live_relay.remove_listener(listener_key)


# ================ ACTIVE BROADCAST -> WEB AUDIENCE SUPERVISION ================
#
# The owner of a Broadcast manages its audience from their own Console and needs
# no supervision permission for that - it is their announcement. A supervisor
# reaching into SOMEBODY ELSE'S room is a different act, and it is gated by
# broadcast.manage_web_audience.
#
# Deliberately NOT satisfied by broadcast.view_ownership. Reading who is
# broadcasting and ejecting a person from their audience are different powers,
# and the second happens where the owning operator cannot see it.


def _authorize_web_audience(sid: int, user: HQUser, *, db: Session):
    """One decision for every audience route. Returns (row, visibility).

    For a cross-owner supervisor the SAME Store Scope containment rule applies
    as for a cross-owner stop: if the Broadcast reaches Stores this account may
    not supervise, it may not reach into that Broadcast at all. Web listeners
    have no Store identity, so without this the audience panel would be a way
    into a Broadcast whose physical half is out of bounds.

    An Only With Link Broadcast has no Stores, so containment is vacuous and
    authorization rests entirely on the explicit permission - which is the point
    of having an explicit permission.
    """
    visibility = abm.resolve_visibility(engine, user)
    rows = _active_management_rows(db, user, visibility)
    row = next((r for r in rows if r.session_id == sid), None)
    if row is None:
        # 404 for "not live" and "not visible to you" alike, so this cannot be
        # used to probe which session ids exist.
        raise HTTPException(status_code=404, detail="No such active broadcast")

    if row.is_mine:
        return row, visibility

    if not visibility.may_manage_web_audience:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to manage another operator's web audience.")

    scope = resolve_store_scope(engine, user)
    outside = abm.stop_scope_refusal(row, scope)
    if outside:
        # The COUNT, never the ids or names.
        raise HTTPException(
            status_code=403,
            detail=(f"This broadcast reaches {len(outside)} Store(s) outside your "
                    "Store Scope, so its web audience was not opened."))
    return row, visibility


def _audience_capabilities(row, visibility) -> dict:
    """What this caller may actually do, decided once and sent to the client.

    The frontend renders from these rather than from role names, so a control
    can never appear for somebody the backend would refuse - and a new role
    cannot silently gain a button.
    """
    manages = row.is_mine or visibility.may_manage_web_audience
    return {
        # The public code is a credential: holding it is enough to attempt a
        # join. It follows the broadcaster's identity.
        "can_view_room_credentials": visibility.may_view_ownership or row.is_mine,
        "can_manage_web_audience": manages,
        "can_approve": manages,
        "can_deny": manages,
        "can_kick": manages,
        "can_toggle_auto_approve": manages,
        # Rotation is OWNER ONLY. It replaces a credential the owner has
        # already shared with an audience, and a supervisor doing that silently
        # would lock the owner out of their own room's future joins.
        "can_rotate_password": row.is_mine,
    }


@api.get("/broadcast/active-management/{sid}/web-audience")
def active_management_web_audience(
    sid: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require(abm.PAGE_CODE)),
):
    """One live Broadcast's web audience, for a supervisor or its owner.

    A separate route rather than more fields on the list, for the same reason
    the Stores are: fifty sessions multiplied by every listener is the payload
    the supervision page exists to avoid.
    """
    row, visibility = _authorize_web_audience(sid, user, db=db)
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    if room is None:
        raise HTTPException(status_code=404, detail="This Broadcast has no web room")

    capabilities = _audience_capabilities(row, visibility)
    state = _room_state(sid, room=room)

    payload = {
        "session_id": sid,
        "campaign_name": row.campaign_name,
        "started_at": row.started_at,
        "is_mine": row.is_mine,
        "target_store_count": len(row.visible_targets),
        "status": state["status"],
        "auto_approve": state["auto_approve"],
        "delivery": state["delivery"],
        "counts": state["counts"],
        "waiting": state["waiting"],
        "listeners": state["listeners"],
        "capabilities": capabilities,
    }
    if capabilities["can_view_room_credentials"]:
        payload["public_code"] = state["public_code"]
        payload["password"] = state["password"]
        payload["password_available"] = bool(state["password"])
    if visibility.may_view_ownership or row.is_mine:
        payload["owner_user_id"] = row.owner_user_id
        payload["owner_display_name"] = row.owner_display_name
    return payload


def _supervised_participant_action(sid: int, pid: int, user: HQUser,
                                   db: Session, action):
    """Shared body for approve/deny/kick from the supervision page.

    The room lifecycle lives in web_rooms and is called, never reimplemented:
    a second copy of "is this participant in this room" would eventually
    disagree with the first.
    """
    row, visibility = _authorize_web_audience(sid, user, db=db)
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    if room is None:
        raise HTTPException(status_code=404, detail="This Broadcast has no web room")
    try:
        return action(room)
    except web_rooms.ParticipantNotAdmissibleError as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))


@api.post("/broadcast/active-management/{sid}/web-audience/{pid}/approve")
async def supervised_approve_participant(
    sid: int, pid: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require(abm.PAGE_CODE)),
):
    def act(room):
        participant, token = web_rooms.approve_participant(
            engine, room_id=room.id, participant_id=pid)
        if token is not None:
            _PENDING_LISTENER_TOKENS[participant.id] = token
        return _room_state(sid, room=room)
    return _supervised_participant_action(sid, pid, user, db, act)


@api.post("/broadcast/active-management/{sid}/web-audience/{pid}/deny")
async def supervised_deny_participant(
    sid: int, pid: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require(abm.PAGE_CODE)),
):
    def act(room):
        web_rooms.deny_participant(engine, room_id=room.id, participant_id=pid)
        _PENDING_LISTENER_TOKENS.pop(pid, None)
        return _room_state(sid, room=room)
    return _supervised_participant_action(sid, pid, user, db, act)


@api.post("/broadcast/active-management/{sid}/web-audience/{pid}/kick")
async def supervised_kick_participant(
    sid: int, pid: int, db: Session = Depends(get_db),
    user: HQUser = Depends(require(abm.PAGE_CODE)),
):
    """Remove one listener from another operator's Broadcast.

    Session invalidated first, then the socket closed, exactly as the owner's
    own Kick does - so a reconnect racing the close has nothing valid left.
    """
    row, _ = _authorize_web_audience(sid, user, db=db)
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    if room is None:
        raise HTTPException(status_code=404, detail="This Broadcast has no web room")
    try:
        web_rooms.kick_participant(engine, room_id=room.id, participant_id=pid)
    except web_rooms.ParticipantNotAdmissibleError as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))

    _PENDING_LISTENER_TOKENS.pop(pid, None)
    socket = web_participants.socket_for(pid)
    web_participants.detach(participant_id=pid)
    relay = manager.broadcasts.web_relay(sid)
    if relay is not None:
        await relay.remove_listener(str(pid))
    if socket is not None:
        await _close_listener_socket(socket, {"type": "kicked"})
    if not row.is_mine:
        # A cross-owner intervention the owning operator cannot see happening
        # is exactly the thing that must leave a trace.
        _write_log(db, "warn",
                   f"Cross-owner web listener removed: actor_user_id={user.id} "
                   f"session_id={sid} participant_id={pid}")
    return _room_state(sid, room=room)


@api.put("/broadcast/active-management/{sid}/web-audience/auto-approve")
async def supervised_set_auto_approve(
    sid: int, payload: WebRoomAutoApproveUpdate, db: Session = Depends(get_db),
    user: HQUser = Depends(require(abm.PAGE_CODE)),
):
    _authorize_web_audience(sid, user, db=db)
    try:
        web_rooms.set_auto_approve(engine, session_id=sid,
                                   enabled=payload.auto_approve)
    except web_rooms.RoomNotOpenError:
        raise HTTPException(status_code=409, detail="This Broadcast has ended")
    room = web_rooms.get_room_for_session(engine, session_id=sid)
    return _room_state(sid, room=room)


# Include routes
#: The first Store Kit whose Receiver can play a recorded announcement.
#: Anything older connects, broadcasts and ignores every announcement command
#: it is sent - which is indistinguishable from silence unless somebody says
#: so out loud.
#: 1.7.6, not 1.7.5.
#:
#: Six Receivers went out on one day carrying the version 1.7.5, and only the
#: last of them could actually play an announcement - the earlier ones asked
#: for stereo on a mono device, looked for ffmpeg where it was not, or never
#: opened the speaker at all. A version names a release, not a build, so every
#: 1.7.5 in the field is one of those. Requiring 1.7.6 makes the console say
#: "Receiver too old" for exactly the Stores that cannot play.
ANNOUNCEMENTS_NEED_VERSION = (1, 7, 6)


def _supports_announcements(version: str) -> bool:
    """Is this Store new enough to play announcements at all?

    An unknown version answers True. A Store that has not told us is not
    evidence of an old build, and marking every quiet Store as unsupported
    would be a guess dressed as a fact - the opposite of the point.
    """
    if not version or version == "unknown":
        return True
    # The version may carry its build - "1.7.5 (0eddbfa)" - because a version
    # alone cannot say which of six builds in one day a shop is running.
    version = str(version).split(" ")[0]
    parts = []
    for piece in version.split("."):
        digits = "".join(character for character in piece
                         if character.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3]) >= ANNOUNCEMENTS_NEED_VERSION


#: What each connected Store's own speaker is set to, by store id.
#:
#: In memory beside the connection, like the version: it describes a machine
#: that is on the end of a socket right now. A remembered volume for an
#: offline Store would be a claim about a computer nobody is talking to.
STORE_MASTER_VOLUME: dict[int, dict] = {}


#: What each connected Store says it is running, by store id.
#:
#: In memory on purpose. It is a property of a live connection, not of the
#: Store - a Store that is offline is not running anything, and reporting a
#: remembered version for one would be the same class of claim this project
#: keeps removing.
RECEIVER_VERSIONS: dict[int, str] = {}


def _handle_output_device_report(store_id: int, data: dict) -> None:
    """What a Store says about its speakers.

    Never raises: a malformed report from one Store must not drop that Store's
    connection, because the connection is also carrying its broadcast audio.
    """
    try:
        if data.get("type") == "output_devices":
            devices = data.get("devices")
            if not isinstance(devices, list):
                return
            # Trimmed to the fields HQ shows. A Store is free to send more,
            # and storing whatever arrives would mean HQ's table growing a
            # column every time the Receiver changes.
            trimmed = [
                {
                    "index": device.get("index"),
                    "name": str(device.get("name") or "")[:200],
                    "selector": str(device.get("selector") or "")[:255],
                    "verified_selector": str(
                        device.get("verified_selector") or "")[:255],
                    "is_default": bool(device.get("is_default")),
                    "looks_wireless": bool(device.get("looks_wireless")),
                }
                for device in devices if isinstance(device, dict)
            ]
            receiver_output_device.record_reported_devices(
                engine, store_id=store_id, devices=trimmed)
            return

        result = data.get("result")
        if result not in receiver_output_device.RESULTS:
            return
        receiver_output_device.record_result(
            engine, store_id=store_id, result=result,
            applied_selector=data.get("applied_selector"),
            applied_device_name=data.get("applied_device_name"),
            error=data.get("error"))
    except Exception:  # noqa: BLE001
        logger.exception("Could not record the speaker report from Store %s",
                         store_id)


# ================ REMOTE SPEAKER SELECTION ================
#
# See receiver_output_device.py for why this is shaped around the fact that
# nobody at HQ can hear the result.

@api.get("/stores/{store_id}/audio-output")
def get_store_audio_output(
    store_id: int,
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """Which speakers this Store has, and which one it is playing through."""
    state = receiver_output_device.get_state(engine, store_id=store_id)
    state["summary"] = receiver_output_device.describe(state)
    state["online"] = store_id in manager.receivers
    return state


@api.post("/stores/{store_id}/audio-output/refresh")
async def refresh_store_audio_output(
    store_id: int,
    user: HQUser = Depends(require("receiver.set_output_device")),
):
    """Ask the Store to enumerate its speakers again.

    Needed because the list HQ holds is a snapshot of another computer at a
    moment. A speaker plugged in five minutes ago is not on it, and offering a
    stale list is how somebody selects an endpoint that no longer exists.
    """
    if store_id not in manager.receivers:
        raise HTTPException(
            status_code=409,
            detail="That Store is offline, so it cannot be asked what "
                   "speakers it has. The list below is from the last time it "
                   "was connected.")
    await manager.send_to_receiver(store_id, {"type": "list_output_devices"})
    return {"ok": True, "store_id": store_id}


@api.post("/stores/{store_id}/audio-output")
async def set_store_audio_output(
    store_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("receiver.set_output_device")),
):
    """Change which speaker a Store plays through.

    Its own permission, not part of managing Devices: this was only ever
    possible standing at the Store PC, where the person who could get it wrong
    could also hear the result. From here, nobody can.
    """
    state = receiver_output_device.get_state(engine, store_id=store_id)
    try:
        device = receiver_output_device.validate_selector_is_one_the_store_offered(
            state, payload.get("selector"))
    except receiver_output_device.OutputDeviceRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    if store_id not in manager.receivers:
        raise HTTPException(
            status_code=409,
            detail="That Store is offline. Changing its speaker while it "
                   "cannot answer would leave HQ claiming a change nobody has "
                   "confirmed.")

    selector = device.get("verified_selector") or device.get("selector")
    receiver_output_device.record_request(engine, store_id=store_id,
                                          selector=selector, actor_id=user.id)
    await manager.send_to_receiver(store_id, {
        "type": "set_output_device", "selector": selector})
    _write_log(db, "warn",
               f"store_output_device_requested store={store_id} "
               f"device={device.get('name')!r} by={user.username}")

    result = receiver_output_device.get_state(engine, store_id=store_id)
    result["summary"] = receiver_output_device.describe(result)
    # Deliberately not "changed". Nothing has changed in the shop until the
    # Store answers, and a message saying otherwise is the one an operator
    # would believe instead of checking.
    result["note"] = ("Sent. The Store will confirm which speaker it ended up "
                      "on - nobody at HQ can hear the result, so check the "
                      "confirmation before you consider this done.")
    return result


# ================ GROUP BROADCASTING ================
#
# Two ways in, and the difference is a permission: broadcast.group_join walks
# in, everybody else asks the host. See broadcast_group.py.


def _live_session_or_404(sid: int, db: Session) -> BroadcastSession:
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if session is None or session.status != "live":
        raise HTTPException(status_code=404,
                            detail="That broadcast is not live.")
    return session


def _require_group_host(session: BroadcastSession, user: HQUser) -> None:
    """Only the host answers requests.

    The host is the one person who can hear what is happening on that
    broadcast right now, which is the whole reason a request goes to them
    rather than to whoever is nearest an admin account. broadcast.group_host
    lets somebody OPEN their own broadcast to others; it does not let them
    answer for somebody else's.
    """
    if session.started_by != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the broadcaster who started this broadcast can answer "
                   "requests to join it.")


@api.get("/broadcast/sessions/{sid}/group")
def list_group_participants(
    sid: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.broadcast.view")),
):
    session = _live_session_or_404(sid, db)
    participants = broadcast_group.list_participants(engine, session_id=sid)
    mine = broadcast_group.get_participant(engine, session_id=sid, user_id=user.id)
    return {
        "session_id": sid,
        "host_user_id": session.started_by,
        "is_host": session.started_by == user.id,
        "me": mine,
        "my_status": broadcast_group.describe(mine),
        # The full list only for the host. Who else asked to speak on somebody
        # else's broadcast is the host's business, not every account's.
        "participants": participants if session.started_by == user.id
                        else [p for p in participants
                              if p["state"] == broadcast_group.STATE_JOINED],
    }


@api.post("/broadcast/sessions/{sid}/group/join")
def join_group_broadcast(
    sid: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.start")),
):
    """Join, or ask to.

    broadcast.start is required either way: this puts a voice on the
    loudspeakers of real shops, and an account that may not broadcast at all
    must not reach that by asking somebody else nicely.
    """
    session = _live_session_or_404(sid, db)
    if session.started_by == user.id:
        raise HTTPException(status_code=400,
                            detail="You are already the host of this broadcast.")
    # The same resolver every route's dependency uses, so "may they join
    # directly" cannot drift from what the permissions page shows.
    may_join_directly = "broadcast.group_join" in resolve_effective_permissions(
        engine, user)
    try:
        participant = broadcast_group.join_or_request(
            engine, session_id=sid, user_id=user.id,
            may_join_directly=may_join_directly)
    except broadcast_group.GroupRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))

    _write_log(db, "info",
               f"group_broadcast_{participant['state'].lower()} session={sid} "
               f"user={user.username} by_right={bool(may_join_directly)}")
    return {"participant": participant,
            "status": broadcast_group.describe(participant),
            "on_air": participant["state"] == broadcast_group.STATE_JOINED}


@api.post("/broadcast/sessions/{sid}/group/requests/{user_id}/approve")
def approve_group_request(
    sid: int, user_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.group_host")),
):
    session = _live_session_or_404(sid, db)
    _require_group_host(session, user)
    try:
        participant = broadcast_group.decide(engine, session_id=sid,
                                             user_id=user_id, approve=True,
                                             decided_by=user.id)
    except broadcast_group.GroupRefused as refusal:
        raise HTTPException(status_code=404, detail=str(refusal))
    _write_log(db, "info",
               f"group_broadcast_approved session={sid} user_id={user_id} "
               f"by={user.username}")
    return participant


@api.post("/broadcast/sessions/{sid}/group/requests/{user_id}/deny")
def deny_group_request(
    sid: int, user_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("broadcast.group_host")),
):
    session = _live_session_or_404(sid, db)
    _require_group_host(session, user)
    try:
        participant = broadcast_group.decide(engine, session_id=sid,
                                             user_id=user_id, approve=False,
                                             decided_by=user.id)
    except broadcast_group.GroupRefused as refusal:
        raise HTTPException(status_code=404, detail=str(refusal))
    _write_log(db, "info",
               f"group_broadcast_denied session={sid} user_id={user_id} "
               f"by={user.username}")
    return participant


@api.post("/broadcast/sessions/{sid}/group/leave")
def leave_group_broadcast(
    sid: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.broadcast.view")),
):
    """Leave, or withdraw a request that has not been answered.

    Deliberately not gated on broadcast.start: an account that has been
    demoted since joining must still be able to take itself off air, and
    refusing that would leave a voice on the loudspeakers with no way to
    remove it except stopping the whole broadcast.
    """
    _live_session_or_404(sid, db)
    participant = broadcast_group.leave(engine, session_id=sid, user_id=user.id)
    if participant is None:
        raise HTTPException(status_code=404,
                            detail="You are not on this broadcast.")
    _write_log(db, "info",
               f"group_broadcast_left session={sid} user={user.username}")
    return participant






# ================ EXPORT ================
#
# CSV, with a UTF-8 byte-order mark. Excel opens it directly and treats it as
# a spreadsheet; without the mark it mangles every non-ASCII Store name, which
# on this estate is most of them.
#
# Deliberately not .xlsx. That needs a new dependency on a live estate, and
# these are flat tables of text and numbers - there is no formatting, formula
# or sheet structure that CSV would lose. If a real workbook is ever wanted,
# the dependency is the decision, not the code here.

#: Which lists can be exported, and how. Each entry reuses the SAME function
#: the page's own list route uses, so an export can never disagree with the
#: table it came from - and can never widen what the caller may see, because
#: the scope and permission live in that function.
EXPORTS = {
    "announcement-status": {
        "permission": "menu.announcements.view",
        "columns": [("store_code", "Store code"), ("store_name", "Store"),
                    ("zone", "Zone"), ("state", "State"),
                    ("template_name", "Template"), ("audio_title", "Recording"),
                    ("volume_percent", "Volume %"), ("updated_at", "Updated")],
        "sorts": "ANNOUNCEMENT_STATUS_SORTS",
    },
    "announcement-history": {
        "permission": "menu.announcements.view",
        "columns": [("store_code", "Store code"), ("store_name", "Store"),
                    ("zone", "Zone"), ("audio_title", "Recording"),
                    ("template_name", "Template"), ("started_at", "Started"),
                    ("ended_at", "Ended"), ("duration_seconds", "Duration (s)"),
                    ("ended_reason", "Because"),
                    ("ended_by_username", "Ended by")],
        "sorts": "ANNOUNCEMENT_HISTORY_SORTS",
    },
    "announcement-templates": {
        "permission": "menu.announcements.view",
        "columns": [("name", "Template"), ("description", "Description"),
                    ("window", "Window"), ("status", "Status"),
                    ("starts_at", "Starts"), ("expires_at", "Stops")],
        "sorts": "ANNOUNCEMENT_TEMPLATE_SORTS",
    },
    "announcement-recordings": {
        "permission": "menu.announcements.view",
        "columns": [("title", "Title"), ("original_filename", "File"),
                    ("byte_size", "Bytes"), ("uploaded_at", "Uploaded"),
                    ("status", "Status")],
        "sorts": "ANNOUNCEMENT_AUDIO_SORTS",
    },
    "receiver-status": {
        "permission": "menu.receivers.view",
        "columns": [("store_code", "Code"), ("store_name", "Store"),
                    ("city", "City"), ("region", "Zone"),
                    ("device_count", "Devices"), ("has_primary", "Primary"),
                    ("status", "Status")],
        "sorts": "RECEIVER_STATUS_SORTS",
    },
    "stores": {
        "permission": "menu.stores.view",
        "columns": [("store_code", "Code"), ("store_name", "Store"),
                    ("city", "City"), ("region", "Zone"),
                    ("lifecycle_state", "Lifecycle"), ("status", "Status")],
        "sorts": "STORE_SORTS",
    },
    "users": {
        "permission": "menu.users.view",
        "columns": [("username", "Username"), ("display_name", "Name"),
                    ("role", "Role"), ("state", "State"),
                    ("created_at", "Created"), ("last_login_at", "Last sign-in")],
        "sorts": "USER_SORTS",
    },
    "broadcast-history": {
        "permission": "menu.history.view",
        "columns": [("campaign_name", "Campaign"),
                    ("started_by_display_name", "Broadcaster"),
                    ("started_at", "Started"), ("ended_at", "Ended"),
                    ("status", "Status"),
                    ("online_store_count", "Stores reached"),
                    ("offline_store_count", "Stores offline")],
        "sorts": "BROADCAST_HISTORY_SORTS",
    },
    "receiver-devices": {
        "permission": "menu.receivers.view",
        "columns": [("display_name", "Device"), ("public_id", "Identifier"),
                    ("store_code", "Store code"), ("store_name", "Store"),
                    ("city", "City"), ("region", "Zone"),
                    ("is_primary", "Primary"), ("status", "Status"),
                    ("last_seen_at", "Last seen")],
        "sorts": "RECEIVER_DEVICE_SORTS",
    },
    "active-broadcasts": {
        "permission": "broadcast.active_view",
        "columns": [("campaign_name", "Campaign"), ("target_mode", "Mode"),
                    ("started_by_display_name", "Broadcaster"),
                    ("started_at", "Started"),
                    ("target_store_count", "Stores")],
        "sorts": "ACTIVE_BROADCAST_SORTS",
    },
    "system-logs": {
        "permission": "menu.logs.view",
        "columns": [("created_at", "Time"), ("level", "Level"),
                    ("message", "Message")],
        "sorts": "SYSTEM_LOG_SORTS",
    },
}

#: Sort allowlists for the lists that had none of their own.
RECEIVER_STATUS_SORTS = {
    "store_code": lambda row: row.get("store_code"),
    "store_name": lambda row: row.get("store_name"),
    "city": lambda row: row.get("city"),
    "region": lambda row: row.get("region"),
    "status": lambda row: row.get("status"),
    "device_count": lambda row: row.get("device_count"),
}
STORE_SORTS = {
    "store_code": lambda row: row.get("store_code"),
    "store_name": lambda row: row.get("store_name"),
    "city": lambda row: row.get("city"),
    "region": lambda row: row.get("region"),
    "status": lambda row: row.get("status"),
    "lifecycle": lambda row: row.get("lifecycle_state"),
    "type": lambda row: row.get("is_online_store"),
}
BROADCAST_HISTORY_SORTS = {
    "campaign_name": lambda row: row.get("campaign_name"),
    "target_mode": lambda row: row.get("target_mode"),
    "started_at": lambda row: row.get("started_at"),
    "ended_at": lambda row: row.get("ended_at"),
    "status": lambda row: row.get("status"),
    "started_by": lambda row: row.get("started_by_display_name")
                              or row.get("started_by_username"),
}
RECEIVER_DEVICE_SORTS = {
    "role": lambda row: row.get("is_primary"),
    "lifecycle": lambda row: row.get("lifecycle"),
    "display_name": lambda row: row.get("display_name"),
    "public_id": lambda row: row.get("public_id"),
    "store_name": lambda row: row.get("store_name"),
    "city": lambda row: row.get("city"),
    "region": lambda row: row.get("region"),
    "status": lambda row: row.get("status"),
    "last_seen_at": lambda row: row.get("last_seen_at"),
}
ACTIVE_BROADCAST_SORTS = {
    "campaign_name": lambda row: row.get("campaign_name"),
    "target_mode": lambda row: row.get("target_mode"),
    "started_at": lambda row: row.get("started_at"),
    "target_store_count": lambda row: row.get("target_store_count"),
    "status": lambda row: row.get("status"),
    "started_by": lambda row: row.get("started_by_display_name")
                              or row.get("started_by_username"),
}
SYSTEM_LOG_SORTS = {
    "created_at": lambda row: row.get("created_at"),
    "level": lambda row: row.get("level"),
    "message": lambda row: row.get("message"),
}

#: A ceiling on one export, so a single request cannot try to render an
#: estate's entire history into memory. Deliberately generous: the point is to
#: stop a runaway, not to make somebody export twice.
MAX_EXPORT_ROWS = 10000



def _export_rows(dataset: str, params: dict, user: HQUser, db: Session) -> list[dict]:
    if dataset == "announcement-status":
        return announcement_service.live_status(
            engine, search=params.get("q") or "", zone=params.get("zone") or "",
            state=params.get("state") or "", store_id=params.get("store_id"))
    if dataset == "announcement-history":
        return announcement_service.list_history(
            engine, search=params.get("q") or "", zone=params.get("zone") or "",
            reason=params.get("reason") or "", since=params.get("since") or "",
            until=params.get("until") or "",
            include_archived=str(params.get("include_archived", "")).lower()
                             in ("1", "true", "yes"))
    if dataset == "announcement-templates":
        return announcement_service.list_templates(
            engine, search=params.get("q") or "",
            status=params.get("status") or "active",
            zone=params.get("zone") or "", store_id=params.get("store_id"),
            window=params.get("window") or "")
    if dataset == "announcement-recordings":
        return announcement_service.list_audio(
            engine, search=params.get("q") or "",
            status=params.get("status") or "active")
    # The remaining datasets reuse each page's OWN list route, called
    # in-process with the same parameters. Reimplementing their queries here
    # would be a second set of filters to keep in step with the first, and the
    # way that drifts is an export quietly showing a row the page does not -
    # or, worse, one the reader is not allowed to see.
    if dataset == "receiver-status":
        return search_receiver_status(
            q=params.get("q"), city=params.get("city"),
            region=params.get("region"), store_id=params.get("store_id"),
            status_f=params.get("status"), has_primary=None,
            sort=params.get("sort"), dir=params.get("dir", "asc"),
            page=1, page_size=MAX_EXPORT_ROWS, db=db, user=user)["items"]
    if dataset == "stores":
        return search_stores(
            q=params.get("q"), city=params.get("city"),
            region=params.get("region"),
            lifecycle=params.get("lifecycle") or "active",
            sort=params.get("sort"), dir=params.get("dir", "asc"),
            page=1, page_size=MAX_EXPORT_ROWS, db=db, user=user)["items"]
    if dataset == "users":
        return search_users(
            q=params.get("q"), role=params.get("role"),
            state=params.get("state"),
            scope_store_id=params.get("scope_store_id"),
            scope_city=params.get("scope_city"),
            scope_region=params.get("scope_region"),
            sort=params.get("sort"), dir=params.get("dir", "asc"),
            page=1, page_size=MAX_EXPORT_ROWS, user=user)["items"]
    if dataset == "broadcast-history":
        return search_broadcast_history(
            q=params.get("q"), status_f=params.get("status"),
            date_from=params.get("date_from"), date_to=params.get("date_to"),
            started_by=_as_int(params.get("started_by")),
            store_id=_as_int(params.get("store_id")),
            city=params.get("city"), region=params.get("region"),
            include_archived=str(params.get("include_archived", "")).lower()
                             in ("1", "true", "yes"),
            archived_only=str(params.get("archived_only", "")).lower()
                          in ("1", "true", "yes"),
            sort=params.get("sort"), dir=params.get("dir", "asc"),
            page=1, page_size=MAX_EXPORT_ROWS, db=db, user=user)["items"]
    if dataset == "receiver-devices":
        return search_receiver_devices(
            q=params.get("q"), store_id=_as_int(params.get("store_id")),
            city=params.get("city"), region=params.get("region"),
            status_f=params.get("status"),
            lifecycle=params.get("lifecycle"),
            is_primary=None, include_archived=True,
            page=1, page_size=MAX_EXPORT_ROWS, db=db, user=user)["items"]
    if dataset == "active-broadcasts":
        return active_management_list(
            q=params.get("q"), owner=params.get("owner") or "all",
            owner_user_id=_as_int(params.get("owner_user_id")),
            store_id=_as_int(params.get("store_id")),
            sort=params.get("sort") or abm.SORT_NEWEST,
            page=1, page_size=MAX_EXPORT_ROWS, db=db, user=user)["items"]
    if dataset == "system-logs":
        return search_logs(
            q=params.get("q"), level=params.get("level"),
            date_from=params.get("date_from"), date_to=params.get("date_to"),
            actor_user_id=_as_int(params.get("actor_user_id")),
            store_id=_as_int(params.get("store_id")),
            device_public_id=params.get("device_public_id"),
            include_archived=str(params.get("include_archived", "")).lower()
                             in ("1", "true", "yes"),
            archived_only=str(params.get("archived_only", "")).lower()
                          in ("1", "true", "yes"),
            page=1, page_size=MAX_EXPORT_ROWS, db=db, user=user)["items"]
    raise HTTPException(status_code=404, detail="There is no such export.")


def _as_int(value):
    """A filter that is not a number is no filter, not a failed page."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@api.get("/export/{dataset}")
def export_dataset(
    dataset: str,
    request: Request,
    db: Session = Depends(get_db),
    user: HQUser = Depends(get_current_user),
):
    """Everything the CURRENT filters select, as a spreadsheet.

    Not the page on screen - the whole filtered set. An export that gave
    fifty rows while the table said 184 would be read as the answer and acted
    on, and nobody would know to check.

    The permission is the same one the page needs. It is checked here rather
    than trusted from the caller, because a download URL is the easiest thing
    in a product to share by accident.
    """
    definition = EXPORTS.get(dataset)
    if definition is None:
        raise HTTPException(status_code=404, detail="There is no such export.")
    # The fine-grained codes, through the same resolver every route dependency
    # uses. Converted to a 403 here: PermissionDenied is raised for the
    # dependency wrapper to translate, and reaching an ordinary route body it
    # would surface as a 500 - an unexplained failure for a refusal that has a
    # perfectly good explanation.
    permissions = resolve_effective_permissions(engine, user)
    if definition["permission"] not in permissions:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to read that list.")
    # And the right to take a COPY away, which is not the right to look. A
    # file gets emailed, copied to a laptop, and outlives every permission
    # change made after it.
    if "dashboard.export" not in permissions:
        raise HTTPException(
            status_code=403,
            detail="You may read this list but not export it. Exporting is a "
                   "separate right, because a file outlives the permissions "
                   "that produced it.")

    params = dict(request.query_params)
    rows = _export_rows(dataset, params, user, db)
    rows = sort_rows(rows, params.get("sort"), params.get("dir"),
                     globals()[definition["sorts"]])

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([heading for _, heading in definition["columns"]])
    for row in rows:
        writer.writerow([_export_cell(row.get(key)) for key, _ in definition["columns"]])

    _write_log(db, "info",
               f"export dataset={dataset} rows={len(rows)} by={user.username}")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # The BOM is what makes Excel read this as UTF-8. Without it every
    # non-ASCII Store name arrives mangled, and on this estate that is most of
    # them.
    body = "﻿" + buffer.getvalue()
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="speaklink-{dataset}-{stamp}.csv"'},
    )


def _export_cell(value):
    """One value, rendered so a spreadsheet reads it as what it is.

    A leading =, +, - or @ makes Excel treat a cell as a FORMULA. A Store
    named "=SUM(A1)" is not malicious in this product, but a value that
    executes when somebody opens the file is a bad habit to leave lying
    around, so it is prefixed with a quote.
    """
    if value is None:
        return ""
    text_value = str(value)
    if text_value[:1] in ("=", "+", "-", "@"):
        return "'" + text_value
    return text_value




# ================ ANNOUNCEMENT LISTENING LINKS ================
#
# A separate link, with its own ID and password, created from HQ. See
# announcement_rooms.py for why this is not the broadcast room.


@api.post("/announcements/templates/{template_id}/room", status_code=201)
def create_announcement_room(
    template_id: int,
    payload: Optional[dict] = None,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.rooms.manage")),
):
    """Open a listening link for this template.

    The password is returned ONCE and never stored in a form anything can read
    back. A page that could show it again would make "who has this link"
    unanswerable; the honest answer to "I lost it" is a new link, which is why
    closing and reopening costs nothing.
    """
    template = _template_or_404(template_id)
    body = payload or {}
    try:
        room, password = announcement_rooms.create_room(
            engine, template_id=template_id,
            label=body.get("label") or template["name"],
            created_by=user.id, hash_password=hash_password,
            code=body.get("id") or body.get("code"),
            password=body.get("password"),
            no_password=bool(body.get("no_password")))
    except announcement_rooms.RoomRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    _write_log(db, "info",
               f"announcement_room_opened template={template_id} "
               f"code={room['public_code']} by={user.username}")
    out = _room_out(room)
    return {
        "room": out,
        # Once, and only when there is one. Named plainly so the UI cannot
        # mistake it for something it can fetch again later.
        "password_shown_once": None if body.get("no_password") else password,
        # A link that carries the password, for the common case of pasting it
        # into a message. Whoever receives it is one click from listening -
        # which is the point, and also the risk, so the UI says both.
        "share_link": (out["listen_path"] if body.get("no_password")
                       else f"{out['listen_path']}&k={quote(password)}"),
    }


@api.get("/announcements/rooms")
def list_announcement_rooms(
    template_id: Optional[int] = None,
    user: HQUser = Depends(require("menu.announcements.view")),
):
    rooms = announcement_rooms.list_rooms(engine, template_id=template_id)
    return {"items": [_room_out(row) for row in rooms], "total": len(rooms)}


@api.get("/announcements/rooms/{room_id}/listeners")
def list_announcement_room_listeners(
    room_id: int,
    user: HQUser = Depends(require("menu.announcements.view")),
):
    """Who followed this link.

    The name is the one they typed, and the page says so: a self-declared
    name is worth exactly what a self-declared name is worth. The joined and
    last-seen times beside it are what this program actually observed.
    """
    if announcement_rooms.get_room(engine, room_id=room_id) is None:
        raise HTTPException(status_code=404, detail="No such listening link.")
    listeners = announcement_rooms.list_listeners(engine, room_id=room_id)
    return {"items": listeners, "total": len(listeners)}


@api.post("/announcements/rooms/{room_id}/listeners/{listener_id}/remove")
def remove_announcement_room_listener(
    room_id: int, listener_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.rooms.manage")),
):
    """Turn ONE listener away, without withdrawing the link from everybody.

    Closing the link is the blunt instrument; this is the one for a single
    person who should not have followed it.
    """
    if not announcement_rooms.remove_listener(engine, room_id=room_id,
                                              listener_id=listener_id):
        raise HTTPException(status_code=404,
                            detail="That listener is not on this link.")
    _write_log(db, "info",
               f"announcement_room_listener_removed room={room_id} "
               f"listener={listener_id} by={user.username}")
    return {"ok": True}


@api.post("/announcements/rooms/{room_id}/close")
def close_announcement_room(
    room_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.rooms.manage")),
):
    """Withdraw the link, and turn away everybody already using it.

    A closed room whose existing listeners kept playing would be a link that
    cannot actually be withdrawn - which is the only thing this button is for.
    """
    if announcement_rooms.get_room(engine, room_id=room_id) is None:
        raise HTTPException(status_code=404, detail="No such listening link.")
    room = announcement_rooms.close_room(engine, room_id=room_id,
                                         closed_by=user.id)
    _write_log(db, "warn",
               f"announcement_room_closed id={room_id} by={user.username}")
    return _room_out(room)


def _room_out(room: dict) -> dict:
    """The wire shape. Never the password hash, and never the plaintext."""
    if room is None:
        return {}
    return {
        "id": room["id"],
        "template_id": room["template_id"],
        "template_name": room.get("template_name"),
        "public_code": room["public_code"],
        "label": room.get("label") or "",
        "status": room["status"],
        "created_at": room.get("created_at"),
        "closed_at": room.get("closed_at"),
        "listener_count": room.get("listener_count", 0),
        "requires_password": bool(room.get("requires_password", 1)),
        # A ready-made link, because the alternative is somebody assembling it
        # by hand and getting the host wrong.
        "listen_path": f"/announce?id={room['public_code']}",
    }


# ---- The listener's own surface, unauthenticated by design ---------------

@api.post("/announce/join")
def join_announcement_room(payload: dict, request: Request,
                           db: Session = Depends(get_db)):
    """A listener presenting an ID and a password.

    No HQ account is involved: whoever holds the link is not a user of this
    product and must never need to be. What they get is a token scoped to one
    room, which stops working the moment that room is closed.
    """
    # Checked here, not only in the form. HQ's "who is listening" list and its
    # Remove button are worth nothing against a page of anonymous rows, and a
    # rule that lives only in the browser is a rule anybody can skip with a
    # curl command.
    key = _client_key(request)
    _refuse_if_limited(announce_join_limiter, key)
    announce_join_limiter.record_attempt(key)

    listener_name = ((payload or {}).get("name") or "").strip()
    if len(listener_name) < 2:
        raise HTTPException(
            status_code=400,
            detail="Please give your name, so the people running this "
                   "announcement know who is listening.")
    try:
        room, token = announcement_rooms.admit(
            engine,
            code=(payload or {}).get("id") or (payload or {}).get("code") or "",
            password=(payload or {}).get("password") or "",
            display_name=listener_name,
            verify_password=verify_password)
    except announcement_rooms.RoomRefused as refusal:
        # One refusal for a wrong ID and a wrong password: telling a stranger
        # that an ID exists but the password is wrong tells them which half to
        # keep guessing at.
        raise HTTPException(status_code=401, detail=str(refusal))
    announce_join_limiter.forget(key)
    _write_log(db, "info",
               f"announcement_room_joined code={room['public_code']}")
    return {"token": token, "room": {"public_code": room["public_code"],
                                     "label": room.get("label") or ""}}


@api.get("/announce/state")
def announcement_room_state(authorization: Optional[str] = Header(None)):
    """What this link should be playing, right now.

    Deliberately a poll rather than a socket. The state changes when somebody
    at HQ presses play or pause - minutes apart, not milliseconds - and a
    socket per listener would be a connection held open for a page that needs
    a sentence every few seconds.

    The page follows WHETHER the announcement is running, not where a
    particular shop's speaker has got to. Two people opening the link a minute
    apart are a minute apart in the audio, and the page says so rather than
    implying a sync it cannot deliver.
    """
    token = (authorization or "").removeprefix("Bearer ").strip()
    listener = announcement_rooms.listener_for_token(engine, token=token)
    if listener is None:
        raise HTTPException(
            status_code=401,
            detail="This listening link is no longer open. Ask for a new one.")

    template = None
    for row in announcement_service.list_templates(engine, status="all"):
        if row["id"] == listener["template_id"]:
            template = row
            break
    if template is None:
        return {"playing": False, "reason": "That announcement no longer exists.",
                "audio": None}

    live = announcement_service.template_is_live(template)
    first = (template.get("items") or [None])[0]
    audio_id = first.get("audio_id") if first else None

    # Whether ANY Store is actually running it. A link that kept playing a
    # campaign HQ had stopped is the one failure that would embarrass somebody
    # in front of a customer.
    # Told who is actually connected, like every other reader of this table.
    # This one was left out - and it is the one place the claim is made to a
    # member of the public, who hears the recording in their browser while no
    # shop is playing anything at all.
    running = any(row.get("template_id") == template["id"]
                  and row.get("state") == announcements.STATE_PLAYING
                  and row.get("reachable", True)
                  for row in announcement_service.live_status(
                      engine, connected_store_ids=manager.online_store_ids()))

    return {
        "label": listener.get("label") or template["name"],
        "template_name": template["name"],
        "playing": bool(live and running),
        "reason": ("" if live and running
                   else ("This announcement is paused right now."
                         if live else
                         f"This announcement is {announcement_service.describe_template_window(template)}.")),
        "audio": ({"id": audio_id,
                   "url": f"/api/announce/audio/{audio_id}",
                   "volume_percent": (first or {}).get("volume_percent", 80)}
                  if audio_id else None),
        "window": template["window"],
    }


@api.get("/announce/audio/{audio_id}")
def announcement_room_audio(audio_id: int,
                            authorization: Optional[str] = Header(None),
                            token: Optional[str] = None):
    """The recording itself, for an admitted listener.

    The token may arrive as a header OR as a query parameter, because an
    <audio> element cannot send headers. That is a real limitation of the
    element rather than a shortcut: the token is single-room, revocable by
    closing the link, and carries no account.
    """
    presented = ((authorization or "").removeprefix("Bearer ").strip()
                 or (token or "").strip())
    listener = announcement_rooms.listener_for_token(engine, token=presented)
    if listener is None:
        raise HTTPException(status_code=401,
                            detail="This listening link is no longer open.")

    # And only the recording THIS room's template plays. A room token that
    # could fetch any recording by number would be a link to one campaign that
    # opened every campaign.
    template = None
    for row in announcement_service.list_templates(engine, status="all"):
        if row["id"] == listener["template_id"]:
            template = row
            break
    allowed = {item.get("audio_id") for item in (template or {}).get("items", [])}
    if audio_id not in allowed:
        raise HTTPException(
            status_code=404,
            detail="That recording is not part of this announcement.")

    for row in announcement_service.list_audio(engine, status="all"):
        if row["id"] == audio_id:
            path = announcements.audio_directory() / row["storage_name"]
            if not path.is_file():
                raise HTTPException(status_code=404,
                                    detail="That recording is not on this HQ.")
            return FileResponse(str(path),
                                media_type=row["content_type"] or "audio/mpeg")
    raise HTTPException(status_code=404, detail="No such recording.")


@api.post("/announce/leave")
def leave_announcement_room(authorization: Optional[str] = Header(None)):
    token = (authorization or "").removeprefix("Bearer ").strip()
    announcement_rooms.leave(engine, token=token)
    return {"ok": True}


# ================ DASHBOARD ================

#: How far back an account without dashboard.full_history may look.
#:
#: A week, because that is the operating question - "what happened this week" -
#: and everything longer is comparison and trend, which is management
#: information.
DASHBOARD_LIMITED_DAYS = 7


@api.get("/dashboard/summary")
def dashboard_summary(
    days: int = 30,
    since: Optional[str] = None,
    until: Optional[str] = None,
    zone: Optional[str] = None,
    city: Optional[str] = None,
    store_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.dashboard.view")),
):
    """One screen's worth of numbers, computed in one place.

    WHY THE SERVER COMPUTES THESE

    Every one of these figures is an aggregate over the whole estate and its
    whole history. Sending the rows to the browser to add up would mean
    shipping thousands of them to draw six shapes - and, worse, it would mean
    the totals depended on which page happened to be loaded. A dashboard that
    disagrees with the list it summarises is a dashboard nobody trusts twice.

    SCOPE IS APPLIED, NOT ASSUMED

    A scoped operator sees their own Stores' figures. A total that quietly
    included shops the reader may not open would be a leak dressed as a
    statistic - "47 broadcasts" is itself information about an estate you were
    not given.

    WHAT IS DELIBERATELY ABSENT

    No "uptime", no "delivery rate", no health score. Every number here is
    something the database actually recorded: sessions started, minutes
    broadcast, which account did it, what an announcement did. A percentage
    that averages a Store nobody has heard from with a Store that is fine
    reads as reassurance, and reassurance is the one thing this product must
    not invent.
    """
    # An explicit range wins over the day count.
    #
    # "Today" and "yesterday" are not day counts: yesterday is a window with
    # BOTH ends, and expressing it as "last 1 day" would silently include this
    # morning. A caller that names dates gets exactly those dates.
    def parse_day(value, *, end_of_day=False):
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if end_of_day and len(str(value)) <= 10:
            moment = moment + timedelta(days=1) - timedelta(microseconds=1)
        return moment

    window_from = parse_day(since)
    window_to = parse_day(until, end_of_day=True)
    if window_from is None:
        window_from = datetime.now(timezone.utc) - timedelta(
            days=max(1, min(days, 365)))

    # How far back this account may look.
    #
    # A day-to-day operator answers "what happened this week"; a quarter of
    # history is where trends and comparisons live, and that is a different
    # kind of access. The window is CLAMPED rather than refused: a caller who
    # asks for a year gets the week they are allowed and is told so, which is
    # more useful than an error and cannot be mistaken for "nothing happened".
    permissions = resolve_effective_permissions(engine, user)
    horizon_days = None
    if "dashboard.full_history" not in permissions:
        horizon_days = DASHBOARD_LIMITED_DAYS
        earliest = datetime.now(timezone.utc) - timedelta(days=horizon_days)
        if window_from < earliest:
            window_from = earliest

    scope = resolve_store_scope(engine, user)

    query = db.query(BroadcastSession).filter(
        BroadcastSession.started_at.isnot(None),
        BroadcastSession.started_at >= window_from)
    if window_to is not None:
        query = query.filter(BroadcastSession.started_at <= window_to)
    owners = int_list(owner_user_id)
    if owners:
        query = query.filter(BroadcastSession.started_by.in_(owners))
    sessions = query.all()

    # Zone, city and Store narrow by what a broadcast REACHED, not by anything
    # about the session itself. "What happened in the North" is a question
    # about shops, and a session's own row knows nothing about shops.
    wanted_zones, wanted_cities = value_list(zone), value_list(city)
    wanted_stores = int_list(store_id)
    if wanted_zones or wanted_cities or wanted_stores:
        store_query = db.query(Store.id).filter(Store.is_active.is_(True))
        if wanted_zones:
            store_query = store_query.filter(Store.region.in_(wanted_zones))
        if wanted_cities:
            store_query = store_query.filter(Store.city.in_(wanted_cities))
        if wanted_stores:
            store_query = store_query.filter(Store.id.in_(wanted_stores))
        matching = {row[0] for row in store_query.all()}
        target_rows = db.query(BroadcastTarget).filter(
            BroadcastTarget.session_id.in_([s.id for s in sessions] or [0])).all()
        reached = {}
        for row in target_rows:
            reached.setdefault(row.session_id, set()).add(row.store_id)
        sessions = [s for s in sessions if reached.get(s.id, set()) & matching]
    else:
        matching = None

    if scope is not None:
        # A session counts if it reached at least one Store this account may
        # see. Counting by ownership instead would hide a colleague's
        # broadcast into shops this reader is responsible for.
        reachable = set(scope)
        target_rows = db.query(BroadcastTarget).filter(
            BroadcastTarget.session_id.in_([s.id for s in sessions] or [0])).all()
        by_session = {}
        for row in target_rows:
            by_session.setdefault(row.session_id, set()).add(row.store_id)
        sessions = [s for s in sessions
                    if by_session.get(s.id, set()) & reachable]

    def minutes(session) -> float:
        if not session.started_at:
            return 0.0
        end = session.ended_at or datetime.now(timezone.utc)
        start = session.started_at
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return max(0.0, (end - start).total_seconds() / 60.0)

    # ---- Per broadcaster ----------------------------------------------
    #
    # Counted AND timed, because they answer different questions. Ten
    # thirty-second interruptions and one five-minute campaign are not the
    # same working day, and a single "broadcasts" number cannot tell them
    # apart.
    #: Names for the accounts that appear, resolved once. The denormalised
    #: columns on BroadcastSession are empty on older rows, and labelling every
    #: one of them "unknown" makes the chart useless precisely where the
    #: history is longest.
    account_ids = {s.started_by for s in sessions if s.started_by}
    accounts = {row.id: (row.display_name or row.username)
                for row in db.query(HQUser).filter(
                    HQUser.id.in_(account_ids or [0])).all()}

    per_user: dict[str, dict] = {}
    for session in sessions:
        name = (session.started_by_display_name or session.started_by_username
                or accounts.get(session.started_by)
                or "no longer recorded")
        entry = per_user.setdefault(name, {"user": name, "broadcasts": 0,
                                           "minutes": 0.0})
        entry["broadcasts"] += 1
        entry["minutes"] += minutes(session)
    broadcasters = sorted(per_user.values(),
                          key=lambda row: row["minutes"], reverse=True)
    for row in broadcasters:
        row["minutes"] = round(row["minutes"], 1)

    # ---- Per day -------------------------------------------------------
    per_day: dict[str, dict] = {}
    for session in sessions:
        day = session.started_at.strftime("%Y-%m-%d")
        entry = per_day.setdefault(day, {"day": day, "broadcasts": 0,
                                         "minutes": 0.0})
        entry["broadcasts"] += 1
        entry["minutes"] += minutes(session)
    daily = sorted(per_day.values(), key=lambda row: row["day"])
    for row in daily:
        row["minutes"] = round(row["minutes"], 1)

    # ---- Per Store and per zone ----------------------------------------
    #
    # Built from the TARGET rows, because "how much did this shop hear" is not
    # a fact about a session - a five-minute broadcast to forty shops is five
    # minutes each, not five minutes divided by forty. Summing per Store is
    # the only reading that answers "which shops get interrupted most", which
    # is the question a district manager actually brings.
    session_by_id = {session.id: session for session in sessions}
    target_rows = db.query(BroadcastTarget).filter(
        BroadcastTarget.session_id.in_(list(session_by_id) or [0])).all()
    store_rows = {row.id: row for row in db.query(Store).filter(
        Store.id.in_({t.store_id for t in target_rows} or {0})).all()}

    per_store: dict[int, dict] = {}
    per_zone: dict[str, dict] = {}
    for target in target_rows:
        session = session_by_id.get(target.session_id)
        store = store_rows.get(target.store_id)
        if session is None or store is None:
            continue
        if scope is not None and store.id not in set(scope):
            continue
        if matching is not None and store.id not in matching:
            continue
        length = minutes(session)

        entry = per_store.setdefault(store.id, {
            "store_id": store.id, "store_code": store.store_code,
            "store_name": store.store_name, "zone": store.region,
            "city": store.city, "broadcasts": 0, "minutes": 0.0})
        entry["broadcasts"] += 1
        entry["minutes"] += length

        zone_entry = per_zone.setdefault(store.region or "unassigned", {
            "zone": store.region or "unassigned", "broadcasts": 0,
            "minutes": 0.0, "stores": set()})
        zone_entry["broadcasts"] += 1
        zone_entry["minutes"] += length
        zone_entry["stores"].add(store.id)

    by_store = sorted(per_store.values(), key=lambda row: row["minutes"],
                      reverse=True)
    for row in by_store:
        row["minutes"] = round(row["minutes"], 1)
    by_zone = sorted(per_zone.values(), key=lambda row: row["minutes"],
                     reverse=True)
    for row in by_zone:
        row["minutes"] = round(row["minutes"], 1)
        row["stores"] = len(row["stores"])

    # ---- Announcements, right now --------------------------------------
    # Told who is actually connected, so PLAYING stays a claim about shops HQ
    # can observe. Without this the tile counted every shop HQ had SENT a play
    # to - 44 playing with 2 online, which is the same lie the status table
    # used to tell and worse here, because a headline number gets believed.
    connected_now = manager.online_store_ids()
    playback = announcement_service.live_status(
        engine, connected_store_ids=connected_now)
    if scope is not None:
        playback = [row for row in playback if row["store_id"] in set(scope)]
    if matching is not None:
        playback = [row for row in playback if row["store_id"] in matching]
    states = {"PLAYING": 0, "PAUSED": 0, "DUCKED": 0, "STOPPED": 0,
              "UNREACHABLE": 0}
    for row in playback:
        if row["state"] in ("PLAYING", "DUCKED") and not row.get("reachable", True):
            # HQ asked and nothing answered. Counting it as playing would put
            # a number on audio nobody can hear.
            states["UNREACHABLE"] += 1
            continue
        states[row["state"]] = states.get(row["state"], 0) + 1

    # ---- Stores, right now ---------------------------------------------
    # Connected NOW, from the runtime - not the `status` column, which records
    # what a Store was last set to rather than whether it is on the end of a
    # socket at this moment.
    online = sum(1 for row in playback if row["store_id"] in connected_now)

    return {
        "since": window_from.isoformat(),
        "until": window_to.isoformat() if window_to else None,
        "days": days,
        "broadcasts": {
            "total": len(sessions),
            "minutes": round(sum(minutes(s) for s in sessions), 1),
            "live_now": sum(1 for s in sessions if s.status == "live"),
            # The longest one is worth its own number: a broadcast nobody
            # stopped is the failure this figure exists to surface, and an
            # average hides it by construction.
            "longest_minutes": round(max((minutes(s) for s in sessions),
                                         default=0.0), 1),
        },
        # Figures BY PERSON are their own right. Without it the dashboard
        # still answers "how much was the estate interrupted"; it simply does
        # not hand a shift manager a timesheet for their colleagues.
        "by_user": (broadcasters
                    if "dashboard.view_by_user" in permissions else []),
        "may_view_by_user": "dashboard.view_by_user" in permissions,
        "may_export": "dashboard.export" in permissions,
        #: Set when the window was shortened, so the page can say so rather
        #: than showing a quiet week and letting the reader draw a conclusion.
        "horizon_days": horizon_days,
        "by_day": daily,
        "by_store": by_store,
        "by_zone": by_zone,
        "announcements": {
            "states": states,
            "stores": len(playback),
        },
        "stores": {"total": len(playback), "online": online,
                   "offline": len(playback) - online},
    }




#: What each list may be sorted by, and how to read the value out of a row.
#:
#: An allowlist rather than "sort by whatever name arrives": a parameter that
#: can name any key is a way to probe what a row holds, and an unknown name
#: would otherwise fail a whole page instead of being ignored.
ANNOUNCEMENT_STATUS_SORTS = {
    "store_code": lambda row: row.get("store_code"),
    "store_name": lambda row: row.get("store_name"),
    "zone": lambda row: row.get("zone"),
    "state": lambda row: row.get("state"),
    "template_name": lambda row: row.get("template_name"),
    "audio_title": lambda row: row.get("audio_title"),
    "volume_percent": lambda row: row.get("volume_percent"),
    "updated_at": lambda row: row.get("updated_at"),
}

ANNOUNCEMENT_TEMPLATE_SORTS = {
    "name": lambda row: row.get("name"),
    "plays_in": lambda row: row.get("plays_in"),
    "status": lambda row: row.get("status"),
    "window": lambda row: row.get("window"),
    "starts_at": lambda row: row.get("starts_at"),
    "expires_at": lambda row: row.get("expires_at"),
    "created_at": lambda row: row.get("created_at"),
}

ANNOUNCEMENT_AUDIO_SORTS = {
    "title": lambda row: row.get("title"),
    "original_filename": lambda row: row.get("original_filename"),
    "byte_size": lambda row: row.get("byte_size"),
    "uploaded_at": lambda row: row.get("uploaded_at"),
    "status": lambda row: row.get("status"),
}

ANNOUNCEMENT_HISTORY_SORTS = {
    "store_code": lambda row: row.get("store_code"),
    "store_name": lambda row: row.get("store_name"),
    "zone": lambda row: row.get("zone"),
    "audio_title": lambda row: row.get("audio_title"),
    "template_name": lambda row: row.get("template_name"),
    "started_at": lambda row: row.get("started_at"),
    "ended_at": lambda row: row.get("ended_at"),
    "ended_reason": lambda row: row.get("ended_reason"),
    # Still-playing rows have no duration. Sorted as -1 so they group together
    # at one end instead of scattering among short runs.
    "duration_seconds": lambda row: (row.get("duration_seconds")
                                     if row.get("duration_seconds") is not None
                                     else -1),
}

# ================ RECORDED ANNOUNCEMENTS ================

#: How often the scheduler looks at the clock. Thirty seconds, because a shop
#: opening "at ten" means within half a minute of ten, and a tighter loop
#: would spend the day waking up to do nothing.
SCHEDULE_TICK_SECONDS = 30


async def _apply_announcement_schedules(*, now: datetime | None = None) -> dict:
    """Start and stop templates that carry a daily window.

    IT OPENS AND CLOSES THE WINDOW. IT DOES NOT POLICE WHAT HAPPENS INSIDE IT.
    A shop paused by a person at eleven stays paused at noon: a pause that a
    machine undoes thirty seconds later is not a pause, and an operator who
    silenced a shop for a reason must not have to argue with a timer.

    So: at the moment a window contains the time, any Store of that template
    which is STOPPED is started. At the moment it does not, any Store PLAYING
    that template is stopped. Everything else is left exactly as it is.
    """
    moment = now or datetime.now()
    started: list[int] = []
    stopped: list[int] = []
    connected = manager.online_store_ids()

    retired: list[int] = []

    for template in announcement_service.list_templates(engine, status="active"):
        # AN EXPIRED CAMPAIGN STOPS, AND STOPS BEING CHOSEN.
        #
        # Playing a template was already refused once its end date had passed,
        # and the daily window would not start it - but nothing stopped a shop
        # that was ALREADY playing when the date arrived. It carried on, and
        # the console went on naming the finished promotion as the thing that
        # shop was playing, which is how an expired jingle ran into a day it
        # was never meant to see.
        #
        # This clears the assignment, unlike Stop: the end date was decided in
        # advance by the person who set it, so nobody is surprised by it.
        if not announcement_service.template_is_live(template):
            for store_id in announcement_service.stores_for_template(
                    engine, template_id=template["id"]):
                current = announcement_service.get_playback(engine,
                                                            store_id=store_id)
                if current.get("template_id") != template["id"]:
                    continue
                row = announcement_service.retire(engine, store_id=store_id)
                await _dispatch_announcement(store_id, row)
                retired.append(store_id)
            continue

        window_start = template.get("daily_start")
        window_end = template.get("daily_end")
        if not window_start or not window_end:
            continue
        inside = announcement_schedule.is_within(
            window_start, window_end, template.get("daily_days"), moment)
        # A campaign whose dates have passed is never started by the clock -
        # the daily window says WHEN in the day, not whether the campaign is
        # still running at all.
        if inside and not announcement_service.template_is_live(template):
            inside = False

        target_stores = announcement_service.stores_for_template(
            engine, template_id=template["id"])
        first_item = (template.get("items") or [None])[0]
        first_audio = (first_item or {}).get("audio_id")

        for store_id in target_stores:
            current = announcement_service.get_playback(engine, store_id=store_id)
            state = current["state"]
            if inside:
                if state != announcements.STATE_STOPPED:
                    continue          # already running, ducked, or paused by a person
                row = announcement_service.set_state(
                    engine, store_id=store_id,
                    state=announcements.STATE_PLAYING,
                    template_id=template["id"], audio_id=first_audio,
                    actor_id=None, reachable=store_id in connected)
                await _dispatch_announcement(store_id, row)
                started.append(store_id)
            else:
                if (state == announcements.STATE_STOPPED
                        or current.get("template_id") != template["id"]):
                    continue
                # THE CLOCK PUTS AWAY WHAT THE CLOCK STARTED.
                #
                # Reported from the estate: somebody pressed Play, and twelve
                # seconds later the scheduler stopped it, because the
                # template's daily window had already closed. That is the
                # window overruling a person - the exact thing this scheduler
                # is written not to do at the other edge, and I missed it at
                # this one.
                #
                # A play started by hand carries the account that started it;
                # one the clock started carries nobody. So the clock only ever
                # closes its own, and a person who starts a campaign outside
                # its hours keeps it until they stop it themselves.
                if current.get("updated_by") is not None:
                    continue
                row = announcement_service.stop(engine, store_id=store_id,
                                                actor_id=None)
                await _dispatch_announcement(store_id, row)
                stopped.append(store_id)

    return {"started": started, "stopped": stopped, "retired": retired}


async def _announcement_schedule_loop() -> None:
    """The clock, running for as long as HQ does.

    Every failure is caught and logged rather than ending the loop: a
    scheduler that dies on one bad template stops opening every OTHER
    campaign's window, and does it silently.
    """
    while True:
        try:
            outcome = await _apply_announcement_schedules()
            if outcome["started"] or outcome["stopped"] or outcome["retired"]:
                logger.info(
                    "Announcement schedule: started %s, stopped %s, "
                    "retired %s (expired)",
                    len(outcome["started"]), len(outcome["stopped"]),
                    len(outcome["retired"]))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("The announcement scheduler could not run a tick")
        await asyncio.sleep(SCHEDULE_TICK_SECONDS)


async def _dispatch_announcement(store_id: int, row: dict) -> None:
    """Tell one Store Receiver what its announcement should be doing now.

    Called after every state change, from the one place that made it, so the
    database and the shop cannot disagree about what is playing. A Store that
    is offline is skipped silently: the state is recorded, and the Receiver
    asks for it when it reconnects - which is the same way broadcast targets
    already behave.

    Never raises. A Receiver that cannot be reached must not fail the HQ
    request that changed the state: the operator would see an error for an
    action that did take effect, and would press it again.
    """
    try:
        state = row.get("state")
        if state == announcements.STATE_PLAYING:
            audio_id = row.get("audio_id")
            if audio_id is None:
                return
            audio = None
            for candidate in announcement_service.list_audio(engine, status="all"):
                if candidate["id"] == audio_id:
                    audio = candidate
                    break
            if audio is None:
                return
            message = announcement_protocol.play_command(
                audio_id=audio_id,
                sha256=audio["sha256"],
                download_path=announcement_protocol.download_path(audio_id),
                content_type=audio.get("content_type") or "audio/mpeg",
                volume_percent=row.get("volume_percent",
                                       announcements.DEFAULT_VOLUME),
                template_id=row.get("template_id"),
            )
        elif state == announcements.STATE_PAUSED:
            message = announcement_protocol.pause_command(reason="hq")
        elif state == announcements.STATE_DUCKED:
            # Identical at the speaker, distinguishable in the Store's log.
            # A log that cannot tell a person pausing from a broadcast
            # arriving cannot answer "why did it go quiet at 4pm".
            message = announcement_protocol.pause_command(reason="broadcast")
        else:
            message = announcement_protocol.stop_command()
        await manager.send_to_receiver(store_id, message)
    except Exception:  # noqa: BLE001
        logger.exception("Could not send the announcement command to Store %s",
                         store_id)


async def _dispatch_announcement_volume(store_id: int, volume_percent: int) -> None:
    """Volume on its own, without restating what is playing.

    Sending a play command to change the level would restart the recording
    from the beginning - the shop would hear the jingle jump back to its first
    word every time somebody nudged the slider.
    """
    try:
        await manager.send_to_receiver(
            store_id,
            announcement_protocol.set_volume_command(
                volume_percent=volume_percent))
    except Exception:  # noqa: BLE001
        logger.exception("Could not set the announcement volume for Store %s",
                         store_id)


@api.get("/receiver/announcements/{audio_id}/download")
def download_announcement_for_receiver(
    audio_id: int,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """A Store fetching the recording it was told to play.

    Authenticated with the Receiver's OWN credential, through the same
    authenticator the audio socket uses - not with an HQ account, and not with
    a shared secret in the command. A download link that worked without a
    credential would let anybody who saw one command pull every recording the
    estate plays.
    """
    presented = (authorization or "").removeprefix("Bearer ").strip()
    if not presented:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    authenticator = getattr(app.state, "receiver_runtime_authenticator", None) \
        or default_receiver_runtime_authenticator
    try:
        identity = authenticator.authenticate(
            presented_token=presented,
            authenticated_at=datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - one refusal shape, deliberately
        raise HTTPException(status_code=401, detail="Not authenticated.")

    # ONLY a recording this Store is actually told to play.
    #
    # The credential sits on a shop-floor desktop. Without this, one Receiver
    # could walk the numbers and pull the estate's whole announcement library,
    # including campaigns for regions it has no part in - while this route's
    # own docstring claimed a narrower guarantee than the code delivered. The
    # listener link at /api/announce/audio already scopes exactly this way.
    #
    # The Store comes from the AUTHENTICATED IDENTITY, never from the request:
    # a caller who could name their own Store would be back where they started.
    store_id = getattr(identity, "store_id", None)
    if store_id is None:
        raise HTTPException(status_code=401, detail="Not authenticated.")
    reachable = set()
    for template in announcement_service.list_templates(engine, status="all"):
        if store_id in announcement_service.stores_for_template(
                engine, template_id=template["id"]):
            reachable.update(item.get("audio_id")
                             for item in (template.get("items") or []))
    if audio_id not in reachable:
        raise HTTPException(
            status_code=404,
            detail="That recording is not part of anything this Store plays.")

    for row in announcement_service.list_audio(engine, status="all"):
        if row["id"] == audio_id:
            path = announcements.audio_directory() / row["storage_name"]
            if not path.is_file():
                raise HTTPException(status_code=404,
                                    detail="That recording is not on this HQ.")
            return FileResponse(str(path),
                                media_type=row["content_type"] or "audio/mpeg",
                                headers={"X-SpeakLink-SHA256": row["sha256"]})
    raise HTTPException(status_code=404, detail="No such recording.")



#
# The permission on each route is the whole authorization story - there is no
# second check in the frontend that matters. Four separate codes because the
# four questions have different answers in a real shop: see permission_catalog.


def _announcement_or_404(audio_id: int) -> dict:
    for row in announcement_service.list_audio(engine, status="all"):
        if row["id"] == audio_id:
            return row
    raise HTTPException(status_code=404, detail="No such recording.")


def _template_or_404(template_id: int) -> dict:
    for row in announcement_service.list_templates(engine, status="all"):
        if row["id"] == template_id:
            return row
    raise HTTPException(status_code=404, detail="No such template.")


@api.get("/announcements/audio")
def list_announcement_audio(
    q: Optional[str] = None,
    status: str = "active",
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    user: HQUser = Depends(require("menu.announcements.view")),
):
    page, page_size = normalize_paging(page, page_size)
    rows = announcement_service.list_audio(engine, search=q or "", status=status)
    rows = sort_rows(rows, sort, dir, ANNOUNCEMENT_AUDIO_SORTS)
    offset = (page - 1) * page_size
    return Page(items=rows[offset:offset + page_size], total=len(rows),
                page=page, page_size=page_size).as_dict()


@api.post("/announcements/audio", status_code=201)
async def upload_announcement_audio(
    file: UploadFile = File(...),
    title: str = Form(""),
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.upload")),
):
    """Put a new recording on HQ.

    Refused before a byte is written, and stored under a name this program
    chose. The uploaded filename is kept only to show back to the person who
    uploaded it: it is the one part of an upload a stranger picks, so it
    decides nothing about where the bytes land.
    """
    raw = await file.read()
    try:
        extension = announcements.validate_upload(
            raw, file.content_type or "", file.filename or "")
    except announcements.AnnouncementRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    storage_name = announcements.new_storage_name(extension)
    destination = announcements.audio_directory() / storage_name
    destination.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()

    with engine.begin() as connection:
        result = connection.execute(text(
            "INSERT INTO " + announcements.AUDIO_TABLE +
            " (title, original_filename, storage_name, content_type,"
            "  byte_size, sha256, uploaded_by, uploaded_at, status)"
            " VALUES (:title, :original, :storage, :content_type, :size,"
            "         :digest, :uploaded_by, :uploaded_at, 'active')"), {
            "title": (title or file.filename or "Untitled").strip()[:200],
            "original": (file.filename or "")[:255],
            "storage": storage_name,
            "content_type": (file.content_type or "").split(";", 1)[0],
            "size": len(raw), "digest": digest,
            "uploaded_by": user.id,
            "uploaded_at": announcements.utcnow().isoformat(),
        })
        new_id = result.lastrowid
    _write_log(db, "info",
               f"announcement_uploaded id={new_id} bytes={len(raw)} by={user.username}")
    return _announcement_or_404(new_id)


@api.get("/announcements/audio/{audio_id}/stream")
def stream_announcement_audio(
    audio_id: int,
    user: HQUser = Depends(require("menu.announcements.view")),
):
    """Play the recording back at HQ, so somebody can hear what they are about
    to send to a shop before they send it."""
    row = _announcement_or_404(audio_id)
    path = announcements.audio_directory() / row["storage_name"]
    if not path.is_file():
        raise HTTPException(
            status_code=404,
            detail="The recording is listed but its file is missing from this "
                   "HQ. Upload it again.")
    return FileResponse(str(path), media_type=row["content_type"] or "audio/mpeg")


@api.delete("/announcements/audio/{audio_id}")
def archive_announcement_audio(
    audio_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.upload")),
):
    """Archive, not delete. A template may still name this recording and the
    history of what played must stay readable; permanent deletion is its own
    permission and its own route."""
    row = _announcement_or_404(audio_id)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE " + announcements.AUDIO_TABLE +
            " SET status = 'archived' WHERE id = :id"), {"id": audio_id})
    _write_log(db, "info",
               f"announcement_archived id={audio_id} by={user.username}")
    return {"ok": True, "id": audio_id, "title": row["title"]}




@api.post("/announcements/audio/{audio_id}/delete-permanently")
def delete_announcement_audio_permanently(
    audio_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.delete_permanently")),
):
    """Really gone: the row AND the file on disk.

    WHY THIS EXISTS SEPARATELY FROM ARCHIVING

    Archiving was the only option, and the button that did it wore a wastebin.
    Somebody archived a recording, watched it vanish from the list, and found
    the bytes still on the server - the interface had said "deleted" and meant
    "hidden". Two different actions were being spelled the same way.

    So there are now two, and they are honestly named. Archiving is the
    everyday one and is reversible. This one is not, which is why it needs its
    own permission, the typed confirmation word, and a refusal when a template
    still depends on the recording: deleting the audio out from under a live
    campaign would leave a template that plays nothing and says nothing about
    why.
    """
    confirmation = (payload or {}).get("confirmation", "")
    if str(confirmation).strip().upper() != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="Type DELETE to confirm. This removes the recording and its "
                   "file permanently and cannot be undone.")

    row = _announcement_or_404(audio_id)
    users = [template["name"] for template in
             announcement_service.list_templates(engine, status="all")
             if any(item.get("audio_id") == audio_id
                    for item in template.get("items") or [])]
    if users:
        raise HTTPException(
            status_code=409,
            detail=("This recording is still used by: " + ", ".join(users[:5])
                    + ". Remove it from those templates first, or archive it "
                      "instead - deleting it would leave a template that plays "
                      "nothing and cannot say why."))

    # The file first. A row without its file is a listing that fails when
    # somebody presses play; a file without its row is bytes nobody can reach,
    # which is untidy but harmless. If the delete of one has to fail, this is
    # the order that fails the safer way.
    path = announcements.audio_directory() / row["storage_name"]
    try:
        if path.is_file():
            path.unlink()
    except OSError as failure:
        raise HTTPException(
            status_code=500,
            detail=f"The recording's file could not be removed ({failure}). "
                   "Nothing has been deleted.")

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM " + announcements.AUDIO_TABLE + " WHERE id = :id"),
            {"id": audio_id})
    _write_log(db, "warn",
               f"announcement_deleted_permanently id={audio_id} "
               f"title={row['title']!r} by={user.username}")
    return {"ok": True, "id": audio_id, "title": row["title"]}


# ---- Templates ----------------------------------------------------------

@api.get("/announcements/templates")
def list_announcement_templates(
    q: Optional[str] = None,
    status: str = "active",
    zone: Optional[str] = None,
    store_id: Optional[str] = None,
    window: Optional[str] = None,
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    user: HQUser = Depends(require("menu.announcements.view")),
):
    page, page_size = normalize_paging(page, page_size)
    rows = announcement_service.list_templates(
        engine, search=q or "", status=status, zone=zone or "",
        store_id=store_id, window=window or "")
    rows = sort_rows(rows, sort, dir, ANNOUNCEMENT_TEMPLATE_SORTS)
    offset = (page - 1) * page_size
    return Page(items=rows[offset:offset + page_size], total=len(rows),
                page=page, page_size=page_size).as_dict()


@api.post("/announcements/templates", status_code=201)
def create_announcement_template(
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.templates.manage")),
):
    """The point of the whole feature: decide once what plays where and until
    when, then never choose again - only press play and pause."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A template needs a name.")
    items = payload.get("items") or []
    if not items:
        raise HTTPException(
            status_code=400,
            detail="A template with no lines plays nothing. Add at least one "
                   "recording and the Store or zone it plays in.")
    for item in items:
        try:
            announcements.item_targets_exactly_one(item.get("store_id"),
                                                   item.get("zone"))
            announcements.validate_volume(item.get("volume_percent",
                                                   announcements.DEFAULT_VOLUME))
        except announcements.AnnouncementRefused as refusal:
            raise HTTPException(status_code=400, detail=str(refusal))

    try:
        daily_start, daily_end = announcement_schedule.validate_window(
            payload.get("daily_start"), payload.get("daily_end"))
        daily_days = (announcement_schedule.parse_days(payload.get("daily_days"))
                      if daily_start else "")
    except announcement_schedule.ScheduleRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    now = announcements.utcnow().isoformat()
    with engine.begin() as connection:
        result = connection.execute(text(
            "INSERT INTO " + announcements.TEMPLATE_TABLE +
            " (name, description, created_by, created_at, updated_at,"
            "  starts_at, expires_at, daily_start, daily_end, daily_days,"
            "  status)"
            " VALUES (:name, :description, :created_by, :now, :now,"
            "         :starts_at, :expires_at, :daily_start, :daily_end,"
            "         :daily_days, 'active')"), {
            "daily_start": daily_start, "daily_end": daily_end,
            "daily_days": daily_days,
            "name": name[:120],
            "description": (payload.get("description") or "")[:500],
            "created_by": user.id, "now": now,
            "starts_at": payload.get("starts_at") or None,
            "expires_at": payload.get("expires_at") or None,
        })
        template_id = result.lastrowid
        for position, item in enumerate(items):
            connection.execute(text(
                "INSERT INTO " + announcements.ITEM_TABLE +
                " (template_id, audio_id, store_id, zone, position,"
                "  volume_percent)"
                " VALUES (:template_id, :audio_id, :store_id, :zone, :position,"
                "         :volume)"), {
                "template_id": template_id, "audio_id": item.get("audio_id"),
                "store_id": item.get("store_id"), "zone": item.get("zone"),
                "position": position,
                "volume": announcements.validate_volume(
                    item.get("volume_percent", announcements.DEFAULT_VOLUME)),
            })
    _write_log(db, "info",
               f"announcement_template_created id={template_id} "
               f"lines={len(items)} by={user.username}")
    return _template_or_404(template_id)


@api.put("/announcements/templates/{template_id}")
def update_announcement_template(
    template_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.templates.manage")),
):
    """Edit a template in place: its name, its window, and its lines.

    WHY THE LINES ARE REPLACED RATHER THAN PATCHED

    A template's lines are one decision - "this recording, in these places" -
    and editing them one at a time would mean inventing ids for rows nobody
    ever refers to. The whole set arrives together and replaces the old set in
    one transaction, so a half-applied edit cannot leave a campaign playing a
    recording in shops it was just taken out of.

    Editing does NOT stop what is currently playing. A shop already running
    this template keeps running, and picks up the new lines the next time it
    is started - stopping the estate because somebody fixed a typo in a name
    would be a far worse surprise than the delay.
    """
    existing = _template_or_404(template_id)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="A template needs a name.")
    items = payload.get("items")
    if items is not None and not items:
        raise HTTPException(
            status_code=400,
            detail="A template with no lines plays nothing. Add at least one "
                   "recording and the Store or zone it plays in.")
    if items is not None:
        for item in items:
            try:
                announcements.item_targets_exactly_one(item.get("store_id"),
                                                       item.get("zone"))
                announcements.validate_volume(
                    item.get("volume_percent", announcements.DEFAULT_VOLUME))
            except announcements.AnnouncementRefused as refusal:
                raise HTTPException(status_code=400, detail=str(refusal))

    try:
        # Absent means "leave the schedule alone"; empty means "no schedule".
        # Collapsing those would drop a shop-hours window off a template
        # somebody only renamed.
        if "daily_start" in payload or "daily_end" in payload:
            daily_start, daily_end = announcement_schedule.validate_window(
                payload.get("daily_start"), payload.get("daily_end"))
            daily_days = (announcement_schedule.parse_days(payload.get("daily_days"))
                          if daily_start else "")
        else:
            daily_start = existing.get("daily_start") or ""
            daily_end = existing.get("daily_end") or ""
            daily_days = existing.get("daily_days") or ""
    except announcement_schedule.ScheduleRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))

    now = announcements.utcnow().isoformat()
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE " + announcements.TEMPLATE_TABLE +
            " SET name = :name, description = :description,"
            "     starts_at = :starts_at, expires_at = :expires_at,"
            "     daily_start = :daily_start, daily_end = :daily_end,"
            "     daily_days = :daily_days,"
            "     updated_at = :now"
            " WHERE id = :id"), {
            "id": template_id,
            "daily_start": daily_start, "daily_end": daily_end,
            "daily_days": daily_days,
            "name": name[:120],
            "description": (payload.get("description") or "")[:500],
            # Absent means "leave it"; empty string means "no limit". Those are
            # different intentions and collapsing them would silently drop the
            # end date off a campaign somebody only renamed.
            "starts_at": (payload["starts_at"] or None
                          if "starts_at" in payload else existing.get("starts_at")),
            "expires_at": (payload["expires_at"] or None
                           if "expires_at" in payload else existing.get("expires_at")),
            "now": now,
        })
        if items is not None:
            connection.execute(text(
                "DELETE FROM " + announcements.ITEM_TABLE +
                " WHERE template_id = :id"), {"id": template_id})
            for position, item in enumerate(items):
                connection.execute(text(
                    "INSERT INTO " + announcements.ITEM_TABLE +
                    " (template_id, audio_id, store_id, zone, position,"
                    "  volume_percent)"
                    " VALUES (:template_id, :audio_id, :store_id, :zone,"
                    "         :position, :volume)"), {
                    "template_id": template_id, "audio_id": item.get("audio_id"),
                    "store_id": item.get("store_id"), "zone": item.get("zone"),
                    "position": position,
                    "volume": announcements.validate_volume(
                        item.get("volume_percent", announcements.DEFAULT_VOLUME)),
                })
    _write_log(db, "info",
               f"announcement_template_updated id={template_id} by={user.username}")
    return _template_or_404(template_id)


@api.put("/announcements/audio/{audio_id}")
def update_announcement_audio(
    audio_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.upload")),
):
    """Rename a recording.

    The title only. The FILE is deliberately not replaceable: templates,
    history and every Store's cache all point at this recording by id and by
    content hash, and swapping the audio underneath would rewrite what a shop
    played last week without leaving a trace. To play something else, upload
    it and point the template at it - which is one action longer and honest.

    Behind announcements.upload rather than templates.manage: this is the
    right that says who owns the library of recordings.
    """
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="A recording needs a title.")
    with engine.begin() as connection:
        changed = connection.execute(text(
            "UPDATE " + announcements.AUDIO_TABLE +
            " SET title = :title WHERE id = :id"),
            {"id": audio_id, "title": title[:200]}).rowcount
    if not changed:
        raise HTTPException(status_code=404, detail="No such recording.")
    _write_log(db, "info",
               f"announcement_audio_renamed id={audio_id} by={user.username}")
    for row in announcement_service.list_audio(engine, status="all"):
        if row["id"] == audio_id:
            return row
    raise HTTPException(status_code=404, detail="No such recording.")


async def _stop_stores_running_template(template_id: int, *, actor_id=None) -> list[int]:
    """Stop every Store pointed at this template, and forget the pointer.

    Clearing template_id and audio_id is the half that was missing. A Store row
    still naming a template nobody can open reads, on the console, as a shop
    playing something - and there is no way to act on it from there. "Nothing
    chosen" is both true and actionable.
    """
    stopped = []
    for row in announcement_service.live_status(engine):
        if row.get("template_id") != template_id:
            continue
        updated = announcement_service.set_state(
            engine, store_id=row["store_id"],
            state=announcements.STATE_STOPPED, actor_id=actor_id)
        await _dispatch_announcement(row["store_id"], updated)
        stopped.append(row["store_id"])
    if stopped:
        with engine.begin() as connection:
            connection.execute(text(
                "UPDATE " + announcements.PLAYBACK_TABLE +
                " SET template_id = NULL, audio_id = NULL "
                "WHERE template_id = :template_id"), {"template_id": template_id})
    return stopped


@api.delete("/announcements/templates/{template_id}")
async def archive_announcement_template(
    template_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.templates.manage")),
):
    """Withdraw the plan - which means the shops running it stop.

    They did not, and a Store showed it: a template was archived, vanished
    from the list, and Krishna Nagar went on reporting it as the thing it was
    playing. There was no row left to press Pause on, so the only way to
    silence that shop was to know its Store page.

    Archiving a plan and leaving shops running it is not a smaller version of
    withdrawing it. It is the plan being gone from everywhere except the one
    place it is actually happening.
    """
    _template_or_404(template_id)
    with engine.begin() as connection:
        connection.execute(text(
            "UPDATE " + announcements.TEMPLATE_TABLE +
            " SET status = 'archived', updated_at = :now WHERE id = :id"),
            {"id": template_id, "now": announcements.utcnow().isoformat()})

    stopped = await _stop_stores_running_template(template_id, actor_id=user.id)
    _write_log(db, "info",
               f"announcement_template_archived id={template_id} "
               f"stopped={len(stopped)} by={user.username}")
    return {"ok": True, "id": template_id, "stopped_stores": stopped,
            "note": (f"Archived. {len(stopped)} Store(s) were playing it and "
                     "have been stopped.") if stopped else "Archived."}




@api.get("/announcements/history")
def announcement_history(
    q: Optional[str] = None,
    zone: Optional[str] = None,
    reason: Optional[str] = None,
    store_id: Optional[str] = None,
    template_id: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    include_archived: bool = False,
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    user: HQUser = Depends(require("menu.announcements.view")),
):
    """What played, where, and why it stopped.

    Rows OPEN and CLOSE rather than being a stream of events, because the
    question people ask is "what was this shop playing at four o'clock" - and
    answering that from events means replaying them, while a row with a start
    and an end answers it with a comparison.

    ``reason=open`` selects what is still playing. That is the ABSENCE of an
    end rather than a reason, and spelling it here is better than leaving
    somebody to discover that an empty filter means something different.
    """
    page, page_size = normalize_paging(page, page_size)
    rows = announcement_service.list_history(
        engine, connected_store_ids=manager.online_store_ids(), search=q or "", zone=zone or "", reason=reason or "",
        store_id=store_id, template_id=template_id,
        since=since or "", until=until or "",
        include_archived=include_archived)
    rows = sort_rows(rows, sort, dir, ANNOUNCEMENT_HISTORY_SORTS)
    offset = (page - 1) * page_size
    return Page(items=rows[offset:offset + page_size], total=len(rows),
                page=page, page_size=page_size).as_dict()


@api.post("/announcements/history/{entry_id}/archive")
def archive_announcement_history(
    entry_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.templates.manage")),
):
    """Out of the everyday list, still in the record.

    The same shape as Broadcast History: archiving is reversible and is the
    everyday tidy-up; destroying the record of what a shop played is a
    different act with a different permission.
    """
    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE " + announcements.HISTORY_TABLE +
            " SET archived_at = :now WHERE id = :id AND archived_at IS NULL"),
            {"id": entry_id, "now": announcements.utcnow().isoformat()})
    if result.rowcount == 0:
        raise HTTPException(status_code=404,
                            detail="No such history entry, or it is already archived.")
    _write_log(db, "info",
               f"announcement_history_archived id={entry_id} by={user.username}")
    return {"ok": True, "id": entry_id}


@api.post("/announcements/history/{entry_id}/delete-permanently")
def delete_announcement_history_permanently(
    entry_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.delete_permanently")),
):
    """Really gone.

    Its own permission and the typed word, because this destroys the answer to
    "what was that shop playing" for a moment that has passed - and unlike a
    recording, there is nothing to re-upload.
    """
    if str((payload or {}).get("confirmation", "")).strip().upper() != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="Type DELETE to confirm. This removes the record of what "
                   "played and cannot be undone.")
    with engine.begin() as connection:
        result = connection.execute(text(
            "DELETE FROM " + announcements.HISTORY_TABLE + " WHERE id = :id"),
            {"id": entry_id})
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such history entry.")
    _write_log(db, "warn",
               f"announcement_history_deleted id={entry_id} by={user.username}")
    return {"ok": True, "id": entry_id}




@api.post("/announcements/templates/{template_id}/delete-permanently")
async def delete_announcement_template_permanently(
    template_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.delete_permanently")),
):
    """Really gone: the template and its lines.

    Archiving was the only option here too, and it had the same problem the
    recordings had - the row left the list and stayed in the database, so
    "delete" meant "hide". Both actions exist now and are named for what they
    do.

    Any Store still playing this template is STOPPED first, and told. Deleting
    the plan while shops are running it would leave them playing something
    with no name, which nobody could then pause from the templates list
    because the list no longer has a row to press.

    The history is deliberately untouched: it carries its own copy of the
    template's name, so what a shop played last week remains readable after
    the plan itself is gone.
    """
    if str((payload or {}).get("confirmation", "")).strip().upper() != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="Type DELETE to confirm. This removes the template and its "
                   "lines permanently and cannot be undone.")

    template = _template_or_404(template_id)

    playing = await _stop_stores_running_template(template_id, actor_id=user.id)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM " + announcements.ITEM_TABLE +
            " WHERE template_id = :id"), {"id": template_id})
        connection.execute(text(
            "DELETE FROM " + announcements.TEMPLATE_TABLE + " WHERE id = :id"),
            {"id": template_id})

    _write_log(db, "warn",
               f"announcement_template_deleted_permanently id={template_id} "
               f"name={template['name']!r} stopped={len(playing)} "
               f"by={user.username}")
    return {"ok": True, "id": template_id, "name": template["name"],
            "stopped_stores": playing,
            "note": (f"Deleted. {len(playing)} Store(s) were playing it and "
                     "have been stopped.") if playing else "Deleted."}




def _announcement_history_ids_matching(filters, _db=None) -> list[int]:
    """Every id the CURRENT filters select, not just the page on screen.

    This is what makes "Select All Filtered (184)" mean what it says. Resolving
    it on the server rather than in the browser is the point: the browser only
    ever holds one page, so a selection built there could only ever act on
    fifty rows while claiming to act on all of them.
    """
    rows = announcement_service.list_history(
        engine,
        search=(filters or {}).get("q") or "",
        zone=(filters or {}).get("zone") or "",
        reason=(filters or {}).get("reason") or "",
        since=(filters or {}).get("since") or "",
        until=(filters or {}).get("until") or "",
        include_archived=str((filters or {}).get("include_archived")).lower()
                         in ("1", "true", "yes"),
    )
    return [row["id"] for row in rows]


@api.post("/announcements/history/archive")
def archive_announcement_history_bulk(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.templates.manage")),
):
    ids, matched = _resolve_bulk(payload, _announcement_history_ids_matching)
    if not ids:
        return {"requested": 0, "affected": 0, "matched": matched}
    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE " + announcements.HISTORY_TABLE +
            " SET archived_at = :now WHERE id IN :ids AND archived_at IS NULL"
        ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids, "now": announcements.utcnow().isoformat()})
    _write_log(db, "info",
               f"announcement_history_archived count={result.rowcount} "
               f"by={user.username}")
    return {"requested": len(ids), "affected": result.rowcount, "matched": matched}


@api.post("/announcements/history/unarchive")
def unarchive_announcement_history_bulk(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.templates.manage")),
):
    ids, matched = _resolve_bulk(payload, _announcement_history_ids_matching)
    if not ids:
        return {"requested": 0, "affected": 0, "matched": matched}
    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE " + announcements.HISTORY_TABLE +
            " SET archived_at = NULL WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids})
    _write_log(db, "info",
               f"announcement_history_unarchived count={result.rowcount} "
               f"by={user.username}")
    return {"requested": len(ids), "affected": result.rowcount, "matched": matched}


@api.post("/announcements/history/delete")
def delete_announcement_history_bulk(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.delete_permanently")),
):
    """Permanent, for many rows at once.

    The typed word is still required, and it is required ONCE for the whole
    selection rather than once per row. Asking two hundred times is not two
    hundred times the protection - it is a person clicking through a dialog
    without reading it, which is less protection than asking once and saying
    how many rows are about to go.
    """
    if str(payload.confirm or "").strip().upper() != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="Type DELETE to confirm. This removes the record of what "
                   "played and cannot be undone.")
    ids, matched = _resolve_bulk(payload, _announcement_history_ids_matching)
    if not ids:
        return {"requested": 0, "affected": 0, "matched": matched}
    with engine.begin() as connection:
        result = connection.execute(text(
            "DELETE FROM " + announcements.HISTORY_TABLE + " WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids})
    _write_log(db, "warn",
               f"announcement_history_deleted count={result.rowcount} "
               f"by={user.username}")
    return {"requested": len(ids), "affected": result.rowcount, "matched": matched}




# ---- Bulk actions on recordings and templates ---------------------------
#
# The same two-mode selection the History and System Logs pages use: explicit
# ids for "this page", or the current filters resolved on the SERVER for "all
# filtered". The browser holds one page, so a selection built there could only
# ever act on one page while a button claimed otherwise.

def _audio_ids_matching(filters, _db=None) -> list[int]:
    return [row["id"] for row in announcement_service.list_audio(
        engine, search=(filters or {}).get("q") or "",
        status=(filters or {}).get("status") or "active")]


def _template_ids_matching(filters, _db=None) -> list[int]:
    return [row["id"] for row in announcement_service.list_templates(
        engine, search=(filters or {}).get("q") or "",
        status=(filters or {}).get("status") or "active",
        zone=(filters or {}).get("zone") or "")]


@api.post("/announcements/audio/archive")
def archive_announcement_audio_bulk(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.upload")),
):
    ids, matched = _resolve_bulk(payload, _audio_ids_matching)
    if not ids:
        return {"requested": 0, "affected": 0, "matched": matched}
    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE " + announcements.AUDIO_TABLE +
            " SET status = 'archived' WHERE id IN :ids AND status = 'active'"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids})
    _write_log(db, "info",
               f"announcement_audio_archived count={result.rowcount} by={user.username}")
    return {"requested": len(ids), "affected": result.rowcount, "matched": matched}


@api.post("/announcements/audio/delete")
def delete_announcement_audio_bulk(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.delete_permanently")),
):
    """Permanent, for several recordings at once.

    Recordings still used by a template are SKIPPED rather than failing the
    whole call, and the answer names them. Failing everything because one of
    forty is in use would leave the operator selecting the other thirty-nine
    by hand; silently deleting it would leave a template that plays nothing
    and cannot say why.
    """
    if str(payload.confirm or "").strip().upper() != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="Type DELETE to confirm. This removes the recordings and "
                   "their files permanently and cannot be undone.")
    ids, matched = _resolve_bulk(payload, _audio_ids_matching)
    if not ids:
        return {"requested": 0, "affected": 0, "matched": matched, "skipped": []}

    in_use = {}
    for template in announcement_service.list_templates(engine, status="all"):
        for item in template.get("items") or []:
            if item.get("audio_id") in ids:
                in_use.setdefault(item["audio_id"], template["name"])

    deletable = [audio_id for audio_id in ids if audio_id not in in_use]
    rows = {row["id"]: row for row in announcement_service.list_audio(
        engine, status="all")}
    removed = 0
    for audio_id in deletable:
        row = rows.get(audio_id)
        if row is None:
            continue
        path = announcements.audio_directory() / row["storage_name"]
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            # Reported through the count, not raised: one locked file must not
            # abandon the other thirty-nine half-way through.
            continue
        with engine.begin() as connection:
            connection.execute(text(
                "DELETE FROM " + announcements.AUDIO_TABLE + " WHERE id = :id"),
                {"id": audio_id})
        removed += 1

    _write_log(db, "warn",
               f"announcement_audio_deleted count={removed} "
               f"skipped={len(in_use)} by={user.username}")
    return {
        "requested": len(ids), "affected": removed, "matched": matched,
        "skipped": [{"id": audio_id, "used_by": name}
                    for audio_id, name in in_use.items()],
        "note": (f"{removed} deleted. {len(in_use)} kept because a template "
                 "still uses them.") if in_use else f"{removed} deleted.",
    }


@api.post("/announcements/templates/archive")
def archive_announcement_templates_bulk(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.templates.manage")),
):
    ids, matched = _resolve_bulk(payload, _template_ids_matching)
    if not ids:
        return {"requested": 0, "affected": 0, "matched": matched}
    with engine.begin() as connection:
        result = connection.execute(text(
            "UPDATE " + announcements.TEMPLATE_TABLE +
            " SET status = 'archived', updated_at = :now "
            "WHERE id IN :ids AND status = 'active'"
        ).bindparams(bindparam("ids", expanding=True)),
            {"ids": ids, "now": announcements.utcnow().isoformat()})
    _write_log(db, "info",
               f"announcement_templates_archived count={result.rowcount} "
               f"by={user.username}")
    return {"requested": len(ids), "affected": result.rowcount, "matched": matched}


@api.post("/announcements/templates/delete")
async def delete_announcement_templates_bulk(
    payload: BulkIdsRequest,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.delete_permanently")),
):
    """Permanent, for several templates at once.

    Every shop still playing one of them is STOPPED first and the answer says
    how many - deleting a plan while shops run it leaves them playing
    something with no name, and no row in the list to press Pause on.
    """
    if str(payload.confirm or "").strip().upper() != "DELETE":
        raise HTTPException(
            status_code=400,
            detail="Type DELETE to confirm. This removes the templates and "
                   "their lines permanently and cannot be undone.")
    ids, matched = _resolve_bulk(payload, _template_ids_matching)
    if not ids:
        return {"requested": 0, "affected": 0, "matched": matched}

    playing = [row for row in announcement_service.live_status(engine)
               if row.get("template_id") in ids
               and row.get("state") != announcements.STATE_STOPPED]
    for row in playing:
        updated = announcement_service.set_state(
            engine, store_id=row["store_id"],
            state=announcements.STATE_STOPPED, actor_id=user.id)
        await _dispatch_announcement(row["store_id"], updated)

    with engine.begin() as connection:
        connection.execute(text(
            "DELETE FROM " + announcements.ITEM_TABLE +
            " WHERE template_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids})
        result = connection.execute(text(
            "DELETE FROM " + announcements.TEMPLATE_TABLE + " WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": ids})

    _write_log(db, "warn",
               f"announcement_templates_deleted count={result.rowcount} "
               f"stopped={len(playing)} by={user.username}")
    return {
        "requested": len(ids), "affected": result.rowcount, "matched": matched,
        "stopped_stores": [row["store_id"] for row in playing],
        "note": (f"{result.rowcount} deleted. {len(playing)} Store(s) were "
                 "playing them and have been stopped.") if playing
                else f"{result.rowcount} deleted.",
    }


# ---- Playing and pausing ------------------------------------------------

@api.get("/announcements/status")
def announcement_status(
    q: Optional[str] = None,
    zone: Optional[str] = None,
    state: Optional[str] = None,
    store_id: Optional[str] = None,
    sort: Optional[str] = None,
    dir: str = "asc",
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    user: HQUser = Depends(require("menu.announcements.view")),
):
    """What every Store is doing right now.

    Every active Store appears, including the ones that have never played
    anything. Listing only Stores with a playback row would hide exactly the
    shops somebody is looking for when they ask why a campaign is not running
    everywhere.
    """
    page, page_size = normalize_paging(page, page_size)
    rows = announcement_service.live_status(
        engine, search=q or "", zone=zone or "", state=state or "",
        store_id=store_id,
        connected_store_ids=manager.online_store_ids())
    # WHICH RECEIVER EACH SHOP IS RUNNING.
    #
    # Announcements need a Receiver new enough to have them. A day was spent
    # on a shop that could not have played anything because it was running a
    # build from before the feature existed - and every screen HQ has was
    # silent about that while showing the campaign as live.
    for row in rows:
        row["receiver_version"] = RECEIVER_VERSIONS.get(row["store_id"], "")
        row["announcements_supported"] = _supports_announcements(
            row["receiver_version"])
        # THE SHOP'S OWN LEVEL, when it has told us.
        #
        # `volume_percent` is what HQ last SET. This is what the speaker is
        # actually on, including a change somebody made at the till - which
        # the console could not see at all, so it showed a number nobody had
        # touched in hours as though it were the shop's.
        reported = STORE_MASTER_VOLUME.get(row["store_id"])
        row["store_volume_percent"] = (reported or {}).get("volume_percent")
        row["store_muted"] = (reported or {}).get("muted")
    rows = sort_rows(rows, sort, dir, ANNOUNCEMENT_STATUS_SORTS)
    offset = (page - 1) * page_size
    return Page(items=rows[offset:offset + page_size], total=len(rows),
                page=page, page_size=page_size).as_dict()


@api.post("/announcements/templates/{template_id}/play")
async def play_announcement_template(
    template_id: int,
    payload: Optional[dict] = None,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.control")),
):
    """Start a template in the Stores it names - or in a subset of them.

    One user can run several templates in several places at once. Nothing here
    is owned by a session or a person: pressing play records who did it and
    moves on, so a colleague can pause it without needing the first person's
    account.
    """
    template = _template_or_404(template_id)
    if not announcement_service.template_is_live(template):
        window = announcement_service.describe_template_window(template)
        raise HTTPException(
            status_code=400,
            detail=f"That template is {window}, so it will not play. Change "
                   "its dates or use another one.")

    wanted = (payload or {}).get("store_ids")
    reachable = announcement_service.stores_for_template(engine,
                                                         template_id=template_id)
    targets = [s for s in reachable if s in set(wanted)] if wanted else reachable
    if not targets:
        raise HTTPException(
            status_code=400,
            detail="That template reaches no active Store. Its Stores may have "
                   "been archived, or its zone may now be empty.")

    first_audio = (template["items"][0].get("audio_id")
                   if template.get("items") else None)
    started, refused = [], []
    # Read once, not per Store: this is a live registry, and forty lookups
    # against a moving set is forty chances for the same run to be half
    # recorded and half not.
    connected_now = manager.online_store_ids()
    for store_id in targets:
        current = announcement_service.get_playback(engine, store_id=store_id)
        try:
            state = announcements.next_state_for_play(current["state"])
        except announcements.AnnouncementRefused as refusal:
            refused.append({"store_id": store_id, "reason": str(refusal)})
            continue
        row = announcement_service.set_state(
            engine, store_id=store_id, state=state, template_id=template_id,
            audio_id=first_audio, actor_id=user.id,
            reachable=store_id in connected_now)
        await _dispatch_announcement(store_id, row)
        started.append(store_id)
    _write_log(db, "info",
               f"announcement_play template={template_id} started={len(started)} "
               f"refused={len(refused)} by={user.username}")
    return {"template_id": template_id, "started": started, "refused": refused}


@api.post("/announcements/stores/{store_id}/pause")
async def pause_announcement_in_store(
    store_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.control")),
):
    """Pause one Store.

    Pausing a Store that is standing aside for a broadcast is meaningful and is
    honoured: it says "do not come back when the broadcast ends". That is
    exactly what keeps auto-resume from starting something somebody silenced.
    """
    current = announcement_service.get_playback(engine, store_id=store_id)
    state = announcements.next_state_for_pause(current["state"])
    row = announcement_service.set_state(engine, store_id=store_id, state=state,
                                         ducked_from=None, actor_id=user.id)
    await _dispatch_announcement(store_id, row)
    _write_log(db, "info",
               f"announcement_paused store={store_id} by={user.username}")
    return row


@api.post("/announcements/stores/{store_id}/play")
async def play_announcement_in_store(
    store_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.control")),
):
    current = announcement_service.get_playback(engine, store_id=store_id)
    if current.get("template_id") is None:
        raise HTTPException(
            status_code=400,
            detail="Nothing has been chosen for this Store yet. Start a "
                   "template first, then this button resumes it.")
    try:
        state = announcements.next_state_for_play(current["state"])
    except announcements.AnnouncementRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    row = announcement_service.set_state(
        engine, store_id=store_id, state=state, actor_id=user.id,
        reachable=store_id in manager.online_store_ids())
    await _dispatch_announcement(store_id, row)
    _write_log(db, "info",
               f"announcement_resumed store={store_id} by={user.username}")
    return row


@api.post("/announcements/stores/{store_id}/stop")
async def stop_announcement_in_store(
    store_id: int,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.control")),
):
    """Stop one Store, and forget what it was playing.

    Its own action rather than a pause with a flag: after this the Store has
    nothing chosen, and the console's Play button refuses instead of quietly
    resuming a campaign somebody finished.
    """
    row = announcement_service.stop(engine, store_id=store_id, actor_id=user.id)
    await _dispatch_announcement(store_id, row)
    _write_log(db, "info",
               f"announcement_stopped store={store_id} by={user.username}")
    return row


@api.post("/announcements/stop-all")
async def stop_all_announcements(
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.control_all")),
):
    """Every Store at once, and nothing left selected anywhere.

    Behind control_all for the same reason Pause All is: reaching every shop
    in the estate in one action has the reach of an emergency stop, and is
    grantable separately from running one shop's announcements.
    """
    stopped = []
    for row in announcement_service.live_status(engine):
        if row["state"] == announcements.STATE_STOPPED:
            # Already stopped. Rewriting it would put a second identical entry
            # in the history for a shop where nothing changed.
            continue
        updated = announcement_service.stop(
            engine, store_id=row["store_id"], actor_id=user.id)
        await _dispatch_announcement(row["store_id"], updated)
        stopped.append(row["store_id"])
    _write_log(db, "warn",
               f"announcement_stop_all stores={len(stopped)} by={user.username}")
    return {"stopped": stopped, "count": len(stopped)}


@api.post("/announcements/pause-all")
async def pause_all_announcements(
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.control_all")),
):
    """Every Store at once.

    Its own permission, not announcements.control. Pausing one shop is a local
    decision; reaching every shop in the estate in one action has the same
    reach as an emergency stop and should be grantable separately.
    """
    rows = announcement_service.live_status(engine)
    paused = []
    for row in rows:
        state = announcements.next_state_for_pause(row["state"])
        if state == row["state"]:
            continue
        updated = announcement_service.set_state(
            engine, store_id=row["store_id"], state=state, ducked_from=None,
            actor_id=user.id)
        await _dispatch_announcement(row["store_id"], updated)
        paused.append(row["store_id"])
    _write_log(db, "warn",
               f"announcement_pause_all stores={len(paused)} by={user.username}")
    return {"paused": paused, "count": len(paused)}


@api.post("/announcements/play-all")
async def play_all_announcements(
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.control_all")),
):
    """Resume every Store that has something chosen and is not standing aside
    for a broadcast.

    A Store with no template is skipped rather than failing the whole call: an
    estate always has a shop that was added yesterday, and one of those must
    not stop the other two hundred from resuming.
    """
    rows = announcement_service.live_status(engine)
    started, skipped = [], []
    connected_all = manager.online_store_ids()
    for row in rows:
        if row.get("template_id") is None:
            skipped.append(row["store_id"])
            continue
        try:
            state = announcements.next_state_for_play(row["state"])
        except announcements.AnnouncementRefused:
            skipped.append(row["store_id"])
            continue
        updated = announcement_service.set_state(
            engine, store_id=row["store_id"], state=state, actor_id=user.id,
            reachable=row["store_id"] in connected_all)
        await _dispatch_announcement(row["store_id"], updated)
        started.append(row["store_id"])
    _write_log(db, "info",
               f"announcement_play_all started={len(started)} "
               f"skipped={len(skipped)} by={user.username}")
    return {"started": started, "skipped": skipped, "count": len(started)}


@api.post("/announcements/stores/{store_id}/volume")
async def set_announcement_volume(
    store_id: int,
    payload: dict,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("announcements.volume")),
):
    """Volume alone. Turning a jingle down must not start it and must not stop
    it, which is why this is not a field on the play call."""
    try:
        row = announcement_service.set_volume(
            engine, store_id=store_id,
            volume_percent=payload.get("volume_percent"), actor_id=user.id)
    except announcements.AnnouncementRefused as refusal:
        raise HTTPException(status_code=400, detail=str(refusal))
    # The level reaches the shop immediately, whatever the announcement is
    # doing - a volume change that only took effect on the next play would be
    # a slider that appears broken.
    await _dispatch_announcement_volume(store_id, row["volume_percent"])
    _write_log(db, "info",
               f"announcement_volume store={store_id} "
               f"value={row['volume_percent']} by={user.username}")
    return row


app.include_router(api)


def allowed_cors_origins() -> list[str]:
    """Exact origins only. Never a wildcard.

    This used to default to ``"*"`` with ``allow_credentials=True``. Browsers
    refuse that combination outright, so it was not doing what it looked like it
    was doing - but a deployment that set one real origin and kept the default
    elsewhere would have been genuinely open, and a misconfiguration that opens
    a credentialed API to every origin is not the kind that announces itself.

    The default is loopback, which is what a developer on this machine actually
    needs. A LAN pilot passes its own address explicitly; production passes its
    real origin. A ``*`` in the variable is refused rather than honoured.
    """
    LOOPBACK_ORIGINS = ["http://localhost:3000", "http://127.0.0.1:3000"]
    configured = os.environ.get("CORS_ORIGINS", "").strip()
    if not configured:
        return LOOPBACK_ORIGINS

    origins = [origin.strip() for origin in configured.split(",") if origin.strip()]
    if any(origin == "*" for origin in origins):
        raise RuntimeError(
            "CORS_ORIGINS contains '*'. This API sends credentials, so every "
            "allowed origin must be named exactly."
        )
    return origins


class UnhandledErrorAsApiResponse(BaseHTTPMiddleware):
    """Turn an unhandled exception into an honest API error.

    WHY THIS EXISTS - AND WHY IT IS ADDED **BEFORE** THE CORS MIDDLEWARE

    Starlette's ServerErrorMiddleware sits OUTSIDE every middleware added with
    ``add_middleware``, including CORS. So when a route raised, the 500 that
    reached the browser had never passed through CORSMiddleware and carried no
    ``Access-Control-Allow-Origin`` header. Chrome then reported, truthfully
    but very unhelpfully:

        No 'Access-Control-Allow-Origin' header is present on the requested
        resource.

    An operator reads that as "the CORS configuration is broken" and goes
    looking in the wrong place entirely - which is exactly what happened with
    Broadcast History permanent deletion, where the real fault was a foreign
    key constraint. A backend defect must never disguise itself as a transport
    problem.

    ``add_middleware`` inserts at the FRONT of the user middleware list, so the
    LAST one added ends up outermost. This is therefore registered first and
    CORS second, which puts CORS outside it: the JSON response produced here
    passes back out through CORSMiddleware and gets its headers like any
    ordinary response.

    The response body stays deliberately bare. The exception text can carry a
    file path, a SQL statement or a column value, and none of that belongs in
    a browser. The detail goes to the log, where an operator can find it.
    """

    async def dispatch(self, request, call_next):
        try:
            return await call_next(request)
        except Exception:                                   # noqa: BLE001
            logger.exception("Unhandled error serving %s %s",
                             request.method, request.url.path)
            return JSONResponse(
                status_code=500,
                content={
                    "detail": {
                        "code": "INTERNAL_ERROR",
                        "message": "That action could not be completed because "
                                   "of a server error. It has been logged.",
                    }
                },
            )


# Order matters - see the docstring above. This one first...
app.add_middleware(UnhandledErrorAsApiResponse)
# ...so that this one ends up outside it and can add headers to its response.
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=allowed_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# The built React application, served by this same process
# ===========================================================================
#: Where the production build lives. Set by the repo-native launcher; absent in
#: development and in the legacy two-port layout, where a separate server (the
#: CRA dev server, or tools/spa_server.py) serves the frontend instead.
FRONTEND_BUILD_ENV = "SPEAKLINK_FRONTEND_BUILD"


def _mount_frontend(application: FastAPI) -> Path | None:
    """Serve the SPA from this process, on this origin.

    WHY THIS IS WORTH DOING

    The legacy layout ran two servers on two ports, which made every browser
    request cross-origin and put CORS on the critical path of an internal LAN
    tool. One origin removes that entire class of problem from production: no
    preflight, no allow-list to keep in step with the machine's address, and no
    "it works until somebody opens it by hostname instead of IP".

    ORDER MATTERS

    This runs AFTER app.include_router(api), so /api/* and the WebSocket routes
    are matched first and a catch-all here can never shadow them. A request for
    an unknown /api path must still be a JSON 404 from the router, not the HTML
    index - an operator debugging a typo in an endpoint should not be handed a
    web page that looks like it worked.

    WHY A CATCH-ALL AND NOT StaticFiles(html=True) AT "/"

    React Router owns paths like /active-broadcasts that exist only in the
    browser. A plain static mount answers 404 for them, so a reload or a
    bookmark breaks. Real files are served when they exist; everything else
    falls through to index.html and the router sorts it out.
    """
    configured = os.environ.get(FRONTEND_BUILD_ENV, "").strip()
    if not configured:
        return None
    build = Path(configured).expanduser().resolve()
    index = build / "index.html"
    if not index.is_file():
        raise RuntimeError(
            f"{FRONTEND_BUILD_ENV} points at {build}, which has no index.html. "
            "Build the frontend first."
        )

    # Hashed assets under /static are immutable by construction - the filename
    # changes whenever the content does - so they are safe to cache hard.
    application.mount(
        "/static", StaticFiles(directory=str(build / "static")), name="static")

    @application.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        # /api is the router's. If execution reaches here with an /api path, the
        # router already declined it, and the honest answer is 404 - never the
        # SPA, which would turn a mistyped endpoint into an HTML page and a very
        # confusing bug report.
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")

        # A real file, if there is one - favicon.ico, manifest.json, robots.txt.
        # Resolved and then checked to be INSIDE the build directory, so a
        # crafted path cannot escape it.
        if full_path:
            candidate = (build / full_path).resolve()
            if candidate.is_file() and build in candidate.parents:
                return FileResponse(str(candidate))

        return FileResponse(str(index))

    return build


FRONTEND_BUILD_DIR = _mount_frontend(app)
if FRONTEND_BUILD_DIR:
    logger.info("Serving the React application from %s on this origin",
                FRONTEND_BUILD_DIR)
