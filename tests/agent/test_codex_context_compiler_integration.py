from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.codex_runtime import (
    _compile_codex_app_server_input,
    run_codex_app_server_turn,
)


def _agent(*, context_window: int = 128_000, tools=()):
    return SimpleNamespace(
        session_id="hermes-session-1",
        model="codex-model",
        provider="openai-codex",
        max_tokens=8_000,
        context_compressor=SimpleNamespace(context_length=context_window),
        tools=list(tools),
        _cached_system_prompt="Hermes system instructions.",
        ephemeral_system_prompt=None,
    )


def _large_tool(index: int) -> dict:
    return {
        "type": "function",
        "function": {
            "name": f"office_tool_{index}",
            "description": "Office automation schema. " * 40,
            "parameters": {
                "type": "object",
                "properties": {
                    f"field_{part}": {
                        "type": "string",
                        "description": "Required synthetic schema detail. " * 10,
                    }
                    for part in range(8)
                },
            },
        },
    }


def test_codex_path_compiles_prior_canonical_history_and_current_turn() -> None:
    messages = [
        {"role": "user", "content": "Remember OLIVE-42.", "_row_id": 41},
        {"role": "assistant", "content": "Stored.", "_row_id": 42},
        {
            "role": "user",
            "content": "What was the code?",
            "_db_persisted": True,
            "_row_id": 43,
        },
    ]

    result = _compile_codex_app_server_input(
        _agent(),
        messages=messages,
        user_message="What was the code?",
        effective_task_id="turn-3",
        current_turn_user_idx=2,
    )

    assert result.compiled is not None
    compiled = result.compiled
    assert compiled.receipt.retained_event_ids == ("db:41", "db:42", "db:43")
    assert [message["role"] for message in compiled.messages] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert "OLIVE-42" in str(compiled.messages)
    assert compiled.messages[-1]["content"] == "What was the code?"


def test_mandatory_no_fit_stops_before_codex_session_call() -> None:
    agent = _agent(
        context_window=16_000,
        tools=tuple(_large_tool(index) for index in range(165)),
    )
    agent._codex_session = MagicMock()

    result = run_codex_app_server_turn(
        agent,
        user_message="Continue.",
        original_user_message="Continue.",
        messages=[{"role": "user", "content": "Continue."}],
        effective_task_id="turn-no-fit",
        current_turn_user_idx=0,
    )

    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert result["context_compilation_failure"]["reason"] == (
        "mandatory_envelope_exceeds_capacity"
    )
    agent._codex_session.run_turn.assert_not_called()


def test_same_hermes_revision_recompiles_after_provider_session_loss() -> None:
    messages = [
        {"role": "user", "content": "Remember OLIVE-42."},
        {"role": "assistant", "content": "Stored."},
        {"role": "user", "content": "Recall it."},
    ]
    first = _compile_codex_app_server_input(
        _agent(),
        messages=messages,
        user_message="Recall it.",
        effective_task_id="turn-restart",
        current_turn_user_idx=2,
    )
    replacement = _compile_codex_app_server_input(
        _agent(),
        messages=messages,
        user_message="Recall it.",
        effective_task_id="turn-restart",
        current_turn_user_idx=2,
    )

    assert first.compiled is not None and replacement.compiled is not None
    assert first.compiled.messages == replacement.compiled.messages
    assert first.compiled.context_fingerprint == replacement.compiled.context_fingerprint
    assert "OLIVE-42" in str(replacement.compiled.messages)
