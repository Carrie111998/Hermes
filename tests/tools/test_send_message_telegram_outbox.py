"""Regression tests for the durable Telegram outbox (2026-07-22).

send_message_tool's Telegram text-send path now records a pending outbox
entry before attempting delivery and marks it sent afterward — so a send
that dies mid-flight (SIGKILL, host reboot; anything a signal handler can't
catch) leaves a durable record that outbox_drain() can retry later, instead
of being silently lost.

These tests exercise the wiring inside send_message_tool.py end-to-end
(mocking _send_to_platform / load_gateway_config, same pattern as
test_send_message_tool.py), not the outbox module's own unit tests (see
tests/tools/test_telegram_outbox.py for those).
"""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _run_async_immediately(coro):
    return asyncio.run(coro)


def _make_telegram_config():
    from gateway.config import Platform

    telegram_cfg = SimpleNamespace(enabled=True, token="***", extra={})
    return SimpleNamespace(
        platforms={Platform.TELEGRAM: telegram_cfg},
        get_home_channel=lambda _platform: None,
    )


@pytest.fixture(autouse=True)
def _isolated_outbox_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a scratch dir so tests never touch the real
    ~/.hermes/state/telegram-outbox.jsonl."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


class TestTelegramOutboxWiring:
    def test_successful_send_appends_then_marks_sent(self):
        from tools.send_message_tool import send_message_tool
        from tools.telegram_outbox import outbox_pending_entries

        config = _make_telegram_config()
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "telegram:12345",
                    "message": "hello outbox",
                })
            )

        assert result["success"] is True
        # Entry was appended and then immediately marked sent on success —
        # nothing should remain pending afterward.
        assert outbox_pending_entries() == []

    def test_failed_send_leaves_entry_pending(self):
        from tools.send_message_tool import send_message_tool
        from tools.telegram_outbox import outbox_pending_entries

        config = _make_telegram_config()
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"error": "boom"})), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            send_message_tool({
                "action": "send",
                "target": "telegram:12345",
                "message": "will fail",
            })

        pending = outbox_pending_entries()
        assert len(pending) == 1
        assert pending[0]["message"] == "will fail"
        assert pending[0]["chat_id"] == "12345"

    def test_exception_during_send_leaves_entry_pending(self):
        """A raised exception (not just a {"error": ...} result) must also
        leave the outbox entry pending — this is the exact failure mode a
        SIGKILL mid-send would produce (send never returns at all)."""
        from tools.send_message_tool import send_message_tool
        from tools.telegram_outbox import outbox_pending_entries

        config = _make_telegram_config()
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(side_effect=RuntimeError("network died"))), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "telegram:12345",
                    "message": "died mid-send",
                })
            )

        assert "error" in result
        pending = outbox_pending_entries()
        assert len(pending) == 1
        assert pending[0]["message"] == "died mid-send"

    def test_media_send_does_not_use_outbox(self, tmp_path):
        """Scoped to text-only for this pass — media re-send on drain would
        need file paths that may no longer exist after a crash."""
        from tools.send_message_tool import send_message_tool
        from tools.telegram_outbox import outbox_pending_entries

        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg-bytes")

        config = _make_telegram_config()
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"error": "boom"})), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            send_message_tool({
                "action": "send",
                "target": "telegram:12345",
                "message": f"[[media:{img}]] caption",
            })

        assert outbox_pending_entries() == []

    def test_skip_outbox_flag_prevents_reentrant_append(self):
        """outbox_drain()'s own resend path sets _skip_outbox=True so a
        retry-of-a-retry doesn't keep appending fresh pending entries for
        what is logically the same message."""
        from tools.send_message_tool import send_message_tool
        from tools.telegram_outbox import outbox_pending_entries

        config = _make_telegram_config()
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"error": "still down"})), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            send_message_tool({
                "action": "send",
                "target": "telegram:12345",
                "message": "retried by drain",
                "_skip_outbox": True,
            })

        assert outbox_pending_entries() == []

    def test_non_telegram_platform_does_not_use_outbox(self):
        from gateway.config import Platform
        from tools.send_message_tool import send_message_tool
        from tools.telegram_outbox import outbox_pending_entries

        ntfy_cfg = SimpleNamespace(enabled=True, token=None, extra={"topic": "hermes-in"})
        config = SimpleNamespace(
            platforms={Platform("ntfy"): ntfy_cfg},
            get_home_channel=lambda _platform: None,
        )
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("gateway.channel_directory.resolve_channel_name", side_effect=AssertionError("should not resolve")), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"error": "boom"})), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            send_message_tool({
                "action": "send",
                "target": "ntfy:alerts-channel",
                "message": "not telegram",
            })

        assert outbox_pending_entries() == []

    def test_outbox_append_failure_never_blocks_the_real_send(self, monkeypatch):
        """If outbox bookkeeping itself is broken, the actual Telegram send
        must still go through unaffected — durability is best-effort and
        must never gate whether a real message can be delivered."""
        from tools.send_message_tool import send_message_tool

        def _broken_append(*a, **kw):
            raise RuntimeError("disk full")

        monkeypatch.setattr("tools.telegram_outbox.outbox_append", _broken_append)

        config = _make_telegram_config()
        with patch("gateway.config.load_gateway_config", return_value=config), \
             patch("tools.interrupt.is_interrupted", return_value=False), \
             patch("model_tools._run_async", side_effect=_run_async_immediately), \
             patch("tools.send_message_tool._send_to_platform", new=AsyncMock(return_value={"success": True})), \
             patch("gateway.mirror.mirror_to_session", return_value=True):
            result = json.loads(
                send_message_tool({
                    "action": "send",
                    "target": "telegram:12345",
                    "message": "outbox is broken but I still send",
                })
            )

        assert result["success"] is True
