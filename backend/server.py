"""EchoCast Live - main FastAPI application.

This is a standalone module. It does NOT touch or share state with any
existing system. Uses its own SQLite DB (echocast_live.db).
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
from datetime import datetime, timezone
from typing import List, Optional, Set

from fastapi import FastAPI, APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect, status
from starlette.middleware.cors import CORSMiddleware
from sqlalchemy import func, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, ValidationError

from db import engine, get_db, SessionLocal
from models import (
    Base, HQUser, Store, BroadcastSession, BroadcastTarget, ReceiverEvent, SystemLog
)
from schemas import (
    LoginRequest, LoginResponse, UserOut,
    StoreCreate, StoreUpdate, StoreOut, StoresMetaOut,
    SessionCreate, SessionOut, SessionDetailOut, TargetOut,
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
from permission_catalog import (
    OwnerOverrideRefused,
    UnknownPermissionCode,
    describe_user_permissions,
    ensure_permission_schema,
    has_permission_code,
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
from enrolment_refusal import classify_enrolment_refusal
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
    permanently_delete_store_with_history,
)
from admin_search import (
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
    permanently_delete_user_with_history,
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
from audio_protocol import build_prepare_message
from auth import verify_password, hash_password, create_access_token, get_current_user
from seed import seed_admin, seed_stores
from ws_manager import manager
from receiver_connection_inventory import ReceiverConnectionInventoryError
from receiver_runtime_auth import (
    DualRuntimeAuthenticator,
    LegacyStoreTokenRuntimeAuthenticator,
    MigrationAwareReceiverRuntimeAuthenticator,
)
from receiver_contract import (
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
    configured = os.environ.get("ECHOCAST_KEY_CONTAINER")
    return Path(configured) if configured else SERVICE_CONTAINER_PATH


def receiver_key_protector():
    """One protector choice, for the same reason."""
    if os.environ.get("ECHOCAST_KEY_PROTECTOR") == "fake":
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
# In memory is correct here: EchoCast runs exactly one Uvicorn worker because
# Receiver connection state is already process-local.
ws_ticket_store = WebSocketTicketStore()

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("echocast")

RECEIVER_AUTH_FAILURE_CODE = 4401
RECEIVER_AUTH_FAILURE_REASON = "Receiver authentication failed"
RECEIVER_CONNECTION_FAILURE_CODE = 1013
RECEIVER_CONNECTION_FAILURE_REASON = "Receiver connection unavailable"
MAX_RECEIVER_TOKEN_LENGTH = 128


# ---- app + startup ----
app = FastAPI(title="EchoCast Live", version="1.0.0")


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
        with engine.connect() as connection:
            present = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
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

    # Additive and idempotent - a new table, no ALTER on anything that already
    # exists - so it is safe on every boot rather than needing a maintenance
    # window. A host without the phase-one schema simply has an empty one.
    try:
        ensure_primary_device_schema(engine)
    except Exception:
        logger.warning("Receiver primary-device table could not be prepared", exc_info=False)

    # One additive column plus a backfill from is_active. Cheap on SQLite - no
    # row rewrite, no index rebuild - so it belongs at startup, not in a window.
    try:
        ensure_store_lifecycle_schema(engine)
    except Exception:
        logger.warning("Store lifecycle column could not be prepared", exc_info=False)

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
        _write_log(db, "info", "EchoCast Live server started")
    logger.info("EchoCast Live startup complete")


api = APIRouter(prefix="/api")


@api.get("/")
def root():
    return {"service": "EchoCast Live", "status": "ok"}


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
    scope_store_id: Optional[int] = None,
    scope_city: Optional[str] = None,
    scope_region: Optional[str] = None,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    user: HQUser = Depends(require("menu.users.view")),
):
    """Filtered, paginated User Management.

    Permanently deleted accounts are excluded unless include_deleted, and
    there is deliberately no restore path for them anywhere.
    """
    page, page_size = normalize_paging(page, page_size)
    records = list_users(engine, include_deleted=include_deleted)

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
    if scope_store_id is not None or scope_city or scope_region:
        matching_users = set()
        with engine.connect() as connection:
            if scope_store_id is not None:
                matching_users |= {r[0] for r in connection.execute(text(
                    "SELECT user_id FROM user_store_scope "
                    "WHERE scope_type = 'STORE' AND store_id = :i"),
                    {"i": scope_store_id})}
            if scope_city:
                matching_users |= {r[0] for r in connection.execute(text(
                    "SELECT user_id FROM user_store_scope "
                    "WHERE scope_type = 'CITY' AND scope_value = :v"),
                    {"v": scope_city})}
            if scope_region:
                matching_users |= {r[0] for r in connection.execute(text(
                    "SELECT user_id FROM user_store_scope "
                    "WHERE scope_type = 'REGION' AND scope_value = :v"),
                    {"v": scope_region})}
        records = [r for r in records if r["id"] in matching_users]

    total = len(records)
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
        _write_log(db, "warn", f"enrollment_code_rejected category={category.value}")
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
    store_id: Optional[int] = None,
    status_f: Optional[str] = Query(None, alias="status"),
    is_primary: Optional[bool] = None,
    lifecycle: Optional[str] = None,
    include_archived: bool = True,
    include_deleted: bool = False,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """Filtered, paginated Receiver Devices ACROSS Stores.

    archived and deleted are different states and are reported separately in
    ``lifecycle``: an archived Device can be restored, a permanently deleted
    one never can. Deleted Devices are hidden unless asked for.
    """
    page, page_size = normalize_paging(page, page_size)
    where = ["1=1"]
    params = {}
    term = like_term(q)
    if term:
        params["term"] = term
        where.append("(d.public_id LIKE :term OR d.display_name LIKE :term "
                     "OR s.store_code LIKE :term OR s.store_name LIKE :term)")
    if city:
        params["city"] = city; where.append("s.city = :city")
    if region:
        params["region"] = region; where.append("s.region = :region")
    if store_id is not None:
        params["store_id"] = store_id; where.append("d.store_id = :store_id")
    if status_f:
        params["status_f"] = status_f; where.append("d.status = :status_f")
    if not include_deleted:
        where.append("d.deleted_at IS NULL")
    if not include_archived:
        where.append("d.archived_at IS NULL")
    if lifecycle == "deleted":
        where.append("d.deleted_at IS NOT NULL")
    elif lifecycle == "archived":
        where.append("d.archived_at IS NOT NULL AND d.deleted_at IS NULL")
    elif lifecycle == "active":
        where.append("d.archived_at IS NULL AND d.deleted_at IS NULL")
    if is_primary is not None:
        exists = ("EXISTS (SELECT 1 FROM receiver_store_primary_device p "
                  "WHERE p.device_id = d.id)")
        where.append(exists if is_primary else f"NOT {exists}")

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


@api.post("/receiver-devices/{public_id}/disable", response_model=ReceiverDeviceOut)
def disable_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.disable")),
):
    """Stop this one computer. Its Store and every other Device keep working."""
    try:
        device = disable_device(engine, public_id=public_id)
    except DeviceNotFound:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))
    _write_log(db, "warn", f"receiver_device_disabled device={public_id} by={user.username}")
    return ReceiverDeviceOut(**device)


@api.post("/receiver-devices/{public_id}/archive", response_model=ReceiverDeviceOut)
def archive_receiver_device(
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
def tombstone_receiver_device(public_id: str, payload: DeviceTombstoneRequest,
                              db: Session = Depends(get_db),
                              user: HQUser = Depends(require("devices.delete_permanently"))):
    """Permanently remove a Receiver Device from operational EchoCast even
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
def revoke_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("devices.revoke")),
):
    """Retire this one computer permanently."""
    try:
        device = revoke_device(engine, public_id=public_id)
    except DeviceNotFound:
        raise HTTPException(status_code=404, detail="Receiver Device not found")
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))
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
    if not manager.is_live():
        return set()
    return set(manager.live_target_store_ids)


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
    """Permanently remove a Store from operational EchoCast even though it has
    history. The row is never deleted - see store_deletion.py - so every
    Broadcast Target, Receiver event, Device and enrollment code that refers
    to it stays exactly as readable as it was.

    stores.delete_permanently defaults to SUPER ADMIN/OWNER only. This is
    deliberately a different, stronger action than DELETE /stores/{id}
    /permanently, which only ever removes a Store nothing has ever
    referenced.
    """
    if not payload.acknowledged:
        raise HTTPException(
            status_code=400,
            detail="The 'this Store cannot be restored' acknowledgement is required.",
        )
    try:
        result = permanently_delete_store_with_history(
            engine, store_id=store_id, typed_confirmation=payload.confirm,
            actor_user_id=user.id, live_store_ids=_live_store_ids(),
        )
    except StoreDeletionRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    _write_log(
        db, "warn",
        f"STORE_PERMANENTLY_DELETED store_id={result.store_id} "
        f"code={result.store_code} history_counts={result.dependency_counts} "
        f"devices={len(result.device_public_ids)} "
        f"credentials_revoked={result.credentials_revoked} "
        f"enrollment_codes_revoked={result.enrollment_codes_revoked} "
        f"by={user.username}",
    )
    return {
        "ok": True,
        "store_id": result.store_id,
        "store_code": result.store_code,
        "store_name": result.store_name,
        "deleted_at": result.deleted_at,
        "dependency_counts": result.dependency_counts,
        "device_public_ids": result.device_public_ids,
        "credentials_revoked": result.credentials_revoked,
        "enrollment_codes_revoked": result.enrollment_codes_revoked,
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
# Reserved for OWNER, the same way restoring an archived Store is
# (`require_super_admin`) - not `require("users.permissions.manage")`, on
# purpose. `users.permissions.manage` is a role-default flag ADMIN never gets;
# gating these two routes on `require_super_admin` instead means the check is
# "is this account literally OWNER right now", independent of the override
# system these very routes edit. An override can never grant an ADMIN a path
# to grant themselves more.
@api.get("/users/{user_id}/permissions")
def read_user_permission_overrides(user_id: int, user: HQUser = Depends(require_super_admin)):
    existing = _user_or_404(user_id)
    role = parse_role(existing["role"])
    if role is None:
        raise HTTPException(status_code=400, detail="That account has no recognised role.")
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
    user: HQUser = Depends(require_super_admin),
):
    existing = _user_or_404(user_id)
    role = parse_role(existing["role"])
    if role is None:
        raise HTTPException(status_code=400, detail="That account has no recognised role.")
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
    """Permanently remove an account from operational EchoCast even though it
    is the recorded actor in Broadcast and audit history.

    The hq_users row is never deleted - see user_deletion.py - so every
    broadcast_sessions.started_by and audit reference stays valid and
    readable. The account is tombstoned instead: it cannot sign in, its live
    sessions end immediately, and it can never be restored.

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
        result = permanently_delete_user_with_history(
            engine, user_id=user_id, typed_confirmation=payload.confirm,
            actor_user_id=user.id,
        )
    except UserDeletionRefused as refusal:
        raise HTTPException(status_code=409, detail=str(refusal))
    _write_log(
        db, "warn",
        f"USER_PERMANENTLY_DELETED user_id={result.user_id} "
        f"username={result.username} role={result.role} "
        f"history={result.history_counts} by={user.username}",
    )
    return {
        "ok": True,
        "user_id": result.user_id,
        "username": result.username,
        "role": result.role,
        "deleted_at": result.deleted_at,
        "history_counts": result.history_counts,
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
def _resolve_targets(db: Session, payload: SessionCreate, user: HQUser) -> List[Store]:
    q = db.query(Store).filter(Store.is_active.is_(True))
    mode = payload.target_mode
    if mode == "all":
        targets = q.all()
    elif mode == "selected":
        if not payload.store_ids:
            raise HTTPException(status_code=400, detail="store_ids required for target_mode=selected")
        targets = q.filter(Store.id.in_(payload.store_ids)).all()
    elif mode == "region":
        if not payload.region:
            raise HTTPException(status_code=400, detail="region required")
        targets = q.filter(Store.region == payload.region).all()
    elif mode == "city":
        if not payload.city:
            raise HTTPException(status_code=400, detail="city required")
        targets = q.filter(Store.city == payload.city).all()
    elif mode == "online_only":
        targets = q.filter(Store.is_online_store.is_(True)).all()
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
    if not targets:
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
    _write_log(db, "info", f"Session created #{session.id} '{session.campaign_name}' targets={len(targets)}")
    return session


@api.post("/broadcast/sessions/{sid}/start", response_model=SessionOut)
async def start_session(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.START_BROADCAST))):
    if manager.is_live():
        raise HTTPException(status_code=409, detail="A broadcast is already live")
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "pending":
        raise HTTPException(status_code=400, detail=f"Session cannot start (status={session.status})")

    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == sid).all()
    online_ids = manager.online_store_ids()
    target_store_ids = {t.store_id for t in targets}

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

    manager.start_live_session(session.id, target_store_ids)
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
    await manager.notify_dashboards({"type": "session_started", "session_id": session.id})
    _write_log(db, "info", f"Session #{session.id} started; {session.online_store_count}/{session.selected_store_count} online")
    return session


async def _end_session(db: Session, session: BroadcastSession, final_status: str, reason: str = "", broadcast_to_all: bool = False):
    now = datetime.now(timezone.utc)
    session.status = final_status
    session.ended_at = now
    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == session.id).all()
    for t in targets:
        if t.play_status == "playing":
            t.play_status = "stopped"
            t.stopped_at = now
    db.commit()
    # Broadcast stop to receivers. For emergency stop, notify ALL connected
    # receivers as a safety net; for normal stop, notify only targeted ones.
    if broadcast_to_all:
        stop_ids = set(manager.live_target_store_ids) | set(manager.receivers.keys())
    else:
        stop_ids = set(manager.live_target_store_ids)
    for sid_ in stop_ids:
        await manager.send_to_receiver(sid_, {"type": "stop", "session_id": session.id, "reason": reason})
    # Close every bounded Store audio queue and cancel its sender task before
    # clearing live state, so no orphan queue or task survives the session.
    await manager.stop_audio_fanout()
    manager.stop_live_session()
    # Force-close the active broadcaster WS so its slot is freed immediately
    if manager.active_broadcaster_ws is not None:
        try:
            await manager.active_broadcaster_ws.close(code=1000)
        except Exception:
            pass
    await manager.notify_dashboards({"type": "session_ended", "session_id": session.id, "status": final_status})


@api.post("/broadcast/sessions/{sid}/stop", response_model=SessionOut)
async def stop_session(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.STOP_BROADCAST))):
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
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
    per_store = manager.audio_metrics()
    return {
        "capacity": manager.audio_fanout.capacity,
        "store_count": len(per_store),
        "stores": [dict(metrics) for _store_id, metrics in sorted(per_store.items())],
    }


@api.post("/broadcast/emergency-stop")
async def emergency_stop(db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.EMERGENCY_STOP))):
    session = None
    if manager.live_session_id:
        session = db.query(BroadcastSession).filter(BroadcastSession.id == manager.live_session_id).first()
    if session and session.status == "live":
        await _end_session(db, session, "emergency_stopped", reason="emergency", broadcast_to_all=True)
        _write_log(db, "error", f"EMERGENCY STOP triggered by {user.username} on session #{session.id}")
        return {"ok": True, "session_id": session.id}
    # No live session â€” still broadcast a STOP to all receivers for safety
    for sid_ in list(manager.receivers.keys()):
        await manager.send_to_receiver(sid_, {"type": "stop", "reason": "emergency"})
    _write_log(db, "warn", f"Emergency stop invoked with no live session by {user.username}")
    return {"ok": True, "session_id": None}


@api.get("/broadcast/current")
def current_broadcast(db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.VIEW_STATUS))):
    if not manager.live_session_id:
        return {"live": False}
    session = db.query(BroadcastSession).filter(BroadcastSession.id == manager.live_session_id).first()
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
        return sessions
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
    return visible


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
    if filters.get("started_by") is not None:
        query = query.filter(BroadcastSession.started_by == filters["started_by"])

    target_conditions = []
    if filters.get("store_id") is not None:
        target_conditions.append(BroadcastTarget.store_id == filters["store_id"])
    if filters.get("city") or filters.get("region"):
        store_match = []
        if filters.get("city"):
            store_match.append(Store.city == filters["city"])
        if filters.get("region"):
            store_match.append(Store.region == filters["region"])
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
    result = delete_sessions_permanently(
        engine, session_ids=ids, actor_user_id=user.id,
        filters=payload.filters)
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
    if city:
        params["city"] = city; where.append("s.city = :city")
    if region:
        params["region"] = region; where.append("s.region = :region")
    if store_id is not None:
        params["store_id"] = store_id; where.append("s.id = :store_id")
    if status_f:
        params["status_f"] = status_f; where.append("s.status = :status_f")
    clause = " AND ".join(where)
    clause += _receiver_scope_clause(user, params)
    return clause, params


@api.get("/receivers/search")
def search_receiver_status(
    q: Optional[str] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    store_id: Optional[int] = None,
    status_f: Optional[str] = Query(None, alias="status"),
    has_primary: Optional[bool] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require("menu.receivers.view")),
):
    """Filtered, paginated Receiver Status.

    Reports only what EchoCast can actually prove. ``speaker_verified`` is
    NEVER derived from connected/ready/audio-receiving/playback-confirmed:
    acoustic verification arrives on a separate trusted path, and inferring
    it from software liveness is exactly the claim this project refuses to
    make. It is reported as null until that proof exists.
    """
    page, page_size = normalize_paging(page, page_size)
    where, params = _receiver_status_where(
        user, q=q, city=city, region=region, store_id=store_id, status_f=status_f)

    if has_primary is not None:
        exists = ("EXISTS (SELECT 1 FROM receiver_store_primary_device p "
                  "WHERE p.store_id = s.id)")
        where += f" AND {'' if has_primary else 'NOT '}{exists}"

    total = db.execute(
        text(f"SELECT COUNT(*) FROM stores s WHERE {where}"), params).scalar_one()
    rows = db.execute(text(
        "SELECT s.id, s.store_code, s.store_name, s.city, s.region, s.status, "
        "  EXISTS (SELECT 1 FROM receiver_store_primary_device p WHERE p.store_id = s.id) "
        "    AS has_primary, "
        "  (SELECT COUNT(*) FROM receiver_devices d WHERE d.store_id = s.id "
        "     AND d.deleted_at IS NULL) AS device_count "
        f"FROM stores s WHERE {where} ORDER BY s.store_code "
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


@api.get("/logs/search")
def search_logs(
    q: Optional[str] = None,
    level: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    actor_user_id: Optional[int] = None,
    store_id: Optional[int] = None,
    device_public_id: Optional[str] = None,
    include_archived: bool = False,
    archived_only: bool = False,
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
    if actor_user_id is not None:
        query = query.filter(SystemLog.actor_user_id == actor_user_id)
    if store_id is not None:
        query = query.filter(SystemLog.store_id == store_id)
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
    started_by: Optional[int] = None,
    store_id: Optional[int] = None,
    city: Optional[str] = None,
    region: Optional[str] = None,
    include_archived: bool = False,
    archived_only: bool = False,
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
    if started_by is not None:
        query = query.filter(BroadcastSession.started_by == started_by)

    target_conditions = []
    if store_id is not None:
        target_conditions.append(BroadcastTarget.store_id == store_id)
    if city or region:
        store_match = []
        if city:
            store_match.append(Store.city == city)
        if region:
            store_match.append(Store.region == region)
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
    rows = apply_paging(query.order_by(BroadcastSession.id.desc()), page, page_size).all()
    return Page(items=rows, total=total, page=page, page_size=page_size).as_dict(
        lambda row: SessionOut.model_validate(row).model_dump(mode="json"))


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

        # If a session is currently live and this store is a target -> send PLAY immediately
        if (
            connection_manager.is_live()
            and store_id in connection_manager.live_target_store_ids
        ):
            connection_manager.prepare_receiver_session(
                store_id,
                connection_manager.live_session_id,
            )
            await connection_manager.send_to_receiver(
                store_id,
                {
                    "type": "play",
                    "session_id": connection_manager.live_session_id,
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
async def ws_broadcaster(websocket: WebSocket, ticket: str = Query(...)):
    """HQ mic audio uplink. Only one active broadcaster allowed.

    THE MOST PRIVILEGED SOCKET IN THE SYSTEM, and it used to be the least
    guarded: it redeemed a ticket, THREW THE USER ID AWAY, and accepted audio.
    No permission, no role lookup, no re-read. Any authenticated account -
    including a read-only VIEWER refused by every broadcast HTTP route - could
    push arbitrary audio to the loudspeakers of every targeted Store, or occupy
    this single slot and deny it to whoever was allowed to use it.

    Two checks now, deliberately both: the ticket must have been minted FOR this
    socket (so a dashboard ticket is refused), and the account it was minted for
    must STILL hold START_BROADCAST when the handshake arrives. A permission
    verified only at mint time is verified once, and an operator can be demoted
    or disabled in the seconds between minting and connecting.
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

    await websocket.accept()
    ok = await manager.set_broadcaster(websocket)
    if not ok:
        await websocket.send_text('{"type":"error","message":"Another broadcaster is already active"}')
        await websocket.close(code=4409)
        return

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                await manager.fanout_audio(data)
            # text messages ignored for now
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"Broadcaster WS error: {e}")
    finally:
        await manager.clear_broadcaster(websocket)
        # Safety: if session still live when broadcaster drops, auto-stop it
        if manager.is_live():
            db = SessionLocal()
            try:
                session = db.query(BroadcastSession).filter(BroadcastSession.id == manager.live_session_id).first()
                if session and session.status == "live":
                    await _end_session(db, session, "ended", reason="broadcaster_disconnected")
                    _write_log(db, "warn", f"Session #{session.id} auto-stopped: broadcaster disconnected")
            finally:
                db.close()


# Include routes
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


app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=allowed_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
)
