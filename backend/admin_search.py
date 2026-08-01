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
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE_SIZE",
    "Page",
    "apply_paging",
    "combine",
    "like_term",
    "normalize_paging",
    "parse_date",
    "text_search",
]
