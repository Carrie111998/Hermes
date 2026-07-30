"""Codex ``incomplete`` exhaustion must return what the model already wrote.

When ``api_mode == "codex_responses"`` and three consecutive responses come
back with ``finish_reason == "incomplete"``, the turn is given up.  The visible
text the model produced across those attempts is already parked in ``messages``
as interim assistant turns -- throwing it away and sending only the service
string "Codex response remained incomplete after 3 continuation attempts" is
what the user saw in Telegram after a long silence.
"""

import sys
import types
from types import SimpleNamespace

import pytest


sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

import run_agent


SERVICE_LINE = "Codex response remained incomplete after 3 continuation attempts"


@pytest.fixture(autouse=True)
def _no_codex_backoff(monkeypatch):
    """Short-circuit retry backoff so the Codex retry path doesn't sleep."""
    import time as _time
    monkeypatch.setattr(run_agent, "jittered_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)


def _build_agent(monkeypatch):
    monkeypatch.setattr(
        run_agent,
        "get_tool_definitions",
        lambda **kwargs: [
            {
                "type": "function",
                "function": {
                    "name": "terminal",
                    "description": "Run shell commands.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    )
    monkeypatch.setattr(run_agent, "check_toolset_requirements", lambda: {})

    agent = run_agent.AIAgent(
        model="gpt-5-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key="codex-token",
        quiet_mode=True,
        max_iterations=6,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._cleanup_task_resources = lambda task_id: None
    agent._persist_session = lambda messages, history=None: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    return agent


def _codex_incomplete_message_response(text: str):
    content = []
    if text:
        content.append(SimpleNamespace(type="output_text", text=text))
    return SimpleNamespace(
        output=[
            SimpleNamespace(
                type="message",
                status="in_progress",
                content=content,
            )
        ],
        usage=SimpleNamespace(input_tokens=4, output_tokens=2, total_tokens=6),
        status="in_progress",
        model="gpt-5-codex",
    )


def test_codex_incomplete_exhaustion_returns_accumulated_text(monkeypatch):
    """Three incomplete responses WITH content -> the user gets that content."""
    agent = _build_agent(monkeypatch)
    responses = [
        _codex_incomplete_message_response("Step one: the ledger reconciles."),
        _codex_incomplete_message_response(" Step two: the invoice matches."),
        _codex_incomplete_message_response(" Step three: remaining delta is"),
    ]
    monkeypatch.setattr(
        agent, "_interruptible_api_call", lambda api_kwargs: responses.pop(0)
    )

    result = agent.run_conversation("reconcile the ledger")

    final = result["final_response"]
    assert result["completed"] is False
    assert result["partial"] is True
    assert final != SERVICE_LINE
    assert "Step one: the ledger reconciles." in final
    assert "Step two: the invoice matches." in final
    assert "Step three: remaining delta is" in final
    # ... plus an honest note that the answer is cut off.
    assert "incomplete" in final.lower()
    # Programmatic error detection must keep working for the gateway.
    assert result["error"] == SERVICE_LINE


def test_codex_incomplete_exhaustion_without_text_keeps_service_message(monkeypatch):
    """Three incomplete responses with NO visible content -> old behaviour."""
    agent = _build_agent(monkeypatch)
    responses = [
        _codex_incomplete_message_response(""),
        _codex_incomplete_message_response(""),
        _codex_incomplete_message_response(""),
    ]
    monkeypatch.setattr(
        agent, "_interruptible_api_call", lambda api_kwargs: responses.pop(0)
    )

    result = agent.run_conversation("reconcile the ledger")

    assert result["final_response"] == SERVICE_LINE
    assert result["error"] == SERVICE_LINE
    assert result["completed"] is False
    assert result["partial"] is True
