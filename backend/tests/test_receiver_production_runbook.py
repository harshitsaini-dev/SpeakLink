"""Pure documentation-contract tests for the Receiver production cutover plan."""

from pathlib import Path
import re


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RUNBOOK_PATH = REPOSITORY_ROOT / "RECEIVER_PRODUCTION_CUTOVER_RUNBOOK.md"
CUSTODY_PATH = REPOSITORY_ROOT / "RECEIVER_HMAC_KEY_CUSTODY.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _code_blocks(markdown: str) -> str:
    return "\n".join(re.findall(r"```[^\n]*\n(.*?)```", markdown, re.DOTALL))


def test_required_runbook_phases_and_emergency_controls_exist():
    runbook = _read(RUNBOOK_PATH)
    required_headings = (
        "Phase A - Authorization and scheduling",
        "Phase B - Preflight",
        "Phase C - Stop and backup",
        "Phase D - Backup verification",
        "Phase E - Additive schema migration",
        "Phase F - Legacy backfill",
        "Phase G - Controlled dual verification pilot",
        "Phase H - Hash-only readiness",
        "Phase I - Hash-only transition",
        "Phase J - Rollback",
        "Phase K - Post-cutover validation",
        "Phase L - Expansion policy",
        "Do not proceed",
        "Emergency abort",
    )
    for heading in required_headings:
        assert heading in runbook


def test_transition_order_worker_limit_and_source_blockers_are_explicit():
    runbook = _read(RUNBOOK_PATH)
    assert "backfilled -> dual_verify -> hash_only" in runbook
    assert "hash_only -> dual_verify -> backfilled" in runbook
    assert "exactly one Uvicorn worker" in runbook
    assert "legacy_authenticated_count = 0" in runbook
    assert "hashed_authenticated_count = 0" in runbook
    assert "fresh" in runbook.lower()


def test_database_and_key_backups_are_separate_and_both_required():
    combined = _read(RUNBOOK_PATH) + _read(CUSTODY_PATH)
    assert "separate from the database backup" in combined
    assert "both" in combined.lower() and "recovery" in combined.lower()
    assert "WAL" in combined and "SHM" in combined


def test_key_policy_covers_versions_storage_roles_loss_and_rotation():
    custody = _read(CUSTODY_PATH)
    required = (
        "Key purpose",
        "Key versioning",
        "Storage decision matrix",
        "Access-control roles",
        "Key handling",
        "Key backup and restore rehearsal",
        "Key loss and compromise",
        "Rotation design",
        "Unknown key versions fail closed",
        "cannot be converted",
    )
    for text in required:
        assert text in custody


def test_pilot_expands_in_controlled_stages():
    runbook = _read(RUNBOOK_PATH)
    stages = re.findall(r"Stage\s+\d+:\s+([^\n]+)", runbook)
    assert stages == ["1 Store", "3 Stores", "5 Stores", "10 Stores", "remaining Stores"]
    assert "observation period" in runbook.lower()
    assert "rollback criteria" in runbook.lower()
    assert "No direct all-40-Store cutover" in runbook


def test_authentication_readiness_playback_and_acoustic_evidence_are_separate():
    runbook = _read(RUNBOOK_PATH)
    for heading in ("Authentication", "Connection health", "Readiness", "Playback", "Acoustic", "Operational"):
        assert f"### {heading}" in runbook
    assert "Authentication success does not prove audible speaker output" in runbook
    assert "Do not combine these checks into one" in runbook


def test_raw_neutralization_is_excluded_from_first_pilot_and_is_irreversible():
    runbook = _read(RUNBOOK_PATH)
    assert "raw_neutralized is not part of the first production pilot" in runbook
    assert "separate reviewed migration" in runbook
    assert "Raw tokens cannot be reconstructed from hashes" in runbook
    assert "irreversible without a verified backup" in runbook


def test_documents_contain_no_executable_secret_or_destructive_examples():
    combined = _read(RUNBOOK_PATH) + "\n" + _read(CUSTODY_PATH)
    code = _code_blocks(combined).lower()
    prohibited_commands = (
        "git reset --hard",
        "git clean -fd",
        "git push --force",
        "remove-item backend/echocast_live.db",
        "del backend\\echocast_live.db",
        "rm backend/echocast_live.db",
    )
    for command in prohibited_commands:
        assert command not in code
    assert not re.search(r"(?i)authorization:\s*bearer\s+[a-z0-9._-]{16,}", combined)
    assert not re.search(r"(?i)(hmac|secret)[_-]?key\s*[=:]\s*[a-z0-9+/]{24,}", combined)
    assert "<APPROVED_BACKUP_DIRECTORY>" in combined
    assert "<KEY_VERSION>" in combined


def test_documentation_does_not_claim_or_execute_production_work():
    combined = _read(RUNBOOK_PATH) + "\n" + _read(CUSTODY_PATH)
    required_boundaries = (
        "This document does not authorize or execute a production cutover",
        "No real secret is included",
        "Do not delete the original database",
        "Do not retry blindly",
    )
    for text in required_boundaries:
        assert text in combined
