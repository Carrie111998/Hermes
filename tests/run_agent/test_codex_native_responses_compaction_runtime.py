"""Mocked end-to-end tests for native Responses compaction."""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

import pytest

sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent
from agent.responses_compaction import NativeCompactionPolicy, route_for_request
from hermes_state import SessionDB


@pytest.fixture(autouse=True)
def _isolated_hermes_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(run_agent, "_hermes_home", tmp_path)


def _agent(
    monkeypatch,
    *,
    compaction_mode="hermes",
    compression_enabled=True,
):
    monkeypatch.setattr(run_agent, "get_tool_definitions", lambda **kwargs: [])
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})
    agent = run_agent.AIAgent(
        model="gpt-5.6-sol",
        provider="openai-codex",
        api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="test-token",
        quiet_mode=True,
        max_iterations=3,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cleanup_task_resources = lambda task_id: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    session_db = SessionDB(db_path=run_agent._hermes_home / "state.db")
    session_id = f"runtime-{uuid.uuid4().hex}"
    session_db.create_session(session_id, "test", model=agent.model)
    agent._session_db = session_db
    agent.session_id = session_id
    agent._session_db_created = True
    agent.request_overrides = {}
    agent._native_compaction_policy = NativeCompactionPolicy(
        route=route_for_request(
            provider=agent.provider,
            endpoint=agent.base_url,
            model=agent.model,
        )
    )
    agent.codex_responses_auto_compaction = compaction_mode
    agent.compression_enabled = compression_enabled
    agent.codex_responses_compact_threshold = 200_000
    return agent


def _response(text="OK", *, compact=False):
    output = []
    if compact:
        output.append(
            SimpleNamespace(
                type="compaction",
                id="cmp_runtime",
                encrypted_content="opaque-runtime",
                created_by=None,
                status=None,
            )
        )
    output.append(
        SimpleNamespace(
            type="message",
            id="msg_runtime",
            role="assistant",
            status="completed",
            phase=None,
            content=[SimpleNamespace(type="output_text", text=text)],
        )
    )
    return SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=output,
        usage=SimpleNamespace(input_tokens=5, output_tokens=2, total_tokens=7),
    )


class StructuredCompactionError(Exception):
    status_code = 400
    body = {
        "error": {
            "code": "unknown_parameter",
            "param": "context_management",
            "message": "unknown parameter",
        }
    }


def test_default_builder_is_inert_and_native_mode_injects(monkeypatch):
    agent = _agent(monkeypatch)
    agent.codex_responses_auto_compaction = "hermes"
    kwargs = agent._build_api_kwargs([{"role": "user", "content": "hello"}])
    assert "context_management" not in kwargs

    agent.codex_responses_auto_compaction = "native"
    kwargs = agent._build_api_kwargs([{"role": "user", "content": "hello"}])
    assert kwargs["context_management"] == [
        {"type": "compaction", "compact_threshold": 200_000}
    ]


def test_structured_unsupported_error_rebuilds_once_without_native(monkeypatch):
    agent = _agent(monkeypatch)
    agent.codex_responses_auto_compaction = "native"
    calls = []

    def api_call(kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise StructuredCompactionError("unsupported")
        return _response("fallback OK")

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    result = agent.run_conversation("Say OK")

    assert result["completed"] is True
    assert len(calls) == 2
    assert "context_management" in calls[0]
    assert "context_management" not in calls[1]
    assert agent._native_compaction_policy.capability == "unsupported"
    assert agent._native_compaction_policy.fallback_count == 1


def test_compaction_output_is_replayed_and_verified_on_next_turn(monkeypatch):
    agent = _agent(monkeypatch)
    agent.codex_responses_auto_compaction = "native"
    calls = []
    responses = [_response("first", compact=True), _response("second")]

    def api_call(kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    first = agent.run_conversation("First")
    assert first["completed"] is True
    assert agent._native_compaction_policy.capability == "item_observed"
    assert first["messages"][-1]["codex_output_items"][0]["type"] == "compaction"
    assert first["messages"][-1]["codex_output_items"][0]["_compaction_route"] == (
        agent._native_compaction_policy.route.to_dict()
    )

    second = agent.run_conversation(
        "Second", conversation_history=first["messages"]
    )
    assert second["completed"] is True
    assert calls[1]["input"][0]["type"] == "compaction"
    assert calls[1]["input"][0]["encrypted_content"] == "opaque-runtime"
    assert agent._native_compaction_policy.capability == "replay_verified"


def test_model_switch_uses_canonical_history_not_opaque_sidecar(monkeypatch):
    agent = _agent(monkeypatch)
    agent.codex_responses_auto_compaction = "native"
    calls = []
    responses = [_response("first", compact=True), _response("second")]

    def api_call(kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", api_call)
    first = agent.run_conversation("First")
    agent.model = "gpt-5.6-mini"
    second = agent.run_conversation("Second", conversation_history=first["messages"])

    assert second["completed"] is True
    assert all(item.get("type") != "compaction" for item in calls[1]["input"])
    assert any(item.get("content") == "First" for item in calls[1]["input"])


def test_observed_native_owner_suppresses_every_automatic_hermes_gate(monkeypatch):
    agent = _agent(monkeypatch, compaction_mode="native")
    responses = [_response("first", compact=True), _response("second")]
    monkeypatch.setattr(
        agent, "_interruptible_api_call", lambda _kwargs: responses.pop(0)
    )
    first = agent.run_conversation("First")
    compression_routes = []

    def _recording_compress(messages, system_message, **_kwargs):
        compression_routes.append(agent._native_compaction_policy.route.model)
        return messages, system_message

    agent.context_compressor.should_defer_preflight_to_real_usage = MagicMock(
        return_value=False
    )
    agent.context_compressor.should_compress = MagicMock(return_value=True)
    monkeypatch.setattr(agent, "_compress_context", _recording_compress)

    second = agent.run_conversation("Second", conversation_history=first["messages"])

    assert second["completed"] is True
    assert compression_routes == []


def test_model_switch_reconciles_policy_before_turn_preflight(monkeypatch):
    agent = _agent(monkeypatch, compaction_mode="native")
    responses = [_response("first", compact=True), _response("second")]
    monkeypatch.setattr(
        agent, "_interruptible_api_call", lambda _kwargs: responses.pop(0)
    )
    first = agent.run_conversation("First")
    agent.model = "gpt-5.6-mini"
    compression_routes = []

    def _recording_compress(messages, system_message, **_kwargs):
        compression_routes.append(agent._native_compaction_policy.route.model)
        return messages, system_message

    agent.context_compressor.should_defer_preflight_to_real_usage = MagicMock(
        return_value=False
    )
    agent.context_compressor.should_compress = MagicMock(return_value=True)
    monkeypatch.setattr(agent, "_compress_context", _recording_compress)

    second = agent.run_conversation("Second", conversation_history=first["messages"])

    assert second["completed"] is True
    assert compression_routes
    assert compression_routes[0] == "gpt-5.6-mini"
    assert agent._native_compaction_policy.route.model == "gpt-5.6-mini"


@pytest.mark.parametrize(
    "compaction_mode,compression_enabled",
    [("off", True), ("native", False)],
)
def test_disabled_auto_modes_send_no_native_field_and_do_not_compact(
    monkeypatch, compaction_mode, compression_enabled
):
    agent = _agent(
        monkeypatch,
        compaction_mode=compaction_mode,
        compression_enabled=compression_enabled,
    )
    calls = []
    monkeypatch.setattr(
        agent,
        "_interruptible_api_call",
        lambda kwargs: calls.append(kwargs) or _response("done"),
    )
    compress_calls = []
    agent.context_compressor.should_defer_preflight_to_real_usage = MagicMock(
        return_value=False
    )
    agent.context_compressor.should_compress = MagicMock(return_value=True)
    monkeypatch.setattr(
        agent,
        "_compress_context",
        lambda messages, system_message, **_kwargs: (
            compress_calls.append(True) or messages,
            system_message,
        ),
    )

    result = agent.run_conversation("Diagnostic")

    assert result["completed"] is True
    assert compress_calls == []
    assert "context_management" not in calls[0]


def test_compaction_only_response_is_replayed_into_continuation(monkeypatch):
    agent = _agent(monkeypatch, compaction_mode="native")
    compact_only = SimpleNamespace(
        status="completed",
        incomplete_details=None,
        output_text=None,
        output=[
            SimpleNamespace(
                type="compaction",
                id="cmp_only",
                encrypted_content="opaque-only",
            )
        ],
        usage=SimpleNamespace(input_tokens=5, output_tokens=1, total_tokens=6),
    )
    responses = [compact_only, _response("continued")]
    calls = []

    def _api_call(kwargs):
        calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(agent, "_interruptible_api_call", _api_call)
    result = agent.run_conversation("Continue after checkpoint")

    assert result["completed"] is True
    assert result["final_response"] == "continued"
    assert len(calls) == 2
    assert calls[1]["input"][0] == {
        "type": "compaction",
        "encrypted_content": "opaque-only",
    }
    assert agent._native_compaction_policy.capability == "replay_verified"
