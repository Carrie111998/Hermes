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
    provider_call = MagicMock(return_value=_mock_response("should-not-run"))
    agent._interruptible_api_call = provider_call

    result = agent.run_conversation("CLASSIFICATION=C2_LOCAL_ONLY do not leave local")

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
    provider_call = MagicMock(return_value=_mock_response("guarded ok"))
    agent._interruptible_api_call = provider_call

    result = agent.run_conversation("CLASSIFICATION=C0_PUBLIC hello")

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
