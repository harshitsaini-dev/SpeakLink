"""Permission-shaped supervision view over the live broadcasts.

WHY THIS IS A MODULE AND NOT A SECOND ENDPOINT'S BODY

There is exactly one definition of "a broadcast is active": a session id in
``BroadcastRuntime``, backed by a ``BroadcastSession`` row whose status is
live, holding Store leases in ``broadcast_store_leases``. ``GET
/api/broadcast/active`` already reads it. A supervision page that computed
active-ness its own way would eventually disagree with the console - and the
disagreement would surface as a Store that one screen calls busy and the
other calls free, which is the hardest class of bug to believe. So the rows
are built here, once, from the runtime, and both endpoints call in.

THE REDACTION RULE

Every hidden field is hidden by NOT BEING BUILT, never by being built and
then dropped in React. A field that reaches the browser has been disclosed
whatever the interface does with it afterwards: it is in the network tab, the
response cache, and any proxy log that records bodies. The tests assert on
the serialized dictionary for that reason, not on what a component renders.

FOUR INDEPENDENT QUESTIONS

    broadcast.active_view    may I open this page at all
    broadcast.view_ownership may I know WHO is broadcasting
    broadcast.view_targets   may I know WHICH Stores
    broadcast.stop_any       may I stop somebody else's broadcast

None of them implies another. A supervisor may be trusted to end a broadcast
on Stores they administer without being told whose campaign it was; an
auditor may need to see which Stores are in use without any power to
intervene. Implication would collapse all four into one, and the whole point
of the operator's request was that they are different.

STORE SCOPE IS NOT A PERMISSION

Scope narrows WHICH Stores exist for this account, and it applies underneath
every permission above. A user with view_targets still sees only in-scope
Stores. This page must never become the one place where scope does not apply.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.engine import Engine

from permission_catalog import has_permission_code

#: The page permission. Everything else on this screen is layered on top of it,
#: so it is checked first and alone - a caller without it learns nothing at
#: all, not even how many broadcasts exist.
PAGE_CODE = "broadcast.active_view"
OWNERSHIP_CODE = "broadcast.view_ownership"
TARGETS_CODE = "broadcast.view_targets"
STOP_ANY_CODE = "broadcast.stop_any"
MANAGE_WEB_AUDIENCE_CODE = "broadcast.manage_web_audience"

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100

SORT_NEWEST = "newest"
SORT_OLDEST = "oldest"
SORTS = (SORT_NEWEST, SORT_OLDEST)

OWNER_FILTERS = ("all", "mine", "others")


class TargetVisibilityDenied(PermissionError):
    """Asked for exact Stores without broadcast.view_targets."""


class OwnershipVisibilityDenied(PermissionError):
    """Asked to search or filter by owner without broadcast.view_ownership."""


@dataclass(frozen=True, slots=True)
class Visibility:
    """What this account may know and do on the supervision page.

    Resolved once per request and passed down, so a single request cannot
    answer the same permission question two different ways - which is how a
    list that hides owners ends up beside a detail view that shows them.
    """

    may_view_page: bool
    may_view_ownership: bool
    may_view_targets: bool
    may_stop_any: bool
    #: Manage ANOTHER operator's web audience. Deliberately its own question:
    #: reading who is broadcasting and ejecting their listeners are different
    #: powers, and the second is invisible to the operator it happens to.
    may_manage_web_audience: bool = False

    def as_dict(self) -> dict:
        """Sent to the client so it can render the right controls.

        Advertising a capability the caller already holds discloses nothing:
        they can determine each of these by making the request anyway. What
        must never be sent is the DATA behind a capability they lack.
        """
        return {
            "may_view_ownership": self.may_view_ownership,
            "may_view_targets": self.may_view_targets,
            "may_stop_any": self.may_stop_any,
            "may_manage_web_audience": self.may_manage_web_audience,
        }


def resolve_visibility(engine: Engine, user) -> Visibility:
    return Visibility(
        may_view_page=has_permission_code(engine, user, PAGE_CODE),
        may_view_ownership=has_permission_code(engine, user, OWNERSHIP_CODE),
        may_view_targets=has_permission_code(engine, user, TARGETS_CODE),
        may_stop_any=has_permission_code(engine, user, STOP_ANY_CODE),
        may_manage_web_audience=has_permission_code(
            engine, user, MANAGE_WEB_AUDIENCE_CODE),
    )


@dataclass(frozen=True, slots=True)
class WebRoomSummary:
    """The compact room facts an authorised supervisor may see on the list.

    Deliberately small. The participant lists live behind their own route, for
    exactly the reason the Store targets do: fifty sessions multiplied by every
    listener is the payload this page exists to avoid.

    ``password`` is present only when the running process still holds the
    plaintext it generated. It is never read back from storage, because it is
    not stored - only a bcrypt hash is. ``password_available`` says which of
    those two worlds the caller is in, so the UI can be truthful rather than
    printing asterisks for a value nothing knows.
    """

    public_code: str
    status: str
    auto_approve: bool
    password: str | None
    waiting_count: int
    connected_count: int
    listening_count: int

    @property
    def password_available(self) -> bool:
        return bool(self.password)

    def as_dict(self) -> dict:
        return {
            "public_code": self.public_code,
            "status": self.status,
            "auto_approve": self.auto_approve,
            # The plaintext when this process still has it, and null - never a
            # masked placeholder - when it does not.
            "password": self.password,
            "password_available": self.password_available,
            "waiting_count": self.waiting_count,
            "connected_count": self.connected_count,
            "listening_count": self.listening_count,
        }


@dataclass(frozen=True, slots=True)
class StoreTarget:
    store_id: int
    store_code: str
    store_name: str

    def as_dict(self) -> dict:
        return {
            "store_id": self.store_id,
            "store_code": self.store_code,
            "store_name": self.store_name,
        }


@dataclass
class ActiveRow:
    """One live broadcast, with everything known about it internally.

    Deliberately holds the unredacted truth - owner id, owner username, full
    target list - because filtering and searching have to run against real
    data. ``serialize`` is the only way out, and it is where redaction
    happens, so no caller can accidentally return this object.
    """

    session_id: int
    campaign_name: str
    started_at: str | None
    status: str
    owner_user_id: int
    owner_username: str | None
    owner_display_name: str | None
    #: Already intersected with the viewer's Store Scope.
    visible_targets: list[StoreTarget]
    #: Every Store this session targets, scope or no scope. Used ONLY to
    #: decide whether a cross-owner stop is fully inside scope; never
    #: serialized.
    all_target_store_ids: frozenset[int]
    is_mine: bool
    #: This Broadcast's web room, or None if it has none. Held unredacted like
    #: everything else on this row; ``serialize`` is the only way out.
    web_room: "WebRoomSummary | None" = None

    def serialize(self, visibility: Visibility) -> dict:
        """The wire shape, redacted to this account.

        The list never carries exact Stores even with view_targets: that is
        what the detail endpoint is for, and sending 50 sessions x every
        target by default is the thing the operator asked us to avoid. What
        view_targets buys HERE is the right to ask for detail, and to search
        and filter by Store.
        """
        row = {
            "session_id": self.session_id,
            "campaign_name": self.campaign_name,
            "started_at": self.started_at,
            "status": self.status,
            # The count is of SCOPE SURVIVORS, not the real total. Reporting
            # the true total to a scoped viewer would let them measure exactly
            # how much they are not allowed to see; a count is a disclosure
            # like any other.
            "target_store_count": len(self.visible_targets),
            "is_mine": self.is_mine,
        }
        # Your own broadcast is yours - identifying yourself to yourself is
        # not an ownership disclosure, and hiding it would make your own row
        # unreadable on a page you are permitted to open.
        if visibility.may_view_ownership or self.is_mine:
            row["owner_user_id"] = self.owner_user_id
            row["owner_username"] = self.owner_username
            row["owner_display_name"] = self.owner_display_name
            # The room's public code IS a credential: anybody holding it can
            # attempt to join, and with Auto Approve on they are in. So it is
            # governed by the same permission as the broadcaster's identity,
            # and for a caller without it the key is absent entirely rather
            # than present-and-null - an absent key cannot be un-hidden by a
            # frontend, and a null one invites somebody to try.
            if self.web_room is not None:
                row["web_room"] = self.web_room.as_dict()
        return row


def collect_active_rows(
    *,
    runtime,
    session_lookup,
    owner_lookup,
    store_lookup,
    scope: frozenset[int] | None,
    viewer_user_id: int,
    web_room_lookup=None,
) -> list[ActiveRow]:
    """Build the unredacted row set from the ONE active-truth source.

    ``runtime`` is the live ``BroadcastRuntime``; the lookups are injected so
    this is testable without a FastAPI app and without a real database, and so
    the endpoint keeps its own query style. Sessions the runtime knows about
    but the database does not are skipped rather than guessed at.
    """
    rows: list[ActiveRow] = []
    for session_id in runtime.active_session_ids():
        live = runtime.get(session_id)
        if live is None:
            continue
        session = session_lookup(session_id)
        if session is None:
            continue

        all_targets = frozenset(live.target_store_ids)
        in_scope_ids = sorted(
            store_id for store_id in all_targets
            if scope is None or store_id in scope
        )
        targets: list[StoreTarget] = []
        for store_id in in_scope_ids:
            store = store_lookup(store_id)
            if store is None:
                continue
            targets.append(StoreTarget(
                store_id=store_id,
                store_code=store.store_code,
                store_name=store.store_name,
            ))

        is_mine = live.owner_user_id == viewer_user_id
        # A broadcast that touches NONE of this viewer's Stores is not their
        # business, and listing it - even with a zero count and no owner -
        # would disclose that it exists. Scope narrows which Stores exist for
        # an account, so a session entirely outside it must not appear at all.
        #
        # Your own broadcast is the deliberate exception: it stays visible
        # even if a later scope change excluded its Stores, because a row you
        # cannot see is a broadcast you cannot find your way back to.
        if not is_mine and scope is not None and not in_scope_ids:
            continue

        owner = owner_lookup(live.owner_user_id)
        rows.append(ActiveRow(
            session_id=session_id,
            campaign_name=getattr(session, "campaign_name", "") or "",
            started_at=(session.started_at.isoformat()
                        if getattr(session, "started_at", None) else None),
            status=getattr(session, "status", "live"),
            owner_user_id=live.owner_user_id,
            owner_username=getattr(owner, "username", None) if owner else None,
            owner_display_name=getattr(owner, "display_name", None) if owner else None,
            visible_targets=targets,
            all_target_store_ids=all_targets,
            is_mine=is_mine,
            web_room=(web_room_lookup(session_id)
                      if web_room_lookup is not None else None),
        ))
    return rows


def _matches_search(row: ActiveRow, term: str, visibility: Visibility) -> bool:
    """Which fields a search may look at depends on what the caller may see.

    This is the subtle one. If search matched owner names for somebody
    without view_ownership, then typing "Alice" and getting one result tells
    them Alice is broadcasting - the identity is disclosed by the SHAPE of
    the answer even though no owner field was ever serialized. The same holds
    for Store names. So a field the caller may not see is not searched at
    all, and a query that only would have matched such a field returns
    nothing.
    """
    needle = term.strip().lower()
    if not needle:
        return True

    haystack = [row.campaign_name or ""]
    if visibility.may_view_ownership or row.is_mine:
        haystack.append(row.owner_username or "")
        haystack.append(row.owner_display_name or "")
    if visibility.may_view_targets:
        for target in row.visible_targets:
            haystack.append(target.store_code or "")
            haystack.append(target.store_name or "")
    return any(needle in value.lower() for value in haystack)


def filter_and_sort(
    rows: list[ActiveRow],
    *,
    visibility: Visibility,
    search: str | None = None,
    owner_filter: str = "all",
    owner_user_id: int | None = None,
    store_id: int | None = None,
    sort: str = SORT_NEWEST,
) -> list[ActiveRow]:
    """Apply the permitted filters. Unauthorized ones raise rather than being
    quietly ignored - a filter that silently does nothing gives the caller a
    result set they will misread as the truth."""
    if owner_user_id is not None and not visibility.may_view_ownership:
        raise OwnershipVisibilityDenied()
    if store_id is not None and not visibility.may_view_targets:
        raise TargetVisibilityDenied()
    if owner_filter not in OWNER_FILTERS:
        owner_filter = "all"
    if sort not in SORTS:
        sort = SORT_NEWEST

    out = list(rows)
    # Mine/Others needs no permission: it partitions on the viewer's OWN
    # identity, which they already know, and reveals nothing about who owns
    # the rest.
    if owner_filter == "mine":
        out = [r for r in out if r.is_mine]
    elif owner_filter == "others":
        out = [r for r in out if not r.is_mine]

    if owner_user_id is not None:
        out = [r for r in out if r.owner_user_id == owner_user_id]
    if store_id is not None:
        out = [r for r in out if any(t.store_id == store_id for t in r.visible_targets)]
    if search:
        out = [r for r in out if _matches_search(r, search, visibility)]

    # started_at can be None for a session the runtime holds before the row is
    # stamped; sort those last rather than raising on the comparison.
    out.sort(key=lambda r: (r.started_at or "", r.session_id),
             reverse=(sort == SORT_NEWEST))
    return out


def paginate(rows: list[ActiveRow], *, page: int, page_size: int) -> tuple[list[ActiveRow], int, int, int]:
    """Server-side, over the already-redacted-and-filtered set.

    ``total`` is the length of what THIS caller is allowed to know about,
    which is why paging happens after filtering rather than before: a total
    computed over all sessions would disclose the existence of broadcasts the
    caller may not see, even though none of their rows were returned.
    """
    resolved_page = max(1, int(page or 1))
    resolved_size = max(1, min(int(page_size or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    total = len(rows)
    start = (resolved_page - 1) * resolved_size
    return rows[start:start + resolved_size], total, resolved_page, resolved_size


def stop_scope_refusal(row: ActiveRow, scope: frozenset[int] | None) -> frozenset[int]:
    """Which of the session's Stores fall outside the caller's scope.

    Checked against EVERY target, not the scope-visible ones. Stop ends the
    whole session, so a supervisor scoped to two of its three Stores would
    otherwise silence a third Store they have no authority over - and would
    do it without ever seeing that Store on screen. Empty result means the
    stop is fully authorized.

    Partial stops are not offered. A broadcast that continues on some Stores
    and not others is a state no operator asked for and every listener would
    experience as a fault.
    """
    if scope is None:
        return frozenset()
    return frozenset(row.all_target_store_ids - scope)


__all__ = [
    "ActiveRow",
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "OWNERSHIP_CODE",
    "OWNER_FILTERS",
    "OwnershipVisibilityDenied",
    "PAGE_CODE",
    "SORTS",
    "SORT_NEWEST",
    "SORT_OLDEST",
    "STOP_ANY_CODE",
    "StoreTarget",
    "TARGETS_CODE",
    "TargetVisibilityDenied",
    "Visibility",
    "collect_active_rows",
    "filter_and_sort",
    "paginate",
    "resolve_visibility",
    "stop_scope_refusal",
]
