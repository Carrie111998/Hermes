"""Success-only durable provider recovery publication tests."""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.provider_recovery import (
    CREDENTIAL_GENERATION_ENV,
    publish_successful_live_provider_request,
)
from hermes_cli import kanban_db as kb
from run_agent import AIAgent


_SECRET = "do-not-persist-api-key-or-response"


def _tool_defs() -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "test tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]


@pytest.fixture
def agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs()),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            model="test/model",
            provider="openrouter",
            api_key=_SECRET,
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        instance.client = MagicMock()
        instance.session_id = "session-r2a"
        instance._cached_system_prompt = "You are helpful."
        instance._use_prompt_caching = False
        instance.tool_delay = 0
        instance.compression_enabled = False
        instance.save_trajectories = False
        # One allowed attempt is required to exercise the real transport seam;
        # zero means the production retry loop deliberately skips transport.
        instance._api_max_retries = 1
        return instance


@pytest.fixture
def recovery_db(tmp_path: Path):
    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    conn.close()
    try:
        yield db_path
    finally:
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _set_supervised_env(monkeypatch, db_path: Path, *, profile="alpha", generation="7"):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setenv("HERMES_PROFILE", profile)
    monkeypatch.setenv(CREDENTIAL_GENERATION_ENV, generation)


def _response(content: str = "provider response"):
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _event_rows(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT * FROM provider_recovery_events ORDER BY id"
        ).fetchall()
    finally:
        conn.close()


def _run(agent):
    def _fake_transport(api_kwargs):
        return agent.client.chat.completions.create(**api_kwargs)

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=_fake_transport),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("hello")


def test_real_normalized_response_publishes_exactly_one_safe_event(
    agent, recovery_db, monkeypatch
):
    _set_supervised_env(monkeypatch, recovery_db)
    agent.client.chat.completions.create.return_value = _response(_SECRET)

    before = int(time.time())
    result = _run(agent)
    after = int(time.time())

    assert agent.client.chat.completions.create.call_count == 1
    assert result["completed"] is True
    assert result["final_response"] == _SECRET
    rows = _event_rows(recovery_db)
    assert len(rows) == 1
    row = rows[0]
    assert row["profile"] == "alpha"
    assert row["provider"] == "openrouter"
    assert row["credential_generation"] == 7
    assert row["proof_kind"] == "live_request_succeeded"
    assert before <= row["provider_observed_at"] <= after
    assert row["stable_proof_id"].startswith("provider-request:")
    assert row["consumed_at"] is None if "consumed_at" in row.keys() else True
    assert _SECRET not in " ".join(str(value) for value in row)


def test_provider_failure_no_response_and_retry_after_publish_nothing(
    agent, recovery_db, monkeypatch
):
    _set_supervised_env(monkeypatch, recovery_db)

    class RateLimitWithRetryAfter(RuntimeError):
        status_code = 429
        response = SimpleNamespace(
            status_code=429,
            headers={"Retry-After": _SECRET},
        )

    for outcome in (None, RateLimitWithRetryAfter("rate limited")):
        if isinstance(outcome, BaseException):
            agent.client.chat.completions.create.side_effect = outcome
            agent.client.chat.completions.create.return_value = None
        else:
            agent.client.chat.completions.create.side_effect = None
            agent.client.chat.completions.create.return_value = outcome
        _run(agent)

    assert _event_rows(recovery_db) == []


def test_synthetic_post_api_request_hook_cannot_publish(
    recovery_db, monkeypatch
):
    _set_supervised_env(monkeypatch, recovery_db)
    from hermes_cli.plugins import invoke_hook

    invoke_hook(
        "post_api_request",
        session_id="session-r2a",
        api_request_id="request-r2a",
        provider="openrouter",
        response={"content": _SECRET},
        usage={"secret": _SECRET},
    )

    assert _event_rows(recovery_db) == []


def test_direct_publication_is_exact_and_idempotent(recovery_db, monkeypatch):
    _set_supervised_env(monkeypatch, recovery_db)
    kwargs = {
        "provider": "open-router",
        "request_id": "request-42",
        "session_id": "session-42",
        "provider_observed_at": 1_900_000_000,
    }

    assert publish_successful_live_provider_request(**kwargs) is True
    assert publish_successful_live_provider_request(**kwargs) is True

    rows = _event_rows(recovery_db)
    assert len(rows) == 1
    assert dict(rows[0]) | {} == dict(rows[0])
    assert rows[0]["profile"] == "alpha"
    assert rows[0]["provider"] == "openrouter"
    assert rows[0]["credential_generation"] == 7
    assert rows[0]["provider_observed_at"] == 1_900_000_000


@pytest.mark.parametrize(
    ("db_value", "profile", "generation"),
    [
        (None, "alpha", "7"),
        ("relative.db", "alpha", "7"),
        ("valid", None, "7"),
        ("valid", " Alpha ", "7"),
        ("valid", "alpha", None),
        ("valid", "alpha", "0"),
        ("valid", "alpha", "+7"),
        ("valid", "alpha", "not-an-int"),
    ],
)
def test_missing_malformed_or_unsupervised_scope_publishes_nothing(
    recovery_db, monkeypatch, db_value, profile, generation
):
    for name in ("HERMES_KANBAN_DB", "HERMES_PROFILE", CREDENTIAL_GENERATION_ENV):
        monkeypatch.delenv(name, raising=False)
    if db_value is not None:
        monkeypatch.setenv(
            "HERMES_KANBAN_DB",
            str(recovery_db) if db_value == "valid" else db_value,
        )
    if profile is not None:
        monkeypatch.setenv("HERMES_PROFILE", profile)
    if generation is not None:
        monkeypatch.setenv(CREDENTIAL_GENERATION_ENV, generation)

    assert publish_successful_live_provider_request(
        provider="openrouter",
        request_id="request-42",
        session_id="session-42",
        provider_observed_at=1_900_000_000,
    ) is False
    assert _event_rows(recovery_db) == []


def test_database_failure_is_bounded_non_secret_and_fail_open(
    agent, recovery_db, monkeypatch, caplog
):
    _set_supervised_env(monkeypatch, recovery_db)
    agent.client.chat.completions.create.return_value = _response("successful result")
    caplog.set_level(logging.WARNING, logger="agent.provider_recovery")

    with patch(
        "agent.provider_recovery._open_recovery_db",
        side_effect=sqlite3.OperationalError(_SECRET),
    ):
        result = _run(agent)

    assert result["completed"] is True
    assert result["final_response"] == "successful result"
    assert _event_rows(recovery_db) == []
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "agent.provider_recovery"
    ]
    assert messages == [
        "Provider recovery proof publication failed (OperationalError)"
    ]
    assert _SECRET not in caplog.text
