"""Producer-side tests for Kanban provider exit dispositions."""

from __future__ import annotations

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
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kb.connect()
    task_id = kb.create_task(conn, title="producer", assignee="worker")
    claimed = kb.claim_task(conn, task_id)
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))
    yield conn, task_id, claimed.current_run_id
    conn.close()


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
