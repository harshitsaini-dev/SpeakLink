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
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import ValidationError

from db import engine, get_db, SessionLocal
from models import (
    Base, HQUser, Store, BroadcastSession, BroadcastTarget, ReceiverEvent, SystemLog
)
from schemas import (
    LoginRequest, LoginResponse, UserOut,
    StoreCreate, StoreUpdate, StoreOut, StoresMetaOut,
    SessionCreate, SessionOut, SessionDetailOut, TargetOut,
    SystemLogOut,
    EnrollmentCodeRequest, EnrollmentCodeResponse,
    DeviceEnrollmentRequest, DeviceEnrollmentResponse,
    ReceiverDeviceOut, CredentialRotationResponse,
    HQUserOut, HQUserCreate, HQUserUpdate, HQUserRoleUpdate,
    PasswordChangeIn, PasswordResetIn, PasswordResetOut,
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
    may_manage_role,
    migrate_legacy_roles,
    parse_role,
    require_permission,
)
from deletion_safety import (
    DeletionRefused,
    delete_store_if_unused,
    delete_user_if_unused,
    store_dependencies,
    user_dependencies,
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
from receiver_enrollment_codes import CODE_TTL_SECONDS
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
from ws_tickets import TICKET_TTL_SECONDS, TicketRejected, WebSocketTicketStore
from pathlib import Path

from key_custody import (
    DpapiProtector,
    FakeProtector,
    KeyCustodyError,
    ProtectionScope,
    load_key_ring,
)
from key_custody_acl import SERVICE_CONTAINER_PATH
from receiver_enrollment_api import (
    DeviceNotFound,
    EnrollmentRefused,
    EnrollmentUnavailable,
    TooManyOutstandingCodes,
    create_enrollment_code,
    disable_device,
    list_devices,
    read_device,
    redeem_and_enroll,
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


def receiver_key_ring():
    """Open the Receiver HMAC key container, or return None.

    None rather than an exception so the caller can answer 503 with a message
    about the server rather than one that looks like a bad enrolment code. The
    container is never created here: DPAPI CURRENT_USER binds it to the identity
    that sealed it, so it must be minted by this process under the service
    account, deliberately, not as a side effect of the first request.
    """
    configured = os.environ.get("ECHOCAST_KEY_CONTAINER")
    path = Path(configured) if configured else SERVICE_CONTAINER_PATH
    try:
        if os.environ.get("ECHOCAST_KEY_PROTECTOR") == "fake":
            # Local staging only. Never reachable unless explicitly configured.
            protector = FakeProtector()
        else:
            protector = DpapiProtector(scope=ProtectionScope.CURRENT_USER)
        return load_key_ring(path, protector=protector)
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
        return default_receiver_runtime_authenticator
    try:
        with engine.connect() as connection:
            present = {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        if not {"receiver_devices", "receiver_credentials"} <= present:
            return default_receiver_runtime_authenticator
        return DualRuntimeAuthenticator(
            device=MigrationAwareReceiverRuntimeAuthenticator(
                engine, hash_keys=key_ring.as_mapping()
            ),
            legacy=default_receiver_runtime_authenticator,
        )
    except Exception:
        # Never let a probe failure take the Receivers offline.
        return default_receiver_runtime_authenticator
configure_receiver_runtime(
    app,
    authenticator=build_receiver_runtime_authenticator(),
    connection_manager=manager,
)


def require(permission: Permission):
    """A dependency that admits only accounts holding one permission.

    Written as a factory so every route names its permission at the point of
    definition. A route with no `require(...)` is authenticated-only, and that
    now has to be a visible choice rather than an omission - a test walks the
    routing table and fails on any authenticated route without one.
    """

    def guard(user: HQUser = Depends(get_current_user)) -> HQUser:
        try:
            require_permission(user, permission)
        except PermissionDenied as refusal:
            # 403, not 404: the caller is authenticated and the resource exists.
            # The message is identical for every permission, so a refusal never
            # maps out what the system can do.
            raise HTTPException(status_code=403, detail=str(refusal))
        return user

    return guard


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
    authenticator = build_receiver_runtime_authenticator()
    configure_receiver_runtime(app, authenticator=authenticator, connection_manager=manager)
    logger.info(
        "Receiver authentication: %s",
        "device credentials + legacy store tokens"
        if isinstance(authenticator, DualRuntimeAuthenticator)
        else "legacy store tokens only",
    )

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


@api.post("/auth/ws-ticket")
def issue_websocket_ticket(user: HQUser = Depends(get_current_user)):
    """Mint a single-use handshake ticket for an HQ WebSocket.

    A browser cannot set an Authorization header on a WebSocket handshake, so
    something has to travel in the URL - and Uvicorn logs the URL in full. This
    endpoint is reached over the normal authenticated HTTP API, where the JWT
    stays in a header, and returns a credential that is worthless seconds later
    and after a single use.
    """
    ticket = ws_ticket_store.issue(user_id=str(user.id))
    return {"ticket": ticket, "expires_in": TICKET_TTL_SECONDS}


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
def list_hq_users(user: HQUser = Depends(require(Permission.MANAGE_USERS))):
    return [HQUserOut(**record) for record in list_users(engine)]


@api.get("/users/{user_id}", response_model=HQUserOut)
def read_hq_user(user_id: int, user: HQUser = Depends(require(Permission.MANAGE_USERS))):
    return HQUserOut(**_user_or_404(user_id))


@api.post("/users", response_model=HQUserOut, status_code=201)
def create_hq_user(
    payload: HQUserCreate,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.MANAGE_USERS)),
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
    user: HQUser = Depends(require(Permission.MANAGE_USERS)),
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
    user: HQUser = Depends(require(Permission.MANAGE_USERS)),
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
                    user: HQUser = Depends(require(Permission.MANAGE_USERS))):
    return _lifecycle_endpoint(disable_user, "disabled")(user_id, db, user)


@api.post("/users/{user_id}/enable", response_model=HQUserOut)
def enable_hq_user(user_id: int, db: Session = Depends(get_db),
                   user: HQUser = Depends(require(Permission.MANAGE_USERS))):
    return _lifecycle_endpoint(enable_user, "enabled", level="info")(user_id, db, user)


@api.post("/users/{user_id}/archive", response_model=HQUserOut)
def archive_hq_user(user_id: int, db: Session = Depends(get_db),
                    user: HQUser = Depends(require(Permission.MANAGE_USERS))):
    return _lifecycle_endpoint(archive_user, "archived")(user_id, db, user)


@api.post("/users/{user_id}/restore", response_model=HQUserOut)
def restore_hq_user(user_id: int, db: Session = Depends(get_db),
                    user: HQUser = Depends(require(Permission.MANAGE_USERS))):
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
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
):
    """Mint a one-time code for one Store. Shown once, never stored raw."""
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
    except EnrollmentRefused:
        enrollment_limiter.record_attempt(client_key)
        # One generic refusal: saying which of unknown, expired or already-used
        # applied would let a caller map out valid codes.
        _write_log(db, "warn", "enrollment_code_rejected")
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
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
):
    try:
        return [ReceiverDeviceOut(**row) for row in list_devices(engine, store_id=store_id)]
    except EnrollmentUnavailable as unavailable:
        raise HTTPException(status_code=503, detail=str(unavailable))


@api.get("/receiver-devices/{public_id}", response_model=ReceiverDeviceOut)
def read_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
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
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
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


@api.post("/receiver-devices/{public_id}/promote", response_model=List[ReceiverDeviceOut])
def promote_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
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
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
):
    """Every Device of one Store with its primary/standby role. No credentials."""
    try:
        return describe_store_devices(engine, store_id=store_id)
    except Exception:
        raise HTTPException(status_code=503, detail="Receiver Device roles are unavailable")


@api.post("/receiver-devices/{public_id}/rotate-credential",
          response_model=CredentialRotationResponse)
def rotate_receiver_device(
    public_id: str,
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
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
    user: HQUser = Depends(require(Permission.MANAGE_DEVICES)),
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
    user: HQUser = Depends(require(Permission.VIEW_STATUS)),
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
    if city:
        query = query.filter(Store.city == city)
    if region:
        query = query.filter(Store.region == region)
    if status_f:
        query = query.filter(Store.status == status_f)
    if q:
        like = f"%{q}%"
        query = query.filter((Store.store_name.ilike(like)) | (Store.store_code.ilike(like)))
    # Reflect actual online status from live WS state
    stores = query.order_by(Store.store_code).all()
    online_ids = manager.online_store_ids()
    for s in stores:
        if s.status not in ("playing", "error"):
            s.status = "online" if s.id in online_ids else "offline"
    return stores


@api.post("/stores", response_model=StoreOut, status_code=201)
def create_store(payload: StoreCreate, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    if db.query(Store).filter(Store.store_code == payload.store_code).first():
        raise HTTPException(status_code=409, detail="store_code already exists")
    s = Store(**payload.model_dump(), receiver_token=uuid.uuid4().hex)
    db.add(s)
    db.commit()
    db.refresh(s)
    _write_log(db, "info", f"Store created: {s.store_code} by {user.username}")
    return s


@api.put("/stores/{store_id}", response_model=StoreOut)
def update_store(store_id: int, payload: StoreUpdate, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    """Edit a Store's details. Never its state, and never its credentials."""
    s = db.query(Store).filter(Store.id == store_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Store not found")

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
def disable_store_endpoint(store_id: int, user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    """Switch a Store off. Reversible; its history is untouched."""
    return _lifecycle_action(disable_store, store_id, user)


@api.post("/stores/{store_id}/enable", response_model=StoreOut)
def enable_store_endpoint(store_id: int, user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    """Switch a Store back on. An archived Store is not reachable from here."""
    return _lifecycle_action(enable_store, store_id, user, use_live_guard=False)


@api.post("/stores/{store_id}/archive", response_model=StoreOut)
def archive_store_endpoint(store_id: int, user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    """Retire a Store. Nothing is deleted - the row, its Devices, its sessions
    and its events all stay readable."""
    return _lifecycle_action(archive_store, store_id, user)


@api.post("/stores/{store_id}/restore", response_model=StoreOut)
def restore_store_endpoint(store_id: int, user: HQUser = Depends(require_super_admin)):
    """Bring an archived Store back to DISABLED, never straight to active."""
    return _lifecycle_action(restore_store, store_id, user, use_live_guard=False)


@api.delete("/stores/{store_id}")
def delete_store(store_id: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    """Delete means archive. A Store owns Devices, sessions, targets and events;
    removing the row would destroy the only record of what was announced where."""
    _lifecycle_action(archive_store, store_id, user)
    return {"ok": True, "archived": True}


@api.get("/stores/{store_id}/dependencies")
def read_store_dependencies(store_id: int,
                            user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    """What still refers to this Store, so the UI can show it before offering
    a delete that would be refused anyway."""
    summary = store_dependencies(engine, store_id=store_id)
    if not summary.exists:
        raise HTTPException(status_code=404, detail="Store not found")
    return {"counts": summary.counts, "unchecked": summary.unchecked,
            "total": summary.total, "deletable": summary.deletable,
            "explanation": summary.explain()}


@api.delete("/stores/{store_id}/permanently")
def hard_delete_store(store_id: int, confirm: str,
                      db: Session = Depends(get_db),
                      user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    """Remove a never-used Store for real. Refuses anything with history.

    Separate from DELETE /stores/{id}, which archives. Two verbs that do very
    different things must not be the same endpoint with a flag - somebody
    eventually passes the flag by accident.
    """
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


@api.get("/users/{user_id}/dependencies")
def read_user_dependencies(user_id: int,
                           user: HQUser = Depends(require(Permission.MANAGE_USERS))):
    summary = user_dependencies(engine, user_id=user_id)
    if not summary.exists:
        raise HTTPException(status_code=404, detail="No such HQ User")
    return {"counts": summary.counts, "unchecked": summary.unchecked,
            "total": summary.total, "deletable": summary.deletable,
            "explanation": summary.explain()}


@api.delete("/users/{user_id}/permanently")
def hard_delete_user(user_id: int, confirm: str,
                     db: Session = Depends(get_db),
                     user: HQUser = Depends(require(Permission.MANAGE_USERS))):
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
def regenerate_token(store_id: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.MANAGE_STORES))):
    s = db.query(Store).filter(Store.id == store_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Store not found")
    s.receiver_token = uuid.uuid4().hex
    db.commit()
    db.refresh(s)
    _write_log(db, "info", f"Regenerated token for store {s.store_code}")
    return s


@api.get("/stores/meta/regions-cities", response_model=StoresMetaOut)
def stores_meta(db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.VIEW_STATUS))):
    regions = [r[0] for r in db.query(Store.region).distinct().order_by(Store.region).all() if r[0]]
    cities = [c[0] for c in db.query(Store.city).distinct().order_by(Store.city).all() if c[0]]
    return StoresMetaOut(regions=regions, cities=cities)


# ================ BROADCAST ================
def _resolve_targets(db: Session, payload: SessionCreate) -> List[Store]:
    q = db.query(Store).filter(Store.is_active.is_(True))
    mode = payload.target_mode
    if mode == "all":
        return q.all()
    if mode == "selected":
        if not payload.store_ids:
            raise HTTPException(status_code=400, detail="store_ids required for target_mode=selected")
        return q.filter(Store.id.in_(payload.store_ids)).all()
    if mode == "region":
        if not payload.region:
            raise HTTPException(status_code=400, detail="region required")
        return q.filter(Store.region == payload.region).all()
    if mode == "city":
        if not payload.city:
            raise HTTPException(status_code=400, detail="city required")
        return q.filter(Store.city == payload.city).all()
    if mode == "online_only":
        return q.filter(Store.is_online_store.is_(True)).all()
    raise HTTPException(status_code=400, detail="Invalid target_mode")


@api.post("/broadcast/sessions", response_model=SessionOut, status_code=201)
def create_session(payload: SessionCreate, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.START_BROADCAST))):
    targets = _resolve_targets(db, payload)
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
    return {
        "live": True,
        "session": SessionOut.model_validate(session).model_dump(mode="json"),
        "targets": [TargetOut.model_validate(t).model_dump(mode="json") for t in targets],
        "online_receivers": list(manager.online_store_ids()),
        # READY comes only from an explicit receiver_ready acknowledgement.
        # Being connected is never enough, so these two lists are separate.
        "ready_receivers": list(manager.ready_store_ids()),
    }


@api.get("/broadcast/history", response_model=List[SessionOut])
def broadcast_history(limit: int = 50, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.VIEW_HISTORY))):
    return db.query(BroadcastSession).order_by(BroadcastSession.id.desc()).limit(limit).all()


@api.get("/broadcast/sessions/{sid}", response_model=SessionDetailOut)
def session_detail(sid: int, db: Session = Depends(get_db), user: HQUser = Depends(require(Permission.VIEW_HISTORY))):
    session = db.query(BroadcastSession).filter(BroadcastSession.id == sid).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    targets = db.query(BroadcastTarget).filter(BroadcastTarget.session_id == sid).all()
    out = SessionDetailOut.model_validate(session)
    out.targets = [TargetOut.model_validate(t) for t in targets]
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
    db: Session = Depends(get_db),
    user: HQUser = Depends(require(Permission.VIEW_LOGS)),
):
    q = db.query(SystemLog)
    if level:
        q = q.filter(SystemLog.level == level)
    return q.order_by(SystemLog.id.desc()).limit(limit).all()


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
                acknowledgement, _ = connection_manager.apply_receiver_payload(
                    store_id,
                    data,
                    received_at,
                )
            except (ReceiverContractError, ValidationError, ValueError, json.JSONDecodeError) as error:
                code = _receiver_rejection_code(error)
                await websocket.send_text(json.dumps({"type": "ack_rejected", "code": code}))
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
    try:
        user_id = ws_ticket_store.redeem(ticket)
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
    """HQ mic audio uplink. Only one active broadcaster allowed."""
    # A single-use ticket, not a reusable JWT: Uvicorn logs this URL in full.
    try:
        ws_ticket_store.redeem(ticket)
    except TicketRejected:
        await websocket.close(code=4401)
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
