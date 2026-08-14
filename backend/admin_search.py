"""Server-side search, filtering and pagination for the admin screens.

WHY NEW ENDPOINTS RATHER THAN CHANGING THE EXISTING ONES

``GET /api/logs`` and ``GET /api/broadcast/history`` return bare JSON
arrays today, and the frontend, the Playwright mocks and the migration
tooling all depend on that shape. Adding pagination by changing them would
mean either a second response shape behind a flag - two contracts wearing
one name - or breaking every existing caller. So the filtered, paginated
views live at their own ``/search`` paths and the originals are untouched.

WHY SERVER-SIDE

System Logs and Broadcast History are the two tables that grow without
bound. Loading them into React to filter there is fine at 335 rows and
ruinous at 335,000, and the failure arrives silently as a slow page rather
than as an error. Every filter here narrows in SQL, and every response
carries the total match count so the UI can say "12 of 4,318" honestly.

WHAT IS DELIBERATELY NOT OFFERED

``system_logs`` gained ``actor_user_id``/``store_id``/``device_public_id``
only recently, and they are populated for NEW rows only. Filtering on them
is offered and clearly reported as covering newer logs (see
``entity_filter_coverage`` in the response) - the alternative, regexing the
free-text message of older rows into relationships, would present guesses
as facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import and_, or_

MAX_PAGE_SIZE = 200
DEFAULT_PAGE_SIZE = 50


@dataclass
class Page:
    items: list = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = DEFAULT_PAGE_SIZE
    #: Extra, screen-specific truth the UI needs in order not to mislead.
    meta: dict = field(default_factory=dict)

    def as_dict(self, serialize=lambda row: row) -> dict:
        pages = (self.total + self.page_size - 1) // self.page_size if self.page_size else 0
        return {
            "items": [serialize(row) for row in self.items],
            "total": self.total,
            "page": self.page,
            "page_size": self.page_size,
            "pages": pages,
            "has_more": self.page * self.page_size < self.total,
            **({"meta": self.meta} if self.meta else {}),
        }


def normalize_paging(page: int | None, page_size: int | None) -> tuple[int, int]:
    """Bounded on purpose. An unbounded page_size is a full-table scan with
    extra steps, and the caller is usually a URL somebody edited."""
    resolved_page = max(1, int(page or 1))
    resolved_size = int(page_size or DEFAULT_PAGE_SIZE)
    resolved_size = max(1, min(resolved_size, MAX_PAGE_SIZE))
    return resolved_page, resolved_size


def apply_paging(query, page: int, page_size: int):
    return query.offset((page - 1) * page_size).limit(page_size)


def parse_date(value: str | None, *, end_of_day: bool = False):
    """Accept a plain date or a full timestamp. Refuses nonsense rather than
    silently ignoring it - a filter that quietly does nothing is worse than
    one that says it could not be applied."""
    if not value:
        return None
    text = value.strip()
    try:
        if len(text) == 10:  # YYYY-MM-DD
            parsed = datetime.strptime(text, "%Y-%m-%d")
            if end_of_day:
                parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
            return parsed
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Could not read a date from {value!r}. Use YYYY-MM-DD.")


def like_term(search: str | None) -> str | None:
    """One escaped LIKE term. Never interpolated into SQL - the caller binds
    it as a parameter."""
    if not search:
        return None
    cleaned = search.strip()
    if not cleaned:
        return None
    # Escape the wildcards so a user typing % does not match everything.
    cleaned = cleaned.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{cleaned}%"


def text_search(columns, term: str):
    """OR across the given columns, case-insensitive."""
    return or_(*[column.ilike(term) for column in columns])


def combine(*conditions):
    real = [c for c in conditions if c is not None]
    return and_(*real) if real else None


__all__ = [
    "BULK_MODES",
    "BulkSelectionError",
    "DEFAULT_PAGE_SIZE",
    "sort_rows",
    "value_list",
    "matches_any",
    "int_list",
    "MAX_PAGE_SIZE",
    "Page",
    "apply_paging",
    "combine",
    "like_term",
    "normalize_paging",
    "parse_date",
    "resolve_bulk_selection",
    "text_search",
]


# ===========================================================================
# Filter-based bulk selection ("Select All Filtered")
# ===========================================================================
#: Two ways to name the rows a bulk action applies to.
#:
#: "ids"      - an explicit list. What Select Page sends, and what a single
#:              row action sends. Unambiguous and small.
#: "filtered" - the SAME filter object the matching /search endpoint accepts.
#:              The backend resolves it to a row set inside the caller's own
#:              authorization scope.
#:
#: The second mode exists because "Select All Filtered" must mean every
#: server-side match, including rows on pages the operator never loaded.
#: Building that id list in React would mean paging through the entire
#: result set just to enumerate it - slow, fragile, and silently wrong the
#: moment a page is missed. Sending the filter instead keeps one definition
#: of "what is selected" and evaluates it where the authorization lives.
BULK_MODES = ("ids", "filtered")


class BulkSelectionError(ValueError):
    """The selection could not be resolved. Never carries row content."""


def resolve_bulk_selection(mode, ids, filters, *, resolver):
    """Return (row_ids, matched_count).

    ``resolver`` is a callable taking the validated filter dict and
    returning the full list of matching ids, already narrowed to the
    caller's scope by whichever search query backs that screen. Bulk and
    search therefore cannot drift apart: they run the same narrowing.
    """
    selected_mode = (mode or "ids").strip().lower()
    if selected_mode not in BULK_MODES:
        raise BulkSelectionError(
            f"Unknown selection mode {selected_mode!r}. Expected one of {BULK_MODES}.")
    if selected_mode == "ids":
        row_ids = list(ids or [])
        return row_ids, len(row_ids)
    matched = resolver(filters or {})
    return list(matched), len(matched)

# ===========================================================================
# Filters that name more than one value
#
# A dropdown that admits one Store answers "how is Nehru Place doing". It
# cannot answer "how are these six shops doing", which is the question people
# actually bring - a zone with an exception in it, a handful of shops in one
# market, the three that were complaining this morning. Repeating a search six
# times and comparing six screens is not an answer, it is arithmetic done by
# the reader.
#
# So every filter accepts a comma-separated list, and one value is simply a
# list of one. That keeps every existing link, bookmark and test working
# unchanged: `?zone=NORTH` still means what it always meant.
# ===========================================================================

def value_list(raw) -> list[str]:
    """The values a filter parameter names, in order, without blanks.

    Accepts a list (from repeated query parameters), a comma-separated string,
    or a single value. Anything empty yields an empty list, which every caller
    reads as "no filter" - so an accidental `?zone=` does not silently select
    nothing.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    else:
        candidates = str(raw).split(",")
    return [str(value).strip() for value in candidates if str(value).strip()]


def matches_any(value, raw) -> bool:
    """Does this row's value match a filter that may name several?

    An empty filter matches everything, deliberately: the alternative is that
    clearing a filter hides every row, which reads as the page being broken.
    """
    wanted = value_list(raw)
    if not wanted:
        return True
    return str(value if value is not None else "") in wanted


def int_list(raw) -> list[int]:
    """The same, for ids. Values that are not numbers are dropped rather than
    raising: a filter is not a place to fail a whole page over one bad token."""
    found = []
    for value in value_list(raw):
        try:
            found.append(int(value))
        except ValueError:
            continue
    return found

# ===========================================================================
# Sorting
#
# Sorting happens on the SERVER, before pagination, and that is the whole
# point. Sorting the rows the browser happens to be holding would order one
# page of fifty and leave the other three hundred where they were - a table
# that claims to be sorted and is not, which is worse than an unsorted one
# because the reader stops checking.
# ===========================================================================

def sort_rows(rows: list, sort: str | None, direction: str | None,
              allowed: dict) -> list:
    """Order rows by a NAMED, allowed column.

    ``allowed`` maps the name a caller may send to a function that reads the
    value out of a row. An allowlist rather than getattr on whatever arrives:
    a sort parameter that can name any attribute is a way to probe what a row
    holds, and an unknown name would otherwise fail a whole page.

    An unknown or absent name leaves the order exactly as it was, which is the
    order the caller's own query already chose.
    """
    if not sort or sort not in allowed:
        return rows
    read = allowed[sort]
    descending = str(direction or "asc").lower() == "desc"

    def key(row):
        value = read(row)
        # None sorts last in both directions. It usually means "not recorded
        # yet", and burying those at the end is what somebody scanning for the
        # biggest or the smallest actually wants.
        missing = value is None or value == ""
        if isinstance(value, str):
            value = value.lower()
        return (missing, value if not missing else "")

    return sorted(rows, key=key, reverse=descending)

