"""Safe in-process smoke tests using an isolated temporary SQLite database."""
import importlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

import pytest
import requests
from sqlalchemy import text


@pytest.fixture(scope="module")
def isolated_backend(tmp_path_factory):
    backend_dir = Path(__file__).resolve().parents[1]
    database_path = tmp_path_factory.mktemp("speaklink-smoke") / "smoke.db"
    environment = {
        "SPEAKLINK_DB_PATH": str(database_path),
        "JWT_SECRET": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": f"smoke-{secrets.token_hex(6)}",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "CORS_ORIGINS": "http://localhost:3000",
    }
    previous_environment = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    sys.path.insert(0, str(backend_dir))

    try:
        db = importlib.import_module("db")
        server = importlib.import_module("server")
        assert Path(db.DB_PATH) == database_path.resolve()
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]

        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "server:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--workers",
                "1",
            ],
            cwd=backend_dir,
            env=os.environ.copy(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Isolated Uvicorn process exited during startup")
            try:
                if requests.get(f"{base_url}/docs", timeout=0.5).status_code == 200:
                    break
            except requests.RequestException:
                time.sleep(0.1)
        else:
            pytest.fail("Isolated Uvicorn process did not become ready")

        yield {
            "base_url": base_url,
            "db": db,
            "server": server,
            "username": environment["ADMIN_USERNAME"],
            "password": environment["ADMIN_PASSWORD"],
            "database_path": database_path,
        }
    finally:
        if "process" in locals() and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if "db" in locals():
            db.engine.dispose()
        if sys.path and sys.path[0] == str(backend_dir):
            sys.path.pop(0)
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_backend_app_import(isolated_backend):
    assert isolated_backend["server"].app.title == "SpeakLink"


def test_sqlite_test_database_connection(isolated_backend):
    db = isolated_backend["db"]
    with db.engine.connect() as connection:
        assert connection.scalar(text("SELECT 1")) == 1
        database_file = connection.exec_driver_sql("PRAGMA database_list").one()[2]
        assert Path(database_file) == isolated_backend["database_path"].resolve()
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1


@pytest.mark.parametrize("path", ["/docs", "/openapi.json"])
def test_documentation_endpoints(isolated_backend, path):
    response = requests.get(f"{isolated_backend['base_url']}{path}", timeout=5)
    assert response.status_code == 200


def test_auth_me_requires_token(isolated_backend):
    response = requests.get(
        f"{isolated_backend['base_url']}/api/auth/me",
        timeout=5,
    )
    assert response.status_code == 401


def test_development_login_and_store_listing(isolated_backend):
    login_response = requests.post(
        f"{isolated_backend['base_url']}/api/auth/login",
        json={
            "username": isolated_backend["username"],
            "password": isolated_backend["password"],
        },
        timeout=5,
    )
    assert login_response.status_code == 200
    token = login_response.json()["access_token"]

    stores_response = requests.get(
        f"{isolated_backend['base_url']}/api/stores",
        headers={"Authorization": f"Bearer {token}"},
        timeout=5,
    )
    assert stores_response.status_code == 200
    stores = stores_response.json()
    assert stores
    assert all(store["status"] == "offline" for store in stores)
