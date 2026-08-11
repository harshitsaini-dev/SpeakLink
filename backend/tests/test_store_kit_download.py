"""Handing the Store Kit out from HQ.

A kit fetched from HQ is the kit HQ has - which is the whole point, because a
USB stick cannot tell anybody which build a shop received. So the tests are
about identity and reach: the bytes match, the checksum travels with them, and
only accounts holding the right can ask.

The traversal test matters more than it looks. The download route takes a
filename from the caller, and the safe way to handle that is never to build a
path from it - the name is matched against the directory listing instead. A
test that only tried "../../etc/passwd" would pass against a weaker
implementation that stripped dots; this one checks the file is not reachable at
all.
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import zipfile
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

PASSWORD = "a-long-enough-temporary-password"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("SPEAKLINK_DB_PATH", str(tmp_path / "hq.db"))
    monkeypatch.setenv("SPEAKLINK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JWT_SECRET", "test-only-secret-value-for-this-module")
    monkeypatch.setenv("ADMIN_USERNAME", "founder")
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    for module in [name for name in list(sys.modules)
                   if name in ("server", "db", "models", "seed", "auth", "rbac",
                               "user_lifecycle", "schemas", "permission_catalog",
                               "store_scope", "store_kits")]:
        sys.modules.pop(module, None)
    from fastapi.testclient import TestClient
    import server as server_module
    with TestClient(server_module.app) as made:
        made.server_module = server_module
        made.data_root = tmp_path / "data"
        yield made


def sign_in(client, username="founder", password=PASSWORD):
    response = client.post("/api/auth/login",
                           json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


@pytest.fixture()
def owner(client):
    return sign_in(client)


def put_kit(client, name: str, contents: bytes = b"receiver payload") -> bytes:
    directory = client.data_root / "store-kits"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("SpeakLinkReceiverBackground.exe", contents)
    return path.read_bytes()


# ===========================================================================
# What HQ says it has
# ===========================================================================

def test_an_hq_with_no_kit_says_so_rather_than_failing(client, owner):
    """"No kits" and "the feature is broken" look identical otherwise, and the
    fix for the first one is a build."""
    answer = client.get("/api/store-kits", headers=owner)
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["kits"] == [] and body["latest"] is None
    assert body["directory_exists"] is False


def test_a_kit_is_listed_with_its_size_and_checksum(client, owner):
    payload = put_kit(client, "SpeakLinkStoreKit-1.6.0.zip")

    body = client.get("/api/store-kits", headers=owner).json()
    assert len(body["kits"]) == 1
    kit = body["kits"][0]
    assert kit["name"] == "SpeakLinkStoreKit-1.6.0.zip"
    assert kit["size_bytes"] == len(payload)
    assert kit["sha256"] == hashlib.sha256(payload).hexdigest()
    assert body["latest"]["name"] == kit["name"]


def test_the_newest_kit_wins_by_timestamp_not_by_name(client, owner):
    """Version strings sort wrongly - 1.10.0 before 1.9.0 - and a name is only
    a claim. The file's own timestamp is what the machine observed."""
    import time
    put_kit(client, "SpeakLinkStoreKit-1.10.0.zip")
    time.sleep(1.1)
    put_kit(client, "SpeakLinkStoreKit-1.9.0.zip")

    body = client.get("/api/store-kits", headers=owner).json()
    assert body["latest"]["name"] == "SpeakLinkStoreKit-1.9.0.zip"


def test_a_zip_that_is_not_a_store_kit_is_not_offered(client, owner):
    put_kit(client, "SpeakLinkStoreKit-1.6.0.zip")
    put_kit(client, "somebody-elses-backup.zip")

    names = [kit["name"] for kit in client.get("/api/store-kits", headers=owner).json()["kits"]]
    assert names == ["SpeakLinkStoreKit-1.6.0.zip"]


# ===========================================================================
# The download itself
# ===========================================================================

def test_the_downloaded_bytes_are_the_bytes_hq_holds(client, owner):
    payload = put_kit(client, "SpeakLinkStoreKit-1.6.0.zip")

    answer = client.get("/api/store-kits/latest/download", headers=owner)
    assert answer.status_code == 200, answer.text
    assert answer.content == payload
    assert answer.headers["content-type"] == "application/zip"
    # The checksum travels with the file, so a Store can check what it received
    # without a second request.
    assert answer.headers["x-speaklink-kit-sha256"] == hashlib.sha256(payload).hexdigest()
    assert "SpeakLinkStoreKit-1.6.0.zip" in answer.headers["content-disposition"]


def test_a_named_kit_can_be_fetched_deliberately(client, owner):
    older = put_kit(client, "SpeakLinkStoreKit-1.5.0.zip", b"older payload")
    put_kit(client, "SpeakLinkStoreKit-1.6.0.zip", b"newer payload")

    answer = client.get("/api/store-kits/SpeakLinkStoreKit-1.5.0.zip/download",
                        headers=owner)
    assert answer.status_code == 200
    assert answer.content == older


def test_downloading_when_there_is_nothing_says_what_to_do(client, owner):
    answer = client.get("/api/store-kits/latest/download", headers=owner)
    assert answer.status_code == 404
    assert "Build one" in answer.json()["detail"]


@pytest.mark.parametrize("name", [
    "../../../../Windows/System32/drivers/etc/hosts",
    "..%2F..%2Fhq.db",
    "SpeakLinkStoreKit-1.6.0.zip/../../hq.db",
    "not-a-kit.zip",
])
def test_a_name_that_is_not_in_the_listing_is_simply_not_there(client, owner, name):
    """The name is matched against the directory listing rather than joined
    onto a path, so traversal is not defended against - it is impossible."""
    put_kit(client, "SpeakLinkStoreKit-1.6.0.zip")
    answer = client.get(f"/api/store-kits/{name}/download", headers=owner)
    assert answer.status_code in (404, 405), answer.text
    if answer.status_code == 404 and answer.headers.get("content-type", "").startswith("application/json"):
        assert "kit" in answer.json()["detail"].lower() or "not found" in answer.json()["detail"].lower()


# ===========================================================================
# Who may fetch it
# ===========================================================================

def test_an_unauthenticated_caller_gets_nothing(client, owner):
    put_kit(client, "SpeakLinkStoreKit-1.6.0.zip")
    assert client.get("/api/store-kits").status_code == 401
    assert client.get("/api/store-kits/latest/download").status_code == 401


def test_a_viewer_cannot_download_the_software_the_estate_runs(client, owner):
    put_kit(client, "SpeakLinkStoreKit-1.6.0.zip")
    made = client.post("/api/users", headers=owner, json={
        "username": "watcher", "display_name": "Watcher", "role": "VIEWER",
        "password": PASSWORD})
    assert made.status_code == 201, made.text
    viewer = sign_in(client, "watcher")

    assert client.get("/api/store-kits", headers=viewer).status_code == 403
    assert client.get("/api/store-kits/latest/download",
                      headers=viewer).status_code == 403


def test_the_download_is_recorded(client, owner):
    """Which build a shop received, and when, is the reason HQ serves it."""
    put_kit(client, "SpeakLinkStoreKit-1.6.0.zip")
    client.get("/api/store-kits/latest/download", headers=owner)

    logs = client.get("/api/logs", headers=owner, params={"page_size": 50}).json()
    entries = logs.get("items", logs) if isinstance(logs, dict) else logs
    text = " ".join(str(entry) for entry in entries)
    assert "STORE_KIT_DOWNLOADED" in text
    assert "SpeakLinkStoreKit-1.6.0.zip" in text


# ===========================================================================
# Uploading one from the Console
# ===========================================================================

def upload(client, headers, name, payload):
    return client.post("/api/store-kits", headers=headers,
                       files={"file": (name, payload, "application/octet-stream")})


def test_an_installer_can_be_uploaded_and_is_then_offered(client, owner):
    payload = b"MZ" + b"installer bytes" * 100
    answer = upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", payload)
    assert answer.status_code == 200, answer.text
    assert answer.json()["sha256"] == hashlib.sha256(payload).hexdigest()

    listed = client.get("/api/store-kits", headers=owner).json()
    assert listed["latest"]["name"] == "SpeakLinkStoreInstaller-1.6.0.exe"
    fetched = client.get("/api/store-kits/latest/download", headers=owner)
    assert fetched.content == payload


def test_the_stored_name_is_built_by_hq_not_taken_from_the_request(client, owner):
    """A filename that arrives over HTTP is input, and input joined onto a
    directory is how a traversal happens. HQ builds the name instead."""
    answer = upload(client, owner, "../../evil/SpeakLink kit v2.exe", b"MZ payload")
    assert answer.status_code == 200, answer.text
    stored = answer.json()["name"]
    assert "/" not in stored and "\\" not in stored and ".." not in stored
    assert stored.startswith("SpeakLink") and stored.endswith(".exe")
    # And it really is in the kits directory, not above it.
    assert (client.data_root / "store-kits" / stored).exists()


@pytest.mark.parametrize(("name", "payload", "because"), [
    ("SpeakLinkKit.txt", b"hello", "only an installer or a zip"),
    ("SpeakLinkKit.exe", b"not an executable", "the magic bytes are wrong"),
    ("SpeakLinkKit.zip", b"MZ not a zip", "the magic bytes are wrong"),
    ("SpeakLinkKit.exe", b"", "an empty file"),
])
def test_an_upload_that_is_not_a_kit_is_refused(client, owner, name, payload, because):
    answer = upload(client, owner, name, payload)
    assert answer.status_code == 400, f"{because}: {answer.text}"
    assert client.get("/api/store-kits", headers=owner).json()["kits"] == []


def test_an_upload_replaces_whatever_was_there_whatever_it_is_called(client, owner):
    """HQ holds exactly one kit.

    A list of builds means somebody eventually installs the wrong one, and
    "which build is that Store on?" stops having a single answer.
    """
    upload(client, owner, "SpeakLinkStoreInstaller-1.5.0.exe", b"MZ older")

    newer = upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ newer")
    assert newer.status_code == 200, newer.text
    assert newer.json()["superseded"] == ["SpeakLinkStoreInstaller-1.5.0.exe"]

    listed = client.get("/api/store-kits", headers=owner).json()
    assert [kit["name"] for kit in listed["kits"]] == ["SpeakLinkStoreInstaller-1.6.0.exe"]
    assert client.get("/api/store-kits/latest/download",
                      headers=owner).content == b"MZ newer"


def test_the_same_name_uploaded_again_simply_overwrites(client, owner):
    upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ first")

    again = upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ second")
    assert again.status_code == 200, again.text
    assert again.json()["superseded"] == []
    assert again.json()["sha256"] == hashlib.sha256(b"MZ second").hexdigest()
    assert client.get("/api/store-kits/latest/download",
                      headers=owner).content == b"MZ second"
    assert len(client.get("/api/store-kits", headers=owner).json()["kits"]) == 1


def test_a_failed_write_leaves_the_existing_build_alone(client, owner, monkeypatch):
    """The old kit is removed only AFTER the new one is safely in place -
    otherwise a failed upload leaves an HQ with nothing to hand out."""
    upload(client, owner, "SpeakLinkStoreInstaller-1.5.0.exe", b"MZ older")

    import store_kits
    def explode(*args, **kwargs):
        raise OSError("the disk went away")
    monkeypatch.setattr(store_kits.os, "replace", explode)

    crashed = upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ newer")
    assert crashed.status_code >= 400

    listed = client.get("/api/store-kits", headers=owner).json()
    assert [kit["name"] for kit in listed["kits"]] == ["SpeakLinkStoreInstaller-1.5.0.exe"]
    assert client.get("/api/store-kits/latest/download",
                      headers=owner).content == b"MZ older"


def test_a_zip_and_an_exe_do_not_coexist_either(client, owner):
    """One kit means one FILE, not one of each kind."""
    upload(client, owner, "SpeakLinkStoreKit-1.6.0.zip", b"PK payload")
    upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ payload")

    listed = client.get("/api/store-kits", headers=owner).json()
    assert [kit["name"] for kit in listed["kits"]] == ["SpeakLinkStoreInstaller-1.6.0.exe"]


def test_a_kit_can_be_removed_again(client, owner):
    upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ payload")
    removed = client.delete("/api/store-kits/SpeakLinkStoreInstaller-1.6.0.exe",
                            headers=owner)
    assert removed.status_code == 200, removed.text
    assert client.get("/api/store-kits", headers=owner).json()["kits"] == []


def test_only_an_account_with_the_manage_right_may_upload(client, owner):
    made = client.post("/api/users", headers=owner, json={
        "username": "runner", "display_name": "Runner", "role": "BROADCASTER",
        "password": PASSWORD})
    assert made.status_code == 201, made.text
    runner = sign_in(client, "runner")

    refused = upload(client, runner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ x")
    assert refused.status_code == 403
    assert client.delete("/api/store-kits/anything.exe",
                         headers=runner).status_code == 403


def test_an_upload_is_recorded_with_who_did_it(client, owner):
    """Whoever can upload decides what software every Store installs next, so
    the account that did it is the thing worth recording."""
    upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ payload")
    logs = client.get("/api/logs", headers=owner, params={"page_size": 50}).json()
    entries = logs.get("items", logs) if isinstance(logs, dict) else logs
    text = " ".join(str(entry) for entry in entries)
    assert "STORE_KIT_UPLOADED" in text
    assert "founder" in text


def test_the_log_names_what_an_upload_replaced(client, owner):
    """Overwriting the build every Store downloads is worth a record naming
    what went, not just what arrived."""
    upload(client, owner, "SpeakLinkStoreInstaller-1.5.0.exe", b"MZ older")
    upload(client, owner, "SpeakLinkStoreInstaller-1.6.0.exe", b"MZ newer")

    logs = client.get("/api/logs", headers=owner, params={"page_size": 50}).json()
    entries = logs.get("items", logs) if isinstance(logs, dict) else logs
    text = " ".join(str(entry) for entry in entries)
    assert "superseded=SpeakLinkStoreInstaller-1.5.0.exe" in text
