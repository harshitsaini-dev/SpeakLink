"""Seed the default admin user and the canonical Zone/Store catalog.

The Store catalog is defined once in ``store_catalog`` and is the single
source of truth. The React dashboard reads it through the existing Store API
instead of keeping its own copy.
"""
import os
import uuid
from sqlalchemy.orm import Session
from models import HQUser, Store
from auth import hash_password, verify_password
from store_catalog import CANONICAL_STORES, validate_catalog


def seed_admin(db: Session):
    username = os.environ.get("ADMIN_USERNAME", "admin")
    password = os.environ.get("ADMIN_PASSWORD", "admin123")
    existing = db.query(HQUser).filter(HQUser.username == username).first()
    if existing is None:
        db.add(HQUser(username=username, password_hash=hash_password(password), role="admin"))
        db.commit()
    else:
        # idempotent: keep hash aligned with env password
        if not verify_password(password, existing.password_hash):
            existing.password_hash = hash_password(password)
            db.commit()


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
