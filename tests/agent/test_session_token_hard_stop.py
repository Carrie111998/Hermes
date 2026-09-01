"""Tests for the per-session billed-token fuse (#96814).

Covers:

1. Normalization of ``session_token_hard_stop`` / ``session_token_warn``
2. Documented billed-token formula (fresh + cache + output)
3. Conversation-loop hard stop BEFORE the next provider call
4. One-shot warn notice (explicit threshold and 80% default)
5. Config plumbing + unimplemented older key warnings
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.session_token_hard_stop import (
    SESSION_TOKEN_WARN_NOTICE,
    billed_session_tokens,
    hard_stop_exhausted,
    hard_stop_user_message,
    normalize_session_token_limit,
    resolve_warn_threshold,
    should_emit_warn,
    unimplemented_budget_keys_present,
)
from hermes_cli.config import validate_config_structure
from run_agent import AIAgent


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    (None, None),
    (0, None),
    (-5, None),
    ("abc", None),
    (True, None),
    (False, None),
    (5000, 5000),
    ("850", 850),
    (1.9, 1),
])
def test_normalize_session_token_limit(raw, expected):
    assert normalize_session_token_limit(raw) == expected


# --------------------------------------------------------------------------------------
# Formula
# --------------------------------------------------------------------------------------


def test_billed_session_tokens_sums_canonical_split():
    agent = SimpleNamespace(
        session_input_tokens=100,
        session_cache_read_tokens=1000,
        session_cache_write_tokens=50,
        session_output_tokens=25,
        session_total_tokens=9999,
    )
    assert billed_session_tokens(agent) == 1175


def test_billed_session_tokens_falls_back_to_session_total():
    agent = SimpleNamespace(session_total_tokens=42)
    assert billed_session_tokens(agent) == 42


def test_hard_stop_exhausted_requires_positive_cap():
    agent = SimpleNamespace(
        session_token_hard_stop=None,
        session_input_tokens=999,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_output_tokens=0,
    )
    assert hard_stop_exhausted(agent) is False
    agent.session_token_hard_stop = 0
    assert hard_stop_exhausted(agent) is False
    agent.session_token_hard_stop = 100
    assert hard_stop_exhausted(agent) is True
    agent.session_input_tokens = 99
    assert hard_stop_exhausted(agent) is False


def test_warn_threshold_defaults_to_80_percent():
    agent = SimpleNamespace(session_token_hard_stop=1000, session_token_warn=None)
    assert resolve_warn_threshold(agent) == 800


def test_explicit_warn_threshold_wins():
    agent = SimpleNamespace(session_token_hard_stop=1000, session_token_warn=200)
    assert resolve_warn_threshold(agent) == 200


def test_should_emit_warn_latches_and_skips_when_already_stopped():
    agent = SimpleNamespace(
        session_token_hard_stop=100,
        session_token_warn=None,
        session_input_tokens=80,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_output_tokens=0,
        _session_token_warn_injected=False,
    )
    assert should_emit_warn(agent) is True
    agent._session_token_warn_injected = True
    assert should_emit_warn(agent) is False
    agent._session_token_warn_injected = False
    agent.session_input_tokens = 100
    assert hard_stop_exhausted(agent) is True
    assert should_emit_warn(agent) is False


def test_unimplemented_budget_keys_present_scans_agent_and_root():
    keys = unimplemented_budget_keys_present({
        "agent": {"gateway_usage_hard_tokens": 1},
        "gateway_usage_hard_api_calls": 9,
    })
    assert keys == ["gateway_usage_hard_api_calls", "gateway_usage_hard_tokens"]
    assert unimplemented_budget_keys_present({"agent": {"session_token_hard_stop": 5}}) == []


# --------------------------------------------------------------------------------------
# Config plumbing
# --------------------------------------------------------------------------------------


def _write_config(tmp_path, body: str) -> None:
    (tmp_path / "config.yaml").write_text(body or "{}\n", encoding="utf-8")


def _make_agent(tmp_path, monkeypatch, config_body: str = "", **overrides):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    _write_config(tmp_path, config_body)
    kwargs = dict(
        model="gpt-5.5",
        provider="openai",
        api_key="sk-dummy",
        base_url="https://api.openai.com/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    kwargs.update(overrides)
    return AIAgent(**kwargs)


def test_no_hard_stop_by_default(monkeypatch, tmp_path):
    agent = _make_agent(tmp_path, monkeypatch)
    assert agent.session_token_hard_stop is None
    assert agent.session_token_warn is None
    assert agent._session_token_warn_injected is False


def test_config_key_sets_hard_stop(monkeypatch, tmp_path):
    agent = _make_agent(
        tmp_path, monkeypatch,
        config_body="agent:\n  session_token_hard_stop: 2500\n  session_token_warn: 2000\n",
    )
    assert agent.session_token_hard_stop == 2500
    assert agent.session_token_warn == 2000


def test_constructor_arg_wins_over_config(monkeypatch, tmp_path):
    agent = _make_agent(
        tmp_path, monkeypatch,
        config_body="agent:\n  session_token_hard_stop: 2500\n",
        session_token_hard_stop=900,
    )
    assert agent.session_token_hard_stop == 900


def test_reset_session_state_clears_warn_latch(monkeypatch, tmp_path):
    agent = _make_agent(tmp_path, monkeypatch, session_token_hard_stop=100)
    agent._session_token_warn_injected = True
    agent.session_input_tokens = 50
    agent.reset_session_state()
    assert agent._session_token_warn_injected is False
    assert agent.session_input_tokens == 0


def test_validate_config_warns_on_unimplemented_budget_keys():
    issues = validate_config_structure({
        "agent": {"gateway_usage_hard_tokens": 1_000_000},
    })
    warnings = [i for i in issues if i.severity == "warning"]
    assert any("gateway_usage_hard_tokens" in i.message and "not implemented" in i.message for i in warnings)


def test_validate_config_does_not_warn_on_session_token_hard_stop():
    issues = validate_config_structure({
        "agent": {"session_token_hard_stop": 5_000_000},
    })
    assert not any("not implemented" in i.message for i in issues)


# --------------------------------------------------------------------------------------
# Conversation loop
# --------------------------------------------------------------------------------------


def _tool_call() -> SimpleNamespace:
    return SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(name="web_search", arguments='{"query": "x"}'),
    )


def _tool_response(prompt_tokens: int) -> SimpleNamespace:
    message = SimpleNamespace(
        content=None,
        reasoning_content=None,
        reasoning=None,
        tool_calls=[_tool_call()],
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=1,
            total_tokens=prompt_tokens + 1,
        ),
    )


def _final_response() -> SimpleNamespace:
    message = SimpleNamespace(
        content="done",
        reasoning_content=None,
        reasoning=None,
        tool_calls=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _tool_definition() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    }


def _make_loop_agent(**overrides):
    with (
        patch("run_agent.get_tool_definitions", return_value=[_tool_definition()]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("agent.model_metadata.get_model_context_length", return_value=256_000),
        patch("agent.context_compressor.get_model_context_length", return_value=256_000),
    ):
        kwargs = dict(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=10,
        )
        kwargs.update(overrides)
        agent = AIAgent(**kwargs)

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.max_compression_attempts = 1

    compressor = MagicMock()
    compressor.protect_first_n = 3
    compressor.protect_last_n = 20
    compressor.threshold_tokens = 999_999_999
    compressor.context_length = 1_000_000_000
    compressor.last_prompt_tokens = -1
    compressor._verify_compaction_cleared_threshold = False
    compressor.awaiting_real_usage_after_compression = False
    compressor.should_compress.return_value = False
    compressor.should_compress_info.return_value = (False, None)
    compressor.should_compress_preflight.return_value = False
    compressor.should_defer_preflight_to_real_usage.return_value = False
    compressor.get_active_compression_failure_cooldown.return_value = None
    compressor.select_context.return_value = None
    compressor.get_automatic_compaction_status_message.return_value = ""
    agent.compression_enabled = False
    agent.context_compressor = compressor

    def _fake_execute_tool_calls(assistant_message, messages, *_args):
        tool_call = assistant_message.tool_calls[0]
        messages.append(
            {
                "role": "tool",
                "name": tool_call.function.name,
                "tool_call_id": tool_call.id,
                "content": "ok",
            }
        )

    agent._execute_tool_calls = _fake_execute_tool_calls
    return agent


def _run_with_responses(agent, responses):
    agent.client.chat.completions.create.side_effect = responses
    with (
        patch.object(agent, "_flush_messages_to_session_db", return_value=True),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation("do some tool work")


def test_hard_stop_already_crossed_makes_zero_provider_calls():
    agent = _make_loop_agent(session_token_hard_stop=100)
    agent.session_input_tokens = 100
    agent.session_output_tokens = 0
    result = _run_with_responses(agent, [_final_response(), _final_response()])
    assert agent.client.chat.completions.create.call_count == 0
    assert result.get("turn_exit_reason") == "token_hard_stop"
    assert "hard stop" in (result.get("final_response") or "").lower()


def test_hard_stop_after_crossing_blocks_next_provider_call():
    agent = _make_loop_agent(session_token_hard_stop=100)
    responses = [
        _tool_response(99),
        _tool_response(99),
        _final_response(),
    ]
    result = _run_with_responses(agent, responses)
    assert agent.client.chat.completions.create.call_count == 1
    assert billed_session_tokens(agent) >= 100
    assert result.get("turn_exit_reason") == "token_hard_stop"
    assert "hard stop" in (result.get("final_response") or "").lower()


def test_disabled_hard_stop_leaves_loop_unbounded():
    agent = _make_loop_agent()
    responses = [
        _tool_response(50_000),
        _tool_response(50_000),
        _tool_response(50_000),
        _final_response(),
    ]
    result = _run_with_responses(agent, responses)
    assert agent.client.chat.completions.create.call_count == 4
    assert result["completed"] is True
    assert result["final_response"] == "done"


def test_warn_notice_appended_once_to_tool_result():
    from agent.conversation_loop import _maybe_inject_session_token_warn

    agent = SimpleNamespace(
        session_token_hard_stop=1000,
        session_token_warn=100,
        session_input_tokens=100,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_output_tokens=0,
        _session_token_warn_injected=False,
    )
    messages = [{"role": "tool", "content": "ok", "tool_call_id": "t1"}]
    assert _maybe_inject_session_token_warn(agent, messages) is True
    assert SESSION_TOKEN_WARN_NOTICE in messages[0]["content"]
    assert agent._session_token_warn_injected is True
    assert _maybe_inject_session_token_warn(agent, messages) is False
    assert messages[0]["content"].count(SESSION_TOKEN_WARN_NOTICE) == 1
