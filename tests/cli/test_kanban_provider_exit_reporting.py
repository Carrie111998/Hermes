"""Producer-side tests for Kanban provider exit dispositions."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli
from agent.conversation_loop import (
    _billing_failure_result,
    _content_policy_blocked_result,
    _kanban_safety_refusal_blocks_fallback,
)
from agent.error_classifier import ClassifiedError, FailoverReason
from hermes_cli import kanban_db as kb


def _prepare_isolated_kanban_home(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    db_path = home / "kanban-test.db"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert kb.kanban_db_path().resolve() == db_path.resolve()
    assert kb.kanban_db_path().resolve().is_relative_to(tmp_path.resolve())
    probe = sqlite3.connect(db_path)
    try:
        attached = Path(probe.execute("PRAGMA database_list").fetchone()[2]).resolve()
        assert attached == db_path.resolve()
        assert attached.is_relative_to(tmp_path.resolve())
    finally:
        probe.close()
    kb.init_db()
    return home, db_path


def _assert_connection_is_isolated(conn, db_path, tmp_path):
    attached = Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    assert attached == db_path.resolve()
    assert attached.is_relative_to(tmp_path.resolve())


def _recorded_disposition(conn, run_id):
    envelope = kb._provider_exit_for_run(conn, run_id)
    assert envelope is not None
    return envelope["disposition"]


@pytest.mark.parametrize(
    ("status_code", "reason", "retryable", "expected"),
    [
        (401, "auth", False, "terminal"),
        (402, "billing", False, "terminal"),
        (403, "auth_permanent", False, "terminal"),
        (404, "model_not_found", False, "terminal"),
        (429, "rate_limit", True, "transient"),
        (503, "overloaded", True, "transient"),
        (None, "timeout", True, "transient"),
        (400, "content_policy_blocked", False, "safety_refusal"),
    ],
)
def test_provider_exit_disposition_uses_structured_provider_semantics(
    status_code, reason, retryable, expected
):
    result = {
        "failed": True,
        "provider_failure": {
            "status_code": status_code,
            "classification": reason,
            "retryable": retryable,
            "provider": "provider-a",
            "model": "model-a",
        },
    }
    assert cli._provider_exit_disposition(result) == expected


def test_unknown_failed_result_does_not_replace_protocol_violation_semantics():
    assert cli._provider_exit_disposition({"failed": True, "error": "unknown"}) is None


def test_safety_fallback_suppression_is_owned_worker_only(monkeypatch):
    from agent.delegation_context import non_dispatcher_owned_context

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert _kanban_safety_refusal_blocks_fallback() is False
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_owned")
    assert _kanban_safety_refusal_blocks_fallback() is True
    with non_dispatcher_owned_context():
        assert _kanban_safety_refusal_blocks_fallback() is False


def test_billing_result_exposes_sanitized_provider_descriptor():
    classified = ClassifiedError(
        reason=FailoverReason.billing,
        status_code=402,
        provider="provider-a",
        model="model-a",
        retryable=False,
    )
    result = _billing_failure_result(
        classified=classified,
        summary="payment required",
        messages=[],
        api_call_count=1,
        provider="provider-a",
        base_url="https://example.invalid",
        model="model-a",
    )
    assert result["provider_failure"] == {
        "classification": "billing",
        "status_code": 402,
        "retryable": False,
        "provider": "provider-a",
        "model": "model-a",
    }


def test_safety_result_has_separate_provider_descriptor():
    result = _content_policy_blocked_result(
        [],
        1,
        final_response="refused",
        error_detail="policy",
        provider_failure={
            "classification": "content_policy_blocked",
            "status_code": 400,
            "retryable": False,
            "provider": "provider-a",
            "model": "model-a",
        },
    )
    assert result["provider_failure"]["classification"] == "content_policy_blocked"
    assert cli._provider_exit_disposition(result) == "safety_refusal"


@pytest.fixture
def active_task(tmp_path, monkeypatch):
    _home, db_path = _prepare_isolated_kanban_home(tmp_path, monkeypatch)
    conn = kb.connect()
    _assert_connection_is_isolated(conn, db_path, tmp_path)
    task_id = kb.create_task(conn, title="producer", assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    yield conn, task_id, claimed.current_run_id
    conn.close()


@pytest.fixture
def active_goal_task(tmp_path, monkeypatch):
    _home, db_path = _prepare_isolated_kanban_home(tmp_path, monkeypatch)
    conn = kb.connect()
    _assert_connection_is_isolated(conn, db_path, tmp_path)
    task_id = kb.create_task(
        conn,
        title="goal producer",
        body="finish safely",
        assignee="worker",
        goal_mode=True,
        goal_max_turns=2,
    )
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    yield conn, task_id, claimed.current_run_id
    conn.close()


def _provider_failure_result(classification, status_code, retryable):
    return {
        "failed": True,
        "final_response": "provider stopped",
        "provider_failure": {
            "classification": classification,
            "status_code": status_code,
            "retryable": retryable,
            "provider": "provider-a",
            "model": "model-a",
        },
    }


def _quiet_fake_cli_class(run_conversation):
    class FakeCLI:
        def __init__(self, **_kwargs):
            self.provider = "provider-a"
            self.model = "model-a"
            self.session_id = "session-a"
            self.conversation_history = []
            self._active_agent_route_signature = "same-route"
            self.agent = SimpleNamespace(
                provider="provider-a",
                model="model-a",
                session_id="session-a",
                platform="cli",
                quiet_mode=False,
                suppress_status_output=False,
                stream_delta_callback=object(),
                tool_gen_callback=object(),
                run_conversation=run_conversation,
            )

        def _claim_active_session(self, _surface, *, stderr=False):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _effective_query):
            return {
                "signature": "same-route",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **_kwargs):
            return True

    return FakeCLI


@pytest.mark.parametrize(
    ("classification", "status_code", "retryable", "disposition"),
    [
        ("billing", 402, False, "terminal"),
        ("content_policy_blocked", 400, False, "safety_refusal"),
    ],
)
def test_goal_mode_first_turn_provider_failure_stages_before_judge(
    active_goal_task,
    monkeypatch,
    classification,
    status_code,
    retryable,
    disposition,
):
    conn, _task_id, run_id = active_goal_task
    fake_cli = SimpleNamespace(
        session_id="session-a",
        agent=SimpleNamespace(provider="provider-a", model="model-a"),
    )
    result = _provider_failure_result(classification, status_code, retryable)
    monkeypatch.setattr(
        "hermes_cli.goals.run_kanban_goal_loop",
        lambda **_kwargs: pytest.fail("provider failure must skip the goal judge"),
    )

    stopped_result = cli._run_kanban_goal_loop_q(
        fake_cli,
        result["final_response"],
        first_result=result,
    )

    assert stopped_result is result
    assert _recorded_disposition(conn, run_id) == disposition


@pytest.mark.parametrize(
    ("classification", "status_code", "retryable", "disposition", "exit_code"),
    [
        ("billing", 402, False, "terminal", kb.KANBAN_PROVIDER_TERMINAL_EXIT_CODE),
        ("content_policy_blocked", 400, False, "safety_refusal", 1),
    ],
)
def test_quiet_main_stages_first_turn_failure_before_goal_loop(
    active_goal_task,
    monkeypatch,
    classification,
    status_code,
    retryable,
    disposition,
    exit_code,
):
    conn, _task_id, run_id = active_goal_task
    result = _provider_failure_result(classification, status_code, retryable)
    monkeypatch.setattr(
        "hermes_cli.goals.run_kanban_goal_loop",
        lambda **_kwargs: pytest.fail("provider failure must skip the goal judge"),
    )
    monkeypatch.setattr(
        cli,
        "HermesCLI",
        _quiet_fake_cli_class(lambda **_kwargs: result),
    )
    monkeypatch.setattr(cli.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_finalize_single_query", lambda _fake_cli: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(query="work", quiet=True, toolsets="terminal")

    assert exc_info.value.code == exit_code
    assert _recorded_disposition(conn, run_id) == disposition


@pytest.mark.parametrize(
    ("classification", "status_code", "retryable", "disposition"),
    [
        ("billing", 402, False, "terminal"),
        ("content_policy_blocked", 400, False, "safety_refusal"),
        ("rate_limit", 429, True, "transient"),
    ],
)
def test_goal_mode_continuation_provider_failure_is_staged_and_returned(
    active_goal_task,
    monkeypatch,
    classification,
    status_code,
    retryable,
    disposition,
):
    conn, _task_id, run_id = active_goal_task
    result = _provider_failure_result(classification, status_code, retryable)
    fake_agent = SimpleNamespace(
        provider="provider-a",
        model="model-a",
        session_id="session-a",
        run_conversation=lambda **_kwargs: result,
    )
    fake_cli = SimpleNamespace(
        session_id="session-a",
        conversation_history=[],
        agent=fake_agent,
    )
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda *_args, **_kwargs: ("continue", "work remains", False, None, False),
    )

    stopped_result = cli._run_kanban_goal_loop_q(
        fake_cli,
        "first turn was healthy",
        first_result={"failed": False, "final_response": "first turn was healthy"},
    )

    assert stopped_result is result
    assert _recorded_disposition(conn, run_id) == disposition


@pytest.mark.parametrize(
    ("classification", "status_code", "retryable", "disposition", "exit_code"),
    [
        ("billing", 402, False, "terminal", kb.KANBAN_PROVIDER_TERMINAL_EXIT_CODE),
        ("content_policy_blocked", 400, False, "safety_refusal", 1),
        ("rate_limit", 429, True, "transient", kb.KANBAN_RATE_LIMIT_EXIT_CODE),
    ],
)
def test_quiet_main_preserves_continuation_provider_failure_exit(
    active_goal_task,
    monkeypatch,
    classification,
    status_code,
    retryable,
    disposition,
    exit_code,
):
    conn, _task_id, run_id = active_goal_task
    failure = _provider_failure_result(classification, status_code, retryable)
    results = iter(
        [
            {"failed": False, "final_response": "first turn was healthy"},
            failure,
        ]
    )
    monkeypatch.setattr(
        "hermes_cli.goals.judge_goal",
        lambda *_args, **_kwargs: ("continue", "work remains", False, None, False),
    )
    monkeypatch.setattr(
        cli,
        "HermesCLI",
        _quiet_fake_cli_class(lambda **_kwargs: next(results)),
    )
    monkeypatch.setattr(cli.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_finalize_single_query", lambda _fake_cli: None)

    with pytest.raises(SystemExit) as exc_info:
        cli.main(query="work", quiet=True, toolsets="terminal")

    assert exc_info.value.code == exit_code
    assert _recorded_disposition(conn, run_id) == disposition


def test_worker_records_descriptor_on_active_run(active_task):
    conn, task_id, run_id = active_task
    fake_cli = SimpleNamespace(
        session_id="session-a",
        agent=SimpleNamespace(provider="provider-a", model="model-a"),
    )
    result = {
        "failed": True,
        "provider_failure": {
            "classification": "billing",
            "status_code": 402,
            "retryable": False,
            "provider": "provider-a",
            "model": "model-a",
        },
    }

    assert cli._record_kanban_provider_exit(fake_cli, result) is True
    envelope = kb._provider_exit_for_run(conn, run_id)
    assert envelope["disposition"] == "terminal"
    assert envelope["classification"] == "billing"
    assert envelope["status_code"] == 402
    assert envelope["provider"] == "provider-a"
    assert envelope["model"] == "model-a"


def test_non_kanban_run_keeps_plain_exit_contract(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    fake_cli = SimpleNamespace(
        session_id="session-a",
        agent=SimpleNamespace(provider="provider-a", model="model-a"),
    )
    result = {
        "failed": True,
        "provider_failure": {
            "classification": "billing",
            "status_code": 402,
            "retryable": False,
            "provider": "provider-a",
            "model": "model-a",
        },
    }
    assert cli._record_kanban_provider_exit(fake_cli, result) is False
