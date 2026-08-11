"""No source file starts with a UTF-8 byte-order mark.

Windows PowerShell's ``Set-Content -Encoding utf8`` writes one. Every automated
edit to this repository that went through PowerShell therefore risked adding a
BOM to a Python or PowerShell file, and five files had picked one up before this
test existed.

A BOM in a Python file is *almost* harmless, which is what makes it worth a
test. The interpreter reads source as ``utf-8-sig`` and skips it, so the module
imports, the tests run and a PyInstaller build succeeds. What breaks is anything
that reads the file as text and parses it:

    ast.parse(Path(module).read_text(encoding="utf-8"))
    SyntaxError: invalid non-printable character U+FEFF

Two tests in this suite do exactly that to prove the Agent imports nothing from
the backend, and they failed for a reason that had nothing to do with imports.
A defect that only shows up in the tooling that inspects the code, and never in
the code itself, is the kind that survives a long time.

The same mark has bitten this project three times now, in three different
places - a piped enrolment code, a commit message, and now source files - which
is why the fix is a test rather than another careful edit.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
UTF8_BOM = b"\xef\xbb\xbf"
CHECKED_SUFFIXES = {".py", ".ps1", ".spec", ".md", ".json", ".js", ".jsx"}

#: Directories that are not ours to police.
SKIP_DIRECTORIES = {
    ".git", ".venv", "node_modules", "build", "dist", "artifacts",
    "test-results", "playwright-report", "__pycache__",
}


def _tracked_source_files() -> list[Path]:
    """Files git actually tracks.

    Walking the tree would also find generated output and anything an operator
    happened to leave lying about; the question is only about files this
    repository is responsible for.
    """
    try:
        listing = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=str(REPOSITORY_ROOT), capture_output=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git is not available here")
    if listing.returncode != 0:
        pytest.skip("not a git working tree")

    files = []
    for entry in listing.stdout.split(b"\0"):
        if not entry:
            continue
        relative = Path(entry.decode("utf-8", "replace"))
        if set(relative.parts) & SKIP_DIRECTORIES:
            continue
        if relative.suffix.lower() not in CHECKED_SUFFIXES:
            continue
        absolute = REPOSITORY_ROOT / relative
        if absolute.is_file():
            files.append(absolute)
    return files


def test_no_tracked_source_file_begins_with_a_byte_order_mark():
    offenders = []
    for path in _tracked_source_files():
        with path.open("rb") as handle:
            if handle.read(3) == UTF8_BOM:
                offenders.append(str(path.relative_to(REPOSITORY_ROOT)))
    assert offenders == [], (
        "these files start with a UTF-8 BOM, which breaks any tool that parses "
        "them as text while leaving the interpreter working normally: "
        f"{offenders}. PowerShell's Set-Content -Encoding utf8 writes one; use "
        "[IO.File]::WriteAllText with (New-Object Text.UTF8Encoding $false)."
    )


def test_the_check_covers_the_files_that_actually_got_one():
    """A guard that silently checked nothing would be worse than none."""
    checked = {path.suffix.lower() for path in _tracked_source_files()}
    assert ".py" in checked
    assert ".ps1" in checked


def test_every_python_source_file_parses_as_plain_utf8():
    """The failure mode itself, rather than only its cause.

    A BOM is one way a file stops being parseable as plain UTF-8; this catches
    any other.
    """
    import ast

    offenders = []
    for path in _tracked_source_files():
        if path.suffix.lower() != ".py":
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as failure:
            offenders.append(f"{path.relative_to(REPOSITORY_ROOT)}: {type(failure).__name__}")
    assert offenders == [], f"files that do not parse as plain UTF-8: {offenders}"
