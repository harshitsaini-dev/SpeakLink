"""Serve the built React dashboard, including its client-side routes.

WHY THIS REPLACES python -m http.server

``http.server`` maps a URL to a file. A React single-page application does not
have a file per route: ``/login`` and ``/console`` exist only inside the
JavaScript router. So opening the dashboard worked, and typing the login URL
directly - or pressing F5 anywhere - returned 404 from a server that was working
perfectly.

The fix is the standard SPA fallback: an unknown path that looks like a *route*
serves ``index.html`` and lets the browser router decide.

WHAT IT MUST NOT DO

Fall back for everything. If a missing ``main.abc123.js`` also returned
``index.html``, the browser would receive HTML where it asked for JavaScript,
report a syntax error somewhere inside it, and the real fault - a file absent
from the deployment - would be invisible. **A missing static asset must still be
a 404.**

The distinction used here is the one a browser itself makes: a request carrying a
file extension is an asset; a path without one is a route. ``/static/js/x.js`` is
an asset even though it does not exist; ``/console`` is a route.

Deliberately built on ``http.server`` rather than adding a dependency: an HQ
machine already has this Python, and the frontend is static files on a private
LAN, not a public CDN.
"""

from __future__ import annotations

import argparse
import os
import sys
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

INDEX = "index.html"

#: Served as-is and never rewritten. Everything under here is a real file or a
#: genuine 404.
ASSET_PREFIXES = ("/static/",)


def looks_like_an_asset(path: str) -> bool:
    """Does this request name a file rather than a route?

    A final path segment containing a dot is an asset - ``main.abc.js``,
    ``favicon.ico``, ``logo.png``. A route has no extension: ``/login``,
    ``/console``, ``/stores/14``.
    """
    if any(path.startswith(prefix) for prefix in ASSET_PREFIXES):
        return True
    return "." in path.rsplit("/", 1)[-1]


class SpaRequestHandler(SimpleHTTPRequestHandler):
    """Static files, with index.html for client-side routes only."""

    def log_message(self, fmt, *args):  # pragma: no cover - quiet by design
        """Silenced deliberately.

        The runtime supervises this as a child and captures its output; a request
        log line per asset per page load buries the one message that matters.
        """

    def send_head(self):
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        candidate = Path(self.translate_path(self.path))

        if candidate.is_dir():
            candidate = candidate / INDEX
        if candidate.exists():
            return super().send_head()

        if looks_like_an_asset(route):
            # A real 404. Rewriting this to index.html would hand the browser
            # HTML where it asked for JavaScript, and the actual fault - a file
            # missing from the deployment - would surface as a syntax error
            # inside a page that looks fine.
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        index = Path(self.directory) / INDEX
        if not index.exists():
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return None

        # A client-side route. Serve the app and let its router decide - it is
        # also the only party that knows whether the route is real.
        self.path = "/" + INDEX
        return super().send_head()


def serve(directory: Path, host: str, port: int, *, server_class=ThreadingHTTPServer):
    handler = partial(SpaRequestHandler, directory=str(directory))
    return server_class((host, port), handler)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve a built React app with SPA fallback.")
    parser.add_argument("port", type=int)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--directory", required=True)
    args = parser.parse_args(argv)

    directory = Path(args.directory)
    if not (directory / INDEX).exists():
        print(f"there is no {INDEX} in {directory}", file=sys.stderr)
        return 2

    httpd = serve(directory, args.bind, args.port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
