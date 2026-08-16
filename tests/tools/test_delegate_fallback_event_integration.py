"""Behavior-level coverage for delegated fallback diagnostics."""

from __future__ import annotations

import json
import threading
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
from openai import NotFoundError

from agent.chat_completion_helpers import sanitize_fallback_event
from run_agent import AIAgent
from tools.delegate_tool import delegate_task


def _parent():
    parent = MagicMock()
    parent.base_url = "https://primary.invalid/v1"
    parent.api_key = "test-key"
    parent.provider = "ollama"
    parent.api_mode = "chat_completions"
    parent.model = "nemotron-3.5-lightning:30b-mlx"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._credential_pool = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _completion(text: str):
    message = SimpleNamespace(
        role="assistant",
        content=text,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="glm-5.2",
        usage=None,
    )


def test_raw_child_exception_reaches_delegate_as_only_sanitized_fallback_event(
    tmp_path, monkeypatch
):
    """Exercise raw exception -> classifier -> fallback -> delegate result."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    secret_sentinel = "SENTINEL_SECRET_MUST_NOT_SURVIVE"
    prompt_sentinel = "SENTINEL_PROMPT_MUST_NOT_SURVIVE"
    overlong_value = "Q" * 10_000
    primary_error = RuntimeError(
        f"rate limited {secret_sentinel} {prompt_sentinel} {overlong_value}"
    )
    primary_error.status_code = 429

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        child = AIAgent(
            api_key="test-key",
            base_url="https://primary.invalid/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=4,
            fallback_model=[
                {
                    "provider": "ollama-cloud",
                    "model": "glm-5.2",
                    "api_mode": "chat_completions",
                }
            ],
        )

    child.provider = "ollama"
    child.requested_provider = "ollama"
    child.model = "nemotron-3.5-lightning:30b-mlx"
    child.base_url = "http://localhost:11434/v1"
    child._primary_runtime["provider"] = "ollama"
    child._primary_runtime["model"] = child.model
    primary_client = MagicMock()
    primary_client.chat.completions.create.side_effect = primary_error
    child.client = primary_client

    fallback_client = MagicMock()
    fallback_client.base_url = "https://ollama.com/v1"
    fallback_client.api_key = "test-key"
    fallback_client.chat.completions.create.return_value = _completion("done")

    with (
        patch("tools.delegate_tool._load_config", return_value={}),
        patch("tools.delegate_tool._build_child_agent", return_value=child),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, "glm-5.2"),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, provider: model,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=128_000),
        patch.object(child, "_persist_session"),
        patch.object(child, "_save_trajectory"),
        patch.object(child, "_cleanup_task_resources"),
    ):
        payload = json.loads(
            delegate_task(goal="Exercise fallback diagnostics", parent_agent=_parent())
        )

    entry = payload["results"][0]
    assert entry["status"] == "completed"
    assert entry["model"] == "glm-5.2"
    assert entry["fallback_event"] == {
        "initial_provider": "ollama",
        "initial_model": "nemotron-3.5-lightning:30b-mlx",
        "selected_fallback_provider": "ollama-cloud",
        "selected_fallback_model": "glm-5.2",
        "failure_class": "quota",
        "reason_code": "rate_limit",
        "http_status": 429,
    }
    assert child._last_fallback_event == entry["fallback_event"]
    serialized = json.dumps(entry["fallback_event"], sort_keys=True)
    serialized_payload = json.dumps(payload, sort_keys=True)
    assert set(entry["fallback_event"]) == {
        "initial_provider",
        "initial_model",
        "selected_fallback_provider",
        "selected_fallback_model",
        "failure_class",
        "reason_code",
        "http_status",
    }
    assert secret_sentinel not in serialized
    assert prompt_sentinel not in serialized
    assert overlong_value not in serialized
    assert secret_sentinel not in serialized_payload
    assert prompt_sentinel not in serialized_payload
    assert overlong_value not in serialized_payload
    assert len(serialized.encode("utf-8")) <= 768


def _loop_child(*, base_url: str, model: str, max_iterations: int) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        child = AIAgent(
            api_key="test-key",
            base_url=base_url,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=max_iterations,
            fallback_model=[
                {
                    "provider": "ollama-cloud",
                    "model": "glm-5.2",
                    "api_mode": "chat_completions",
                }
            ],
        )

    child.provider = "ollama"
    child.requested_provider = "ollama"
    child.model = model
    child.base_url = base_url
    child._api_max_retries = 3
    child._primary_runtime["provider"] = "ollama"
    child._primary_runtime["model"] = child.model
    return child


def _run_loop_with_fallback(child: AIAgent, primary_error: Exception):
    calls = []

    def fake_api_call(_api_kwargs):
        calls.append((child.provider, child.model))
        if child.provider == "ollama":
            raise primary_error
        return _completion("done")

    fallback_client = MagicMock()
    fallback_client.base_url = "https://ollama.com/v1"
    fallback_client.api_key = "test-key"

    with (
        patch.object(child, "_interruptible_api_call", side_effect=fake_api_call),
        patch.object(child, "_persist_session"),
        patch.object(child, "_save_trajectory"),
        patch.object(child, "_cleanup_task_resources"),
        patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, "glm-5.2"),
        ),
        patch(
            "hermes_cli.model_normalize.normalize_model_for_provider",
            side_effect=lambda model, provider: model,
        ),
        patch("agent.model_metadata.get_model_context_length", return_value=128_000),
        patch("agent.conversation_loop.jittered_backoff", return_value=0.0),
    ):
        result = child.run_conversation("hello")
    return result, calls


def _assert_sanitized_event(event, expected, sentinels):
    assert event == expected
    serialized = json.dumps(event, sort_keys=True)
    assert set(event) == {
        "initial_provider",
        "initial_model",
        "selected_fallback_provider",
        "selected_fallback_model",
        "failure_class",
        "reason_code",
        "http_status",
    }
    for sentinel in sentinels:
        assert sentinel not in serialized
    assert len(serialized.encode("utf-8")) <= 768


def test_sdk_not_found_max_retry_fallback_retains_only_safe_http_status(
    tmp_path, monkeypatch
):
    """A retried SDK 404 keeps its safe status when fallback activates."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    secret_sentinel = "SENTINEL_SECRET_MUST_NOT_SURVIVE"
    prompt_sentinel = "SENTINEL_PROMPT_MUST_NOT_SURVIVE"
    body_sentinel = "SENTINEL_BODY_MUST_NOT_SURVIVE"
    primary_error = NotFoundError(
        message=f"route missing {secret_sentinel} {prompt_sentinel}",
        response=httpx.Response(
            404,
            request=httpx.Request(
                "POST", "http://localhost:11434/chat/completions"
            ),
        ),
        body={"error": {"message": body_sentinel}},
    )
    child = _loop_child(
        base_url="http://localhost:11434",
        model="nemotron-3.5-lightning:30b-mlx",
        max_iterations=4,
    )
    result, calls = _run_loop_with_fallback(child, primary_error)

    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert calls == [
        ("ollama", "nemotron-3.5-lightning:30b-mlx"),
        ("ollama", "nemotron-3.5-lightning:30b-mlx"),
        ("ollama", "nemotron-3.5-lightning:30b-mlx"),
        ("ollama-cloud", "glm-5.2"),
    ]
    _assert_sanitized_event(
        child._last_fallback_event,
        {
            "initial_provider": "ollama",
            "initial_model": "nemotron-3.5-lightning:30b-mlx",
            "selected_fallback_provider": "ollama-cloud",
            "selected_fallback_model": "glm-5.2",
            "failure_class": "unknown",
            "reason_code": "unknown",
            "http_status": 404,
        },
        (secret_sentinel, prompt_sentinel, body_sentinel),
    )


def test_nonretryable_sdk_error_fallback_retains_classification_without_raw_data(
    tmp_path, monkeypatch
):
    """A non-retryable SDK error forwards only classified fallback facts."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    secret_sentinel = "SENTINEL_SECRET_MUST_NOT_SURVIVE"
    body_sentinel = "SENTINEL_BODY_MUST_NOT_SURVIVE"
    primary_error = NotFoundError(
        message=f"model not found {secret_sentinel}",
        response=httpx.Response(
            404,
            request=httpx.Request(
                "POST", "http://localhost:11434/v1/chat/completions"
            ),
        ),
        body={"error": {"message": f"model not found {body_sentinel}"}},
    )
    child = _loop_child(
        base_url="http://localhost:11434/v1",
        model="missing-model",
        max_iterations=2,
    )
    result, calls = _run_loop_with_fallback(child, primary_error)

    assert result["completed"] is True
    assert result["final_response"] == "done"
    assert calls == [
        ("ollama", "missing-model"),
        ("ollama-cloud", "glm-5.2"),
    ]
    _assert_sanitized_event(
        child._last_fallback_event,
        {
            "initial_provider": "ollama",
            "initial_model": "missing-model",
            "selected_fallback_provider": "ollama-cloud",
            "selected_fallback_model": "glm-5.2",
            "failure_class": "policy",
            "reason_code": "model_not_found",
            "http_status": 404,
        },
        (secret_sentinel, body_sentinel),
    )


def test_fallback_event_rejects_unknown_types_bool_status_and_extra_payloads():
    secret_sentinel = "SENTINEL_SECRET_MUST_NOT_SURVIVE"
    event = sanitize_fallback_event(
        {
            "initial_provider": "ollama",
            "initial_model": "nemotron-3.5-lightning:30b-mlx",
            "selected_fallback_provider": "ollama-cloud",
            "selected_fallback_model": "glm-5.2",
            "failure_class": ["availability"],
            "reason_code": {"value": "overloaded"},
            "http_status": True,
            "raw_exception": secret_sentinel,
            "headers": {"authorization": secret_sentinel},
        }
    )

    assert event is not None
    assert event["failure_class"] == "unknown"
    assert event["reason_code"] == "unknown"
    assert event["http_status"] is None
    assert set(event) == {
        "initial_provider",
        "initial_model",
        "selected_fallback_provider",
        "selected_fallback_model",
        "failure_class",
        "reason_code",
        "http_status",
    }
    assert secret_sentinel not in json.dumps(event, sort_keys=True)
    assert sanitize_fallback_event(MappingProxyType(dict(event))) is None
    assert sanitize_fallback_event({**event, "initial_model": "M" * 121}) is None
