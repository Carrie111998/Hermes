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


@pytest.mark.parametrize("failure_stage", ["db_unavailable", "atomic_write"])
def test_fork_session_fails_closed_without_publishing_child(
    tmp_path, monkeypatch, failure_stage,
):
    """A failed durable fork boundary cannot publish memory or an orphan child."""
    db = SessionDB(tmp_path / f"fork-{failure_stage}.db")
    manager = SessionManager(
        agent_factory=lambda: SimpleNamespace(model="test-model"),
        db=db,
    )
    original = manager.create_session(cwd="/tmp/base")
    original.history.append({"role": "user", "content": "preserve parent"})
    manager.save_session(original.session_id)
    original_ids = {
        row["id"] for row in db.list_sessions_rich(source="acp", limit=1000)
    }
    registered = []
    monkeypatch.setattr(
        acp_session,
        "_register_task_cwd",
        lambda task_id, cwd: registered.append((task_id, cwd)),
    )

    if failure_stage == "db_unavailable":
        monkeypatch.setattr(manager, "_get_db", MagicMock(return_value=None))
    else:
        monkeypatch.setattr(
            db,
            "create_acp_fork",
            MagicMock(side_effect=RuntimeError("atomic fork failed")),
            raising=False,
        )

    forked = manager.fork_session(original.session_id, cwd="/tmp/fork")

    assert forked is None
    assert set(manager._sessions) == {original.session_id}
    assert registered == []
    assert {
        row["id"] for row in db.list_sessions_rich(source="acp", limit=1000)
    } == original_ids
    db.close()


@pytest.mark.parametrize("failure_stage", ["agent_construction", "atomic_write"])
def test_production_fork_failure_never_registers_child_cwd(
    tmp_path, monkeypatch, failure_stage,
):
    import run_agent

    db = SessionDB(tmp_path / "production-fork-failure.db")
    manager = SessionManager(agent_factory=_mock_agent, db=db)
    parent = manager.create_session(cwd="/tmp/parent")
    manager._agent_factory = None
    registered = []
    cleared = []
    monkeypatch.setattr(
        acp_session,
        "_register_task_cwd",
        lambda session_id, cwd: registered.append((session_id, cwd)),
    )
    monkeypatch.setattr(
        acp_session,
        "_clear_task_cwd",
        lambda session_id: cleared.append(session_id),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"model": "test"})
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested=None: {},
    )
    monkeypatch.setattr(
        "hermes_cli.mcp_startup.ensure_mcp_discovery_before_agent_build",
        lambda **kwargs: None,
    )
    if failure_stage == "agent_construction":
        monkeypatch.setattr(
            run_agent,
            "AIAgent",
            MagicMock(side_effect=RuntimeError("agent construction failed")),
        )
    else:
        monkeypatch.setattr(
            run_agent,
            "AIAgent",
            MagicMock(return_value=SimpleNamespace()),
        )
        monkeypatch.setattr(
            db,
            "create_acp_fork",
            MagicMock(side_effect=RuntimeError("atomic write failed")),
        )

    forked = manager.fork_session(parent.session_id, cwd="/tmp/child")

    assert forked is None
    assert registered == []
    assert cleared == []
    assert set(manager._sessions) == {parent.session_id}
    db.close()


def test_create_acp_fork_rolls_back_child_when_lifecycle_step_fails(
    tmp_path, monkeypatch,
):
    """Child row and messages roll back with lifecycle derivation in one transaction."""
    db = SessionDB(tmp_path / "fork-atomic-rollback.db")
    parent_id = db.create_session("parent", "acp", model="test-model")
    db.replace_messages(parent_id, [{"role": "user", "content": "parent"}])
    monkeypatch.setattr(
        db,
        "_clone_codex_responses_compaction_lifecycle_on_connection",
        MagicMock(side_effect=RuntimeError("lifecycle failed")),
        raising=False,
    )

    with pytest.raises(RuntimeError, match="lifecycle failed"):
        db.create_acp_fork(
            parent_session_id=parent_id,
            child_session_id="rejected-child",
            model="test-model",
            cwd="/tmp/fork",
            messages=[{"role": "user", "content": "parent"}],
        )

    assert db.get_session("rejected-child") is None
    assert db.get_messages("rejected-child") == []
    db.close()


def test_fork_session_registers_child_cwd_only_after_durable_publication(
    tmp_path, monkeypatch,
):
    """Successful ACP CWD publication observes an already committed child."""
    db = SessionDB(tmp_path / "fork-post-commit-cwd.db")
    manager = SessionManager(
        agent_factory=lambda: SimpleNamespace(model="test-model"),
        db=db,
    )
    parent = manager.create_session(cwd="/tmp/parent")
    parent.history.append({"role": "user", "content": "durable first"})
    manager.save_session(parent.session_id)
    events = []

    real_create_acp_fork = db.create_acp_fork

    def _record_durable_publication(**kwargs):
        result = real_create_acp_fork(**kwargs)
        child_id = kwargs["child_session_id"]
        assert db.get_session(child_id) is not None
        persisted = db.get_messages_as_conversation(child_id)
        assert [(message["role"], message["content"]) for message in persisted] == [
            ("user", "durable first")
        ]
        events.append(("durable", child_id))
        return result

    def _record_cwd_publication(task_id, cwd):
        assert db.get_session(task_id) is not None
        events.append(("cwd", task_id, cwd))

    monkeypatch.setattr(db, "create_acp_fork", _record_durable_publication)
    monkeypatch.setattr(acp_session, "_register_task_cwd", _record_cwd_publication)

    child = manager.fork_session(parent.session_id, cwd="/tmp/child")

    assert child is not None
    assert events == [
        ("durable", child.session_id),
        ("cwd", child.session_id, "/tmp/child"),
    ]
    assert manager._sessions[child.session_id] is child
    db.close()


def test_fork_cwd_exception_keeps_committed_child(tmp_path, monkeypatch):
    db = SessionDB(tmp_path / "fork-cwd-exception.db")
    manager = SessionManager(
        agent_factory=lambda: SimpleNamespace(model="test-model"), db=db
    )
    parent = manager.create_session(cwd="/tmp/parent")
    parent.history.append({"role": "user", "content": "committed"})
    manager.save_session(parent.session_id)

    def _raise_after_commit(_task_id, _cwd):
        raise RuntimeError("cwd registration failed")

    monkeypatch.setattr(acp_session, "_register_task_cwd", _raise_after_commit)
    child = manager.fork_session(parent.session_id, cwd="/tmp/child")

    assert child is not None
    assert db.get_session(child.session_id) is not None
    assert manager._sessions[child.session_id] is child
    db.close()


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
