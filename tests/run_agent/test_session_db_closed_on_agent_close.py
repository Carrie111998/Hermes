"""Regression: AIAgent.close() must release an owned SessionDB connection.

Observed in the Desktop/`serve --isolated` backend (#fd-leak): every profile-
scoped agent build hands the agent a dedicated ``SessionDB(db_path=.../state.db)``
handle, but ``AIAgent.close()`` only finalized the session ROW (``end_session``)
and never closed the SQLite CONNECTION. After ~2 days the backend hit the
1024-fd soft limit (``OSError: [Errno 24] Too many open files``) and stopped
doing new work.

Contract: after ``agent.close()``, no file descriptor may remain open on an
OWNED session database (state.db or its WAL). Borrowed/shared handles (the
CLI/gateway long-lived SessionDB) must NOT be closed by the agent.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

SESSION_ID = "test-session-db-close"


def _open_fds_to(db_path: Path) -> int:
    """Count this process's open fds on ``db_path`` (including its WAL)."""
    target = str(db_path.resolve())
    count = 0
    try:
        for fd in os.listdir("/proc/self/fd"):
            try:
                link = os.readlink(f"/proc/self/fd/{fd}")
            except OSError:
                continue
            if link == target or link.startswith(target):
                count += 1
    except FileNotFoundError:
        pass
    return count


def _make_agent(session_db=None, *, owns=False):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=session_db,
            session_id=SESSION_ID,
            skip_context_files=True,
            skip_memory=True,
        )
    if owns:
        # Mirrors tui_gateway.server._make_agent, which marks agents handed a
        # dedicated profile-scoped handle this way.
        agent._owns_session_db = True
    agent._ensure_db_session()
    return agent


def test_agent_close_releases_owned_session_db_connection():
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            db.create_session(SESSION_ID, "cli")
            agent = _make_agent(db, owns=True)

            # The write connection must actually be open before close(), or the
            # assertion proves nothing.
            agent._session_db.flush_token_counts()
            assert _open_fds_to(db_path) > 0

            agent.close()

            assert _open_fds_to(db_path) == 0
        finally:
            db.close()


def test_agent_close_does_not_close_borrowed_session_db():
    """The CLI/gateway pass a long-lived shared handle; close() must not release it."""
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "state.db"
        db = SessionDB(db_path=db_path)
        try:
            db.create_session(SESSION_ID, "cli")
            agent = _make_agent(db, owns=False)

            agent._session_db.flush_token_counts()
            assert _open_fds_to(db_path) > 0

            agent.close()

            # Borrowed handle stays open — the process owner keeps using it.
            assert _open_fds_to(db_path) > 0
        finally:
            db.close()


def test_agent_close_releases_recall_fallback_session_db():
    """The recall fallback self-creates a SessionDB; close() must release it.

    This path sets ``_owns_session_db`` inside real production code
    (``AIAgent._get_session_db_for_recall``), so it exercises the full
    flag + close machinery end-to-end.
    """
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "state.db"
        # Seed the canonical default db (no path) so the fallback resolves here.
        seed = SessionDB(db_path=db_path)
        seed.create_session(SESSION_ID, "cli")
        seed.close()

        agent = _make_agent(None)
        # Forces the recall fallback to self-create a SessionDB (resolved to
        # the hermetic test home by the live-DB isolation guard).
        recalled = agent._get_session_db_for_recall()
        assert recalled is not None
        assert getattr(agent, "_owns_session_db", False) is True

        # Resolve the path the fallback actually opened and confirm it leaks today.
        target = Path(recalled.db_path)
        assert _open_fds_to(target) > 0

        agent.close()

        assert _open_fds_to(target) == 0
