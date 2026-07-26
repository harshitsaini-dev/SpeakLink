"""Single-use, short-lived tickets for authenticating a browser WebSocket.

A browser cannot set an Authorization header on a WebSocket handshake, so the
credential has to travel in the URL. A URL is the least private part of a
request: Uvicorn writes it to the access log in full, and proxies, crash
reports and browser history keep copies. Putting a reusable JWT there means
anyone who can read one log line can replay the session.

So the thing in the URL is made worthless instead. A ticket is opaque random
bytes, carries no claims about who asked for it, expires in seconds, and can be
redeemed exactly once - by the handshake it was minted for.

The store is in memory. That is correct here rather than a shortcut: SpeakLink
runs exactly one Uvicorn worker by design, because Receiver connection state is
already process-local. A ticket surviving a restart would be a bug, not a
feature.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone


# Long enough for a page to fetch a ticket and open a socket over loopback or a
# slow link; far too short to be worth harvesting from a log.
TICKET_TTL_SECONDS = 20

# 32 bytes of urandom, URL-safe. Not guessable, and no dots, so it can never be
# mistaken for - or parsed as - a JWT.
TICKET_BYTES = 32


class TicketRejected(Exception):
    """The ticket was unknown, already used, or past its window.

    One exception for all three: a caller must not be able to tell a replayed
    ticket from an invented one.
    """


class WebSocketTicketStore:
    """Mint and redeem handshake tickets. Not thread-safe by design - one
    worker, one event loop, and every method is synchronous and brief."""

    def __init__(self, ttl_seconds: int = TICKET_TTL_SECONDS) -> None:
        self._ttl = timedelta(seconds=ttl_seconds)
        self._tickets: dict[str, tuple[str, datetime]] = {}

    def issue(self, user_id: str, now: datetime | None = None) -> str:
        """Mint a ticket for one upcoming handshake."""
        moment = now or datetime.now(timezone.utc)
        # Purge on write: the store never grows past the handful of tickets
        # issued inside one TTL window, without needing a background task.
        self._purge(moment)
        ticket = secrets.token_urlsafe(TICKET_BYTES)
        self._tickets[ticket] = (str(user_id), moment + self._ttl)
        return ticket

    def redeem(self, ticket: str | None, now: datetime | None = None) -> str:
        """Consume a ticket and return the user it was issued to."""
        moment = now or datetime.now(timezone.utc)
        if not ticket or not isinstance(ticket, str):
            raise TicketRejected("no ticket was presented")

        # Pop first: a ticket is spent by the attempt, so a replay of a valid
        # ticket that arrives late still cannot be reused.
        entry = self._tickets.pop(ticket, None)
        if entry is None:
            raise TicketRejected("the ticket is unknown or has already been used")

        user_id, expires_at = entry
        if moment >= expires_at:
            raise TicketRejected("the ticket has expired")
        return user_id

    def outstanding(self, now: datetime | None = None) -> int:
        """How many unexpired tickets are held. Used by tests and diagnostics."""
        self._purge(now or datetime.now(timezone.utc))
        return len(self._tickets)

    def _purge(self, moment: datetime) -> None:
        expired = [key for key, (_, expires_at) in self._tickets.items() if moment >= expires_at]
        for key in expired:
            del self._tickets[key]
