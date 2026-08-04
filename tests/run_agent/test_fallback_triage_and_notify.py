"""Behavior contract for config-driven triage-and-notify fallback policy.

All provider calls in this module are fake clients.  The tests deliberately
exercise a Codex-shaped primary failure without making a network request.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


class RateLimitError(Exception):
    status_code = 429

    def __init__(self) -> None:
        super().__init__("Error code: 429 - primary quota exhausted")
        self.response = SimpleNamespace(headers={})


def _response(content: str) -> SimpleNamespace:
    message = SimpleNamespace(content=content, tool_calls=None)
    choice = SimpleNamespace(message=message, finish_reason="stop")
    return SimpleNamespace(choices=[choice], model="fake/local", usage=None)


def _make_agent(*, fallback_model, platform: str = "cli") -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI", return_value=MagicMock()),
    ):
        agent = AIAgent(
            api_key="primary-test-key",
            base_url="https://primary.invalid/v1",
            provider="openai",
            model="gpt-5.6-terra",
            api_mode="chat_completions",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            platform=platform,
            fallback_model=fallback_model,
        )
    # The test models a Codex primary while keeping the chat-completions wire
    # shape small and deterministic.
    agent.provider = "openai-codex"
    agent._cached_system_prompt = "Test system prompt"
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 1
    return agent


def _run_turn(
    agent: AIAgent,
    api_calls: list[tuple[str, str]],
    *,
    tool_bomb=None,
    forbid_finalizer_side_effects: bool = False,
    user_message: str = "perform consequential work",
    conversation_history: list[dict] | None = None,
    primary_response=None,
):
    def fake_primary_or_continuation(_api_kwargs):
        api_calls.append((agent.provider, agent.model))
        if primary_response is not None:
            return primary_response
        if len(api_calls) == 1:
            raise RateLimitError()
        return _response("continued by fallback")

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(agent, "_interruptible_api_call", side_effect=fake_primary_or_continuation)
        )
        stack.enter_context(patch.object(agent, "_persist_session"))
        stack.enter_context(patch.object(agent, "_save_trajectory"))
        stack.enter_context(patch.object(agent, "_cleanup_task_resources"))
        stack.enter_context(patch.object(agent, "_try_recover_primary_transport", return_value=False))
        stack.enter_context(patch("agent.conversation_loop.time.sleep"))
        stack.enter_context(
            patch(
                "run_agent.handle_function_call",
                side_effect=tool_bomb or AssertionError("tool execution is forbidden"),
            )
        )
        if forbid_finalizer_side_effects:
            stack.enter_context(
                patch.object(
                    agent,
                    "_sync_external_memory_for_turn",
                    side_effect=AssertionError("external memory sync is forbidden"),
                )
            )
            stack.enter_context(
                patch.object(
                    agent,
                    "_spawn_background_review",
                    side_effect=AssertionError("background review is forbidden"),
                )
            )
            stack.enter_context(
                patch(
                    "agent.conversation_loop._notify_context_engine_turn_complete",
                    side_effect=AssertionError("context observation is forbidden"),
                )
            )

            def lifecycle_hook(name, *args, **kwargs):
                assert name not in {"transform_llm_output", "post_llm_call", "on_session_end"}
                return []

            stack.enter_context(patch("hermes_cli.lifecycle.invoke_hook", side_effect=lifecycle_hook))
        if conversation_history is None:
            return agent.run_conversation(user_message)
        return agent.run_conversation(user_message, conversation_history=conversation_history)


def test_policy_absent_preserves_existing_fallback_continuation():
    """Absent policy keeps the pre-existing full fallback continuation path."""
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "custom",
                "model": "qwen3:8b",
                "base_url": "http://127.0.0.1:11434/v1",
            }
        ]
    )
    fallback_client = MagicMock()
    fallback_client.base_url = "http://127.0.0.1:11434/v1"
    fallback_client.api_key = "local-test-key"
    api_calls: list[tuple[str, str]] = []

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(fallback_client, "qwen3:8b"),
    ) as resolve_fallback:
        result = _run_turn(agent, api_calls)

    assert result["completed"] is True
    assert result.get("held") is not True
    assert result["final_response"] == "continued by fallback"
    assert api_calls == [
        ("openai-codex", "gpt-5.6-terra"),
        ("custom", "qwen3:8b"),
    ]
    resolve_fallback.assert_called_once()


def test_explicit_continue_preserves_existing_fallback_continuation():
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "custom",
                "model": "qwen3:8b",
                "base_url": "http://127.0.0.1:11434/v1",
                "failure_policy": "continue",
            }
        ]
    )
    fallback_client = MagicMock()
    fallback_client.base_url = "http://127.0.0.1:11434/v1"
    fallback_client.api_key = "local-test-key"
    api_calls: list[tuple[str, str]] = []

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(fallback_client, "qwen3:8b"),
    ) as resolve_fallback:
        result = _run_turn(agent, api_calls)

    assert result["completed"] is True
    assert result.get("held") is not True
    assert result["final_response"] == "continued by fallback"
    assert api_calls == [
        ("openai-codex", "gpt-5.6-terra"),
        ("custom", "qwen3:8b"),
    ]
    resolve_fallback.assert_called_once()


def test_malformed_policy_holds_before_resolution_tools_or_later_continuation():
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "custom",
                "model": "malformed-boundary",
                "failure_policy": "triage_and_notfiy",
            },
            {
                "provider": "openrouter",
                "model": "must-not-run",
                "failure_policy": "continue",
            },
        ]
    )
    api_calls: list[tuple[str, str]] = []
    fallback_client = MagicMock()
    fallback_client.base_url = "https://openrouter.ai/api/v1"
    fallback_client.api_key = "must-not-be-used"

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(fallback_client, "must-not-run"),
    ) as resolve_fallback:
        result = _run_turn(agent, api_calls)

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_policy_invalid"
    assert "invalid" in result["final_response"].lower()
    assert "failure_policy" in result["final_response"]
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]
    resolve_fallback.assert_not_called()
    assert agent.provider == "openai-codex"
    assert agent.model == "gpt-5.6-terra"


def test_missing_auth_nous_triage_holds_before_later_continuation():
    """Quinn Q-D2-C-01-002 exact normal-turn missing-auth regression."""
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "nous",
                "model": "local-emergency",
                "failure_policy": "triage_and_notify",
            },
            {
                "provider": "custom",
                "model": "must-not-run",
                "failure_policy": "continue",
            },
        ]
    )
    api_calls: list[tuple[str, str]] = []

    with (
        patch("hermes_cli.auth.get_provider_auth_state", return_value={}),
        patch("agent.auxiliary_client.resolve_provider_client") as resolve_fallback,
    ):
        result = _run_turn(agent, api_calls)

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_triage_held"
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]
    resolve_fallback.assert_not_called()


def test_cron_missing_auth_nous_triage_reports_held_notifier_failure_without_continuation():
    """Quinn Q-D2-C-01-002 exact cron missing-auth regression."""
    agent = _make_agent(
        platform="cron",
        fallback_model=[
            {
                "provider": "nous",
                "model": "local-emergency",
                "failure_policy": "triage_and_notify",
            },
            {
                "provider": "custom",
                "model": "must-not-run",
                "failure_policy": "continue",
            },
        ],
    )
    api_calls: list[tuple[str, str]] = []

    with (
        patch("hermes_cli.auth.get_provider_auth_state", return_value={}),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(None, None),
        ) as resolve_fallback,
    ):
        result = _run_turn(agent, api_calls)

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_triage_local_failed"
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]
    resolve_fallback.assert_called_once()
    assert resolve_fallback.call_args.args[0] == "nous"


def test_same_backend_triage_holds_before_later_continuation():
    """Quinn Q-D2-C-01-002 exact same-backend regression."""
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "openai-codex",
                "model": "gpt-5.6-terra",
                "base_url": "https://primary.invalid/v1",
                "failure_policy": "triage_and_notify",
            },
            {
                "provider": "custom",
                "model": "must-not-run",
                "failure_policy": "continue",
            },
        ]
    )
    api_calls: list[tuple[str, str]] = []

    with patch("agent.auxiliary_client.resolve_provider_client") as resolve_fallback:
        result = _run_turn(agent, api_calls)

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_triage_held"
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]
    resolve_fallback.assert_not_called()


def test_normal_triage_policy_alerts_and_holds_without_local_continuation():
    """A normal high-capability turn is held, not continued on the local model."""
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "custom",
                "model": "qwen3:8b",
                "base_url": "http://127.0.0.1:11434/v1",
                "failure_policy": "triage_and_notify",
            }
        ]
    )
    api_calls: list[tuple[str, str]] = []
    emitted: list[str] = []
    prior_history = [
        {"role": "user", "content": "previous operational request"},
        {"role": "assistant", "content": "previous completed response"},
    ]
    original_history = deepcopy(prior_history)
    huge_primary_context = "PRIMARY_CONTEXT_MUST_NOT_TRANSFER_NORMAL_" + ("x" * 220_000)
    agent._emit_status = emitted.append
    fallback_client = MagicMock()
    fallback_client.base_url = "http://127.0.0.1:11434/v1"
    fallback_client.api_key = "local-test-key"

    with patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(fallback_client, "qwen3:8b"),
    ) as resolve_fallback:
        result = _run_turn(
            agent,
            api_calls,
            forbid_finalizer_side_effects=True,
            user_message=huge_primary_context,
            conversation_history=prior_history,
        )
    assert result["completed"] is False
    assert result["failed"] is True
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_triage_held"
    assert "held" in result["final_response"].lower()
    assert any("fallback" in message.lower() and "held" in message.lower() for message in emitted)
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]
    resolve_fallback.assert_not_called()
    assert agent.provider == "openai-codex"
    assert agent.model == "gpt-5.6-terra"
    assert agent._cached_system_prompt == "Test system prompt"
    assert [message["role"] for message in result["messages"][-2:]] == ["user", "assistant"]
    assert prior_history == original_history
    non_system_messages = [message for message in result["messages"] if message.get("role") != "system"]
    assert [message["role"] for message in non_system_messages] == ["user", "assistant", "user", "assistant"]
    assert non_system_messages[:2] == original_history
    assert [message["content"] for message in non_system_messages if message["role"] == "user"] == [
        "previous operational request",
        huge_primary_context,
    ]


def test_empty_response_triage_overrides_stale_empty_final_response():
    """A triage hold wins even when the triggering primary response was empty."""
    agent = _make_agent(
        fallback_model=[
            {
                "provider": "custom",
                "model": "qwen3:8b",
                "base_url": "http://127.0.0.1:11434/v1",
                "failure_policy": "triage_and_notify",
            }
        ]
    )
    api_calls: list[tuple[str, str]] = []

    with patch("agent.auxiliary_client.resolve_provider_client") as resolve_fallback:
        result = _run_turn(
            agent,
            api_calls,
            forbid_finalizer_side_effects=True,
            primary_response=_response(""),
        )

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_triage_held"
    assert "held" in result["final_response"].lower()
    assert api_calls == [("openai-codex", "gpt-5.6-terra")] * 4
    resolve_fallback.assert_not_called()


def test_chain_exhaustion_remains_an_explicit_terminal_outcome():
    """No fallback chain keeps the existing explicit terminal error behavior."""
    agent = _make_agent(fallback_model=[])
    api_calls: list[tuple[str, str]] = []

    result = _run_turn(agent, api_calls)

    assert result["completed"] is False
    assert result["failed"] is True
    assert result.get("held") is not True
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]


def test_scheduled_triage_uses_only_bounded_toolless_local_notification_context():
    """Cron is the source-metadata lane allowed one isolated local triage call."""
    agent = _make_agent(
        platform="cron",
        fallback_model=[
            {
                "provider": "custom",
                "model": "qwen3:8b",
                "base_url": "http://127.0.0.1:11434/v1",
                "failure_policy": "triage_and_notify",
            }
        ],
    )
    huge_primary_context = "PRIMARY_CONTEXT_MUST_NOT_TRANSFER_" + ("x" * 220_000)
    api_calls: list[tuple[str, str]] = []
    local_client = MagicMock()
    local_client.chat.completions.create.return_value = _response("local triage acknowledged")

    def fake_primary(_api_kwargs):
        api_calls.append((agent.provider, agent.model))
        if agent.provider == "openai-codex":
            raise RateLimitError()
        return _response("unsafe fallback continuation")

    def lifecycle_hook(name, *args, **kwargs):
        assert name not in {"transform_llm_output", "post_llm_call", "on_session_end"}
        return []

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_primary),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch("agent.conversation_loop.time.sleep"),
        patch("run_agent.handle_function_call", side_effect=AssertionError("no tool execution")) as tools,
        patch.object(
            agent,
            "_sync_external_memory_for_turn",
            side_effect=AssertionError("external memory sync is forbidden"),
        ),
        patch.object(
            agent,
            "_spawn_background_review",
            side_effect=AssertionError("background review is forbidden"),
        ),
        patch(
            "agent.conversation_loop._notify_context_engine_turn_complete",
            side_effect=AssertionError("context observation is forbidden"),
        ),
        patch("hermes_cli.lifecycle.invoke_hook", side_effect=lifecycle_hook),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(local_client, "qwen3:8b"),
        ) as resolve_fallback,
    ):
        result = agent.run_conversation(huge_primary_context)

    assert result["completed"] is False
    assert result["failed"] is False
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_triage_notified"
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]
    resolve_fallback.assert_called_once()
    tools.assert_not_called()

    triage_kwargs = local_client.chat.completions.create.call_args.kwargs
    assert triage_kwargs["model"] == "qwen3:8b"
    assert triage_kwargs["max_tokens"] <= 256
    assert triage_kwargs["timeout"] <= 20
    assert "tools" not in triage_kwargs
    serialized_triage = repr(triage_kwargs["messages"])
    assert huge_primary_context not in serialized_triage
    assert "PRIMARY_CONTEXT_MUST_NOT_TRANSFER" not in serialized_triage
    assert len(serialized_triage) < 2_000


def test_scheduled_local_triage_failure_is_explicit_held_terminal_outcome():
    """A failed local triage check is visible; it never resumes the original job."""
    agent = _make_agent(
        platform="cron",
        fallback_model=[
            {
                "provider": "custom",
                "model": "qwen3:8b",
                "base_url": "http://127.0.0.1:11434/v1",
                "failure_policy": "triage_and_notify",
            }
        ],
    )
    api_calls: list[tuple[str, str]] = []
    local_client = MagicMock()
    local_client.chat.completions.create.side_effect = RuntimeError("local unavailable")

    def fake_primary(_api_kwargs):
        api_calls.append((agent.provider, agent.model))
        if agent.provider == "openai-codex":
            raise RateLimitError()
        return _response("unsafe fallback continuation")

    with (
        patch.object(agent, "_interruptible_api_call", side_effect=fake_primary),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_try_recover_primary_transport", return_value=False),
        patch("agent.conversation_loop.time.sleep"),
        patch("run_agent.handle_function_call", side_effect=AssertionError("no tool execution")),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(local_client, "qwen3:8b"),
        ),
    ):
        result = agent.run_conversation("watch operational state")

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["held"] is True
    assert result["turn_exit_reason"] == "fallback_triage_local_failed"
    assert "local triage" in result["final_response"].lower()
    assert api_calls == [("openai-codex", "gpt-5.6-terra")]
