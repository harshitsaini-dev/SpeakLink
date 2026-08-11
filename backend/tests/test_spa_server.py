"""Typing the login URL must not 404.

The dashboard loaded fine from the root, and http://<hq>:3000/login returned 404
from a server that was working perfectly - because `python -m http.server` maps a
URL to a file and a React route is not a file.

The opposite mistake is worse and is what most of these tests guard: falling back
to index.html for EVERYTHING. A missing main.abc123.js would then return HTML
where the browser asked for JavaScript, and the real fault - a file absent from
the deployment - would surface as a syntax error inside a page that looks fine.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_ROOT):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

os.environ.setdefault(
    "SPEAKLINK_DB_PATH",
    str(Path(tempfile.gettempdir()) / "speaklink-tests-default-engine.db"),
)

from tools.spa_server import looks_like_an_asset, serve  # noqa: E402


INDEX_BODY = "<!doctype html><title>SpeakLink</title><div id=root></div>"


@pytest.fixture()
def site(tmp_path):
    (tmp_path / "index.html").write_text(INDEX_BODY, encoding="utf-8")
    static = tmp_path / "static" / "js"
    static.mkdir(parents=True)
    (static / "main.abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "favicon.ico").write_bytes(b"\x00")
    return tmp_path


@pytest.fixture()
def server(site):
    httpd = serve(site, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    host, port = httpd.server_address[0], httpd.server_address[1]
    try:
        yield f"http://{host}:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def get(base: str, path: str):
    try:
        with urllib.request.urlopen(base + path, timeout=10) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", "replace")


# ===========================================================================
# Routes are served the app
# ===========================================================================
@pytest.mark.parametrize("route", ["/login", "/console", "/stores", "/broadcast-history",
                                   "/receiver-devices", "/stores/14", "/console/"])
def test_a_client_side_route_serves_the_app(server, route):
    status, body = get(server, route)
    assert status == 200, f"{route} returned {status}; this is the defect"
    assert "SpeakLink" in body


def test_the_root_still_serves_the_app(server):
    status, body = get(server, "/")
    assert status == 200
    assert "SpeakLink" in body


def test_a_route_with_a_query_string_still_serves_the_app(server):
    status, body = get(server, "/login?next=/console")
    assert status == 200
    assert "SpeakLink" in body


# ===========================================================================
# Assets are NOT rewritten - the important half
# ===========================================================================
def test_a_real_asset_is_served_as_itself(server):
    status, body = get(server, "/static/js/main.abc123.js")
    assert status == 200
    assert "console.log" in body
    assert "SpeakLink" not in body


def test_a_missing_javascript_file_is_a_genuine_404(server):
    """The one that matters. Returning index.html here would hand the browser
    HTML where it asked for JavaScript and hide a broken deployment."""
    status, body = get(server, "/static/js/main.deadbeef.js")
    assert status == 404
    assert "SpeakLink" not in body


@pytest.mark.parametrize("missing", [
    "/static/css/main.deadbeef.css",
    "/missing-image.png",
    "/favicon-not-here.ico",
    "/static/media/logo.abc.svg",
])
def test_every_missing_asset_shape_is_a_genuine_404(server, missing):
    status, _ = get(server, missing)
    assert status == 404


def test_anything_under_static_is_never_rewritten(server):
    """Even an extensionless path under /static/ is a deployment artefact, not a
    route the router could handle."""
    status, body = get(server, "/static/js/whatever")
    assert status == 404
    assert "SpeakLink" not in body


# ===========================================================================
# The classifier itself
# ===========================================================================
@pytest.mark.parametrize("path", ["/static/js/a.js", "/favicon.ico", "/a/b/c.png",
                                  "/manifest.json", "/static/anything"])
def test_asset_paths_are_recognised(path):
    assert looks_like_an_asset(path) is True


@pytest.mark.parametrize("path", ["/login", "/console", "/", "/stores/14",
                                  "/a/b/c", "/broadcast-history"])
def test_route_paths_are_recognised(path):
    assert looks_like_an_asset(path) is False


# ===========================================================================
# Degrading honestly
# ===========================================================================
def test_a_directory_without_an_index_is_a_404(tmp_path):
    """No index.html means nothing to fall back to, and pretending otherwise
    would report a missing build as a working one."""
    empty = tmp_path / "empty"
    empty.mkdir()
    httpd = serve(empty, "127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://{httpd.server_address[0]}:{httpd.server_address[1]}"
    try:
        assert get(base, "/login")[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_the_runtime_serves_the_frontend_with_this_module():
    """The fix has to be on the path the packaged HQ actually runs, not merely
    available. hq_runtime used `python -m http.server`, which has no fallback."""
    source = (REPOSITORY_ROOT / "tools" / "hq_runtime.py").read_text(encoding="utf-8")
    assert "spa_server" in source, (
        "hq_runtime does not serve the frontend through the SPA server, so direct "
        "React routes still 404 on the installed HQ"
    )
