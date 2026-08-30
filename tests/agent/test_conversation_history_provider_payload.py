"""Regression coverage for conversation history at the provider boundary."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import sanitize_api_messages
from agent.ollama_native_adapter import build_native_payload
from hermes_state import SessionDB
from run_agent import AIAgent


ROUND_1_USER = "A=137\nB=HERMES-CONTEXT-TEST\nC=deterministic-first"
ROUND_1_ASSISTANT = "ROUND-1 PASS"
ROUND_2_USER = "请返回 A、B、C。"
ROUND_2_RESPONSE = "A=137\nB=HERMES-CONTEXT-TEST\nC=deterministic-first"


def _response(content: str, prompt_tokens: int):
    message = SimpleNamespace(role="assistant", content=content, tool_calls=None)
    return SimpleNamespace(
        id="native-test",
        model="qwen3.5:9b",
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=4,
            total_tokens=prompt_tokens + 4,
        ),
    )


def _make_history_agent():
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="http://127.0.0.1:11434/v1",
            model="qwen3.5:9b",
            provider="ollama",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "system"
    agent._disable_streaming = True
    agent.save_trajectories = False
    agent._ollama_num_ctx = 65536
    return agent


@pytest.fixture()
def history_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    return _make_history_agent()


def _install_provider_capture(monkeypatch):
    captured = []
    prompt_token_counts = []

    monkeypatch.setattr(
        "agent.ollama_native_adapter.should_use_native_ollama", lambda _agent: True
    )

    def fake_provider(_agent, api_kwargs):
        messages = api_kwargs["messages"]
        captured.append(messages)
        texts = [message.get("content") for message in messages]
        has_history = ROUND_1_USER in texts and ROUND_1_ASSISTANT in texts
        prompt_tokens = sum(max(1, len(str(text or "")) // 4) for text in texts)
        prompt_token_counts.append(prompt_tokens)
        return _response(
            ROUND_2_RESPONSE if has_history else ROUND_1_ASSISTANT,
            prompt_tokens,
        )

    monkeypatch.setattr(
        "agent.ollama_native_adapter.create_native_ollama_chat", fake_provider
    )
    return captured, prompt_token_counts


def _run(agent, user_message, history, task_id):
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        return agent.run_conversation(
            user_message,
            conversation_history=history,
            task_id=task_id,
        )


def test_a_two_turn_continuity_reaches_final_provider_payload(
    history_agent, monkeypatch
):
    captured, prompt_token_counts = _install_provider_capture(monkeypatch)
    round_1 = _run(history_agent, ROUND_1_USER, [], "round-1")
    round_2 = _run(history_agent, ROUND_2_USER, round_1["messages"], "round-2")

    assert ROUND_1_USER in [message.get("content") for message in captured[1]]
    assert ROUND_1_ASSISTANT in [message.get("content") for message in captured[1]]
    assert prompt_token_counts[1] > prompt_token_counts[0]
    assert round_2["final_response"] == ROUND_2_RESPONSE


def test_b_persistence_reload_reaches_provider_payload(
    tmp_path, history_agent, monkeypatch
):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    db.create_session("continued", source="cli")
    db.append_message("continued", role="user", content=ROUND_1_USER)
    db.append_message("continued", role="assistant", content=ROUND_1_ASSISTANT)
    db.close()

    reloaded_db = SessionDB(db_path)
    reloaded = reloaded_db.get_messages_as_conversation("continued")
    reloaded_db.close()
    resumed_agent = _make_history_agent()
    captured, _prompt_token_counts = _install_provider_capture(monkeypatch)
    result = _run(resumed_agent, ROUND_2_USER, reloaded, "continued")

    assert ROUND_1_USER in [message.get("content") for message in captured[0]]
    assert ROUND_1_ASSISTANT in [message.get("content") for message in captured[0]]
    assert result["final_response"] == ROUND_2_RESPONSE


def test_c_compressor_off_path_does_not_drop_history(history_agent, monkeypatch):
    history_agent.compression_enabled = False
    captured, _prompt_token_counts = _install_provider_capture(monkeypatch)
    with patch.object(history_agent, "_compress_context") as compressor:
        _run(
            history_agent,
            ROUND_2_USER,
            [
                {"role": "user", "content": ROUND_1_USER},
                {"role": "assistant", "content": ROUND_1_ASSISTANT},
            ],
            "compressor-off",
        )
    compressor.assert_not_called()
    assert [message["role"] for message in captured[0]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]


def test_d_sanitizer_preserves_normal_history_messages():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": ROUND_1_USER},
        {"role": "assistant", "content": ROUND_1_ASSISTANT},
        {"role": "user", "content": ROUND_2_USER},
    ]

    assert sanitize_api_messages(messages) == messages


def test_native_ollama_payload_honors_context_and_preserves_history():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": ROUND_1_USER},
        {"role": "assistant", "content": ROUND_1_ASSISTANT},
        {"role": "user", "content": ROUND_2_USER},
    ]

    payload = build_native_payload(
        {
            "model": "qwen3.5:9b",
            "messages": messages,
            "extra_body": {"options": {"num_ctx": 65536}},
        }
    )

    assert payload["options"]["num_ctx"] == 65536
    assert payload["messages"] == messages
