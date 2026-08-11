"""Seed the default admin user and the canonical Zone/Store catalog.

The Store catalog is defined once in ``store_catalog`` and is the single
source of truth. The React dashboard reads it through the existing Store API
instead of keeping its own copy.
"""
import logging
import uuid
from sqlalchemy.orm import Session
from models import HQUser, Store
from admin_bootstrap import (
    ALREADY_PRESENT,
    NO_ENABLED_ADMINISTRATOR,
    bootstrap_administrator,
    count_enabled_administrators,
    resolve_bootstrap_credentials,
)
from store_catalog import CANONICAL_STORES, validate_catalog

logger = logging.getLogger("speaklink.seed")


def seed_admin(db: Session):
    """Bootstrap the first administrator, fail-closed - once, not on every boot.

    This used to fall back to a known username and password when the
    environment was unset, and to re-align the stored hash whenever the
    environment disagreed with the database - so an unset ADMIN_PASSWORD reset
    the administrator's password back to a known value on every restart.

    Both behaviours are gone. See ``admin_bootstrap``: missing or blank
    credentials raise before anything is written, and an administrator that
    already exists is never modified by startup. Rotating a password is a
    deliberate administrative action, not a side effect of booting.

    THE DATABASE IS CONSULTED FIRST, AND THAT ORDER IS THE POINT. This function
    used to be two lines:

        credentials = resolve_bootstrap_credentials()   # raises when unset
        return bootstrap_administrator(db, credentials) # ALREADY_PRESENT

    ``bootstrap_administrator`` was idempotent and thoroughly tested - but it was
    never reached, because resolving credentials came first and refused. So a
    plaintext ADMIN_PASSWORD became a precondition of every boot for ever, long
    after the account it describes was created. The installed HQ failed its first
    real start on exactly that, against a database that already held an enabled
    administrator.

    Three outcomes, and only the third reads the environment:

    * an enabled administrator exists -> nothing is read and nothing is written;
    * rows exist but none is an enabled administrator -> reported, and still
      nothing is read or written (see below);
    * the table is empty -> explicit credentials, or a refusal.
    """
    # Raises AdminStateUnavailable if this cannot be established. An unreadable
    # database must never be treated as an empty one, because "empty" is the
    # answer that goes on to create an account.
    if count_enabled_administrators(db) > 0:
        return ALREADY_PRESENT

    # Rows, but no enabled administrator. Neither creating nor refusing, and both
    # alternatives are worse:
    #
    # * Creating one would be startup performing an administrative act. An
    #   operator who deliberately disabled an account would find that a restart
    #   had quietly granted a new one - the class of behaviour this whole module
    #   was written to remove. ``bootstrap_administrator`` has always gated
    #   creation on "does any HQ user exist" for a documented reason, and that
    #   gate is unchanged.
    # * Refusing to start would take the Receivers off the air over an HQ
    #   sign-in problem. A Store plays announcements without anybody signed in.
    #
    # It is also not reachable through supported operations: the lifecycle rules
    # refuse to disable, archive or demote the last active privileged account.
    # So this is logged loudly and left for a human.
    if db.query(HQUser).first() is not None:
        logger.warning(
            "This database holds HQ accounts but none of them is an enabled "
            "administrator. Startup is continuing and has created nothing - "
            "Receivers are unaffected - but nobody can sign in to HQ until an "
            "administrator is re-enabled. This state cannot be produced through "
            "HQ itself."
        )
        return NO_ENABLED_ADMINISTRATOR

    credentials = resolve_bootstrap_credentials()
    return bootstrap_administrator(db, credentials)


def seed_stores(db: Session):
    """Bootstrap the canonical catalog into an empty Store table only.

    This is deliberately a first-run bootstrap, not a reconciler. If the Store
    table already holds any row, nothing is inserted, updated or deleted, so
    startup can never mutate an existing fleet, rotate a ``receiver_token``, or
    disturb Receiver Devices, Broadcast Targets, Events or history.

    Reconciling an already-populated database with the approved catalog
    (including retiring superseded demo Stores) is intentionally out of scope
    here: it requires a verified backup, a dry run and explicit execution
    approval against a real database.
    """
    validate_catalog()

    if db.query(Store).count() > 0:
        return

    for entry in CANONICAL_STORES:
        db.add(Store(
            store_code=entry.short_name,
            store_name=entry.full_name,
            # The approved catalog supplies no separate city data, so the Zone
            # display name is used rather than inventing a city value.
            city=entry.zone,
            region=entry.zone,
            is_online_store=False,
            receiver_token=uuid.uuid4().hex,
        ))
    db.commit()
