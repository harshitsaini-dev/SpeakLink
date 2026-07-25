"""Canonical Zone/Store catalog contract and idempotent seeding tests.

These tests use only pytest temporary SQLite databases. They never open,
copy, migrate or modify ``backend/echocast_live.db``, and they never create
Receiver Devices or production credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db import Base
from models import Store
from seed import seed_stores
from store_catalog import (
    CANONICAL_STORES,
    CANONICAL_ZONES,
    CATALOG_STORE_COUNT,
    CATALOG_ZONE_COUNT,
    CatalogValidationError,
    stores_for_zone,
    validate_catalog,
)


REAL_DATABASE = Path(__file__).resolve().parents[1] / "echocast_live.db"

EXPECTED_ZONES = (
    "UN ZONE",
    "PV ZONE",
    "ME ZONE",
    "RG ZONE",
    "EAST ZONE",
    "SOUTH ZONE",
    "NORTH ZONE",
    "NOIDA & GHAZIABAD",
    "NIT FARIDABAD",
)

# (zone, full name, short name) exactly as approved, in approved order.
EXPECTED_CATALOG = (
    ("UN ZONE", "Uttam Nagar Old", "UN Old"),
    ("UN ZONE", "Uttam Nagar ASR", "UN ASR"),
    ("UN ZONE", "Kiran Garden", "KG"),
    ("UN ZONE", "Mohan Garden", "MG"),
    ("UN ZONE", "Dwarka Mor", "DM"),
    ("UN ZONE", "Rajapuri", "RP"),
    ("UN ZONE", "Vikaspuri", "VP"),
    ("UN ZONE", "Vikaspuri New Store", "VP New"),
    ("UN ZONE", "RRPL New Rajapuri", "RRPL RP"),
    ("PV ZONE", "Paschim Vihar", "PV"),
    ("PV ZONE", "Meerabagh", "MB"),
    ("PV ZONE", "Jwalaheri A6", "JHA6"),
    ("PV ZONE", "Jwalaheri A6 New", "JHA6 New"),
    ("PV ZONE", "Jwalaheri B2", "JHB2"),
    ("PV ZONE", "Nangloi", "NG"),
    ("ME ZONE", "Mahavir Enclave Dashrathpuri", "ME DP"),
    ("ME ZONE", "Mahavir Enclave New", "ME New"),
    ("ME ZONE", "Bindapur", "BP"),
    ("ME ZONE", "Indrapark", "IP"),
    ("ME ZONE", "Palam", "PC"),
    ("ME ZONE", "Nangal Raya", "NR"),
    ("RG ZONE", "Rajouri Garden", "RG"),
    ("RG ZONE", "Rajouri Garden New", "RG New"),
    ("RG ZONE", "Janakpuri", "JP"),
    ("RG ZONE", "Vishnu Garden", "VG"),
    ("RG ZONE", "Vishnu Garden 2", "VG2"),
    ("RG ZONE", "Ganesh Nagar", "GN"),
    ("RG ZONE", "Tilak Nagar", "TN"),
    ("RG ZONE", "Fateh Nagar", "FN"),
    ("EAST ZONE", "Krishna Nagar", "KN"),
    ("EAST ZONE", "Preet Vihar", "PRT"),
    ("EAST ZONE", "Shakarpur", "SP"),
    ("EAST ZONE", "Krishna Nagar 2", "KN2"),
    ("SOUTH ZONE", "Malviya Nagar", "MN"),
    ("SOUTH ZONE", "Kalkaji", "KJ"),
    ("SOUTH ZONE", "Khirki Extension", "KE"),
    ("SOUTH ZONE", "Bhogal", "CR"),
    ("SOUTH ZONE", "Taimoor Nagar", "TNS"),
    ("SOUTH ZONE", "Devli", "DEVLI"),
    ("NORTH ZONE", "Budh Vihar BV2", "BV2"),
    ("NORTH ZONE", "Burari", "BU"),
    ("NOIDA & GHAZIABAD", "Noida Sector 104", "Noida"),
    ("NOIDA & GHAZIABAD", "Ghaziabad Dundahera", "GZB"),
    ("NIT FARIDABAD", "NIT 1 Faridabad", "NIT1"),
)

# Confirmed runtime demo entries retired by this change.
RETIRED_DEMO_STORE_CODES = (
    "MUM-001", "MUM-002", "PUN-001", "DEL-001", "DEL-002", "GUR-001",
    "BLR-001", "BLR-002", "HYD-001", "CHN-001", "KOL-001", "ONL-001", "ONL-002",
)
RETIRED_DEMO_STORE_NAMES = (
    "Mumbai Andheri Flagship", "Mumbai Bandra Outlet", "Pune Koregaon Park",
    "Delhi Connaught Place", "Delhi Saket Mall", "Gurgaon Cyber Hub",
    "Bangalore MG Road", "Bangalore Whitefield", "Hyderabad Banjara Hills",
    "Chennai T. Nagar", "Kolkata Park Street", "Online Store - Web",
    "Online Store - App",
)


def _database_metadata(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


@pytest.fixture()
def temp_session(tmp_path):
    """A pytest temporary file-backed SQLite session; never the real database."""
    database_path = tmp_path / "catalog_test.db"
    engine = create_engine(
        f"sqlite:///{database_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# Pure catalog contract
# ---------------------------------------------------------------------------
def test_catalog_has_exactly_nine_zones():
    assert CATALOG_ZONE_COUNT == 9
    assert len(CANONICAL_ZONES) == 9
    assert len(set(CANONICAL_ZONES)) == 9


def test_catalog_has_exactly_forty_four_stores():
    assert CATALOG_STORE_COUNT == 44
    assert len(CANONICAL_STORES) == 44


def test_zone_names_match_approved_source_exactly():
    assert tuple(CANONICAL_ZONES) == EXPECTED_ZONES


def test_store_full_and_short_names_and_zone_membership_match_exactly():
    actual = tuple(
        (entry.zone, entry.full_name, entry.short_name) for entry in CANONICAL_STORES
    )
    assert actual == EXPECTED_CATALOG


def test_every_store_belongs_to_exactly_one_zone():
    for entry in CANONICAL_STORES:
        assert entry.zone in CANONICAL_ZONES
    zone_totals = sum(len(stores_for_zone(zone)) for zone in CANONICAL_ZONES)
    assert zone_totals == CATALOG_STORE_COUNT


def test_store_ordering_within_each_zone_is_stable_and_approved():
    for zone in CANONICAL_ZONES:
        expected = tuple(
            full_name for entry_zone, full_name, _ in EXPECTED_CATALOG if entry_zone == zone
        )
        actual = tuple(entry.full_name for entry in stores_for_zone(zone))
        assert actual == expected
    # Zones themselves appear in approved order within the flat catalog.
    seen: list[str] = []
    for entry in CANONICAL_STORES:
        if entry.zone not in seen:
            seen.append(entry.zone)
    assert tuple(seen) == EXPECTED_ZONES


def test_no_duplicate_full_names():
    full_names = [entry.full_name for entry in CANONICAL_STORES]
    assert len(set(full_names)) == len(full_names)


def test_no_duplicate_short_names():
    short_names = [entry.short_name for entry in CANONICAL_STORES]
    assert len(set(short_names)) == len(short_names)


def test_no_blank_or_untrimmed_catalog_values():
    for zone in CANONICAL_ZONES:
        assert zone and zone == zone.strip()
    for entry in CANONICAL_STORES:
        for value in (entry.zone, entry.full_name, entry.short_name):
            assert value, "catalog values must not be blank"
            assert value == value.strip(), "catalog values must not have stray whitespace"


def test_unusual_short_names_are_preserved_verbatim():
    by_full_name = {entry.full_name: entry.short_name for entry in CANONICAL_STORES}
    assert by_full_name["Bhogal"] == "CR"
    assert by_full_name["Taimoor Nagar"] == "TNS"
    assert by_full_name["Noida Sector 104"] == "Noida"
    assert by_full_name["Devli"] == "DEVLI"
    assert by_full_name["Krishna Nagar 2"] == "KN2"
    assert by_full_name["NIT 1 Faridabad"] == "NIT1"


def test_validate_catalog_accepts_the_approved_catalog():
    validate_catalog()


def test_validate_catalog_rejects_a_duplicate_short_name():
    broken = CANONICAL_STORES + (CANONICAL_STORES[0],)
    with pytest.raises(CatalogValidationError):
        validate_catalog(broken)


def test_catalog_contains_no_credential_or_token_material():
    for entry in CANONICAL_STORES:
        combined = f"{entry.zone}{entry.full_name}{entry.short_name}".lower()
        for marker in ("token", "secret", "password", "bearer", "echocast_rcv", "hmac"):
            assert marker not in combined
    assert not hasattr(CANONICAL_STORES[0], "receiver_token")


# ---------------------------------------------------------------------------
# Seeding behaviour on isolated temporary databases
# ---------------------------------------------------------------------------
def test_seeding_empty_database_creates_the_canonical_catalog(temp_session):
    before = _database_metadata(REAL_DATABASE)

    seed_stores(temp_session)

    stores = temp_session.query(Store).order_by(Store.id).all()
    assert len(stores) == CATALOG_STORE_COUNT
    assert [s.store_code for s in stores] == [e.short_name for e in CANONICAL_STORES]
    assert [s.store_name for s in stores] == [e.full_name for e in CANONICAL_STORES]
    assert [s.region for s in stores] == [e.zone for e in CANONICAL_STORES]
    assert _database_metadata(REAL_DATABASE) == before


def test_seeded_zone_is_stored_in_the_indexed_region_field(temp_session):
    seed_stores(temp_session)
    zones = {s.region for s in temp_session.query(Store).all()}
    assert zones == set(EXPECTED_ZONES)


def test_seeded_stores_are_active_physical_stores_with_unique_tokens(temp_session):
    seed_stores(temp_session)
    stores = temp_session.query(Store).all()
    assert all(s.is_active for s in stores)
    assert all(s.is_online_store is False for s in stores)
    assert all(s.status == "offline" for s in stores)
    tokens = [s.receiver_token for s in stores]
    assert len(set(tokens)) == len(tokens)


def test_second_seed_is_idempotent_and_creates_no_duplicates(temp_session):
    seed_stores(temp_session)
    first = {s.id: s.store_code for s in temp_session.query(Store).all()}
    first_tokens = {s.store_code: s.receiver_token for s in temp_session.query(Store).all()}

    seed_stores(temp_session)

    stores = temp_session.query(Store).all()
    assert len(stores) == CATALOG_STORE_COUNT
    assert {s.id: s.store_code for s in stores} == first
    # Re-seeding must never rotate an existing Store's receiver credential.
    assert {s.store_code: s.receiver_token for s in stores} == first_tokens


def test_retired_demo_catalog_is_absent_after_seeding(temp_session):
    seed_stores(temp_session)
    codes = {s.store_code for s in temp_session.query(Store).all()}
    names = {s.store_name for s in temp_session.query(Store).all()}
    for code in RETIRED_DEMO_STORE_CODES:
        assert code not in codes
    for name in RETIRED_DEMO_STORE_NAMES:
        assert name not in names


def test_seeding_never_deletes_or_disturbs_a_pre_existing_fleet(temp_session):
    """Seeding is a first-run bootstrap, never a startup reconciler.

    A populated Store table is left byte-for-byte alone, so an existing
    deployment can never have its fleet mutated, or a ``receiver_token``
    rotated, merely by restarting the backend.
    """
    legacy = Store(
        store_code="LEGACY-KEEP-1",
        store_name="Pre-existing Store With History",
        city="Legacy City",
        region="Legacy Region",
        is_online_store=False,
        receiver_token="legacy-token-placeholder-not-a-real-credential",
    )
    temp_session.add(legacy)
    temp_session.commit()
    legacy_id = legacy.id

    seed_stores(temp_session)

    survivor = temp_session.query(Store).filter(Store.id == legacy_id).one()
    assert survivor.store_code == "LEGACY-KEEP-1"
    assert survivor.store_name == "Pre-existing Store With History"
    assert survivor.region == "Legacy Region"
    assert survivor.receiver_token == "legacy-token-placeholder-not-a-real-credential"
    # No canonical rows are injected into an already-populated fleet.
    assert temp_session.query(Store).count() == 1


def test_seeding_a_partially_populated_catalog_makes_no_changes(temp_session):
    """Reconciling a partly-populated catalog is a separate reviewed task."""
    first = CANONICAL_STORES[0]
    temp_session.add(
        Store(
            store_code=first.short_name,
            store_name=first.full_name,
            city=first.zone,
            region=first.zone,
            is_online_store=False,
            receiver_token="existing-token-placeholder-not-a-real-credential",
        )
    )
    temp_session.commit()

    seed_stores(temp_session)

    stores = temp_session.query(Store).all()
    assert len(stores) == 1
    kept = stores[0]
    assert kept.store_code == first.short_name
    assert kept.receiver_token == "existing-token-placeholder-not-a-real-credential"


def test_seeding_creates_no_receiver_device_or_credential_rows(temp_session):
    seed_stores(temp_session)
    tables = set(Base.metadata.tables)
    # Store and Receiver Device stay separate entities; catalog seeding is
    # Store-only and must not introduce credential lifecycle rows.
    assert "receiver_devices" not in tables
    assert "receiver_credentials" not in tables


def test_seeding_does_not_touch_the_protected_real_database(temp_session):
    before = _database_metadata(REAL_DATABASE)
    seed_stores(temp_session)
    seed_stores(temp_session)
    assert _database_metadata(REAL_DATABASE) == before
