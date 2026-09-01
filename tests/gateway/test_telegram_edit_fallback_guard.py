"""The finalize-edit plain-text fallback must not fire on flood control.

``TelegramAdapter.edit_message`` sends the finalized reply with
``parse_mode=MarkdownV2``. When that call raises, an inner handler rewrites the
message as stripped plain text. That rescue is correct for a MarkdownV2 parse
failure, where the markup is what Telegram rejected.

It was applied to flood control too, because the handler caught bare
``Exception``. A ``RetryAfter`` refusal says nothing about the markup, so a
reply whose MarkdownV2 was perfectly valid arrived with its markdown syntax
showing: headings as a literal ``##``, links as a literal ``[text](url)``.

The outer handler in the same method was already written for this: a short
wait retries inline with the formatted content, and an over-cap wait fails
closed so streaming falls back to a normal final send. None of it could run
while the inner handler swallowed the exception first.

The guard is deliberately narrow. Timeouts and network blips keep the
plain-text rescue, because re-raising them returns ``retryable=True`` and
``GatewayStreamConsumer`` does not consult ``retryable`` on an edit failure:
it enters tail-send fallback. A timeout can mean Telegram applied the edit and
only the response was lost, so widening the guard would let a timed-out
finalize edit have its tail sent again on top of the complete answer. That
belongs with the consumer, not here, and
``test_transient_network_error_keeps_the_plain_text_rescue`` pins the
narrowness so it is a decision rather than an oversight.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig


def _ensure_telegram_mock():
    if "telegram" in sys.modules and hasattr(sys.modules["telegram"], "__file__"):
        return
    telegram_mod = MagicMock()
    telegram_mod.ext.ContextTypes.DEFAULT_TYPE = type(None)
    telegram_mod.constants.ParseMode.MARKDOWN_V2 = "MarkdownV2"
    telegram_mod.constants.ChatType.GROUP = "group"
    telegram_mod.constants.ChatType.SUPERGROUP = "supergroup"
    telegram_mod.constants.ChatType.CHANNEL = "channel"
    telegram_mod.constants.ChatType.PRIVATE = "private"
    for name in ("telegram", "telegram.ext", "telegram.constants", "telegram.request"):
        sys.modules.setdefault(name, telegram_mod)


_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402


CHAT_ID = "5230977008"
MESSAGE_ID = "4242"

# A reply shaped like the one that exposed this: headings plus bold and a
# link, all of which format_message converts into valid MarkdownV2.
CONTENT = (
    "## Best souvlaki choice\n"
    "\n"
    "Go to **[Athinaiko Souvlaki](https://maps.example/athinaiko)** at "
    "**Karolou Ntil 23**.\n"
    "\n"
    "- **4.8/5 from 1,025 reviews**\n"
    "- Open today **13:00 to 22:00**\n"
)

_PARSE_ERROR = "Bad Request: can't parse entities: Character '-' is reserved"


class _FloodError(Exception):
    """Mirrors python-telegram-bot's RetryAfter: carries ``retry_after``.

    ``retry_after`` may be a float or, under ``PTB_TIMEDELTA=1``, a
    ``datetime.timedelta``. Both shapes are exercised below.
    """

    def __init__(self, retry_after):
        super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
        self.retry_after = retry_after


class _TextOnlyFloodError(Exception):
    """A flood refusal with no ``retry_after`` attribute at all.

    Not every raiser is PTB's RetryAfter. Detection and the wait have to come
    off Telegram's own wording, which says "Retry in Ns" rather than the
    "retry after" the outer handler historically looked for.
    """

    def __init__(self, seconds):
        super().__init__(f"Flood control exceeded. Retry in {seconds} seconds")


class _RecordingBot:
    """Records every edit attempt and replays a scripted failure sequence.

    ``script`` is consumed one entry per call: an exception is raised, ``None``
    succeeds. Calls past the end of the script succeed, so a short script means
    "fail these, then work".
    """

    def __init__(self, script=()):
        self.script = list(script)
        self.calls: list[dict] = []

    async def edit_message_text(self, **kwargs):
        self.calls.append(kwargs)
        idx = len(self.calls) - 1
        if idx < len(self.script) and self.script[idx] is not None:
            raise self.script[idx]
        return MagicMock(message_id=int(MESSAGE_ID))

    @property
    def formatted_calls(self) -> list[dict]:
        return [c for c in self.calls if c.get("parse_mode")]

    @property
    def plain_calls(self) -> list[dict]:
        return [c for c in self.calls if not c.get("parse_mode")]


def _adapter(bot) -> TelegramAdapter:
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))
    adapter._bot = bot
    return adapter


def _edit(adapter, content: str = CONTENT):
    return asyncio.run(
        adapter.edit_message(CHAT_ID, MESSAGE_ID, content, finalize=True)
    )


# ---------------------------------------------------------------------------
# The regression: a flood refusal must not be mistaken for bad markup.
# ---------------------------------------------------------------------------

def test_over_cap_flood_does_not_rewrite_the_reply_as_plain_text():
    """An over-cap flood wait fails closed and leaves formatting alone."""
    bot = _RecordingBot([_FloodError(269.0)])
    result = _edit(_adapter(bot))

    assert bot.plain_calls == [], (
        "flood control is not a markup problem, so the reply must never be "
        f"re-sent unformatted; got {len(bot.plain_calls)} plain-text edit(s)"
    )
    assert result.success is False
    assert result.error == "flood_control:269.0"
    assert result.retry_after == pytest.approx(269.0)


def test_under_cap_flood_retries_with_the_formatted_content():
    """A short flood wait is retried inline, not downgraded to plain text."""
    bot = _RecordingBot([_FloodError(1.0)])
    result = _edit(_adapter(bot))

    assert result.success is True
    assert bot.plain_calls == [], "the inline flood retry must keep the markup"
    assert len(bot.calls) == 2, "expected the failed edit plus one inline retry"

    # The retry must carry the same MarkdownV2 render as the first attempt,
    # not the raw markdown the user would otherwise see.
    first, retry = bot.calls
    assert retry["text"] == first["text"]
    assert retry["parse_mode"] == first["parse_mode"]
    assert "## Best souvlaki choice" not in retry["text"], (
        "the retry re-sent raw markdown, so the heading syntax would show"
    )


@pytest.mark.parametrize("retry_after", [
    3.0,
    timedelta(seconds=3),
])
def test_flood_wait_is_normalised_whatever_shape_retry_after_has(retry_after):
    """``retry_after`` is a timedelta under PTB_TIMEDELTA=1.

    Comparing that against the inline wait cap raises TypeError from inside
    the outer exception handler, which would escape ``edit_message`` without
    retrying or returning a SendResult. The float path is only reachable in
    the default mode.
    """
    bot = _RecordingBot([_FloodError(retry_after)])
    result = _edit(_adapter(bot))

    assert result.success is True
    assert len(bot.calls) == 2
    assert bot.plain_calls == []


# ---------------------------------------------------------------------------
# The rescue that must survive: a genuine parse failure still falls back.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("message", [
    _PARSE_ERROR,
    "Bad Request: can't parse entities in message text: unexpected end",
    "Bad Request: unsupported markdown in message",
])
def test_genuine_parse_failure_still_falls_back_to_plain_text(message):
    """The markup really was the problem, so the plain-text rescue applies."""
    bot = _RecordingBot([Exception(message)])
    result = _edit(_adapter(bot))

    assert len(bot.plain_calls) == 1, (
        "a parse failure must still be rescued as plain text"
    )
    assert result.success is True


def test_flood_after_a_parse_fallback_retries_the_plain_text_not_the_markup():
    """The degraded payload must survive into the inline flood retry.

    Sequence: the MarkdownV2 edit hits a parse error, the plain-text rescue
    then hits flood control, and the inline retry runs. Retrying the original
    MarkdownV2 there would walk straight back into the parse error that caused
    the fallback in the first place.
    """
    bot = _RecordingBot([Exception(_PARSE_ERROR), _FloodError(1.0), None])
    result = _edit(_adapter(bot))

    assert result.success is True
    assert len(bot.calls) == 3, (
        f"expected markdown attempt, plain rescue, inline retry; got {len(bot.calls)}"
    )
    markdown_attempt, plain_rescue, retry = bot.calls
    assert markdown_attempt.get("parse_mode")
    assert not plain_rescue.get("parse_mode")
    assert not retry.get("parse_mode"), (
        "the flood retry reinstated MarkdownV2 that Telegram just rejected"
    )
    assert retry["text"] == plain_rescue["text"], (
        "the flood retry must re-send the degraded payload, not the markup"
    )


# ---------------------------------------------------------------------------
# The narrowness is deliberate, not an oversight.
# ---------------------------------------------------------------------------

def test_transient_network_error_keeps_the_plain_text_rescue():
    """A timeout keeps the existing behaviour on purpose.

    Re-raising here would return ``retryable=True``, and
    ``GatewayStreamConsumer`` does not consult ``retryable`` on an edit
    failure: it enters tail-send fallback. A timeout can mean Telegram applied
    the edit and only the response was lost, so the tail would be sent again
    on top of the complete answer. Formatting is the lesser cost, and the
    duplicate is the one to avoid until the consumer honours ``retryable``.
    """
    bot = _RecordingBot([Exception("httpx.ReadTimeout: read timed out")])
    result = _edit(_adapter(bot))

    assert len(bot.plain_calls) == 1
    assert result.success is True


def test_message_not_modified_is_still_a_no_op_success():
    """The existing not-modified shortcut is untouched by the guard."""
    bot = _RecordingBot([Exception("Bad Request: message is not modified")])
    result = _edit(_adapter(bot))

    assert bot.plain_calls == []
    assert result.success is True


# ---------------------------------------------------------------------------
# Detection and the wait must not depend on PTB's attribute alone.
# ---------------------------------------------------------------------------

def test_text_only_flood_refusal_is_recognised_and_fails_closed():
    """No ``retry_after`` attribute, so both facts come from the message.

    Detection must not rely on the attribute, and the wait must be read out of
    "Retry in 269 seconds". Defaulting to a one-second wait here would retry
    inside a 269-second window: another request spent during exactly the
    period Telegram asked us to stay quiet.
    """
    bot = _RecordingBot([_TextOnlyFloodError(269)])
    result = _edit(_adapter(bot))

    assert bot.plain_calls == []
    assert result.success is False
    assert result.error == "flood_control:269.0", (
        "the wait encoded in the message must survive into the capped result"
    )
    assert len(bot.calls) == 1, "an over-cap wait must not retry inline"


def test_inline_flood_retry_waits_the_requested_delay(monkeypatch):
    """The normalised wait is what actually reaches asyncio.sleep."""
    slept: list[float] = []

    async def _record(delay):
        slept.append(delay)

    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.asyncio.sleep", _record
    )

    bot = _RecordingBot([_FloodError(timedelta(seconds=3))])
    result = _edit(_adapter(bot))

    assert result.success is True
    assert slept == [pytest.approx(3.0)], (
        f"expected a 3s wait from the timedelta, got {slept}"
    )


# ---------------------------------------------------------------------------
# The same rule has to hold once the reply overflows into a split.
# ---------------------------------------------------------------------------

def test_overflowing_reply_does_not_downgrade_on_flood():
    """A reply past the length cap takes _edit_overflow_split, which had its
    own catch-all MarkdownV2 fallback and bypassed the guard entirely."""
    long_content = CONTENT + ("\nfiller line to push this over the cap. " * 200)
    assert len(long_content) > 4096, "test content must cross the split threshold"

    bot = _RecordingBot([_FloodError(269.0)])
    result = _edit(_adapter(bot), long_content)

    assert bot.plain_calls == [], (
        "the overflow path downgraded a flood-refused chunk to plain text; "
        f"{len(bot.plain_calls)} unformatted edit(s)"
    )
    assert result.success is False
