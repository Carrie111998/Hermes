"""Tests for agent.transports.acp_session_mapping.

Covers the SQLite-backed mapper's CRUD surface and the staleness query.
Each test gets a fresh DB under pytest's ``tmp_path`` so nothing touches the
real ``state.db``.
"""

from __future__ import annotations

import time

import pytest

from agent.transports.acp_session_mapping import (
    ACPSessionBinding,
    SQLiteACPSessionMapper,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _binding(
    *,
    hermes_session_id: str = "hermes-1",
    acp_session_id: str = "acp-1",
    provider: str = "claude",
    last_active_at: float | None = None,
    status: str = "active",
    cwd: str | None = "/tmp/proj",
    model: str | None = None,
    permission_mode: str | None = None,
) -> ACPSessionBinding:
    return ACPSessionBinding(
        hermes_session_id=hermes_session_id,
        acp_session_id=acp_session_id,
        provider=provider,
        cwd=cwd,
        model=model,
        permission_mode=permission_mode,
        last_active_at=last_active_at if last_active_at is not None else time.time(),
        status=status,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bind_and_lookup(tmp_path):
    """A bound Hermes session can be looked back up with its full payload."""
    # Arrange
    mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
    binding = _binding(
        hermes_session_id="h-1",
        acp_session_id="acp-1",
        provider="claude",
        cwd="/home/u/proj",
        model="sonnet",
        permission_mode="acceptEdits",
    )

    # Act
    mapper.bind(binding)
    found = mapper.lookup("h-1")

    # Assert
    assert found is not None
    assert found.hermes_session_id == "h-1"
    assert found.acp_session_id == "acp-1"
    assert found.provider == "claude"
    assert found.cwd == "/home/u/proj"
    assert found.model == "sonnet"
    assert found.permission_mode == "acceptEdits"
    assert found.status == "active"


def test_lookup_by_provider(tmp_path):
    """One Hermes session bound to two providers resolves by provider."""
    # Arrange
    mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
    mapper.bind(_binding(hermes_session_id="h-1", acp_session_id="acp-a", provider="claude"))
    mapper.bind(_binding(hermes_session_id="h-1", acp_session_id="acp-b", provider="codex"))

    # Act
    claude = mapper.lookup("h-1", provider="claude")
    codex = mapper.lookup("h-1", provider="codex")

    # Assert
    assert claude is not None and claude.acp_session_id == "acp-a"
    assert codex is not None and codex.acp_session_id == "acp-b"
    # An unknown provider returns None.
    assert mapper.lookup("h-1", provider="missing") is None


def test_unbind(tmp_path):
    """unbind removes every binding for a Hermes session."""
    # Arrange
    mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
    mapper.bind(_binding(hermes_session_id="h-1", provider="claude"))
    mapper.bind(_binding(hermes_session_id="h-1", provider="codex"))
    mapper.bind(_binding(hermes_session_id="h-2", provider="claude"))

    # Act
    mapper.unbind("h-1")

    # Assert
    assert mapper.lookup("h-1", provider="claude") is None
    assert mapper.lookup("h-1", provider="codex") is None
    # Other sessions are untouched.
    assert mapper.lookup("h-2", provider="claude") is not None


def test_mark_stale(tmp_path):
    """mark_stale flips status to 'stale' without deleting the row."""
    # Arrange
    mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
    mapper.bind(_binding(hermes_session_id="h-1", provider="claude"))
    assert mapper.lookup("h-1").status == "active"

    # Act
    mapper.mark_stale("h-1")

    # Assert
    stale = mapper.lookup("h-1", provider="claude")
    assert stale is not None
    assert stale.status == "stale"


def test_update_activity(tmp_path):
    """update_activity advances last_active_at to ~now."""
    # Arrange
    mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
    old = 1_000_000.0
    mapper.bind(
        _binding(hermes_session_id="h-1", provider="claude", last_active_at=old)
    )
    before = time.time()

    # Act
    mapper.update_activity("h-1")

    # Assert
    found = mapper.lookup("h-1", provider="claude")
    assert found is not None
    assert found.last_active_at > old
    assert found.last_active_at >= before


def test_list_stale(tmp_path):
    """list_stale returns only stale bindings older than the cutoff."""
    # Arrange
    mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")
    # Stale + old → included.
    mapper.bind(
        _binding(
            hermes_session_id="h-old",
            provider="claude",
            last_active_at=100.0,
            status="stale",
        )
    )
    # Stale but recent → excluded by the cutoff.
    mapper.bind(
        _binding(
            hermes_session_id="h-recent",
            provider="claude",
            last_active_at=time.time(),
            status="stale",
        )
    )
    # Old but still active → excluded by status.
    mapper.bind(
        _binding(
            hermes_session_id="h-active",
            provider="claude",
            last_active_at=100.0,
            status="active",
        )
    )

    # Act
    stale = mapper.list_stale(older_than=1_000.0)

    # Assert
    assert len(stale) == 1
    assert stale[0].hermes_session_id == "h-old"


def test_lookup_returns_none_when_empty(tmp_path):
    """Lookup on a never-populated store returns None."""
    # Arrange
    mapper = SQLiteACPSessionMapper(db_path=tmp_path / "state.db")

    # Act / Assert
    assert mapper.lookup("never-bound") is None
    assert mapper.lookup("never-bound", provider="claude") is None
