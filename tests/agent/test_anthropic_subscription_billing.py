"""Behavioral coverage for Claude subscription billing request shaping."""

from unittest.mock import patch

from agent.anthropic_adapter import (
    _CLAUDE_CODE_BILLING_PREFIX,
    _CLAUDE_CODE_SYSTEM_PREFIX,
    _build_claude_code_billing_header,
    build_anthropic_kwargs,
)


def _build(messages, *, is_oauth=True):
    return build_anthropic_kwargs(
        model="claude-haiku-4-5",
        messages=messages,
        tools=None,
        max_tokens=64,
        reasoning_config=None,
        is_oauth=is_oauth,
    )


def test_billing_header_is_stable_as_conversation_grows():
    initial = [{"role": "user", "content": "Explain the cache key."}]
    continued = [
        *initial,
        {"role": "assistant", "content": "It is stable."},
        {"role": "user", "content": "Show an example."},
    ]

    first = _build_claude_code_billing_header(initial, version="2.1.112")
    later = _build_claude_code_billing_header(continued, version="2.1.112")
    changed = _build_claude_code_billing_header(
        [{"role": "user", "content": "A different first turn."}],
        version="2.1.112",
    )

    assert later == first
    assert changed != first
    assert first.startswith(
        "x-anthropic-billing-header: cc_version=2.1.112."
    )
    assert "; cc_entrypoint=sdk-cli; cch=" in first
    assert first.endswith(";")


def test_billing_header_uses_first_text_block_from_multimodal_user_turn():
    text_only = [{"role": "user", "content": "describe this"}]
    multimodal = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            {"type": "text", "text": "describe this"},
        ],
    }]

    assert _build_claude_code_billing_header(
        multimodal, version="2.1.112"
    ) == _build_claude_code_billing_header(text_only, version="2.1.112")


def test_oauth_kwargs_prefix_billing_identity_and_preserve_cache_marker():
    cache_control = {"type": "ephemeral"}
    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "x-anthropic-billing-header: stale"},
                {
                    "type": "text",
                    "text": "Hermes Agent system instructions",
                    "cache_control": cache_control,
                },
            ],
        },
        {"role": "user", "content": "Hello"},
    ]

    with patch(
        "agent.anthropic_adapter._get_claude_code_version",
        return_value="2.1.112",
    ):
        kwargs = _build(messages)

    system = kwargs["system"]
    assert system[0]["text"].startswith(_CLAUDE_CODE_BILLING_PREFIX)
    assert system[1] == {"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX}
    assert system[2] == {
        "type": "text",
        "text": "Claude Code system instructions",
        "cache_control": cache_control,
    }
    assert sum(
        isinstance(block.get("text"), str)
        and block["text"].startswith(_CLAUDE_CODE_BILLING_PREFIX)
        for block in system
    ) == 1


def test_api_key_kwargs_do_not_include_subscription_billing_marker():
    kwargs = _build(
        [
            {"role": "system", "content": "Ordinary API-key system prompt"},
            {"role": "user", "content": "Hello"},
        ],
        is_oauth=False,
    )

    system = kwargs["system"]
    system_text = system if isinstance(system, str) else "\n".join(
        block.get("text", "") for block in system if isinstance(block, dict)
    )
    assert _CLAUDE_CODE_BILLING_PREFIX not in system_text
    assert _CLAUDE_CODE_SYSTEM_PREFIX not in system_text
