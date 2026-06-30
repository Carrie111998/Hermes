"""Runtime integration tests for the A1 model-dispatch guard."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _mock_response(content="ok", finish_reason="stop"):
    msg = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="frontier-fast", usage=None)


def _mock_invalid_response():
    return SimpleNamespace(choices=[], model="frontier-fast", usage=None)


def _make_agent() -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="http://localhost:8787/v1",
            provider="custom:headroom-openrouter-litellm",
            model="frontier-fast",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 1
    return agent


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_a1_guard_denies_c2_frontier_before_provider_dispatch(monkeypatch, tmp_path):
    sink = tmp_path / "a1-dispatch.jsonl"
    monkeypatch.setenv("HERMES_A1_DISPATCH_GUARD", "1")
    monkeypatch.setenv("HERMES_A1_EVIDENCE_SINK", str(sink))
    agent = _make_agent()
    setattr(agent, "hl_aos_taint_classification", "C2_LOCAL_ONLY")
    provider_call = MagicMock(return_value=_mock_response("should-not-run"))
    agent._interruptible_api_call = provider_call

    result = agent.run_conversation("do not leave local")

    provider_call.assert_not_called()
    assert result["failed"] is True
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert events[-1]["rule_id"] == "a1.c2.frontier-deny"
    assert events[-1]["provider_call_attempted"] is False


def test_a1_guard_records_allowed_dispatch_result(monkeypatch, tmp_path):
    sink = tmp_path / "a1-dispatch.jsonl"
    monkeypatch.setenv("HERMES_A1_DISPATCH_GUARD", "1")
    monkeypatch.setenv("HERMES_A1_EVIDENCE_SINK", str(sink))
    agent = _make_agent()
    setattr(agent, "hl_aos_taint_classification", "C0_PUBLIC")
    provider_call = MagicMock(return_value=_mock_response("guarded ok"))
    agent._interruptible_api_call = provider_call

    result = agent.run_conversation("hello")

    provider_call.assert_called_once()
    assert result["completed"] is True
    assert result["final_response"] == "guarded ok"
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert events[-1]["provider_call_attempted"] is True
    assert events[-1]["provider_call_completed"] is True


def test_a1_guard_wraps_streaming_dispatch_before_provider_call(monkeypatch, tmp_path):
    sink = tmp_path / "a1-streaming.jsonl"
    monkeypatch.setenv("HERMES_A1_DISPATCH_GUARD", "1")
    monkeypatch.setenv("HERMES_A1_EVIDENCE_SINK", str(sink))
    agent = _make_agent()
    setattr(agent, "hl_aos_taint_classification", "C0_PUBLIC")
    agent.stream_delta_callback = lambda _delta: None
    streaming_call = MagicMock(return_value=_mock_response("streamed ok"))
    non_streaming_call = MagicMock(return_value=_mock_response("wrong path"))
    agent._interruptible_streaming_api_call = streaming_call
    agent._interruptible_api_call = non_streaming_call

    result = agent.run_conversation("streaming hello")

    streaming_call.assert_called_once()
    non_streaming_call.assert_not_called()
    assert result["completed"] is True
    assert result["final_response"] == "streamed ok"
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert events[-1]["provider_call_attempted"] is True
    assert events[-1]["provider_call_completed"] is True


def test_a1_guard_rechecks_runtime_after_fallback_provider_switch(monkeypatch, tmp_path):
    sink = tmp_path / "a1-fallback.jsonl"
    monkeypatch.setenv("HERMES_A1_DISPATCH_GUARD", "1")
    monkeypatch.setenv("HERMES_A1_EVIDENCE_SINK", str(sink))
    agent = _make_agent()
    setattr(agent, "hl_aos_taint_classification", "C0_PUBLIC")
    agent._fallback_chain = [{"provider": "local-ollama", "model": "qwen3.5:9b"}]
    agent._fallback_index = 0
    provider_call = MagicMock(
        side_effect=[
            _mock_invalid_response(),
            _mock_response("fallback ok"),
        ]
    )
    agent._interruptible_api_call = provider_call

    def activate_fallback(*_args, **_kwargs):
        agent.provider = "local-ollama"
        agent.model = "qwen3.5:9b"
        agent.base_url = "http://localhost:11434/v1"
        agent._fallback_index = 1
        agent._fallback_activated = True
        return True

    agent._try_activate_fallback = MagicMock(side_effect=activate_fallback)

    result = agent.run_conversation("fallback hello")

    assert provider_call.call_count == 2
    assert agent._try_activate_fallback.call_count == 1
    assert result["completed"] is True
    assert result["final_response"] == "fallback ok"
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert events[0]["canonical_provider"] == "custom:headroom-openrouter-litellm"
    assert events[3]["canonical_provider"] == "local-ollama"
    assert events[3]["canonical_base_url_host"] == "localhost:11434"


def test_a1_guard_can_be_enabled_from_profile_config_without_env_override(monkeypatch, tmp_path):
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    sink = tmp_path / "configured-a1.jsonl"
    (hermes_home / "config.yaml").write_text(
        f"""
a1:
  dispatch_guard:
    enabled: true
    evidence_sink: {sink}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.delenv("HERMES_A1_DISPATCH_GUARD", raising=False)
    monkeypatch.delenv("HERMES_A1_EVIDENCE_SINK", raising=False)
    agent = _make_agent()
    setattr(agent, "hl_aos_taint_classification", "C2_LOCAL_ONLY")
    provider_call = MagicMock(return_value=_mock_response("should-not-run"))
    agent._interruptible_api_call = provider_call

    result = agent.run_conversation("do not leave local")

    provider_call.assert_not_called()
    assert result["failed"] is True
    events = _read_jsonl(sink)
    assert [event["event_type"] for event in events] == [
        "resolver_decision",
        "payload_capture",
        "dispatch_result",
    ]
    assert events[-1]["rule_id"] == "a1.c2.frontier-deny"
    assert events[-1]["provider_call_attempted"] is False
