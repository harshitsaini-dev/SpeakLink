"""Pure documentation-contract tests for the Receiver hosting/key-storage ADR
and the accompanying security/operations review.

These tests read Markdown files only. They must never import server.py,
FastAPI, SQLAlchemy, Uvicorn, or any WebSocket library, and must never open
SQLite, contact the network, generate keys, modify environment variables, or
execute any documentation command.
"""

from __future__ import annotations

from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ADR_PATH = REPOSITORY_ROOT / "docs/RECEIVER_HOSTING_KEY_STORAGE_ADR.md"
REVIEW_PATH = REPOSITORY_ROOT / "docs/RECEIVER_SECURITY_OPERATIONS_REVIEW.md"

# Runtime/frontend files this task must never modify. Fingerprinted at
# collection time so a later test in this module can prove they are still
# untouched after the focused documentation tests have executed.
_GUARDED_RUNTIME_FILES = (
    REPOSITORY_ROOT / "backend" / "server.py",
    REPOSITORY_ROOT / "backend" / "ws_manager.py",
    REPOSITORY_ROOT / "backend" / "db.py",
    REPOSITORY_ROOT / "backend" / "models.py",
    REPOSITORY_ROOT / "backend" / "auth.py",
    REPOSITORY_ROOT / "backend" / "migrations.py",
    REPOSITORY_ROOT / "backend" / "receiver_credentials.py",
    REPOSITORY_ROOT / "backend" / "receiver_device_service.py",
    REPOSITORY_ROOT / "backend" / "receiver_credential_backfill.py",
    REPOSITORY_ROOT / "backend" / "receiver_auth_service.py",
    REPOSITORY_ROOT / "backend" / "receiver_migration_transition_service.py",
    REPOSITORY_ROOT / "backend" / "receiver_connection_inventory.py",
    REPOSITORY_ROOT / "backend" / "receiver_runtime_auth.py",
    REPOSITORY_ROOT / "backend" / "receiver_cutover_rehearsal.py",
    REPOSITORY_ROOT / "backend" / "pytest.ini",
    REPOSITORY_ROOT / ".gitignore",
)
_PROTECTED_DATABASE_PATH = REPOSITORY_ROOT / "backend" / "speaklink_live.db"


def _fingerprint(path: Path):
    if not path.exists():
        return None
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


_RUNTIME_FINGERPRINTS_AT_COLLECTION = tuple(
    _fingerprint(path) for path in _GUARDED_RUNTIME_FILES
)
_PROTECTED_DATABASE_FINGERPRINT_AT_COLLECTION = _fingerprint(_PROTECTED_DATABASE_PATH)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_blocks(markdown: str) -> str:
    return "\n".join(re.findall(r"```[^\n]*\n(.*?)```", markdown, re.DOTALL))


# ---------------------------------------------------------------------------
# 1. Both documents exist.
# ---------------------------------------------------------------------------
def test_both_new_documents_exist():
    assert ADR_PATH.is_file()
    assert REVIEW_PATH.is_file()


# ---------------------------------------------------------------------------
# 2. ADR required sections.
# ---------------------------------------------------------------------------
def test_adr_contains_required_sections():
    adr = _read(ADR_PATH)
    required_headings = (
        "Status",
        "Context",
        "Decision drivers",
        "Options considered",
        "Decision matrix",
        "Selected decision",
        "Rejected options",
        "Consequences",
        "Security boundaries",
        "Implementation prerequisites",
        "Revisit triggers",
        "Decision approval",
    )
    for heading in required_headings:
        assert heading in adr


# ---------------------------------------------------------------------------
# 3. ADR clearly selects the provisional pilot model.
# ---------------------------------------------------------------------------
def test_adr_selects_windows_pilot_model():
    adr = _read(ADR_PATH)
    assert "Dedicated supported Windows Server/VM" in adr
    assert "One Uvicorn worker" in adr
    assert "Dedicated non-admin Windows service identity" in adr
    assert "DPAPI-protected versioned HMAC-key container outside Git and SQLite" in adr
    assert "Separate encrypted key backup" in adr


# ---------------------------------------------------------------------------
# 4. ADR marks the decision as proposed, not implemented.
# ---------------------------------------------------------------------------
def test_adr_marked_proposed_not_implemented():
    adr = _read(ADR_PATH)
    assert "Status: Proposed for pilot approval" in adr
    assert "does not implement, configure, or execute" in adr


# ---------------------------------------------------------------------------
# 5. ADR includes alternative hosting, identity, and key-storage options.
# ---------------------------------------------------------------------------
def test_adr_includes_alternative_options():
    adr = _read(ADR_PATH)
    for option in (
        "A. Dedicated Windows Server or Windows VM",
        "B. Dedicated Linux VM",
        "C. Managed application/container platform",
        "D. Developer workstation or shared office PC",
    ):
        assert option in adr
    for option in (
        "A. Dedicated local Windows service identity",
        "B. Domain-managed service account where available",
        "C. Personal administrator account",
        "D. LocalSystem or another highly privileged built-in identity",
    ):
        assert option in adr
    for option in (
        "A. DPAPI-protected versioned secret file/container",
        "B. Plaintext .env file",
        "C. Windows Credential Manager or equivalent service-accessible credential",
        "D. External managed secret service",
        "E. SQLite database or source-code constant",
    ):
        assert option in adr


# ---------------------------------------------------------------------------
# 6. ADR does not claim vendor-specific pricing or compliance certification.
# ---------------------------------------------------------------------------
def test_adr_has_no_pricing_or_compliance_certification_claims():
    adr = _read(ADR_PATH)
    lowered = adr.lower()
    for forbidden in (
        "$",
        "per month",
        "per-month",
        "pricing",
        "soc 2",
        "soc2",
        "iso 27001",
        "hipaa",
        "pci-dss",
        "pci dss",
        "certified",
        "certification",
    ):
        assert forbidden not in lowered


# ---------------------------------------------------------------------------
# 7. Security review required sections.
# ---------------------------------------------------------------------------
def test_review_contains_required_sections():
    review = _read(REVIEW_PATH)
    required_headings = (
        "Executive summary",
        "Reviewed assets",
        "Trust boundaries",
        "Threat review",
        "Least-privilege service identity review",
        "Filesystem layout proposal",
        "Network and TLS review",
        "Database operations review",
        "Logging and audit review",
        "Monitoring and alerting proposal",
        "Backup and recovery ownership",
        "Incident-response outline",
        "Pilot readiness checklist",
        "Risk acceptance",
        "Final recommendation",
    )
    for heading in required_headings:
        assert heading in review


# ---------------------------------------------------------------------------
# 8. Review distinguishes independent status axes.
# ---------------------------------------------------------------------------
def test_review_distinguishes_status_axes():
    review = _read(REVIEW_PATH)
    for term in (
        "Authentication",
        "CONNECTED",
        "READY",
        "AUDIO_RECEIVING",
        "PLAYBACK_CONFIRMED",
        "SPEAKER_VERIFIED",
    ):
        assert term in review


# ---------------------------------------------------------------------------
# 9. Review states the honest current baseline.
# ---------------------------------------------------------------------------
def test_review_states_current_honest_baseline():
    review = _read(REVIEW_PATH)
    for phrase in (
        "one Uvicorn worker",
        "process-local",
        "No production cutover has occurred",
        "No real HMAC key was loaded",
        "No real database was opened, copied, or modified",
        "all-40-Store rollout",
    ):
        assert phrase in review


# ---------------------------------------------------------------------------
# 10. Documentation forbids/rejects unsafe practices.
# ---------------------------------------------------------------------------
def test_documents_forbid_unsafe_practices():
    combined = _read(ADR_PATH) + "\n" + _read(REVIEW_PATH)
    for phrase in (
        "plaintext production key in `.env`",
        "a key stored in SQLite",
        "a hard-coded key",
        "a personal administrator account",
        "developer-laptop production deployment",
        "`LocalSystem` by default",
        "multiple Uvicorn workers",
        "a raw token in a URL",
        "database deletion as migration recovery",
    ):
        assert phrase in combined


# ---------------------------------------------------------------------------
# 11. Documentation requires safe practices.
# ---------------------------------------------------------------------------
def test_documents_require_safe_practices():
    combined = _read(ADR_PATH) + "\n" + _read(REVIEW_PATH)
    for phrase in (
        "separate database and key backups",
        "restricted ACLs",
        "a dedicated service identity",
        "HTTPS/WSS",
        "restricted CORS",
        "audit logs",
        "secret redaction",
        "a restore rehearsal",
        "a one-Store pilot",
    ):
        assert phrase in combined


# ---------------------------------------------------------------------------
# 12. No real-looking production secret appears in either new document.
# ---------------------------------------------------------------------------
def test_documents_contain_no_real_looking_secret():
    combined = _read(ADR_PATH) + "\n" + _read(REVIEW_PATH)
    code = _code_blocks(combined)

    # Long base64-like literal (32+ chars of base64 alphabet, not a placeholder).
    assert not re.search(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=_-])", combined)
    # PEM private-key headers.
    assert "-----BEGIN" not in combined
    # Authorization Bearer values that look like real tokens.
    assert not re.search(r"(?i)authorization:\s*bearer\s+[a-z0-9._-]{16,}", combined)
    # 32-character lowercase hex token examples (legacy receiver-token shape).
    assert not re.search(r"(?<![a-f0-9])[a-f0-9]{32}(?![a-f0-9])", combined)
    # JWT-like three-part dot-separated strings.
    assert not re.search(r"[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}", combined)
    assert code == "" or "<" in code  # any code blocks present must use placeholders


# ---------------------------------------------------------------------------
# 13. No prohibited destructive Git command is recommended.
# ---------------------------------------------------------------------------
def test_documents_recommend_no_destructive_git_command():
    combined = _read(ADR_PATH) + "\n" + _read(REVIEW_PATH)
    code = _code_blocks(combined).lower()
    for command in ("git reset --hard", "git clean -fd", "git push --force"):
        assert command not in code


# ---------------------------------------------------------------------------
# 14. No command deletes SQLite/database/WAL/SHM files.
# ---------------------------------------------------------------------------
def test_documents_recommend_no_database_deletion_command():
    combined = _read(ADR_PATH) + "\n" + _read(REVIEW_PATH)
    code = _code_blocks(combined).lower()
    for command in (
        "remove-item backend/speaklink_live.db",
        "remove-item backend\\speaklink_live.db",
        "del backend\\speaklink_live.db",
        "rm backend/speaklink_live.db",
        "rm -rf backend/speaklink_live.db",
        "drop database",
    ):
        assert command not in code


# ---------------------------------------------------------------------------
# 15. Tests prove no runtime file changed as part of their own execution.
# ---------------------------------------------------------------------------
def test_no_guarded_runtime_file_changed_during_this_test_run():
    current = tuple(_fingerprint(path) for path in _GUARDED_RUNTIME_FILES)
    assert current == _RUNTIME_FINGERPRINTS_AT_COLLECTION
    assert _fingerprint(_PROTECTED_DATABASE_PATH) == _PROTECTED_DATABASE_FINGERPRINT_AT_COLLECTION
