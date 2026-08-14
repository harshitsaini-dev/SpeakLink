"""Fine-grained, per-user permissions layered on top of the four HQ roles.

``rbac.py`` already has a coarse, ten-permission role matrix and a central
``require(Permission.X)`` FastAPI dependency - the right shape, the wrong
grain. "Can edit a Store" and "can archive a Store" were the same
``MANAGE_STORES`` flag, so there was no way to let someone view Stores without
also letting them archive one.

This module adds a second, finer layer UNDER the same central-resolver
philosophy: one canonical catalog of permission codes, one default role
matrix, one table of per-user ALLOW/DENY overrides, and one function -
``resolve_effective_permissions`` - that everything else calls. Nothing
outside this module and ``server.py``'s ``require()`` factory ever compares a
role string directly; that is exactly the scattered ``if user.role == "ADMIN"``
pattern this replaces.

Effective permission, in order:

    explicit DENY override  >  explicit ALLOW override  >  role default  >  deny

Only one override row can exist per (user, permission) - the unique
constraint enforces it - so "DENY beats ALLOW" is really just "an override,
whichever effect it carries, beats the role default." INHERIT is not a stored
value; it is the absence of a row.

Nothing here decides who is signed in or whether their session is still valid
- that remains ``auth.py``/``rbac.py``. This only decides what an
already-authenticated, already-active account may do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import text
from sqlalchemy.engine import Engine

from rbac import PermissionDenied, Role, parse_role


class Effect(str, Enum):
    INHERIT = "INHERIT"
    ALLOW = "ALLOW"
    DENY = "DENY"


@dataclass(frozen=True, slots=True)
class PermissionDefinition:
    code: str
    group: str
    label: str


#: The one catalog. A permission that is not listed here does not exist:
#: ``resolve_effective_permissions`` only ever returns codes drawn from this
#: list, and ``set_permission_overrides`` refuses an unknown code rather than
#: silently storing it.
PERMISSION_DEFINITIONS: tuple[PermissionDefinition, ...] = (
    PermissionDefinition("menu.broadcast.view", "Broadcast", "View Broadcast Console"),
    PermissionDefinition("broadcast.start", "Broadcast", "Start Broadcast"),
    PermissionDefinition("broadcast.stop", "Broadcast", "Stop Broadcast"),
    PermissionDefinition("broadcast.emergency_stop", "Broadcast",
                        "Emergency Stop ALL Broadcasts"),
    PermissionDefinition("broadcast.view_ownership", "Broadcast",
                        "Active Broadcasts - View Broadcaster"),
    PermissionDefinition("broadcast.active_view", "Broadcast",
                        "Active Broadcasts - View Page"),
    PermissionDefinition("broadcast.view_targets", "Broadcast",
                        "Active Broadcasts - View Stores"),
    PermissionDefinition("broadcast.stop_any", "Broadcast",
                        "Active Broadcasts - Stop Other Broadcast"),
    #: Manage the WEB AUDIENCE of somebody else's live Broadcast.
    #:
    #: Separate from broadcast.view_ownership on purpose. Seeing who is
    #: broadcasting and removing a person from their audience are different
    #: powers: the first is a disclosure, the second is an intervention the
    #: owning operator cannot see happening. A supervisor trusted to read the
    #: page is not thereby trusted to eject that page's listeners.
    #:
    #: Covers Approve, Deny, Kick and Auto Approve. Password rotation is
    #: deliberately NOT included - see the room routes.
    PermissionDefinition("broadcast.manage_web_audience", "Broadcast",
                        "Active Broadcasts - Manage Web Audience"),
    #: Change a Store's SpeakLink OUTPUT volume/mute during a live broadcast.
    #:
    #: A new code rather than a reuse. broadcast.start is about beginning a
    #: broadcast, not about steering it afterwards; broadcast.active_view and
    #: broadcast.stop_any are supervision codes, and neither should silently
    #: become "may change how loud another operator's Store is". Ownership of
    #: the session is enforced separately and always - this permission answers
    #: "may this account operate output volume at all", never "whose".
    PermissionDefinition("store_audio.control", "Broadcast",
                        "Control Store Output Volume"),
    #: May this account deliver a broadcast to PHYSICAL Stores at all.
    #:
    #: A separate code because physical delivery and broadcasting are no longer
    #: the same capability. A broadcast can now reach a web audience through a
    #: shared link with no Store targets whatsoever, so "may broadcast" and "may
    #: put sound into a shop" are genuinely different questions - and the second
    #: is the one with a loudspeaker on the end of it.
    #:
    #: This is deliberately NOT solved through Store Scope. Scope answers WHICH
    #: Stores, and a blank scope means unrestricted; using it to express "no
    #: Stores at all" would overload a field whose empty value already means the
    #: opposite. Missing this permission denies every physical target regardless
    #: of what Scope says.
    PermissionDefinition("broadcast.store_delivery", "Broadcast",
                        "Broadcast to Stores / Zones"),

    PermissionDefinition("menu.stores.view", "Stores", "View Store Management"),
    PermissionDefinition("stores.create", "Stores", "Create Store"),
    PermissionDefinition("stores.update", "Stores", "Edit Store"),
    PermissionDefinition("stores.archive", "Stores", "Disable / Archive Store"),
    PermissionDefinition("stores.delete_permanently", "Stores",
                        "Permanently Delete Store (history-preserving tombstone)"),

    PermissionDefinition("menu.receivers.view", "Receivers", "View Receiver Status"),
    PermissionDefinition("devices.enrollment.create", "Receivers", "Create Enrolment Code"),
    PermissionDefinition("devices.primary.assign", "Receivers", "Assign Primary Device"),
    PermissionDefinition("devices.rotate", "Receivers", "Rotate Device Credential"),
    PermissionDefinition("devices.disable", "Receivers", "Disable Device"),
    PermissionDefinition("devices.revoke", "Receivers", "Revoke Device"),
    PermissionDefinition("devices.archive", "Receivers", "Archive Device"),
    PermissionDefinition("devices.delete_permanently", "Receivers", "Permanently Delete Device"),

    PermissionDefinition("menu.history.view", "History", "View Broadcast History"),
    PermissionDefinition("broadcast_history.archive", "History",
                        "Archive Broadcast Sessions"),
    PermissionDefinition("broadcast_history.delete_permanently", "History",
                        "Permanently Delete Broadcast Sessions"),

    #: Reading what an operator REMOVED from a chat.
    #:
    #: Its own right, held by OWNER and ADMIN and by nobody else by default -
    #: not even the operator running the Broadcast. Removing a message is a
    #: moderation act, and the person who moderates is not automatically the
    #: person entitled to keep reading what they took down; that separation is
    #: the whole reason this is a permission rather than a role check.
    #:
    #: It never survives the Broadcast. Deleting the Broadcast from history
    #: erases the messages and their images for everybody, this right included.
    PermissionDefinition("chat.view_deleted", "Broadcast",
                        "See Deleted Chat Messages"),

    #: Downloading the Store Kit from HQ.
    #:
    #: The kit is the software a shop runs, so this is closer to a device
    #: right than a reporting one: whoever can fetch it can install SpeakLink
    #: on any machine they like. It carries no credential and enrols nothing -
    #: a kit without an enrolment code is inert - but it is still the estate's
    #: software and is not something a VIEWER should be handing out.
    PermissionDefinition("store_kit.download", "Receivers",
                        "Download the Store Kit"),
    #: Putting a NEW installer on HQ, and removing one.
    #:
    #: Separate from downloading, and much stronger: whoever can upload decides
    #: what software every Store installs next. It is the one permission here
    #: that can change what runs on machines nobody at HQ can see, so it is
    #: held by OWNER and ADMIN and by nobody else by default.
    PermissionDefinition("store_kit.manage", "Receivers",
                        "Upload and Remove Store Kits"),

    # -----------------------------------------------------------------
    # Recorded announcements
    #
    # Deliberately FOUR codes rather than one "announcements" right, because
    # the four questions have genuinely different answers in a real shop:
    #
    #   who may look at what is playing            announcements.view
    #   who may press play and pause               announcements.control
    #   who may decide what plays and where        announcements.templates.manage
    #   who may put new audio on the estate        announcements.upload
    #
    # A duty manager who should be able to silence a jingle that is annoying
    # customers must not thereby be able to upload a recording that every
    # Store in the country then plays. Bundling them would make the useful
    # grant impossible to give without the dangerous one.
    # -----------------------------------------------------------------
    PermissionDefinition("menu.announcements.view", "Announcements",
                        "View Announcements"),
    PermissionDefinition("announcements.control", "Announcements",
                        "Play / Pause Announcements"),
    #: Play All and Pause All, across the estate in one action.
    #:
    #: Separate from announcements.control on purpose. Pausing one Store is a
    #: local decision anybody running that shop can make; pausing every Store
    #: at once is an estate-wide action with the same reach as an emergency
    #: stop, and it should be possible to grant the first without the second.
    PermissionDefinition("announcements.control_all", "Announcements",
                        "Play All / Pause All"),
    PermissionDefinition("announcements.volume", "Announcements",
                        "Set Announcement Volume"),
    PermissionDefinition("announcements.templates.manage", "Announcements",
                        "Create and Edit Templates"),
    PermissionDefinition("announcements.upload", "Announcements",
                        "Upload Recordings"),
    PermissionDefinition("announcements.delete_permanently", "Announcements",
                        "Permanently Delete Recordings"),

    #: Changing which speaker a Store plays through, from HQ.
    #:
    #: This was only ever possible standing at the Store PC, and that was a
    #: real protection: getting it wrong makes a shop silent, and the person
    #: who could get it wrong was also the person who could hear the result.
    #: Done remotely, nobody at HQ can hear anything - so it is its own
    #: permission rather than part of managing Devices.
    PermissionDefinition("receiver.set_output_device", "Receivers",
                        "Change a Store's Speaker Remotely"),

    #: Joining a broadcast somebody else started, as a second voice.
    PermissionDefinition("broadcast.group_join", "Broadcast",
                        "Join a Group Broadcast"),
    #: Opening a broadcast to other broadcasters in the first place.
    PermissionDefinition("broadcast.group_host", "Broadcast",
                        "Host a Group Broadcast"),

    PermissionDefinition("menu.logs.view", "Logs", "View System Logs"),
    PermissionDefinition("system_logs.archive", "Logs", "Archive System Logs"),
    PermissionDefinition("system_logs.delete_permanently", "Logs",
                        "Permanently Delete System Logs"),

    PermissionDefinition("menu.users.view", "Users", "View User Management"),
    PermissionDefinition("users.create", "Users", "Create User"),
    PermissionDefinition("users.update", "Users", "Edit User"),
    PermissionDefinition("users.disable", "Users", "Disable / Archive User"),
    PermissionDefinition("users.permissions.manage", "Users", "Manage User Rights"),
    PermissionDefinition("users.delete_permanently", "Users",
                        "Permanently Delete User (history-preserving tombstone)"),
)

#: Irreversibly destructive operations. ADMIN is denied all of them by
#: default - running the estate is a different kind of trust than destroying
#: its records - and they are listed once here rather than repeated in the
#: role matrix, so adding a destructive permission cannot silently grant it
#: to ADMIN by omission.
DESTRUCTIVE_CODES: frozenset[str] = frozenset({
    "stores.delete_permanently",
    "devices.delete_permanently",
    "users.delete_permanently",
    "broadcast_history.delete_permanently",
    "system_logs.delete_permanently",
    "announcements.delete_permanently",
})

PERMISSION_CODES: frozenset[str] = frozenset(p.code for p in PERMISSION_DEFINITIONS)
PERMISSIONS_BY_CODE: dict[str, PermissionDefinition] = {p.code: p for p in PERMISSION_DEFINITIONS}

_ALL_CODES = frozenset(PERMISSION_CODES)

#: What running a broadcast needs: open the console, start, stop your own.
#:
#: broadcast.emergency_stop is deliberately NOT here. It used to be, and
#: BROADCASTER inherited it - which was nearly harmless when there was one
#: global broadcast, since the only thing to stop was usually your own. With
#: concurrent sessions the same permission means "terminate every other
#: operator's broadcast estate-wide", and that is not a capability that should
#: arrive by inheritance from the group named after ordinary broadcasting.
#:
#: broadcast.view_ownership is not here either. Knowing a Store is busy is
#: operational information a Broadcaster needs; knowing WHOSE campaign is
#: using it is not.
#:
#: The three Active Broadcast management codes are likewise absent, and each
#: for its own reason rather than as one "supervision" bundle:
#:
#:   broadcast.active_view   - opening the supervision page at all
#:   broadcast.view_targets  - the EXACT Stores another operator is using
#:   broadcast.stop_any      - silencing one specific other operator
#:
#: They are deliberately independent. Holding stop_any does not reveal Store
#: names, holding view_targets does not reveal who owns the broadcast, and
#: neither implies the other - a supervisor may be trusted to end a broadcast
#: on a Store they administer without being told which campaign or which
#: colleague it belonged to. Bundling them would make the finest-grained
#: question ("who may see what") answerable only at the coarsest grain.
#: store_audio.control IS here, unlike the supervision codes above. Setting the
#: output level of the Stores you are broadcasting to is part of running an
#: ordinary broadcast - an operator who may take the estate live but may not
#: stop it deafening one shop has been given the dangerous half of the job.
#: Ownership still gates every individual command, so holding this grants
#: nothing over anybody else's session.
#: broadcast.store_delivery IS here, so every role that can already take the
#: estate live keeps that ability across the upgrade. The new boundary exists to
#: be REMOVED from an account deliberately - creating a link-only broadcaster -
#: not to silently take physical delivery away from every operator who has it.
#: VIEWER is unaffected: it holds none of these codes.
_BROADCAST_CODES = frozenset({
    "menu.broadcast.view", "broadcast.start", "broadcast.stop",
    "store_audio.control", "broadcast.store_delivery",
    # A broadcaster interrupts announcements by definition - starting a
    # broadcast ducks them - so being able to see and settle them is part of
    # the same job. Deciding what exists is not: uploading a recording and
    # writing a template stay with OWNER and ADMIN.
    "menu.announcements.view", "announcements.control", "announcements.volume",
    "broadcast.group_join",
})
_VIEW_ONLY_CODES = frozenset({
    "menu.broadcast.view", "menu.stores.view", "menu.receivers.view",
    "menu.history.view", "menu.logs.view",
    # Looking at what is playing changes nothing, and a VIEWER who cannot see
    # it cannot answer the question a shop actually rings up to ask.
    "menu.announcements.view",
})

#: The default role matrix. This is the ONE place a role's fine-grained rights
#: are decided; overrides are layered on top of it in
#: ``resolve_effective_permissions``, never merged into it.
#:
#: ADMIN deliberately gets everything except ``users.permissions.manage`` and
#: ``devices.delete_permanently`` - the same reasoning ``rbac.ROLE_PERMISSIONS``
#: already applies to ``MANAGE_SECURITY``: running the estate is a different
#: kind of trust than deciding who else gets to run it, or than permanently
#: destroying a Device's row (as opposed to archiving it, which ADMIN may).
#: Permanent deletion is effectively SUPER ADMIN-only until a separately
#: reviewed policy changes that.
DEFAULT_ROLE_PERMISSIONS: dict[Role, frozenset[str]] = {
    Role.OWNER: _ALL_CODES,
    # Everything except granting rights to others, and every irreversibly
    # destructive operation (DESTRUCTIVE_CODES). ADMIN keeps every archive
    # action - archiving is reversible and is the intended everyday tool.
    Role.ADMIN: _ALL_CODES - frozenset({"users.permissions.manage"}) - DESTRUCTIVE_CODES,
    # Broadcast Console, its three controls, History and Receiver Status -
    # nothing that edits a Store, a Device's security, or a User.
    # menu.stores.view is included because Receiver Status (GET /api/stores)
    # requires it - without this, every BROADCASTER without a manually added
    # per-user override got a 403 there and the page silently rendered blank.
    Role.BROADCASTER: _BROADCAST_CODES | frozenset({
        "menu.history.view", "menu.receivers.view", "menu.stores.view",
    }),
    # Read-only, and only over the operational surfaces - not User Management,
    # matching the existing frontend nav restriction (`roles: ["OWNER", "ADMIN"]`
    # on the Users link) and the fact VIEWER never held MANAGE_USERS before.
    Role.VIEWER: _VIEW_ONLY_CODES - frozenset({"menu.users.view"}),
}


def _require_known_code(code: str) -> None:
    if code not in PERMISSION_CODES:
        raise UnknownPermissionCode(code)


class UnknownPermissionCode(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(f"Unknown permission code: {code}")
        self.code = code


class OwnerOverrideRefused(RuntimeError):
    """An OWNER's effective rights are never narrowed by an override.

    OWNER already holds every permission by role default, and OWNER is the
    account of last resort - the one that can always fix a lockout. Allowing a
    DENY override on an OWNER would let a single click on the wrong row create
    exactly the lockout the role exists to prevent, and unlike disabling the
    last OWNER (guarded in ``user_lifecycle.py``) there is no equivalent
    per-permission guard to fall back on, so this is refused outright rather
    than only for the last one.
    """

    def __init__(self) -> None:
        super().__init__("OWNER permissions cannot be overridden.")


class RightsEscalationRefused(RuntimeError):
    """The actor tried to hand out authority they do not hold, or to edit
    their own rights.

    These two guards did not exist while ``users.permissions.manage`` was
    unreachable: both routes were gated on "is this account literally OWNER",
    and an OWNER already holds every permission, so there was nothing to
    escalate TO. Making the permission real for ADMIN is exactly what creates
    the possibility, so the guards arrive in the same change.

    Owned by this module rather than shared with ``rbac`` so that reloading
    either module cannot produce two classes of the same name and silently
    stop an ``except`` clause from matching.
    """


class SelfRightsEditRefused(RightsEscalationRefused):
    """An account may not edit its own permission overrides.

    Not because self-editing is always dangerous - removing one of your own
    permissions is harmless - but because distinguishing the harmless case
    costs a rule nobody can hold in their head, and the dangerous case is
    total: an ADMIN with Manage User Rights who may edit themselves can grant
    themselves every remaining permission in one request. OWNER is unaffected;
    OWNER already holds everything and has nothing to grant itself.
    """

    def __init__(self) -> None:
        super().__init__(
            "You cannot change your own permissions. Ask another authorised "
            "account to make this change."
        )


class GrantBeyondActorRefused(RightsEscalationRefused):
    """An actor may only grant what the actor effectively holds.

    Without this, Manage User Rights would be the single most powerful
    permission in the product: an ADMIN denied ``users.delete_permanently``
    could simply ALLOW it on a BROADCASTER account and sign in as nobody at
    all - the restriction would be decorative. The rule keeps the permission
    meaningful as delegation rather than as a bypass.

    Deliberately one-directional. Granting a permission the actor lacks is
    refused; REVOKING one is not, because taking authority away can never
    raise the actor's own, and an administrator who can see a permission
    should be able to withdraw it.
    """

    def __init__(self, code: str) -> None:
        super().__init__(
            f"You cannot grant a permission you do not hold yourself: {code}."
        )
        self.code = code


def ensure_permission_schema(engine: Engine) -> None:
    """Additive and idempotent. Never touches ``user_permission_overrides``
    or ``permission_audit_events`` once created - those hold operator
    decisions and history, not derived configuration.

    On PostgreSQL the tables come from the portable ``postgres_schema``
    definitions instead of the raw SQL below, which is SQLite-only
    (``AUTOINCREMENT`` is a syntax error on PostgreSQL, and ``IF NOT EXISTS``
    does not save it - the statement still has to parse).

    Skipping the DDL is not skipping the function. The RESEED below is the
    part that matters on every boot: it is what adds a newly introduced
    permission code to a database created by an older release. Without it a
    cutover would carry the previous release's catalog forever, and every
    feature guarded by a new code would be denied to everybody with nothing
    to indicate why.
    """
    if engine.dialect.name != "sqlite":
        import postgres_schema

        for table in (postgres_schema.permissions,
                      postgres_schema.role_permissions,
                      postgres_schema.user_permission_overrides,
                      postgres_schema.permission_audit_events):
            table.create(bind=engine, checkfirst=True)
        _reseed_permission_catalog(engine)
        return

    with engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS permissions (
                code TEXT PRIMARY KEY,
                permission_group TEXT NOT NULL,
                label TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS role_permissions (
                role TEXT NOT NULL,
                permission_code TEXT NOT NULL REFERENCES permissions(code),
                PRIMARY KEY (role, permission_code)
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_role_permissions_role ON role_permissions(role)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS user_permission_overrides (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES hq_users(id),
                permission_code TEXT NOT NULL REFERENCES permissions(code),
                effect TEXT NOT NULL CHECK (effect IN ('ALLOW', 'DENY')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, permission_code)
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_user_permission_overrides_user "
            "ON user_permission_overrides(user_id)"
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE IF NOT EXISTS permission_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_user_id INTEGER NOT NULL REFERENCES hq_users(id),
                target_user_id INTEGER NOT NULL REFERENCES hq_users(id),
                permission_code TEXT NOT NULL,
                old_value TEXT NOT NULL,
                new_value TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS idx_permission_audit_target "
            "ON permission_audit_events(target_user_id)"
        )

        # The catalog and the default matrix are DERIVED configuration - the
        # code above is the source of truth - so they are safely reseeded on
        # every boot. Re-running this is what keeps a running HQ in sync after
        # a permission is added or a role's default changes upstream.
        for definition in PERMISSION_DEFINITIONS:
            connection.execute(
                text(
                    "INSERT INTO permissions (code, permission_group, label) "
                    "VALUES (:code, :group_name, :label) "
                    "ON CONFLICT(code) DO UPDATE SET "
                    "permission_group=excluded.permission_group, label=excluded.label"
                ),
                {"code": definition.code, "group_name": definition.group, "label": definition.label},
            )
        connection.exec_driver_sql("DELETE FROM role_permissions")
        for role, codes in DEFAULT_ROLE_PERMISSIONS.items():
            for code in codes:
                connection.execute(
                    text(
                        "INSERT INTO role_permissions (role, permission_code) "
                        "VALUES (:role, :code)"
                    ),
                    {"role": role.value, "code": code},
                )


def _reseed_permission_catalog(engine: Engine) -> None:
    """The derived catalog and role matrix, written from code.

    Extracted so both dialects run the SAME reseed - the SQLite path keeps its
    raw-SQL table creation, PostgreSQL gets its tables from postgres_schema,
    and neither has its own private copy of what the catalog should contain.

    ``ON CONFLICT(code) DO UPDATE`` is standard on both PostgreSQL and modern
    SQLite, so the statement itself needs no branch.
    """
    with engine.begin() as connection:
        for definition in PERMISSION_DEFINITIONS:
            connection.execute(
                text(
                    "INSERT INTO permissions (code, permission_group, label) "
                    "VALUES (:code, :group_name, :label) "
                    "ON CONFLICT(code) DO UPDATE SET "
                    "permission_group=excluded.permission_group, label=excluded.label"
                ),
                {"code": definition.code, "group_name": definition.group,
                 "label": definition.label},
            )
        connection.exec_driver_sql("DELETE FROM role_permissions")
        for role, codes in DEFAULT_ROLE_PERMISSIONS.items():
            for code in codes:
                connection.execute(
                    text("INSERT INTO role_permissions (role, permission_code) "
                         "VALUES (:role, :code)"),
                    {"role": role.value, "code": code},
                )


def _role_default_codes(engine: Engine, role: Role) -> frozenset[str]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT permission_code FROM role_permissions WHERE role = :role"),
            {"role": role.value},
        ).all()
    if rows:
        return frozenset(row[0] for row in rows)
    # Table not seeded yet (e.g. a test engine that never ran
    # ensure_permission_schema) - fall back to the in-code matrix so the
    # resolver still fails closed rather than granting nothing by accident
    # only in some environments and something else in others.
    return DEFAULT_ROLE_PERMISSIONS.get(role, frozenset())


def _overrides_for_user(engine: Engine, user_id: int) -> dict[str, Effect]:
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT permission_code, effect FROM user_permission_overrides "
                "WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        ).all()
    return {row[0]: Effect(row[1]) for row in rows}


def resolve_effective_permissions(engine: Engine, user) -> frozenset[str]:
    """What this account can actually do right now, including overrides.

    Fails closed exactly like ``rbac.effective_permissions``: an inactive
    account or an unparsable role grants nothing, never "everything".
    """
    if user is None or not getattr(user, "is_active", False):
        return frozenset()
    role = parse_role(getattr(user, "role", None))
    if role is None:
        return frozenset()

    base = set(_role_default_codes(engine, role))
    for code, effect in _overrides_for_user(engine, user.id).items():
        if effect is Effect.ALLOW:
            base.add(code)
        elif effect is Effect.DENY:
            base.discard(code)
    return frozenset(base)


def has_permission_code(engine: Engine, user, code: str) -> bool:
    return code in resolve_effective_permissions(engine, user)


def require_permission_code(engine: Engine, user, code: str) -> None:
    if not has_permission_code(engine, user, code):
        raise PermissionDenied()


def describe_user_permissions(engine: Engine, *, user_id: int, role: Role) -> list[dict]:
    """Per-permission breakdown for the rights-editor UI: role default,
    stored override (or INHERIT), and the effective result - so the UI can
    show "Role: Allowed / Override: DENY / Effective: Denied" without the
    frontend re-implementing the priority rule."""
    role_defaults = _role_default_codes(engine, role)
    overrides = _overrides_for_user(engine, user_id)
    rows = []
    for definition in PERMISSION_DEFINITIONS:
        role_allowed = definition.code in role_defaults
        override = overrides.get(definition.code, Effect.INHERIT)
        if override is Effect.ALLOW:
            effective = True
        elif override is Effect.DENY:
            effective = False
        else:
            effective = role_allowed
        rows.append({
            "code": definition.code,
            "group": definition.group,
            "label": definition.label,
            "role_allowed": role_allowed,
            "override": override.value,
            "effective": effective,
        })
    return rows


def set_permission_overrides(
    engine: Engine,
    *,
    actor,
    target_user_id: int,
    target_role: Role,
    changes: list[dict],
) -> list[dict]:
    """Apply a batch of {code, effect} changes for one user, audited one row
    at a time.

    Refuses the WHOLE batch - no partial write - if any code is unknown, the
    target is an OWNER, the actor is editing itself, or the actor is trying to
    grant a permission it does not effectively hold. All four are checked
    before the transaction opens, so a refusal leaves nothing behind.

    The route above is gated on ``users.permissions.manage``. That guard
    answers "may this account manage rights at all"; the guards here answer
    "may it make THIS change", which is a different question and cannot be
    expressed as a single permission. Both are server-side, and neither
    depends on the frontend having hidden a control.
    """
    if target_role is Role.OWNER:
        raise OwnerOverrideRefused()

    actor_id = getattr(actor, "id", None)
    if actor_id is not None and actor_id == target_user_id:
        raise SelfRightsEditRefused()

    parsed: list[tuple[str, Effect]] = []
    for change in changes:
        code = change["code"]
        _require_known_code(code)
        parsed.append((code, Effect(change["effect"])))

    # What the actor may hand out. OWNER resolves to the full catalog, so this
    # is a no-op for the account that could already do anything; it bites only
    # for a delegated ADMIN, which is the case it exists for.
    actor_role = parse_role(getattr(actor, "role", None))
    if actor_role is not Role.OWNER:
        # The same resolver every request uses, so what an actor may delegate
        # is by construction what it may itself do - including any DENY
        # override placed on the actor.
        actor_permissions = resolve_effective_permissions(engine, actor)
        for code, effect in parsed:
            # Only ALLOW is checked. DENY and INHERIT can only ever reduce the
            # target's authority, and reducing is not escalation.
            if effect is Effect.ALLOW and code not in actor_permissions:
                raise GrantBeyondActorRefused(code)

    now = datetime.now(timezone.utc).isoformat()
    audit_rows: list[dict] = []
    with engine.begin() as connection:
        existing = {
            row[0]: Effect(row[1])
            for row in connection.execute(
                text(
                    "SELECT permission_code, effect FROM user_permission_overrides "
                    "WHERE user_id = :user_id"
                ),
                {"user_id": target_user_id},
            ).all()
        }
        for code, effect in parsed:
            old_value = existing.get(code, Effect.INHERIT)
            if effect is Effect.INHERIT:
                connection.execute(
                    text(
                        "DELETE FROM user_permission_overrides "
                        "WHERE user_id = :user_id AND permission_code = :code"
                    ),
                    {"user_id": target_user_id, "code": code},
                )
            else:
                connection.execute(
                    text(
                        "INSERT INTO user_permission_overrides "
                        "(user_id, permission_code, effect, created_at, updated_at) "
                        "VALUES (:user_id, :code, :effect, :now, :now) "
                        "ON CONFLICT(user_id, permission_code) DO UPDATE SET "
                        "effect=excluded.effect, updated_at=excluded.updated_at"
                    ),
                    {"user_id": target_user_id, "code": code, "effect": effect.value, "now": now},
                )
            connection.execute(
                text(
                    "INSERT INTO permission_audit_events "
                    "(actor_user_id, target_user_id, permission_code, old_value, "
                    "new_value, created_at) "
                    "VALUES (:actor_id, :target_id, :code, :old_value, :new_value, :now)"
                ),
                {
                    "actor_id": actor.id, "target_id": target_user_id, "code": code,
                    "old_value": old_value.value, "new_value": effect.value, "now": now,
                },
            )
            audit_rows.append({"code": code, "old_value": old_value.value, "new_value": effect.value})
    return audit_rows


__all__ = [
    "DEFAULT_ROLE_PERMISSIONS",
    "Effect",
    "OwnerOverrideRefused",
    "PERMISSION_CODES",
    "PERMISSION_DEFINITIONS",
    "PERMISSIONS_BY_CODE",
    "UnknownPermissionCode",
    "describe_user_permissions",
    "ensure_permission_schema",
    "has_permission_code",
    "require_permission_code",
    "resolve_effective_permissions",
    "set_permission_overrides",
]
