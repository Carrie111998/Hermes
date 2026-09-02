"""Tests for the gateway /debug command."""

from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _make_event(text="/debug", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig()
    runner.adapters = {}
    return runner


class TestHandleDebugCommand:
    @pytest.mark.asyncio
    async def test_bare_debug_is_notice_only(self):
        runner = _make_runner()
        event = _make_event()

        with patch("hermes_cli.debug.build_debug_share") as build:
            result = await runner._handle_debug_command(event)

        build.assert_not_called()
        assert "/debug upload" in result

    @pytest.mark.asyncio
    async def test_debug_upload_is_explicit_consent(self):
        from hermes_cli.debug import DebugShareResult

        runner = _make_runner()
        event = _make_event(text="/debug upload")
        share = DebugShareResult(
            urls={"Report": "https://paste.rs/report"},
            failures=[], redacted=True, auto_delete_seconds=21600,
        )

        with patch("hermes_cli.debug.build_debug_share", return_value=share) as build:
            result = await runner._handle_debug_command(event)

        build.assert_called_once_with(log_lines=200, redact=True)
        assert "https://paste.rs/report" in result

