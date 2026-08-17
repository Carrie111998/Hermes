"""Control sends (approval/clarify prompts) must survive Telegram flood control.

Regression for the 2026-08-16 failure: an exec-approval prompt raised while
the agent was streaming progress edits hit "Flood control exceeded. Retry in
16 seconds", was never retried, and the approval timed out as "user did not
consent" even though the user never saw the prompt.  The fix retries the
control send once after Telegram's RetryAfter hint (capped at
_CONTROL_FLOOD_RETRY_CAP).
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from tests.gateway.test_telegram_approval_buttons import (  # noqa: E402
    _ensure_telegram_mock,
)

_ensure_telegram_mock()

from plugins.platforms.telegram.adapter import TelegramAdapter  # noqa: E402
from gateway.config import PlatformConfig  # noqa: E402


class _FloodError(Exception):
    """Mimics telegram.error.RetryAfter: has .retry_after."""

    def __init__(self, retry_after):
        super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
        self.retry_after = retry_after


def _make_adapter():
    config = PlatformConfig(enabled=True, token="test-token", extra={})
    adapter = TelegramAdapter(config)
    adapter._bot = AsyncMock()
    adapter._app = MagicMock()
    return adapter


@pytest.mark.asyncio
async def test_control_send_retries_after_flood_hint():
    adapter = _make_adapter()
    ok = MagicMock(message_id=42)
    adapter._bot.send_message = AsyncMock(side_effect=[_FloodError(2), ok])

    with patch("asyncio.sleep", new=AsyncMock()) as slept:
        msg = await adapter._send_message_with_thread_fallback(
            chat_id="123", text="approve?"
        )

    assert msg is ok
    assert adapter._bot.send_message.call_count == 2
    # slept the hinted wait (+jitter), not a made-up constant
    assert slept.await_args.args[0] == pytest.approx(2.5)


@pytest.mark.asyncio
async def test_control_send_gives_up_past_cap():
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(
        side_effect=_FloodError(TelegramAdapter._CONTROL_FLOOD_RETRY_CAP + 10)
    )

    with pytest.raises(_FloodError):
        await adapter._send_message_with_thread_fallback(
            chat_id="123", text="approve?"
        )
    # no retry: hint exceeds the cap, caller's text fallback should proceed
    assert adapter._bot.send_message.call_count == 1


@pytest.mark.asyncio
async def test_control_send_non_flood_errors_pass_through():
    adapter = _make_adapter()
    adapter._bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))

    with pytest.raises(RuntimeError):
        await adapter._send_message_with_thread_fallback(
            chat_id="123", text="approve?"
        )
    assert adapter._bot.send_message.call_count == 1
