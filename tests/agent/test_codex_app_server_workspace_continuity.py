"""Workspace-scoped continuity and explicit-cwd regressions for Codex.

These tests use the real AIAgent + SessionDB integration and a protocol fake.
No Codex subprocess, network request, or timing wait is involved.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import agent.transports.codex_app_server_session as session_mod
from agent.codex_runtime import (
    _CODEX_THREAD_MAP_KEY,
    _CODEX_THREAD_MAP_MAX_ENTRIES,
    _persist_codex_workspace_thread,
)
from agent.transports.codex_app_server import CodexAppServerError
from agent.transports.codex_app_server_session import (
    CodexAppServerSession,
    TurnResult,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_state import SessionDB
from run_agent import AIAgent


class _ProtocolClient:
    instances: list["_ProtocolClient"] = []
    stale_resume_attempts = 0
    sequence = 0

    def __init__(self, **_kwargs) -> None:
        type(self).instances.append(self)
        self.requests: list[tuple[str, dict]] = []
        self.closed = False
        self.cwd = _kwargs.get("cwd")

    def initialize(self, **_kwargs):
        return {}

    def request(self, method, params=None, timeout=30):
        params = params or {}
        self.requests.append((method, params))
        if method == "thread/resume":
            if type(self).stale_resume_attempts:
                type(self).stale_resume_attempts -= 1
                raise CodexAppServerError(
                    code=-32600,
                    message="thread not found",
                )
            return {"thread": {"id": params["threadId"]}}
        if method == "thread/start":
            type(self).sequence += 1
            return {"thread": {"id": f"opaque-{type(self).sequence}"}}
        raise AssertionError(f"unexpected protocol method: {method}")

    def close(self):
        self.closed = True

    def stderr_tail(self, _n=20):
        return []


@pytest.fixture
def protocol_fake(monkeypatch):
    _ProtocolClient.instances = []
    _ProtocolClient.stale_resume_attempts = 0
    _ProtocolClient.sequence = 0
    turn_inputs: list[tuple[str, str]] = []

    monkeypatch.setattr(
        session_mod,
        "CodexAppServerClient",
        _ProtocolClient,
    )

    def fake_run_turn(self, user_input, **_kwargs):
        turn_inputs.append((self._cwd, user_input))
        return TurnResult(
            final_text="done",
            projected_messages=[{"role": "assistant", "content": "done"}],
            thread_id=self._thread_id,
            turn_id="opaque-turn",
        )

    monkeypatch.setattr(CodexAppServerSession, "run_turn", fake_run_turn)
    return turn_inputs


def _make_agent(db: SessionDB, session_id: str, cwd: Path) -> AIAgent:
    agent = AIAgent(
        api_key="stub",
        base_url="https://stub.invalid",
        provider="openai",
        api_mode="codex_app_server",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        session_db=db,
        session_id=session_id,
    )
    agent.session_cwd = str(cwd)
    return agent


def _run(agent: AIAgent, message: str):
    with patch.object(agent, "_spawn_background_review", return_value=None):
        return agent.run_conversation(message)


def _mapping(db: SessionDB, session_id: str) -> dict:
    value = db.get_session_model_config_value(
        session_id, _CODEX_THREAD_MAP_KEY, {}
    )
    assert isinstance(value, dict)
    return value


def _close_codex(agent: AIAgent) -> None:
    session = getattr(agent, "_codex_session", None)
    if session is not None:
        session.close()
        agent._codex_session = None


def test_first_start_persists_and_independent_agent_resumes(
    tmp_path, protocol_fake
):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = SessionDB(tmp_path / "state.db")
    session_id = "same-hermes-session"

    first = _make_agent(db, session_id, repo)
    _run(first, "first")

    canonical = str(repo.resolve())
    stored = _mapping(db, session_id)
    assert stored["version"] == 1
    assert canonical in stored["threads"]
    assert [_ProtocolClient.instances[0].requests[0][0]] == ["thread/start"]
    _close_codex(first)

    second = _make_agent(db, session_id, repo)
    _run(second, "second")

    second_requests = _ProtocolClient.instances[1].requests
    assert [method for method, _ in second_requests] == ["thread/resume"]
    assert second_requests[0][1]["cwd"] == canonical
    assert "threadId" in second_requests[0][1]
    _close_codex(second)
    db.close()


def test_stale_resume_replaces_stored_workspace_thread(
    tmp_path, protocol_fake
):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = SessionDB(tmp_path / "state.db")
    session_id = "stale-hermes-session"

    first = _make_agent(db, session_id, repo)
    _run(first, "first")
    canonical = str(repo.resolve())
    before = _mapping(db, session_id)["threads"][canonical]
    _close_codex(first)

    _ProtocolClient.stale_resume_attempts = 1
    second = _make_agent(db, session_id, repo)
    _run(second, "second")

    methods = [method for method, _ in _ProtocolClient.instances[1].requests]
    after = _mapping(db, session_id)["threads"][canonical]
    assert methods == ["thread/resume", "thread/start"]
    assert after != before
    _close_codex(second)
    db.close()


def test_different_hermes_session_does_not_reuse_workspace_mapping(
    tmp_path, protocol_fake
):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = SessionDB(tmp_path / "state.db")

    first = _make_agent(db, "hermes-session-one", repo)
    _run(first, "first")
    _close_codex(first)

    second = _make_agent(db, "hermes-session-two", repo)
    _run(second, "second")

    assert _ProtocolClient.instances[1].requests[0][0] == "thread/start"
    assert _mapping(db, "hermes-session-one")["threads"]
    assert _mapping(db, "hermes-session-two")["threads"]
    _close_codex(second)
    db.close()


def test_reset_same_agent_retires_thread_and_new_session_starts_fresh(
    tmp_path, protocol_fake
):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = SessionDB(tmp_path / "state.db")
    agent = _make_agent(db, "before-reset", repo)

    _run(agent, "first")
    first_client = _ProtocolClient.instances[0]
    agent.session_id = "after-reset"
    agent._session_db_created = False
    agent.reset_session_state()
    _run(agent, "second")

    assert first_client.closed is True
    assert _ProtocolClient.instances[1].requests[0][0] == "thread/start"
    assert _mapping(db, "before-reset")["threads"]
    assert _mapping(db, "after-reset")["threads"]
    _close_codex(agent)
    db.close()


def test_two_marked_repositories_get_distinct_mappings_and_strip_marker(
    tmp_path, protocol_fake
):
    root = tmp_path / "repos"
    repo_a = root / "a"
    repo_b = root / "b"
    repo_a.mkdir(parents=True)
    repo_b.mkdir()
    db = SessionDB(tmp_path / "state.db")
    session_id = "multi-workspace-session"
    agent = _make_agent(db, session_id, root)
    agent.codex_app_server_require_explicit_cwd = True
    agent.codex_app_server_workspace_roots = [str(root)]

    first_message = f"prefix [HERMES_RUNTIME_CWD={repo_a}] suffix"
    _run(agent, first_message)
    _run(agent, f"[HERMES_RUNTIME_CWD={repo_b}]\nsecond")

    mapping = _mapping(db, session_id)
    assert set(mapping["threads"]) == {
        str(repo_a.resolve()),
        str(repo_b.resolve()),
    }
    assert mapping["threads"][str(repo_a.resolve())] != mapping["threads"][
        str(repo_b.resolve())
    ]
    assert protocol_fake[0] == (str(repo_a.resolve()), "prefix  suffix")
    assert protocol_fake[1] == (str(repo_b.resolve()), "\nsecond")
    assert [client.cwd for client in _ProtocolClient.instances] == [
        str(repo_a.resolve()),
        str(repo_b.resolve()),
    ]
    assert all(
        client.requests[0][0] == "thread/start"
        for client in _ProtocolClient.instances
    )
    _close_codex(agent)
    db.close()


@pytest.mark.parametrize(
    "case",
    ["missing", "relative", "outside", "nonexistent", "malformed"],
)
def test_invalid_or_required_missing_marker_fails_before_spawn(
    tmp_path, protocol_fake, case
):
    root = tmp_path / "allowed"
    repo = root / "repo"
    outside = tmp_path / "outside"
    repo.mkdir(parents=True)
    outside.mkdir()
    db = SessionDB(tmp_path / f"{case}.db")
    agent = _make_agent(db, f"cwd-{case}", root)
    agent.codex_app_server_require_explicit_cwd = True
    agent.codex_app_server_workspace_roots = [str(root)]

    messages = {
        "missing": "no structured marker",
        "relative": "[HERMES_RUNTIME_CWD=relative/repo] work",
        "outside": f"[HERMES_RUNTIME_CWD={outside}] work",
        "nonexistent": (
            f"[HERMES_RUNTIME_CWD={root / 'does-not-exist'}] work"
        ),
        "malformed": f"[HERMES_RUNTIME_CWD={repo} work",
    }
    result = _run(agent, messages[case])

    assert result["final_response"] == ""
    assert result["completed"] is False
    assert result["partial"] is True
    assert result["error"]
    assert _ProtocolClient.instances == []
    db.close()


def test_default_config_keeps_implicit_cwd_behavior(tmp_path, protocol_fake):
    repo = tmp_path / "repo"
    repo.mkdir()
    db = SessionDB(tmp_path / "state.db")
    agent = _make_agent(db, "backward-compatible", repo)

    result = _run(agent, "plain handoff")

    assert DEFAULT_CONFIG["agent"][
        "codex_app_server_require_explicit_cwd"
    ] is False
    assert DEFAULT_CONFIG["agent"]["codex_app_server_workspace_roots"] == []
    assert result["completed"] is True
    assert protocol_fake == [(str(repo.resolve()), "plain handoff")]
    _close_codex(agent)
    db.close()


def test_workspace_mapping_is_bounded(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    session_id = "bounded-map"
    db.create_session(session_id, source="test")
    agent = type("Agent", (), {})()
    agent._session_db = db
    agent.session_id = session_id
    agent._persist_disabled = False

    for index in range(_CODEX_THREAD_MAP_MAX_ENTRIES + 1):
        assert _persist_codex_workspace_thread(
            agent,
            str(tmp_path / f"repo-{index}"),
            f"opaque-{index}",
        )

    mapping = _mapping(db, session_id)
    assert len(mapping["threads"]) == _CODEX_THREAD_MAP_MAX_ENTRIES
    assert len(mapping["order"]) == _CODEX_THREAD_MAP_MAX_ENTRIES
    db.close()
