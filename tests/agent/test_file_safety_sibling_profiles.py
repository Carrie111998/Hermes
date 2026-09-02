"""Tests for sibling-profile credential isolation in agent/file_safety.

The read and write guards previously enumerated only the ACTIVE
``HERMES_HOME`` and the Hermes root, so a session running under one
profile could read a *sibling* profile's credential stores
(``auth.json``, ``.anthropic_oauth.json``, ``mcp-tokens/``) and write a
sibling's control files (``.env``, ``config.yaml``, ``auth.json``).
A prompt-injection reaching ``read_file``/``write_file`` in the "work"
profile could exfiltrate or corrupt the "personal" profile's credentials.

These tests verify that sibling profiles under ``<root>/profiles/*`` get
the same credential read-deny and control-file write-deny as the active
profile and root, that active-profile behavior is unchanged, and that the
guard degrades safely when no ``profiles/`` directory exists.

Same defense-in-depth caveat as the rest of this module: the terminal
tool runs as the same OS user and can bypass these guards.
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers — fake Hermes root with an active profile and one sibling,
# monkeypatching the resolver helpers so the guards see the test layout.
# ---------------------------------------------------------------------------


def _create(base: Path, rel: str) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("dummy", encoding="utf-8")
    return p


@pytest.fixture
def fake_hermes(tmp_path, monkeypatch):
    """Build a fake Hermes layout:

        <tmp>/fake-hermes/
          auth.json                       # root credential store
          profiles/
            work/                         # ACTIVE profile
              sessions/log.txt
              skills/my-skill/
            personal/                     # sibling profile
              auth.json
              .anthropic_oauth.json
              .env
              config.yaml
              mcp-tokens/github.json
              pairing/req.json
              skills/.hub/index.json
    """
    import agent.file_safety as fs

    root = tmp_path / "fake-hermes"
    _create(root, "auth.json")

    work = root / "profiles" / "work"
    _create(work, "sessions/log.txt")
    (work / "skills" / "my-skill").mkdir(parents=True)

    personal = root / "profiles" / "personal"
    for rel in (
        "auth.json",
        ".anthropic_oauth.json",
        ".env",
        "config.yaml",
        "mcp-tokens/github.json",
        "pairing/req.json",
        "skills/.hub/index.json",
    ):
        _create(personal, rel)

    monkeypatch.setattr(fs, "_hermes_home_path", lambda: work)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)
    return root


# ---------------------------------------------------------------------------
# Sibling reads are denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "auth.json",
        ".anthropic_oauth.json",
        "mcp-tokens/github.json",
        "skills/.hub/index.json",
    ],
)
def test_sibling_credential_reads_blocked(fake_hermes, rel):
    from agent.file_safety import get_read_block_error

    target = fake_hermes / "profiles" / "personal" / rel
    assert get_read_block_error(str(target)) is not None


def test_root_credential_read_still_blocked(fake_hermes):
    """Existing behavior: the root store stays blocked under a profile."""
    from agent.file_safety import get_read_block_error

    assert get_read_block_error(str(fake_hermes / "auth.json")) is not None


def test_own_profile_ordinary_read_still_allowed(fake_hermes):
    from agent.file_safety import get_read_block_error

    own = fake_hermes / "profiles" / "work" / "sessions" / "log.txt"
    assert get_read_block_error(str(own)) is None


# ---------------------------------------------------------------------------
# Sibling control-file writes are denied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".env",
        "config.yaml",
        "auth.json",
        ".anthropic_oauth.json",
        "mcp-tokens/new-token.json",
        "pairing/req.json",
    ],
)
def test_sibling_control_writes_denied(fake_hermes, rel):
    from agent.file_safety import is_write_denied

    target = fake_hermes / "profiles" / "personal" / rel
    assert is_write_denied(str(target))


def test_own_profile_skill_write_still_allowed(fake_hermes):
    """Sibling denies must not leak onto the active profile's own areas."""
    from agent.file_safety import is_write_denied

    own = fake_hermes / "profiles" / "work" / "skills" / "my-skill" / "SKILL.md"
    assert not is_write_denied(str(own))


# ---------------------------------------------------------------------------
# Safe degradation
# ---------------------------------------------------------------------------


def test_no_profiles_dir_degrades_to_previous_behavior(tmp_path, monkeypatch):
    """Without <root>/profiles the guards behave exactly as before and
    never raise into the tool path."""
    import agent.file_safety as fs

    root = tmp_path / "bare-hermes"
    root.mkdir()
    monkeypatch.setattr(fs, "_hermes_home_path", lambda: root)
    monkeypatch.setattr(fs, "_hermes_root_path", lambda: root)
    monkeypatch.delenv("HERMES_WRITE_SAFE_ROOT", raising=False)

    ordinary = root / "notes.txt"
    ordinary.write_text("dummy", encoding="utf-8")
    assert fs.get_read_block_error(str(ordinary)) is None
    assert not fs.is_write_denied(str(ordinary))
