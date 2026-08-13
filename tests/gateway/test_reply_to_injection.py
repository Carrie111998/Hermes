"""Tests for reply-to pointer injection in _prepare_inbound_message_text.

The `[Replying to: "..."]` prefix is a *disambiguation pointer*, not
deduplication. It must always be injected when the user explicitly replies
to a prior message — even when the quoted text already exists somewhere
in the conversation history. History can contain the same or similar text
multiple times, and without an explicit pointer the agent has to guess
which prior message the user is referencing.
"""
import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _make_runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake")},
    )
    runner.adapters = {}
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    return runner


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="123",
        chat_name="DM",
        chat_type="private",
        user_name="Alice",
    )


@pytest.mark.asyncio
async def test_reply_prefix_injected_when_text_absent_from_history():
    runner = _make_runner()
    source = _source()
    event = MessageEvent(
        text="What's the best time to go?",
        source=source,
        reply_to_message_id="42",
        reply_to_text="Japan is great for culture, food, and efficiency.",
    )

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=[{"role": "user", "content": "unrelated"}],
    )

    assert result is not None
    assert result.startswith(
        '[Replying to: "Japan is great for culture, food, and efficiency."]'
    )
    assert result.endswith("What's the best time to go?")


@pytest.mark.asyncio
async def test_reply_prefix_still_injected_when_text_in_history():
    """Regression test: the pointer must survive even when the quoted text
    already appears in history. Previously a `found_in_history` guard
    silently dropped the prefix, leaving the agent to guess which prior
    message the user was referencing."""
    runner = _make_runner()
    source = _source()
    quoted = "Japan is great for culture, food, and efficiency."
    event = MessageEvent(
        text="What's the best time to go?",
        source=source,
        reply_to_message_id="42",
        reply_to_text=quoted,
    )

    history = [
        {"role": "user", "content": "I'm thinking of going to Japan or Italy."},
        {
            "role": "assistant",
            "content": (
                f"{quoted} Italy is better if you prefer a relaxed pace."
            ),
        },
        {"role": "user", "content": "How long should I stay?"},
        {"role": "assistant", "content": "For Japan, 10-14 days is ideal."},
    ]

    result = await runner._prepare_inbound_message_text(
        event=event,
        source=source,
        history=history,
    )

    assert result is not None
    assert result.startswith(f'[Replying to: "{quoted}"]')
    assert result.endswith("What's the best time to go?")




@pytest.mark.asyncio
async def test_long_quote_is_marked_as_truncated():
    """A clipped quote must say so, with the count of what was dropped.

    Issue #84920. The snippet was cut at 500 chars silently, so the agent could
    not distinguish a genuinely short quote from a long one that had been cut.
    The observed failure: a user replies to a long report the agent itself
    delivered, the agent sees its own text ending mid-sentence, concludes the
    *outbound* delivery was truncated, and re-sends the "missing" remainder —
    when the delivery was complete and only the injected quote was clipped.
    """
    runner = _make_runner()
    source = _source()
    quoted = "A" * 637
    event = MessageEvent(
        text="what about the second half?",
        source=source,
        reply_to_message_id="42",
        reply_to_text=quoted,
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[],
    )

    assert result is not None
    # 500 kept, 137 elided — the count tells the agent how much it is missing,
    # which is what makes fetching the original a decision rather than a guess.
    assert result.startswith(f'[Replying to: "{"A" * 500}...[137 more chars]"]')
    assert result.endswith("what about the second half?")


@pytest.mark.asyncio
@pytest.mark.parametrize("length", [1, 499, 500])
async def test_quote_at_or_under_the_cap_is_left_alone(length: int):
    """No marker unless something was actually dropped.

    The boundary matters: a quote of exactly the cap is complete, and marking it
    would assert data loss that did not happen — the same ambiguity in reverse.
    """
    runner = _make_runner()
    source = _source()
    quoted = "B" * length
    event = MessageEvent(
        text="follow-up",
        source=source,
        reply_to_message_id="42",
        reply_to_text=quoted,
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[],
    )

    assert result is not None
    assert result.startswith(f'[Replying to: "{quoted}"]')
    assert "more chars]" not in result


@pytest.mark.asyncio
async def test_quote_one_char_over_the_cap_is_marked():
    """The other half of the boundary: 500 is clean, 501 is marked.

    Paired with the case above this pins the comparison exactly. A quote one
    char over is the smallest real loss there is, and the marker has to report
    it as `[1 more chars]` — an off-by-one that reported `[0 more chars]` would
    be the silent-truncation bug wearing the fix's clothes.
    """
    runner = _make_runner()
    source = _source()
    quoted = "D" * 501
    event = MessageEvent(
        text="and the rest?",
        source=source,
        reply_to_message_id="42",
        reply_to_text=quoted,
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[],
    )

    assert result is not None
    assert result.startswith(f'[Replying to: "{"D" * 500}...[1 more chars]"]')


@pytest.mark.asyncio
async def test_own_message_branch_marks_truncation_too():
    """Both render paths read the same snippet, so both must carry the marker."""
    runner = _make_runner()
    source = _source()
    quoted = "C" * 700
    event = MessageEvent(
        text="continue",
        source=source,
        reply_to_message_id="42",
        reply_to_text=quoted,
        reply_to_is_own_message=True,
    )

    result = await runner._prepare_inbound_message_text(
        event=event, source=source, history=[],
    )

    assert result is not None
    assert result.startswith(
        f'[Replying to your previous message: "{"C" * 500}...[200 more chars]"]'
    )
