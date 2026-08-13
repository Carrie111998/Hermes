"""MiniMax Anthropic endpoints keep their signed thinking blocks on replay.

MiniMax is an interleaved-thinking model: it signs its thinking blocks and
accepts them back verbatim on replayed assistant turns. Stripping them loses
the prior round's reasoning and degrades agentic performance. Both MiniMax
hosts must keep their blocks; an unrelated third-party host still loses them.
"""

from types import SimpleNamespace

from agent.transports import get_transport
from agent.anthropic_adapter import convert_messages_to_anthropic

SIG = "sig-mm"
MINIMAX_IO = "https://api.minimax.io/anthropic"
MINIMAXI = "https://api.minimaxi.com/anthropic"
OTHER_THIRD_PARTY = "https://api.example.com/anthropic"


def _thinking_on_replay(base_url, signature=SIG):
    """Normalize a thinking+text turn, store it, convert to the next-turn
    request, and return its thinking blocks."""
    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="five: 5 11 27 63 88", signature=signature),
            SimpleNamespace(type="text", text="5 27 88"),
        ],
        stop_reason="end_turn",
        usage=None,
    )
    normalized = get_transport("anthropic_messages").normalize_response(response)
    stored = {
        "role": "assistant",
        "content": normalized.content or "",
        "reasoning_details": (normalized.provider_data or {}).get("reasoning_details"),
    }
    messages = [
        {"role": "user", "content": "q1"},
        stored,
        {"role": "user", "content": "q2"},
    ]
    _sys, out = convert_messages_to_anthropic(messages, base_url=base_url, model="MiniMax-M3")
    assistant = [m for m in out if m.get("role") == "assistant"][0]
    return [b for b in assistant["content"] if isinstance(b, dict) and b.get("type") == "thinking"]


def test_minimax_io_keeps_signed_thinking():
    thinking = _thinking_on_replay(MINIMAX_IO)
    assert thinking and thinking[0].get("signature") == SIG, thinking


def test_minimaxi_keeps_signed_thinking():
    thinking = _thinking_on_replay(MINIMAXI)
    assert thinking and thinking[0].get("signature") == SIG, thinking


def test_unrelated_third_party_strips_thinking():
    thinking = _thinking_on_replay(OTHER_THIRD_PARTY)
    assert not thinking, thinking
