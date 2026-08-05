"""RED regressions for content-free per-turn telemetry."""

from __future__ import annotations

import asyncio
import importlib
import json
from types import SimpleNamespace
import uuid

import pytest


SENSITIVE_VALUE = uuid.uuid4().hex


class CapturingDB:
    def __init__(self, *, fail: bool = False, failure_message: str = ""):
        self.rows: list[dict] = []
        self.fail = fail
        self.failure_message = failure_message

    def record_turn_telemetry(self, **row):
        if self.fail:
            raise RuntimeError(self.failure_message or "telemetry store unavailable")
        self.rows.append(dict(row))


def _telemetry():
    return importlib.import_module("hermes_cli.observability.turn_telemetry")


def _agent(db, *, parent_session_id: str = ""):
    return SimpleNamespace(
        session_id="session-1",
        _parent_session_id=parent_session_id,
        _parent_turn_id="parent-turn" if parent_session_id else "",
        is_subagent=bool(parent_session_id),
        platform="cli",
        requested_provider="openai-codex",
        provider="openai-codex",
        model="gpt-5.5",
        _primary_runtime={
            "requested_provider": "openai-codex",
            "provider": "openai-codex",
            "model": "gpt-5.5",
        },
        session_estimated_cost_usd=0.0,
        session_cost_status="unknown",
        _session_db=db,
    )


def test_main_retry_fallback_usage_and_privacy(monkeypatch):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "quinn-test")
    db = CapturingDB()
    agent = _agent(db)

    binding = telemetry.begin_turn(agent, "turn-1", started_at=100.0)
    telemetry.observe_lifecycle(
        "pre_api_request",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
        provider="openai-codex",
        model="gpt-5.5",
        request={"body": {"messages": [{"content": SENSITIVE_VALUE}]}},
    )
    telemetry.observe_lifecycle(
        "api_request_error",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
        provider="openai-codex",
        model="gpt-5.5",
        status_code=429,
        error={"type": "RateLimitError", "message": SENSITIVE_VALUE},
        reason=SENSITIVE_VALUE,
    )
    agent.provider = "openrouter"
    agent.model = "anthropic/claude-sonnet-4"
    telemetry.observe_lifecycle(
        "pre_api_request",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
    )
    telemetry.observe_lifecycle(
        "post_api_request",
        session_id="session-1",
        turn_id="turn-1",
        api_request_id="request-1",
        provider="openrouter",
        model="anthropic/claude-sonnet-4",
        usage={
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_read_tokens": 3,
            "cache_write_tokens": 2,
            "reasoning_tokens": 4,
            "total_tokens": 18,
        },
        response={"assistant_message": {"content": SENSITIVE_VALUE}},
    )
    agent.session_estimated_cost_usd = 0.0125
    agent.session_cost_status = "estimated"
    telemetry.finish_turn(
        binding,
        result={"completed": True, "final_response": SENSITIVE_VALUE},
        ended_at=100.25,
    )

    assert len(db.rows) == 1
    row = db.rows[0]
    assert row["event_type"] == "turn_terminal"
    assert row["turn_id"] == "turn-1"
    assert row["correlation_id"] == "turn-1"
    assert row["requested_profile"] == "quinn-test"
    assert row["effective_profile"] == "quinn-test"
    assert row["platform"] == "cli"
    assert row["task_class"] == "interactive"
    assert row["route_type"] == "fallback"
    assert row["disposition"] == "fell_back"
    assert row["requested_provider"] == "openai-codex"
    assert row["requested_model"] == "gpt-5.5"
    assert row["effective_provider"] == "openrouter"
    assert row["effective_model"] == "anthropic/claude-sonnet-4"
    assert row["attempt_count"] == 2
    assert row["retry_count"] == 1
    assert row["fallback_count"] == 1
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 7
    assert row["cache_read_tokens"] == 3
    assert row["cache_write_tokens"] == 2
    assert row["reasoning_tokens"] == 4
    assert row["total_tokens"] == 18
    assert row["estimated_cost_usd"] == pytest.approx(0.0125)
    assert row["cost_status"] == "estimated"
    assert row["outcome"] == "success"
    assert row["failure_class"] == ""
    assert row["duration_ms"] == 250
    assert SENSITIVE_VALUE not in json.dumps(row, sort_keys=True)


def test_auxiliary_attempts_and_usage_are_folded_without_content(monkeypatch):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "default")
    db = CapturingDB()
    binding = telemetry.begin_turn(_agent(db), "turn-aux", started_at=10.0)

    telemetry.record_auxiliary_attempt(
        request_id="aux-1",
        task="compression",
        provider="openai-codex",
        model="gpt-5.5-mini",
        retry_count=0,
    )
    telemetry.record_auxiliary_attempt(
        request_id="aux-1",
        task="compression",
        provider="openrouter",
        model="google/gemini-flash",
        retry_count=1,
    )
    telemetry.record_auxiliary_usage(
        input_tokens=20,
        output_tokens=5,
        cache_read_tokens=9,
        cache_write_tokens=1,
        reasoning_tokens=2,
        total_tokens=25,
        estimated_cost_usd=0.004,
        cost_status="estimated",
    )
    telemetry.record_auxiliary_terminal(
        request_id="aux-1",
        outcome="success",
        error_message=SENSITIVE_VALUE,
    )
    telemetry.finish_turn(binding, result={"completed": True}, ended_at=10.1)

    row = db.rows[0]
    assert row["attempt_count"] == 0
    assert row["auxiliary_attempt_count"] == 2
    assert row["retry_count"] == 1
    assert row["fallback_count"] == 1
    assert row["input_tokens"] == 20
    assert row["output_tokens"] == 5
    assert row["total_tokens"] == 25
    assert row["estimated_cost_usd"] == pytest.approx(0.004)
    assert row["cost_status"] == "estimated"
    assert SENSITIVE_VALUE not in json.dumps(row, sort_keys=True)


@pytest.mark.parametrize(
    ("result", "error", "outcome", "failure_class"),
    [
        ({"failed": True, "completed": False, "failure_reason": "billing"}, None, "failed", "billing"),
        ({"status": "held", "completed": False}, None, "held", "triage_held"),
        (
            {"completed": False, "compression_deferred": True},
            None,
            "held",
            "triage_held",
        ),
        ({"status": "refused", "completed": False}, None, "refused", "refused"),
        (
            {
                "completed": False,
                "failed": True,
                "error": "content_policy_blocked",
            },
            None,
            "refused",
            "content_filter",
        ),
        ({"completed": False, "interrupted": True}, None, "cancelled", "cancelled"),
        (None, asyncio.CancelledError(), "cancelled", "cancelled"),
        (None, TimeoutError(SENSITIVE_VALUE), "failed", "timeout"),
    ],
)
def test_terminal_outcomes_are_fixed_taxonomy_and_content_free(
    monkeypatch, result, error, outcome, failure_class
):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "default")
    db = CapturingDB()
    binding = telemetry.begin_turn(_agent(db), f"turn-{outcome}", started_at=1.0)
    telemetry.finish_turn(binding, result=result, error=error, ended_at=1.01)

    row = db.rows[0]
    assert row["outcome"] == outcome
    assert row["failure_class"] == failure_class
    assert SENSITIVE_VALUE not in json.dumps(row, sort_keys=True)


def test_delegated_child_is_separately_attributed(monkeypatch):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "forge")
    db = CapturingDB()
    binding = telemetry.begin_turn(
        _agent(db, parent_session_id="parent-session"),
        "child-turn",
        started_at=2.0,
    )
    telemetry.finish_turn(binding, result={"completed": True}, ended_at=2.02)

    row = db.rows[0]
    assert row["session_id"] == "session-1"
    assert row["parent_session_id"] == "parent-session"
    assert row["parent_turn_id"] == "parent-turn"
    assert row["is_delegated"] is True
    assert row["profile_name"] == "forge"
    assert row["task_class"] == "delegated"
    assert row["route_type"] == "delegated_specialist"


def test_explicit_provider_target_is_classified_without_payload(monkeypatch):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "default")
    db = CapturingDB()
    agent = _agent(db)
    agent.requested_provider = "openrouter"

    binding = telemetry.begin_turn(agent, "turn-explicit", started_at=3.0)
    telemetry.finish_turn(binding, result={"completed": True}, ended_at=3.01)

    row = db.rows[0]
    assert row["requested_provider"] == "openrouter"
    assert row["effective_provider"] == "openai-codex"
    assert row["route_type"] == "explicit_override"


def test_telemetry_store_failure_never_changes_turn_result_or_logs_content(
    monkeypatch, caplog
):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "default")
    caplog.set_level("DEBUG", logger=telemetry.__name__)
    binding = telemetry.begin_turn(
        _agent(CapturingDB(fail=True, failure_message=SENSITIVE_VALUE)),
        "turn-fail",
        started_at=1.0,
    )

    # Must be a non-raising best-effort path even though SessionDB rejects it.
    assert telemetry.finish_turn(
        binding,
        result={"completed": True, "final_response": "unchanged"},
        ended_at=1.01,
    ) is None
    assert "RuntimeError" in caplog.text
    assert SENSITIVE_VALUE not in caplog.text


def test_malformed_lifecycle_metadata_is_isolated_and_content_free(
    monkeypatch, caplog
):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "default")
    caplog.set_level("DEBUG", logger=telemetry.__name__)
    db = CapturingDB()
    binding = telemetry.begin_turn(_agent(db), "turn-malformed", started_at=2.0)

    class RaisingText:
        def __str__(self):
            raise RuntimeError(SENSITIVE_VALUE)

    assert (
        telemetry.observe_lifecycle(
            "pre_api_request",
            turn_id="turn-malformed",
            session_id="session-1",
            api_request_id="malformed-1",
            provider=RaisingText(),
            model="model",
        )
        is None
    )
    telemetry.finish_turn(binding, result={"completed": True}, ended_at=2.01)

    assert db.rows[0]["attempt_count"] == 0
    assert "RuntimeError" in caplog.text
    assert SENSITIVE_VALUE not in caplog.text


def test_gateway_preflight_terminal_is_zero_attempt_and_drops_error_text(monkeypatch):
    telemetry = _telemetry()
    monkeypatch.setattr(telemetry, "_active_profile_name", lambda: "gateway-profile")
    db = CapturingDB()

    telemetry.record_gateway_terminal(
        db,
        session_id="gateway-session",
        turn_id=f"caller-controlled-{SENSITIVE_VALUE}",
        source="telegram",
        failure_class="gateway_preflight",
        error_message=SENSITIVE_VALUE,
        started_at=4.0,
        ended_at=4.0,
    )

    row = db.rows[0]
    assert row["session_id"] == "gateway-session"
    assert row["turn_id"].startswith("gateway-")
    assert len(row["turn_id"].removeprefix("gateway-")) == 32
    assert uuid.UUID(hex=row["turn_id"].removeprefix("gateway-")).version == 4
    assert row["correlation_id"] == row["turn_id"]
    assert row["source"] == "telegram"
    assert row["attempt_count"] == 0
    assert row["event_type"] == "gateway_terminal"
    assert row["task_class"] == "gateway_preflight"
    assert row["route_type"] == "local_triage"
    assert row["disposition"] == "triaged"
    assert row["outcome"] == "refused"
    assert row["failure_class"] == "gateway_preflight"
    assert SENSITIVE_VALUE not in json.dumps(row, sort_keys=True)
