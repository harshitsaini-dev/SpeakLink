"""No production bundle may name a loopback backend.

RC4 shipped a frontend built with

    REACT_APP_BACKEND_URL = loopback, backend port

inlined by Create React App at compile time. HQ was healthy - Runtime READY, the
backend answering on 192.168.4.134:8000, 34 auto-start checks green - and signing
in was impossible, because every browser sent its login to its *own* loopback
address and got ERR_CONNECTION_REFUSED. The request never reached authentication,
and the page reported "Login failed" as though the password had been rejected.

Every automated gate passed. Nothing in the repository was looking at what was
inside the built bundle, because the bundle is generated and gitignored, and
every secret scan in this project walks tracked files. The same blind spot as the
credential archive: *not in git* is not *not in the artifact*.

These tests read the built assets when they exist and skip when they do not, so a
checkout with no build is not reported as a pass.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPOSITORY_ROOT / "frontend"
BUILD = FRONTEND / "build"
API_MODULE = FRONTEND / "src" / "lib" / "api.js"

#: What a browser actually executes. Source maps are shipped too and are checked
#: separately - a string inside a map cannot make a request, but it can make a
#: future audit unable to tell a fixed package from a broken one.
EXECUTABLE_SUFFIXES = {".js", ".css", ".html"}

LOOPBACK_BACKEND = re.compile(rb"(127\.0\.0\.1|localhost|\[::1\]):8000")


def _built_files(suffixes):
    return [p for p in BUILD.rglob("*") if p.is_file() and p.suffix in suffixes]


def _require_build():
    if not BUILD.exists() or not any(BUILD.rglob("*.js")):
        pytest.skip("no production build in frontend/build")


# ===========================================================================
# The bundle
# ===========================================================================
def test_no_executable_asset_names_a_loopback_backend():
    _require_build()
    offenders = []
    for path in _built_files(EXECUTABLE_SUFFIXES):
        for match in LOOPBACK_BACKEND.finditer(path.read_bytes()):
            offenders.append(f"{path.relative_to(BUILD)} -> {match.group().decode()}")
    assert offenders == [], (
        "a production asset names a loopback backend address. A browser on any "
        "other machine will call itself and fail with ERR_CONNECTION_REFUSED:\n  "
        + "\n  ".join(offenders)
    )


def test_no_source_map_names_a_loopback_backend():
    """Maps ship in the HQ package. A string in one cannot make a request, but a
    grep cannot tell it apart from one that still does - and the point of a guard
    is that its answer is unambiguous."""
    _require_build()
    offenders = [
        str(path.relative_to(BUILD))
        for path in _built_files({".map"})
        if LOOPBACK_BACKEND.search(path.read_bytes())
    ]
    assert offenders == [], f"a shipped source map names a loopback backend: {offenders}"


def test_no_asset_bakes_in_a_specific_lan_address():
    """The opposite mistake, and the tempting fix for the first one: replacing
    the loopback constant with today's LAN IP. That works until the HQ machine
    gets a different address, and then fails identically."""
    _require_build()
    lan = re.compile(rb"(192\.168|10\.\d+|172\.(1[6-9]|2\d|3[01]))\.\d+\.\d+")
    offenders = [
        str(path.relative_to(BUILD))
        for path in _built_files(EXECUTABLE_SUFFIXES | {".map"})
        if lan.search(path.read_bytes())
    ]
    assert offenders == [], (
        f"a production asset contains a hard-coded LAN address: {offenders}"
    )


def test_the_bundle_still_reads_the_browser_location():
    """A positive check, so "no loopback" cannot be satisfied by a bundle that
    resolves nothing at all."""
    _require_build()
    joined = b"".join(p.read_bytes() for p in _built_files({".js"}))
    assert b"location" in joined
    assert b"hostname" in joined


# ===========================================================================
# The source it is built from
# ===========================================================================
def test_the_resolver_derives_from_window_location():
    source = API_MODULE.read_text(encoding="utf-8")
    assert "window.location" in source or "currentLocation" in source
    assert "resolveBackendUrl" in source


def test_the_frontend_env_does_not_pin_a_backend_url():
    """CRA inlines every REACT_APP_* value at build time, so this file decides
    what a bundle believes for ever."""
    env = FRONTEND / ".env"
    if not env.exists():
        pytest.skip("no frontend/.env on this checkout")
    for line in env.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        assert key.strip() != "REACT_APP_BACKEND_URL" or not value.strip(), (
            "frontend/.env pins REACT_APP_BACKEND_URL, which is compiled into "
            "the bundle and cannot be corrected on the HQ machine"
        )


def test_the_package_json_does_not_pin_a_backend_url():
    package = json.loads((FRONTEND / "package.json").read_text(encoding="utf-8"))
    serialised = json.dumps(package)
    assert "REACT_APP_BACKEND_URL" not in serialised
