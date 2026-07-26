"""Seed the default admin user and the canonical Zone/Store catalog.

The Store catalog is defined once in ``store_catalog`` and is the single
source of truth. The React dashboard reads it through the existing Store API
instead of keeping its own copy.
"""
import uuid
from sqlalchemy.orm import Session
from models import Store
from admin_bootstrap import bootstrap_administrator, resolve_bootstrap_credentials
from store_catalog import CANONICAL_STORES, validate_catalog


def seed_admin(db: Session):
    """Bootstrap the first administrator, fail-closed.

    This used to fall back to a known username and password when the
    environment was unset, and to re-align the stored hash whenever the
    environment disagreed with the database - so an unset ADMIN_PASSWORD reset
    the administrator's password back to a known value on every restart.

    Both behaviours are gone. See ``admin_bootstrap``: missing or blank
    credentials raise before anything is written, and an administrator that
    already exists is never modified by startup. Rotating a password is a
    deliberate administrative action, not a side effect of booting.
    """
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
