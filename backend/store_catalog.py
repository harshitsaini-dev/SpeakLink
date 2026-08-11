"""Canonical SpeakLink Zone and Store catalog.

This module is the single source of truth for the approved retail catalog:
9 Zones and 44 Stores. The React dashboard must obtain this data through the
existing Store API rather than keeping its own copy.

Mapping onto the existing ``Store`` model (no schema change is required):

- ``Store.store_code``  <- canonical short name (already unique and indexed)
- ``Store.store_name``  <- canonical full name
- ``Store.region``      <- canonical Zone display name (already indexed, and
  already used by the ``region`` broadcast target mode)
- ``Store.city``        <- canonical Zone display name; the approved source
  supplies no separate city data, so no city value is invented here

This module contains catalog identity only. It holds no Receiver credential,
token, key or other secret, and it does not describe Receiver Devices, which
remain a separate entity from a Store.
"""

from __future__ import annotations

from dataclasses import dataclass


class CatalogValidationError(ValueError):
    """Raised when a catalog definition breaks an approved invariant."""


@dataclass(frozen=True, slots=True)
class CanonicalStore:
    """One approved Store entry. Display values are stored verbatim."""

    zone: str
    full_name: str
    short_name: str


# Approved Zone display order.
CANONICAL_ZONES: tuple[str, ...] = (
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

# Approved Store order within each Zone. Short names are intentionally kept
# exactly as supplied, including the ones that do not follow an obvious
# pattern (Bhogal -> RMCR, Mahavir Enclave Dashrathpuri -> RMME,
# Taimoor Nagar -> TNS, Noida Sector 104 -> NS104, Devli -> DEVLI,
# Krishna Nagar 2 -> KN2, NIT Faridabad -> NIT, RRPL -> RRPL).
CANONICAL_STORES: tuple[CanonicalStore, ...] = (
    # ZONE 1 - UN ZONE
    CanonicalStore("UN ZONE", "Uttam Nagar Old", "UN"),
    CanonicalStore("UN ZONE", "Uttam Nagar ASR", "ASR"),
    CanonicalStore("UN ZONE", "Kiran Garden", "KG"),
    CanonicalStore("UN ZONE", "Mohan Garden", "MG"),
    CanonicalStore("UN ZONE", "Dwarka Mor", "DM"),
    CanonicalStore("UN ZONE", "Rajapuri", "RP"),
    CanonicalStore("UN ZONE", "Vikaspuri", "VP"),
    CanonicalStore("UN ZONE", "Vikaspuri New", "VP2"),
    CanonicalStore("UN ZONE", "RRPL", "RRPL"),
    # ZONE 2 - PV ZONE
    CanonicalStore("PV ZONE", "Paschim Vihar", "PV"),
    CanonicalStore("PV ZONE", "Meerabagh", "MB"),
    CanonicalStore("PV ZONE", "Jwalaheri A6", "JHA"),
    CanonicalStore("PV ZONE", "Jwalaheri A6 New", "JHA2"),
    CanonicalStore("PV ZONE", "Jwalaheri B2", "JHB2"),
    CanonicalStore("PV ZONE", "Nangloi", "NG"),
    # ZONE 3 - ME ZONE
    CanonicalStore("ME ZONE", "Mahavir Enclave Dashrathpuri", "RMME"),
    CanonicalStore("ME ZONE", "Mahavir Enclave New", "ME3"),
    CanonicalStore("ME ZONE", "Bindapur", "BP"),
    CanonicalStore("ME ZONE", "Indrapark", "IP"),
    CanonicalStore("ME ZONE", "Palam", "PC"),
    CanonicalStore("ME ZONE", "Nangal Raya", "NR"),
    # ZONE 4 - RG ZONE
    CanonicalStore("RG ZONE", "Rajouri Garden", "RG"),
    CanonicalStore("RG ZONE", "Rajouri Garden New", "RG2"),
    CanonicalStore("RG ZONE", "Janakpuri", "JP"),
    CanonicalStore("RG ZONE", "Vishnu Garden", "VG"),
    CanonicalStore("RG ZONE", "Vishnu Garden New", "VG2"),
    CanonicalStore("RG ZONE", "Ganesh Nagar", "GN"),
    CanonicalStore("RG ZONE", "Tilak Nagar", "TN"),
    CanonicalStore("RG ZONE", "Fateh Nagar", "FN"),
    # ZONE 5 - EAST ZONE
    CanonicalStore("EAST ZONE", "Krishna Nagar", "KN"),
    CanonicalStore("EAST ZONE", "Preet Vihar", "PRT"),
    CanonicalStore("EAST ZONE", "Shakarpur", "SP"),
    CanonicalStore("EAST ZONE", "Krishna Nagar 2", "KN2"),
    # ZONE 6 - SOUTH ZONE
    CanonicalStore("SOUTH ZONE", "Malviya Nagar", "MN"),
    CanonicalStore("SOUTH ZONE", "Kalkaji", "KJ"),
    CanonicalStore("SOUTH ZONE", "Khirki Extension", "KE"),
    CanonicalStore("SOUTH ZONE", "Bhogal", "RMCR"),
    CanonicalStore("SOUTH ZONE", "Taimoor Nagar", "TNS"),
    CanonicalStore("SOUTH ZONE", "Devli", "DEVLI"),
    # ZONE 7 - NORTH ZONE
    CanonicalStore("NORTH ZONE", "Budh Vihar BV2", "BV2"),
    CanonicalStore("NORTH ZONE", "Burari", "BU"),
    # ZONE 8 - NOIDA & GHAZIABAD
    CanonicalStore("NOIDA & GHAZIABAD", "Noida Sector 104", "NS104"),
    CanonicalStore("NOIDA & GHAZIABAD", "Ghaziabad Dundahera", "GZBD"),
    # ZONE 9 - NIT FARIDABAD
    CanonicalStore("NIT FARIDABAD", "NIT Faridabad", "NIT"),
)

CATALOG_ZONE_COUNT = 9
CATALOG_STORE_COUNT = 44

# Column limits enforced by ``models.Store``.
_MAX_SHORT_NAME_LENGTH = 50
_MAX_FULL_NAME_LENGTH = 200
_MAX_ZONE_NAME_LENGTH = 100


def stores_for_zone(zone: str) -> tuple[CanonicalStore, ...]:
    """Return the approved Stores of one Zone, in approved display order."""
    return tuple(entry for entry in CANONICAL_STORES if entry.zone == zone)


def _require_clean(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise CatalogValidationError(f"{label} must be text")
    if not value:
        raise CatalogValidationError(f"{label} must not be blank")
    if value != value.strip():
        raise CatalogValidationError(f"{label} must not have leading/trailing whitespace")
    if len(value) > maximum:
        raise CatalogValidationError(f"{label} exceeds {maximum} characters")
    return value


def validate_catalog(
    entries: tuple[CanonicalStore, ...] | None = None,
    zones: tuple[str, ...] | None = None,
) -> None:
    """Validate the approved catalog invariants; raise on any violation.

    Called with no arguments this validates the module catalog. Explicit
    arguments exist so tests can prove the validation actually rejects a
    broken catalog.
    """
    catalog = CANONICAL_STORES if entries is None else entries
    zone_list = CANONICAL_ZONES if zones is None else zones

    for zone in zone_list:
        _require_clean(zone, "zone name", _MAX_ZONE_NAME_LENGTH)
    if len(set(zone_list)) != len(zone_list):
        raise CatalogValidationError("zone names must be unique")

    full_names: list[str] = []
    short_names: list[str] = []
    for entry in catalog:
        if not isinstance(entry, CanonicalStore):
            raise CatalogValidationError("catalog entries must be CanonicalStore values")
        _require_clean(entry.zone, "store zone", _MAX_ZONE_NAME_LENGTH)
        _require_clean(entry.full_name, "store full name", _MAX_FULL_NAME_LENGTH)
        _require_clean(entry.short_name, "store short name", _MAX_SHORT_NAME_LENGTH)
        if entry.zone not in zone_list:
            raise CatalogValidationError("every Store must belong to an approved Zone")
        full_names.append(entry.full_name)
        short_names.append(entry.short_name)

    if len(set(full_names)) != len(full_names):
        raise CatalogValidationError("Store full names must be unique")
    if len(set(short_names)) != len(short_names):
        raise CatalogValidationError("Store short names must be unique")

    if entries is None and zones is None:
        if len(zone_list) != CATALOG_ZONE_COUNT:
            raise CatalogValidationError("the approved catalog must contain 9 Zones")
        if len(catalog) != CATALOG_STORE_COUNT:
            raise CatalogValidationError("the approved catalog must contain 44 Stores")


# Fail fast at import time if the checked-in catalog is ever edited unsafely.
validate_catalog()
