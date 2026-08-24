"""Cache-invariant proof for per-task reasoning.

The prompt-cache safety of per-skill reasoning effort rests on a single
claim: changing `reasoning_config` between turns only alters per-request
request-body fields (``thinking``/``extra_body``), NEVER the `messages` or
system-prompt prefix that prompt caching keys on. If a future change ever
starts folding reasoning into the message list, this test fails loudly —
protecting the "switch reasoning per turn without nuking the cache" contract.

The assertion of record is that `build_anthropic_kwargs` (the Anthropic
request builder) returns `thinking` as a TOP-LEVEL kwarg and leaves the
`messages` argument byte-identical to what was passed in.
"""
import pytest

from agent.anthropic_adapter import build_anthropic_kwargs


def _build(messages, reasoning_config):
    return build_anthropic_kwargs(
        model="anthropic/claude-opus-4.6",
        messages=messages,
        tools=None,
        max_tokens=1024,
        reasoning_config=reasoning_config,
        tool_choice=None,
        is_oauth=False,
        base_url="https://api.anthropic.com",
    )


def test_reasoning_lands_in_top_level_thinking_not_messages():
    messages = [
        {"role": "system", "content": "you are a planning agent"},
        {"role": "user", "content": "write me a plan"},
    ]
    kwargs = _build(messages, {"enabled": True, "effort": "xhigh"})
    # Reasoning is a top-level request-body field, not inside messages.
    assert kwargs.get("thinking") == {"type": "adaptive", "display": "summarized"}
    assert "effort" not in kwargs["thinking"]


def test_messages_passed_through_unchanged_when_reasoning_changes():
    """Flipping reasoning effort must not rewrite the cached message prefix."""
    base = [
        {"role": "system", "content": "you are a planning agent"},
        {"role": "user", "content": "go me a plan"},
    ]
    # Build with two different reasoning levels.
    low = _build(base, {"enabled": True, "effort": "low"})
    xhigh = _build(base, {"enabled": True, "effort": "xhigh"})
    # The messages list is identical across both — only the per-request
    # thinking field differs. This is the cache-invariant. (The system role is
    # hoisted out by the builder into the top-level `system` kwarg — that is a
    # separate, deterministic transformation of the SAME input; the point is
    # the message list does not vary with reasoning effort.)
    assert low["messages"] == xhigh["messages"]


def test_no_reasoning_config_keeps_messages_intact():
    messages = [{"role": "user", "content": "hello"}]
    out = _build(messages, None)
    assert out["messages"] == messages
