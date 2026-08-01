"""Tests for acp_adapter.session — SessionManager and SessionState."""

import contextlib
import io
import json
import time
from types import SimpleNamespace
import pytest
from unittest.mock import MagicMock, patch

from acp_adapter import session as acp_session
from acp_adapter.session import SessionManager, SessionState
from hermes_state import SessionDB


def _mock_agent():
    return MagicMock(name="MockAIAgent")


@pytest.fixture()
def manager():
    """SessionManager with a mock agent factory (avoids needing API keys)."""
    return SessionManager(agent_factory=_mock_agent)


# ---------------------------------------------------------------------------
# create / get
# ---------------------------------------------------------------------------


class TestCreateSession:
    def test_create_session_returns_state(self, manager):
        state = manager.create_session(cwd="/tmp/work")
        assert isinstance(state, SessionState)
        assert state.cwd == "/tmp/work"
        assert state.session_id
        assert state.history == []
        assert state.agent is not None



    def test_register_task_cwd_translates_windows_drive_for_wsl_tools(self, monkeypatch):
        captured = {}

        def fake_register_task_env_overrides(task_id, overrides):
            captured["task_id"] = task_id
            captured["overrides"] = overrides

        monkeypatch.setattr("hermes_constants._wsl_detected", True)
        monkeypatch.setattr(
            "tools.terminal_tool.register_task_env_overrides",
            fake_register_task_env_overrides,
        )

        acp_session._register_task_cwd("session-1", r"E:\Projects\AI\paperclip")

        assert captured == {
            "task_id": "session-1",
            "overrides": {"cwd": "/mnt/e/Projects/AI/paperclip"},
        }


    def test_get_session(self, manager):
        state = manager.create_session()
        fetched = manager.get_session(state.session_id)
        assert fetched is state


    def test_make_agent_stamps_session_cwd_for_codex_runtime(self, monkeypatch):
        class FakeAgent:
            model = "fake-model"

            def __init__(self, **kwargs):
                self.kwargs = kwargs

        monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
        monkeypatch.setattr(
            "acp_adapter.session.load_config",
            lambda: {
                "model": {
                    "default": "fake-model",
                    "provider": "fake-provider",
                },
                "mcp_servers": {},
            },
            raising=False,
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {
                "model": {
                    "default": "fake-model",
                    "provider": "fake-provider",
                },
                "mcp_servers": {},
            },
        )
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            lambda requested=None: {
                "provider": requested,
                "api_mode": "codex_app_server",
                "base_url": "https://example.invalid",
                "api_key": "test-key",
            },
        )
        monkeypatch.setattr("acp_adapter.session._register_task_cwd", lambda task_id, cwd: None)

        state = SessionManager(db=None).create_session(cwd="/tmp/project")

        assert state.agent.session_cwd == "/tmp/project"




# ---------------------------------------------------------------------------
# WSL cwd translation
# ---------------------------------------------------------------------------


class TestWslCwdTranslation:
    def test_translate_acp_cwd_converts_windows_drive_path_when_wsl(self, monkeypatch):
        monkeypatch.setattr("hermes_constants._wsl_detected", True)

        assert acp_session._translate_acp_cwd(r"E:\Projects\AI\paperclip") == "/mnt/e/Projects/AI/paperclip"





    def test_fork_session_stores_translated_cwd_on_wsl(self, manager, monkeypatch):
        monkeypatch.setattr("hermes_constants._wsl_detected", True)
        original = manager.create_session(cwd="/tmp/base")

        forked = manager.fork_session(original.session_id, cwd=r"D:\work\project")

        assert forked is not None
        assert forked.cwd == "/mnt/d/work/project"

    def test_update_cwd_stores_translated_cwd_on_wsl(self, manager, monkeypatch):
        monkeypatch.setattr("hermes_constants._wsl_detected", True)
        state = manager.create_session(cwd="/tmp/old")

        updated = manager.update_cwd(state.session_id, cwd=r"C:\Users\foo\project")

        assert updated is not None
        assert updated.cwd == "/mnt/c/Users/foo/project"

# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# list / cleanup / remove
# ---------------------------------------------------------------------------


class TestListAndCleanup:
    def test_list_sessions_empty(self, manager):
        assert manager.list_sessions() == []



    def test_save_session_preserves_existing_messages_on_encode_failure(self, manager):
        """Regression for #13675: a bad message in state.history must not
        clobber the previously-persisted transcript.  replace_messages()
        wraps DELETE + INSERT in a single rolled-back-on-exception txn.
        """
        state = manager.create_session()
        state.history.append({"role": "user", "content": "original"})
        manager.save_session(state.session_id)

        # Now swap history with a message whose tool_calls is non-JSON-serializable.
        # _execute_write rolls back; the previously persisted "original" stays.
        state.history = [
            {"role": "user", "content": "replacement"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"bad": object()}],
            },
        ]
        manager.save_session(state.session_id)

        db = manager._get_db()
        messages = db.get_messages_as_conversation(state.session_id)
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert messages[0]["content"] == "original"
        assert isinstance(messages[0].get("timestamp"), (int, float))




    def test_cleanup_clears_all(self, manager):
        s1 = manager.create_session()
        s2 = manager.create_session()
        s1.history.append({"role": "user", "content": "one"})
        s2.history.append({"role": "user", "content": "two"})
        assert len(manager.list_sessions()) == 2
        manager.cleanup()
        assert manager.list_sessions() == []

    def test_remove_session(self, manager):
        state = manager.create_session()
        assert manager.remove_session(state.session_id) is True
        assert manager.get_session(state.session_id) is None
        # Removing again returns False
        assert manager.remove_session(state.session_id) is False


# ---------------------------------------------------------------------------
# persistence — sessions survive process restarts (via SessionDB)
# ---------------------------------------------------------------------------


class TestPersistence:
    """Verify that sessions are persisted to SessionDB and can be restored."""

    # -- cwd persistence: top-level sessions.cwd column ------------------------

    def test_create_persists_top_level_cwd(self, manager):
        """ACP create_session must write cwd to the top-level sessions.cwd column."""
        state = manager.create_session(cwd="/home/user/my-project")
        db = manager._get_db()
        row = db.get_session(state.session_id)
        assert row is not None
        assert row.get("cwd") == "/home/user/my-project", (
            "top-level sessions.cwd must be set on create, not only model_config.cwd"
        )

    def test_update_cwd_persists_top_level_cwd(self, manager):
        """ACP update_cwd must update the top-level sessions.cwd column."""
        state = manager.create_session(cwd="/home/user/project-a")
        manager.update_cwd(state.session_id, "/home/user/project-b")
        db = manager._get_db()
        row = db.get_session(state.session_id)
        assert row.get("cwd") == "/home/user/project-b", (
            "update_cwd must write to top-level sessions.cwd"
        )

    def test_backfill_legacy_acp_row_with_null_cwd(self, manager):
        """Restoring a legacy ACP row (NULL sessions.cwd, valid model_config.cwd)
        must heal the top-level cwd column."""
        db = manager._get_db()
        sid = "legacy-acp-session-1"
        # Simulate a legacy row: top-level cwd is NULL, model_config has cwd.
        db.create_session(
            session_id=sid,
            source="acp",
            model="test-model",
            model_config={"cwd": "/home/user/legacy-project"},
        )
        # Force top-level cwd to NULL to simulate the legacy state.
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET cwd = NULL WHERE id = ?", (sid,)
            )
        )
        # Verify the legacy state.
        row = db.get_session(sid)
        assert row.get("cwd") is None, "precondition: top-level cwd must be NULL"
        mc = json.loads(row["model_config"])
        assert mc.get("cwd") == "/home/user/legacy-project", (
            "precondition: model_config.cwd must be set"
        )

        # Restore the session — this should backfill top-level cwd.
        restored = manager.get_session(sid)
        assert restored is not None
        assert restored.cwd == "/home/user/legacy-project", (
            "restored state must have cwd from model_config"
        )

        # After restore, the top-level cwd should be healed.
        row_after = db.get_session(sid)
        assert row_after.get("cwd") == "/home/user/legacy-project", (
            "legacy row must be healed: top-level cwd backfilled from model_config"
        )

    def test_non_overwrite_existing_top_level_cwd(self, manager):
        """Never overwrite an existing non-empty top-level cwd with model_config.cwd."""
        db = manager._get_db()
        sid = "acp-with-cwd-1"
        # Create a row with BOTH top-level cwd and model_config.cwd set.
        db.create_session(
            session_id=sid,
            source="acp",
            model="test-model",
            cwd="/home/user/real-project",
            model_config={"cwd": "/home/user/real-project"},
        )
        # Now tamper model_config.cwd to a different value (simulating a stale
        # model_config that wasn't updated).
        db._execute_write(
            lambda conn: conn.execute(
                "UPDATE sessions SET model_config = ? WHERE id = ?",
                (json.dumps({"cwd": "/home/user/stale-cwd"}), sid),
            )
        )
        row = db.get_session(sid)
        assert row.get("cwd") == "/home/user/real-project", (
            "precondition: top-level cwd is the real project"
        )
        mc = json.loads(row["model_config"])
        assert mc.get("cwd") == "/home/user/stale-cwd", (
            "precondition: model_config.cwd is stale"
        )

        # Restore — must prefer the existing top-level cwd.
        restored = manager.get_session(sid)
        assert restored is not None
        assert restored.cwd == "/home/user/real-project", (
            "must prefer existing top-level cwd over stale model_config.cwd"
        )
        # Top-level cwd must not be overwritten.
        row_after = db.get_session(sid)
        assert row_after.get("cwd") == "/home/user/real-project", (
            "existing top-level cwd must not be overwritten"
        )

    def test_list_sessions_reads_top_level_cwd(self, manager):
        """list_sessions must return cwd from the top-level column for DB-only rows."""
        db = manager._get_db()
        sid = "acp-dbonly-1"
        db.create_session(
            session_id=sid,
            source="acp",
            model="test-model",
            cwd="/home/user/dbonly-project",
            model_config={"cwd": "/home/user/dbonly-project"},
        )
        # Insert a message so list_sessions includes this row.
        db.append_message(sid, role="user", content="hello from db-only")

        results = manager.list_sessions()
        found = [r for r in results if r["session_id"] == sid]
        assert len(found) == 1, "DB-only ACP session must appear in list_sessions"
        assert found[0]["cwd"] == "/home/user/dbonly-project", (
            "list_sessions must return top-level cwd, not fall back to '.'"
        )

    # -- existing persistence tests -------------------------------------------

    def test_migration_v24_backfills_legacy_acp_cwd(self, tmp_path):
        """Reopening a DB with legacy ACP rows heals sessions.cwd from model_config.

        The migration is version-gated (v24) and scoped to source='acp'. It
        only fills NULL/empty top-level cwd when model_config has a non-empty
        string cwd. Never overwrites an existing cwd.
        """
        import sqlite3 as _sqlite3

        db_path = tmp_path / "legacy_acp.db"

        # Build a v23 DB with a legacy ACP row: NULL top-level cwd, valid
        # model_config.cwd. Also a non-ACP row to prove the migration is scoped.
        raw = _sqlite3.connect(str(db_path))
        raw.executescript(
            """
            CREATE TABLE schema_version (version INTEGER);
            INSERT INTO schema_version VALUES (23);
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, source TEXT, started_at REAL, ended_at REAL,
                message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
                title TEXT, parent_session_id TEXT, model_config TEXT,
                cwd TEXT, git_branch TEXT, git_repo_root TEXT,
                profile_name TEXT
            );
            """
        )
        raw.execute(
            "INSERT INTO sessions (id, source, model_config, cwd, started_at) "
            "VALUES (?, 'acp', ?, NULL, 0)",
            ("acp-legacy-1", json.dumps({"cwd": "/projects/legacy-acp"})),
        )
        raw.execute(
            "INSERT INTO sessions (id, source, model_config, cwd, started_at) "
            "VALUES (?, 'acp', ?, NULL, 0)",
            ("acp-malformed-1", "not-json"),
        )
        raw.execute(
            "INSERT INTO sessions (id, source, model_config, cwd, started_at) "
            "VALUES (?, 'acp', ?, NULL, 0)",
            ("acp-empty-cwd-1", json.dumps({"cwd": "  "})),
        )
        raw.execute(
            "INSERT INTO sessions (id, source, model_config, cwd, started_at) "
            "VALUES (?, 'cli', ?, NULL, 0)",
            ("cli-legacy-1", json.dumps({"cwd": "/projects/legacy-cli"})),
        )
        # Non-empty top-level cwd must NOT be overwritten even if model_config differs.
        raw.execute(
            "INSERT INTO sessions (id, source, model_config, cwd, started_at) "
            "VALUES (?, 'acp', ?, '/projects/real', 0)",
            ("acp-existing-1", json.dumps({"cwd": "/projects/stale"})),
        )
        raw.commit()
        raw.close()

        # Reopen via SessionDB — triggers v24 migration.
        from hermes_state import SessionDB

        db = SessionDB(db_path=db_path)
        try:
            row = db.get_session("acp-legacy-1")
            assert row is not None
            assert row["cwd"] == "/projects/legacy-acp", (
                "v24 migration must backfill top-level cwd from model_config"
            )
            # Malformed JSON is skipped safely.
            r2 = db.get_session("acp-malformed-1")
            assert r2 is not None and r2["cwd"] is None
            # Empty-string cwd in model_config is skipped.
            r3 = db.get_session("acp-empty-cwd-1")
            assert r3 is not None and r3["cwd"] is None
            # Non-ACP rows are untouched.
            r4 = db.get_session("cli-legacy-1")
            assert r4 is not None and r4["cwd"] is None
            # Existing non-empty cwd is NOT overwritten.
            r5 = db.get_session("acp-existing-1")
            assert r5 is not None and r5["cwd"] == "/projects/real"
        finally:
            db.close()

        # Reopening again is idempotent — no further changes, no crash.
        db2 = SessionDB(db_path=db_path)
        try:
            r = db2.get_session("acp-legacy-1")
            assert r is not None and r["cwd"] == "/projects/legacy-acp"
        finally:
            db2.close()

    def test_e2e_acp_session_placed_in_project_tree(self, tmp_path):
        """ACP session with persisted top-level cwd is placed in its project,
        not the Home bucket, by project_tree.build_tree."""
        from tui_gateway.project_tree import build_tree, NO_PROJECT_ID

        workspace = tmp_path / "repo"
        workspace.mkdir()

        db = SessionDB(tmp_path / "state.db")
        mgr = SessionManager(agent_factory=_mock_agent, db=db)
        state = mgr.create_session(cwd=str(workspace))

        # Read the persisted row and shape it for build_tree.
        row = db.get_session(state.session_id)
        assert row is not None
        row = dict(row)
        row["started_at"] = 1000
        row["last_active"] = 1000
        row["title"] = None
        row["preview"] = None
        row["git_branch"] = ""
        row["git_repo_root"] = ""

        project = {
            "id": "p_test",
            "name": "Repo",
            "primary_path": str(workspace),
            "archived": False,
            "folders": [{"path": str(workspace), "is_primary": True}],
        }

        tree = build_tree([project], [row], [], resolve=None, hydrate=True)
        placed = next((p for p in tree["projects"] if p["id"] == "p_test"), None)
        home = next((p for p in tree["projects"] if p["id"] == NO_PROJECT_ID), None)

        assert placed is not None and placed["sessionCount"] == 1, (
            "ACP session must be placed in its project"
        )
        assert home is None or home["sessionCount"] == 0, (
            "ACP session must NOT land in Home"
        )
        db.close()

    def test_only_restores_acp_sessions(self, manager):
        """get_session should not restore non-ACP sessions from DB."""
        db = manager._get_db()
        # Manually create a CLI session in the DB.
        db.create_session(session_id="cli-session-123", source="cli", model="test")
        # Should not be found via ACP SessionManager.
        assert manager.get_session("cli-session-123") is None

    def test_sessions_searchable_via_fts(self, manager):
        """ACP sessions stored in SessionDB are searchable via FTS5."""
        state = manager.create_session()
        state.history.append({"role": "user", "content": "how do I configure nginx"})
        state.history.append({"role": "assistant", "content": "Here is the nginx config..."})
        manager.save_session(state.session_id)

        db = manager._get_db()
        results = db.search_messages("nginx")
        assert len(results) > 0
        session_ids = {r["session_id"] for r in results}
        assert state.session_id in session_ids


    def test_assistant_reasoning_fields_persisted(self, manager):
        """ACP session restore should preserve assistant reasoning context."""
        state = manager.create_session()
        state.history.append({
            "role": "assistant",
            "content": "hello",
            "reasoning": "step-by-step",
            "reasoning_details": [
                {"type": "thinking", "thinking": "first thought"},
            ],
            "codex_reasoning_items": [
                {"type": "reasoning", "id": "rs_123", "encrypted_content": "enc_blob"},
            ],
        })
        manager.save_session(state.session_id)

        with manager._lock:
            del manager._sessions[state.session_id]

        restored = manager.get_session(state.session_id)
        assert restored is not None
        msg = restored.history[0]
        assert isinstance(msg.pop("timestamp", None), (int, float))
        assert restored.history == [{
            "role": "assistant",
            "content": "hello",
            "reasoning": "step-by-step",
            "reasoning_details": [
                {"type": "thinking", "thinking": "first thought"},
            ],
            "codex_reasoning_items": [
                {"type": "reasoning", "id": "rs_123", "encrypted_content": "enc_blob"},
            ],
        }]


    def test_acp_agents_route_human_output_to_stderr(self, tmp_path, monkeypatch):
        """ACP agents must keep stdout clean for JSON-RPC stdio transport."""

        def fake_resolve_runtime_provider(requested=None, **kwargs):
            return {
                "provider": "openrouter",
                "api_mode": "chat_completions",
                "base_url": "https://openrouter.example/v1",
                "api_key": "test-key",
                "command": None,
                "args": [],
            }

        def fake_agent(**kwargs):
            return SimpleNamespace(model=kwargs.get("model"), _print_fn=None)

        monkeypatch.setattr("hermes_cli.config.load_config", lambda: {
            "model": {"provider": "openrouter", "default": "test-model"}
        })
        monkeypatch.setattr(
            "hermes_cli.runtime_provider.resolve_runtime_provider",
            fake_resolve_runtime_provider,
        )
        db = SessionDB(tmp_path / "state.db")

        with patch("run_agent.AIAgent", side_effect=fake_agent):
            manager = SessionManager(db=db)
            state = manager.create_session(cwd="/work")

        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            state.agent._print_fn("ACP noise")

        assert stdout_buf.getvalue() == ""
        assert stderr_buf.getvalue() == "ACP noise\n"
