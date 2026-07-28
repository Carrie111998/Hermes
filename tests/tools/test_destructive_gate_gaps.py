"""Two destructive operations that walked past their gates.

H-24: ``git push origin +main`` is exactly ``git push --force origin main`` —
a leading ``+`` on a refspec force-updates the remote ref — but the classifier
only matched the ``--force`` / ``-f`` flag spellings.

H-25: project-local ``.env`` files were blocked for READS and allowed for
WRITES, so the agent could overwrite or delete a credential store it was not
permitted to open. The equivalent terminal operation is classified dangerous.
"""

from __future__ import annotations

import os
import re
import tempfile

import pytest

from agent.file_safety import get_read_block_error, get_write_denied_error
from tools.approval import DANGEROUS_PATTERNS, _RE_FLAGS


def _flagged(command: str) -> list[str]:
    return [reason for pattern, reason in DANGEROUS_PATTERNS
            if re.search(pattern, command, _RE_FLAGS)]


# ── H-24: force push via + refspec ───────────────────────────────────────────

@pytest.mark.parametrize("command", [
    "git push origin +main",
    "git push origin +refs/heads/main:refs/heads/main",
    "git push origin +feature:main",
    "git push upstream +HEAD:main",
    "git push origin --force main",
    "git push -f origin main",
])
def test_every_force_push_spelling_is_flagged(command):
    assert _flagged(command), f"force push not classified dangerous: {command!r}"


@pytest.mark.parametrize("command", [
    "git push origin main",
    "git push",
    "git push origin HEAD:main",
    "git push --set-upstream origin feature",
    "git status",
    "echo a+b",
    "git push origin main # note the + sign",
])
def test_non_force_pushes_are_not_flagged(command):
    """An over-broad pattern is its own failure: it trains people to bypass."""
    assert not _flagged(command), f"false positive on: {command!r}"


def test_plus_refspec_reason_names_the_mechanism():
    reasons = _flagged("git push origin +main")
    assert any("refspec" in r for r in reasons), (
        "the operator should be told WHY this is a force push"
    )


# ── H-25: .env writes ────────────────────────────────────────────────────────

ENV_NAMES = [".env", ".env.local", ".env.production", ".env.staging", ".envrc"]


@pytest.fixture
def tmpdir_path():
    with tempfile.TemporaryDirectory() as d:
        yield d


@pytest.mark.parametrize("name", ENV_NAMES)
def test_env_files_are_write_denied(name, tmpdir_path):
    target = os.path.join(tmpdir_path, name)
    assert get_write_denied_error(target), (
        f"{name} can be overwritten or deleted without a gate"
    )


@pytest.mark.parametrize("name", ENV_NAMES)
def test_env_files_remain_read_denied(name, tmpdir_path):
    assert get_read_block_error(os.path.join(tmpdir_path, name))


@pytest.mark.parametrize("name", ENV_NAMES)
def test_read_and_write_protection_agree(name, tmpdir_path):
    """The asymmetry WAS the defect: destroyable but not readable.

    Whichever way a future change moves these, it must move both.
    """
    target = os.path.join(tmpdir_path, name)
    assert bool(get_read_block_error(target)) == bool(get_write_denied_error(target))


@pytest.mark.parametrize("name", ["notes.txt", "env.py", "environment.yml",
                                  "readme.env.md", "config.json"])
def test_ordinary_files_stay_writable(name, tmpdir_path):
    """The gate must key on the env-file basename, not merely contain 'env'."""
    assert get_write_denied_error(os.path.join(tmpdir_path, name)) is None


def test_env_denial_is_reported_as_a_credential_file(tmpdir_path):
    message = get_write_denied_error(os.path.join(tmpdir_path, ".env"), verb="Delete")
    assert "Delete denied" in message
    assert "credential" in message.lower()


def test_write_denial_classifier_returns_documented_values(tmpdir_path):
    """_classify_write_denial documents 'credential' | 'safe_root' | None.

    Two branches returned bare True — truthy, so the gate still blocked, but a
    security classifier returning an undocumented type is how the next caller
    that switches on the value gets it wrong.
    """
    from agent.file_safety import _classify_write_denial

    assert _classify_write_denial(os.path.join(tmpdir_path, ".env")) == "credential"
    assert _classify_write_denial(os.path.join(tmpdir_path, "ok.txt")) is None
