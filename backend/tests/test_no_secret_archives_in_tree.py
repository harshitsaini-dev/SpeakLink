"""No archive in the working tree may carry a .env or a database.

WHY THIS EXISTS

The security audit found `echocast-live.zip` in the repository root containing
`backend/.env` — and the JWT_SECRET, ADMIN_USERNAME and ADMIN_PASSWORD inside it
were byte-identical to the CURRENT live values. The signing secret is the whole
authentication system: whoever holds it can mint a valid token for any account.

It had never been committed. `.gitignore` matches `*.zip`, and `git ls-files`
confirms it is untracked — which is exactly why every existing secret scan
missed it. Those scans walk **tracked** files, so an ignored archive is invisible
to all of them. "Not in git" is not the same as "not in the folder somebody
zips up and emails".

WHAT THIS TEST DOES, AND DOES NOT DO

It fails loudly and names the entry. It deletes nothing: removing an operator's
archive is irreversible and rotating a live credential is a human decision, so
both are reported rather than performed.

It reads only entry NAMES from each archive, never contents, so the test itself
cannot become the thing that prints a secret.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

#: Archive kinds a stray export actually arrives as.
ARCHIVE_SUFFIXES = (".zip",)

#: Entry names that must never be inside one. Deliberately about the ENVELOPE
#: (a file called .env, a database, a key container) rather than about matching
#: secret-shaped strings: the point is that this class of file has no business
#: in an archive sitting in a source tree, whatever it happens to contain today.
FORBIDDEN_ENTRY_MARKERS = (
    ".env",
    ".db",
    ".sqlite",
    ".sqlite3",
    "jwt-secret",
    "hmac-keys",
    ".pem",
    ".pfx",
)

#: Directories that legitimately hold build output and are not source tree.
SKIPPED_DIRECTORIES = {"node_modules", ".venv", ".git", "build", "dist"}


def _archives() -> list[Path]:
    found = []
    for path in REPOSITORY_ROOT.rglob("*"):
        if path.suffix.lower() not in ARCHIVE_SUFFIXES or not path.is_file():
            continue
        if any(part in SKIPPED_DIRECTORIES for part in path.parts):
            continue
        found.append(path)
    return found


def test_no_archive_in_the_tree_contains_an_env_file_or_a_database():
    """The finding this file was written for, generalised.

    A pass here means no archive in the tree carries an envelope of this kind.
    It does NOT mean the tree is free of secrets - a loose backend/.env is normal
    and expected during development, and is covered by .gitignore rather than by
    this test.
    """
    offenders: list[str] = []
    unreadable: list[str] = []

    for archive in _archives():
        try:
            with zipfile.ZipFile(archive) as bundle:
                names = bundle.namelist()
        except (zipfile.BadZipFile, OSError) as failure:
            # Unreadable is UNKNOWN, not PASS. Reported so it cannot hide here.
            unreadable.append(f"{archive.relative_to(REPOSITORY_ROOT)} ({failure.__class__.__name__})")
            continue
        for name in names:
            lowered = name.lower()
            if any(marker in lowered for marker in FORBIDDEN_ENTRY_MARKERS):
                offenders.append(
                    f"{archive.relative_to(REPOSITORY_ROOT)} -> {name}")

    assert unreadable == [], f"archive(s) could not be inspected: {unreadable}"
    assert offenders == [], (
        "an archive in the working tree carries an environment file, a database "
        "or a key container. These are invisible to every existing secret scan, "
        "because those scans walk TRACKED files and an ignored archive is not "
        "tracked. Remove the archive and rotate anything it held:\n  "
        + "\n  ".join(offenders)
    )


def test_an_ignored_file_is_invisible_to_a_tracked_file_scan():
    """The gap, demonstrated rather than asserted in prose.

    Every existing repository-wide scan enumerates through `git ls-files`, which
    lists only TRACKED files. `echocast-live.zip` matches `*.zip` in .gitignore,
    so it is absent from that list - and that is precisely why an archive holding
    the live JWT_SECRET went unnoticed by all of them.

    This proves the mechanism against the real file rather than trusting the
    explanation.
    """
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPOSITORY_ROOT,
        capture_output=True, text=True, timeout=120,
    )
    assert tracked.returncode == 0, tracked.stderr[-500:]
    listed = set(tracked.stdout.split())

    ignored_archives = [
        a for a in _archives()
        if a.relative_to(REPOSITORY_ROOT).as_posix() not in listed
    ]
    if not ignored_archives:
        pytest.skip("no untracked archive in the tree to demonstrate against")

    # Untracked, therefore unscanned by anything that walks git ls-files.
    for archive in ignored_archives:
        relative = archive.relative_to(REPOSITORY_ROOT).as_posix()
        assert relative not in listed, (
            f"{relative} is tracked after all - this test's premise is stale")
