"""Every shipped SpeakLink executable carries the SpeakLink icon.

WHY THIS TEST EXISTS

PyInstaller's default icon is a Python logo. Shipping it means a Store PC
desktop, a taskbar and a Start Menu entry that do not say SpeakLink - and
nothing in a build fails when that happens, because a default is not an
error. So the check has to be a test, and it has to walk the specs rather
than trust that somebody remembered.

WHAT IT DOES NOT CLAIM

Nothing here says anything about what Windows Explorer DRAWS. Shell icon
caching is a separate mechanism this project must not touch on a developer's
machine, so these tests verify the icon is configured and - where an
artifact exists - that the resource is really in the file.
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: The specs whose executables are actually shipped to an HQ or a Store.
SHIPPED_SPECS = ("hq_runtime.spec", "receiver_agent.spec", "store_setup.spec")

CANONICAL_ICON = REPOSITORY_ROOT / "assets" / "speaklink.ico"
WEBSITE_FAVICON = REPOSITORY_ROOT / "frontend" / "public" / "favicon.ico"


def _ico_entries(path: Path):
    raw = path.read_bytes()
    _reserved, kind, count = struct.unpack("<HHH", raw[:6])
    assert kind == 1, f"{path} is not an icon file"
    out = []
    for index in range(count):
        offset = 6 + index * 16
        width, height, _c, _r, _p, bpp, size, data = struct.unpack(
            "<BBBBHHII", raw[offset:offset + 16])
        out.append((width or 256, height or 256, bpp, raw[data:data + size]))
    return out


# ===========================================================================
# The asset
# ===========================================================================
def test_the_canonical_icon_exists():
    assert CANONICAL_ICON.exists(), (
        "assets/speaklink.ico is missing - run tools/build_windows_icon.py")


def test_the_icon_is_derived_from_the_website_favicon():
    """Same artwork, not a lookalike. The website entries must appear in the
    Windows icon byte-for-byte, which is what 'derived' has to mean if the
    desktop and the browser tab are to stay in step."""
    website = {(w, h): payload for w, h, _bpp, payload in _ico_entries(WEBSITE_FAVICON)}
    windows = {(w, h): payload for w, h, _bpp, payload in _ico_entries(CANONICAL_ICON)}

    assert website, "the website favicon has no images"
    for size, payload in website.items():
        assert size in windows, f"the Windows icon lost the {size} entry"
        assert windows[size] == payload, (
            f"the {size} entry was re-encoded rather than reused")


def test_the_icon_covers_small_and_large_views():
    sizes = sorted(width for width, _h, _bpp, _payload in _ico_entries(CANONICAL_ICON))
    assert 16 in sizes, "no 16x16 entry for small icon views"
    assert 32 in sizes or 24 in sizes, "no 24/32 entry"
    assert 48 in sizes, "no 48x48 entry for medium views"
    assert max(sizes) >= 128, (
        f"largest entry is {max(sizes)}; Explorer's large views need at least 128")


# ===========================================================================
# The specs
# ===========================================================================
@pytest.mark.parametrize("spec_name", SHIPPED_SPECS)
def test_every_shipped_spec_sets_the_icon(spec_name):
    source = (REPOSITORY_ROOT / spec_name).read_text(encoding="utf-8")
    assert "icon=ICON" in source, (
        f"{spec_name} does not set an icon, so its executable would ship with "
        "PyInstaller's default Python icon")


@pytest.mark.parametrize("spec_name", SHIPPED_SPECS)
def test_every_shipped_spec_defines_the_icon_constant(spec_name):
    source = (REPOSITORY_ROOT / spec_name).read_text(encoding="utf-8")
    assert re.search(r"^ICON = ", source, re.M), (
        f"{spec_name} uses ICON without defining it - the build would fail")


@pytest.mark.parametrize("spec_name", SHIPPED_SPECS)
def test_no_spec_hard_codes_a_developer_path(spec_name):
    """A path under one person's home directory builds on one machine."""
    source = (REPOSITORY_ROOT / spec_name).read_text(encoding="utf-8")
    for forbidden in ("C:\\Users\\", "C:/Users/", "/home/", "Desktop\\SpeakLink"):
        assert forbidden not in source, f"{spec_name} hard-codes {forbidden}"


@pytest.mark.parametrize("spec_name", SHIPPED_SPECS)
def test_every_shipped_spec_points_at_the_one_canonical_icon(spec_name):
    """One source of truth. Per-spec icons drift, and the drift is invisible
    until somebody notices two SpeakLink windows with different logos."""
    source = (REPOSITORY_ROOT / spec_name).read_text(encoding="utf-8")
    assert 'assets" / "speaklink.ico"' in source or "assets/speaklink.ico" in source, (
        f"{spec_name} does not use assets/speaklink.ico")


# ===========================================================================
# Built artifacts, when they exist
# ===========================================================================
def _built_executables():
    """Any isolated build output present. Never the installed live binaries."""
    found = []
    for root in (REPOSITORY_ROOT / "dist" / "branding",):
        if root.exists():
            found.extend(sorted(root.rglob("*.exe")))
    return found


@pytest.mark.skipif(not _built_executables(),
                    reason="no isolated branding build present")
def test_built_executables_carry_an_icon_resource():
    """RT_GROUP_ICON must be present in the PE resources.

    Read from the file rather than asked of the Shell: this says the resource
    is in the executable, and deliberately says nothing about what Explorer
    draws, which depends on a cache this project must not clear for somebody.
    """
    for executable in _built_executables():
        raw = executable.read_bytes()
        # The icon payloads themselves are the surest marker available without
        # a PE parser: every entry of the canonical icon should appear.
        entries = _ico_entries(CANONICAL_ICON)
        biggest = max(entries, key=lambda item: len(item[3]))[3]
        assert biggest in raw, (
            f"{executable.name} does not contain the SpeakLink icon image")
