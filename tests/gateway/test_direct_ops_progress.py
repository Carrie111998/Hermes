"""Direct Operations progress-receipt defaults and upper bound."""

from unittest.mock import AsyncMock

import pytest

from gateway.platforms.base import SendResult
from gateway.run import (
    _bounded_progress_interval,
    _deliver_progress_receipt_after_interval,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_long_turn_progress_receipt_defaults_to_ninety_seconds():
    assert DEFAULT_CONFIG["agent"]["gateway_notify_interval"] == 90


def test_enabled_slower_progress_configuration_is_capped_at_ninety_seconds():
    assert _bounded_progress_interval(180) == 90
    assert _bounded_progress_interval(900) == 90
    assert _bounded_progress_interval(45) == 45
    assert _bounded_progress_interval(0) is None


class _ProgressAdapter:
    def __init__(self):
        self.send = AsyncMock(
            return_value=SendResult(success=True, message_id="progress-1")
        )
        self.edit_message = AsyncMock()


@pytest.mark.asyncio
async def test_ninety_second_progress_receipt_reaches_real_send_seam(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.run.asyncio.sleep", sleep)
    adapter = _ProgressAdapter()

    active, result, message_id = (
        await _deliver_progress_receipt_after_interval(
            interval=_bounded_progress_interval(180),
            adapter=adapter,
            chat_id="office",
            should_emit=lambda: True,
            build_content=lambda: "Working: resolving the exact target set.",
            metadata={"thread_id": "ops"},
        )
    )

    sleep.assert_awaited_once_with(90)
    adapter.send.assert_awaited_once_with(
        "office",
        "Working: resolving the exact target set.",
        metadata={"thread_id": "ops"},
    )
    assert active is True
    assert result.success is True
    assert message_id == "progress-1"


@pytest.mark.asyncio
async def test_completed_run_does_not_emit_stale_progress_receipt(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("gateway.run.asyncio.sleep", sleep)
    adapter = _ProgressAdapter()

    active, result, message_id = (
        await _deliver_progress_receipt_after_interval(
            interval=90,
            adapter=adapter,
            chat_id="office",
            should_emit=lambda: False,
            build_content=lambda: "must not be built",
            metadata=None,
        )
    )

    sleep.assert_awaited_once_with(90)
    adapter.send.assert_not_awaited()
    assert active is False
    assert result is None
    assert message_id is None
