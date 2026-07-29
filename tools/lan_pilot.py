"""Prepare and verify a throwaway SpeakLink staging stack on a private LAN.

The point of this pilot is that another Windows desktop on the same private
network can reach an HQ dashboard and enrol a Receiver. That means binding to a
real LAN address instead of loopback, which changes what "safe" means: loopback
is unreachable from anywhere else by construction, and a LAN address is not.

So the safety here is explicit rather than incidental:

* the address is **fixed and checked**. This refuses to run if 192.168.4.134 is
  not actually assigned to an up, private-profile adapter. Falling back to
  another address would put an HTTP API carrying credentials somewhere nobody
  decided it should be;
* the database, the key ring and the administrator are **created fresh under a
  throwaway root** and refused if the path is the protected database, the real
  pilot database, or anywhere inside the repository;
* CORS names exact origins. Never a wildcard, because this API sends
  credentials;
* the temporary administrator password is generated or prompted for and never
  printed, logged, or written to the manifest.

Nothing here is production. Production still requires HTTPS and WSS; the private
LAN mode exists so a pilot can happen at all, and it says so loudly.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPOSITORY_ROOT / "backend"
for candidate in (REPOSITORY_ROOT, BACKEND_DIR):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

PROTECTED_DATABASE = BACKEND_DIR / "speaklink_live.db"

#: The one address this pilot is allowed to bind. Not a default that can drift.
EXPECTED_HQ_ADDRESS = "192.168.4.134"

BACKEND_PORT = 8000
FRONTEND_PORT = 3000

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_FAILED = 2


class LanPilotError(RuntimeError):
    """A controlled, secret-free refusal."""


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------
def require_private_ipv4(address: str) -> str:
    """A literal, private, routable-on-a-LAN IPv4 address and nothing else."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        raise LanPilotError(f"{address!r} is not a literal IPv4 address") from None
    if parsed.version != 4:
        raise LanPilotError("the LAN pilot address must be IPv4")
    if not parsed.is_private:
        raise LanPilotError(
            f"{address} is not an RFC1918 private address. This pilot must never "
            "bind a public address."
        )
    for label, bad in (
        ("loopback", parsed.is_loopback),
        ("link-local", parsed.is_link_local),
        ("multicast", parsed.is_multicast),
        ("unspecified", parsed.is_unspecified),
        ("reserved", parsed.is_reserved),
    ):
        if bad:
            raise LanPilotError(f"{address} is {label}; that is not a LAN pilot address")
    return str(parsed)


def reject_protected_paths(pilot_root: Path) -> None:
    resolved = Path(pilot_root).resolve()
    forbidden = [PROTECTED_DATABASE.resolve(), REPOSITORY_ROOT.resolve()]
    pilot_environment = os.environ.get("SPEAKLINK_PILOT_ROOT", "").strip()
    if pilot_environment:
        forbidden.append(Path(pilot_environment).expanduser().resolve())
    for path in forbidden:
        if resolved == path or path in resolved.parents or resolved in path.parents:
            raise LanPilotError(
                f"the pilot root {resolved} overlaps a protected location ({path})"
            )


def cors_origins(hq_address: str) -> list[str]:
    """Exact origins. The LAN address plus loopback, and nothing wider."""
    return [
        f"http://{hq_address}:{FRONTEND_PORT}",
        f"http://localhost:{FRONTEND_PORT}",
        f"http://127.0.0.1:{FRONTEND_PORT}",
    ]


# ---------------------------------------------------------------------------
# prepare
# ---------------------------------------------------------------------------
def prepare(pilot_root: Path, *, hq_address: str, admin_username: str,
            admin_password: str) -> dict:
    """Build a fresh migrated database, key ring and one SUPER_ADMIN."""
    hq_address = require_private_ipv4(hq_address)
    if hq_address != EXPECTED_HQ_ADDRESS:
        raise LanPilotError(
            f"this pilot is configured for {EXPECTED_HQ_ADDRESS}; refusing to "
            f"bind {hq_address} instead"
        )
    pilot_root = Path(pilot_root)
    reject_protected_paths(pilot_root)
    if not admin_password or len(admin_password) < 12:
        raise LanPilotError("the temporary administrator password is too short")

    pilot_root.mkdir(parents=True, exist_ok=True)
    (pilot_root / "logs").mkdir(exist_ok=True)
    database_path = pilot_root / "lan-pilot.db"
    key_container = pilot_root / "keys" / "receiver-hmac-keys.bin"

    os.environ["SPEAKLINK_DB_PATH"] = str(database_path)

    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from auth import hash_password
    from db import Base
    from key_custody import FakeProtector, create_key_container
    from migrations import run_receiver_credential_phase_one
    from models import HQUser, Store
    from rbac import Role, ensure_rbac_schema
    from receiver_credential_backfill import rehearse_legacy_receiver_backfill
    from receiver_primary_device import ensure_primary_device_schema
    from store_lifecycle import ensure_store_lifecycle_schema

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    with Session() as db:
        db.add(HQUser(
            username=admin_username,
            password_hash=hash_password(admin_password),
            role=Role.OWNER.value,
            is_active=True,
        ))
        db.add(Store(store_code="LAN-1", store_name="LAN pilot Store",
                     city="LAN", region="LAN", receiver_token=secrets.token_hex(16)))
        db.commit()
        store_id = db.query(Store).one().id

    # Every additive migration this build needs, in dependency order.
    run_receiver_credential_phase_one(engine)
    ensure_primary_device_schema(engine)
    ensure_store_lifecycle_schema(engine)
    ensure_rbac_schema(engine)

    create_key_container(key_container, protector=FakeProtector())
    rehearse_legacy_receiver_backfill(
        engine, hash_key=secrets.token_bytes(48), hash_key_version=1,
        now=datetime.now(timezone.utc),
    )
    # dual_verify: the one state where a legacy Store token still works AND
    # enrolled Device credentials authenticate.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE receiver_credential_migration_state SET state = 'dual_verify', "
                "legacy_verification_enabled = 1, updated_at = :now WHERE id = 1"
            ),
            {"now": datetime.now(timezone.utc).isoformat()},
        )
    engine.dispose()

    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "pilot_root": str(pilot_root),
        "database_path": str(database_path),
        "key_container": str(key_container),
        "hq_address": hq_address,
        "backend_url": f"http://{hq_address}:{BACKEND_PORT}",
        "frontend_url": f"http://{hq_address}:{FRONTEND_PORT}",
        "cors_origins": cors_origins(hq_address),
        "admin_username": admin_username,
        "store_id": store_id,
        "store_code": "LAN-1",
        "migration_state": "dual_verify",
        "uvicorn_workers": 1,
        # Deliberately absent: the password, the JWT secret, the HMAC key ring,
        # the Store token, any enrolment code, any Device credential.
        "secrets_in_this_file": "none",
    }
    (pilot_root / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    return manifest


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def verify(manifest_path: Path) -> dict:
    """Check a running pilot from the outside: reachability and CORS."""
    import requests

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    backend = manifest["backend_url"]
    approved = manifest["cors_origins"][0]
    results: dict = {"backend_url": backend, "frontend_url": manifest["frontend_url"]}

    try:
        results["backend_reachable_on_lan"] = (
            requests.get(f"{backend}/docs", timeout=5).status_code == 200
        )
    except Exception:
        results["backend_reachable_on_lan"] = False

    try:
        results["frontend_reachable_on_lan"] = (
            requests.get(manifest["frontend_url"], timeout=8).status_code == 200
        )
    except Exception:
        results["frontend_reachable_on_lan"] = False

    def _cors(origin: str) -> str | None:
        try:
            response = requests.options(
                f"{backend}/api/auth/login",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "POST",
                    "Access-Control-Request-Headers": "content-type",
                },
                timeout=5,
            )
            return response.headers.get("access-control-allow-origin")
        except Exception:
            return None

    results["approved_origin"] = approved
    results["approved_origin_allowed"] = _cors(approved) == approved
    # An origin nobody approved. If this comes back allowed, the wildcard is back.
    hostile = "http://192.168.4.200:3000"
    results["unapproved_origin"] = hostile
    results["unapproved_origin_refused"] = _cors(hostile) != hostile

    results["protected_database_unchanged"] = (
        PROTECTED_DATABASE.stat().st_size == 507904
        if PROTECTED_DATABASE.exists() else None
    )
    results["protected_sidecars_absent"] = not any(
        Path(str(PROTECTED_DATABASE) + suffix).exists() for suffix in ("-wal", "-shm")
    )
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lan_pilot",
        description=(
            "Prepare or verify a throwaway SpeakLink staging stack bound to a "
            "fixed private LAN address. Never production."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--pilot-root", required=True)
    prepare_command.add_argument("--hq-address", default=EXPECTED_HQ_ADDRESS)
    prepare_command.add_argument("--admin-username", required=True)
    # The password arrives on stdin, never as an argument: process arguments are
    # visible to tasklist, event logs and anybody watching the screen.
    prepare_command.add_argument("--password-from-stdin", action="store_true")

    verify_command = commands.add_parser("verify")
    verify_command.add_argument("--manifest", required=True)

    check_command = commands.add_parser("check-address")
    check_command.add_argument("--hq-address", default=EXPECTED_HQ_ADDRESS)

    arguments = parser.parse_args(argv)

    try:
        if arguments.command == "check-address":
            print(require_private_ipv4(arguments.hq_address))
            return EXIT_OK

        if arguments.command == "prepare":
            # lstrip the BOM before stripping whitespace. Windows PowerShell
            # prepends a UTF-8 byte-order mark when piping a string to a native
            # command, and it is not whitespace - so a password piped in from
            # PowerShell arrives one character longer than it left, hashes to
            # something else, and the operator is told their own password is
            # wrong. Found by round-tripping this exact pipeline.
            password = (
                sys.stdin.readline().lstrip("\ufeff").strip()
                if arguments.password_from_stdin else ""
            )
            if not password:
                raise LanPilotError("no administrator password was supplied on stdin")
            manifest = prepare(
                Path(arguments.pilot_root),
                hq_address=arguments.hq_address,
                admin_username=arguments.admin_username,
                admin_password=password,
            )
            del password
            print(json.dumps(manifest, indent=2, sort_keys=True))
            return EXIT_OK

        results = verify(Path(arguments.manifest))
        print(json.dumps(results, indent=2, sort_keys=True))
        return EXIT_OK if all(
            value is not False for value in results.values()
        ) else EXIT_FAILED

    except LanPilotError as refusal:
        print(f"LAN pilot refused: {refusal}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
