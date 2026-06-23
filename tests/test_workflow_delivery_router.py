"""
Tests for plugins/workflow/delivery_router.py — routing logic,
log file creation, platform dispatch, and edge cases.

Run: python3 -m pytest tests/test_workflow_delivery_router.py -v
"""

import json
import pathlib
import tempfile
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from plugins.workflow.delivery_router import (
    deliver,
    _deliver_local,
    _format_message,
)


# ── Local delivery tests ─────────────────────────────────────────

class TestDeliverLocal:
    """Tests for local (file-based) delivery."""

    def test_deliver_local_writes_log_file(self, tmp_path):
        """delivery='local' should write a log file with the result."""
        result = {"node-a": "done", "node-b": "failed"}
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver(result, "local", "wf-test-20260101", "test-workflow")

        assert out["delivered"] == "local"
        assert out["path"].endswith("wf-test-20260101.log")

        log_file = pathlib.Path(out["path"])
        assert log_file.exists()

        content = log_file.read_text()
        assert "test-workflow" in content
        assert "wf-test-20260101" in content
        assert "node-a" in content
        assert "node-b" in content
        assert "done" in content
        assert "failed" in content

    def test_deliver_local_creates_date_directory(self, tmp_path):
        """Log files should be organized under YYYY-MM-DD directories."""
        result = {"node-a": "done"}
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver(result, None, "wf-test-001", "my-wf")

        log_file = pathlib.Path(out["path"])
        # Directory should be today's date
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in str(log_file.parent)

    def test_deliver_local_with_empty_string(self, tmp_path):
        """Empty string delivery should default to local."""
        result = {"node-a": "done"}
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver(result, "", "wf-test-002", "my-wf")

        assert out["delivered"] == "local"

    def test_deliver_local_with_none(self, tmp_path):
        """None delivery should default to local."""
        result = {"node-a": "done"}
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver(result, None, "wf-test-003", "my-wf")

        assert out["delivered"] == "local"

    def test_log_contains_json_result(self, tmp_path):
        """Log file should contain the full JSON-serialized result."""
        result = {"a": "done", "b": "skipped", "c": "timed_out"}
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver(result, "local", "wf-json-test", "json-wf")

        content = pathlib.Path(out["path"]).read_text()
        # The JSON block should be parseable
        json_start = content.index("{")
        json_end = content.rindex("}") + 1
        parsed = json.loads(content[json_start:json_end])
        assert parsed == result

    def test_log_contains_timestamp(self, tmp_path):
        """Log file should contain a completion timestamp."""
        result = {"a": "done"}
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver(result, "local", "wf-ts-test", "ts-wf")

        content = pathlib.Path(out["path"]).read_text()
        assert "Completed:" in content


# ── Routing logic tests ──────────────────────────────────────────

class TestRoutingLogic:
    """Tests for the deliver() routing dispatch."""

    def test_none_delivers_local(self, tmp_path):
        """delivery=None should route to local."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver({}, None, "run-1", "wf")
        assert out["delivered"] == "local"

    def test_empty_string_delivers_local(self, tmp_path):
        """delivery='' should route to local."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver({}, "", "run-2", "wf")
        assert out["delivered"] == "local"

    def test_local_explicit_delivers_local(self, tmp_path):
        """delivery='local' should route to local."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            out = deliver({}, "local", "run-3", "wf")
        assert out["delivered"] == "local"

    def test_unknown_format_falls_back_to_local(self, tmp_path):
        """delivery='unknown:foo' should fall back to local with a warning."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            with patch("plugins.workflow.delivery_router.log") as mock_log:
                out = deliver({}, "unknown:foo", "run-4", "wf")
        assert out["delivered"] == "local"
        mock_log.warning.assert_called_once()

    def test_discord_format_triggers_discord_delivery(self, tmp_path):
        """delivery='discord:123456789' should call _deliver_discord."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            with patch("plugins.workflow.delivery_router._deliver_discord") as mock:
                mock.return_value = {"delivered": "discord", "status": "sent"}
                out = deliver({"a": "done"}, "discord:123456789", "run-5", "wf")
        mock.assert_called_once()
        assert out["delivered"] == "discord"

    def test_discord_thread_format(self, tmp_path):
        """delivery='discord:123:456' should pass thread_id."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            with patch("plugins.workflow.delivery_router._deliver_discord") as mock:
                mock.return_value = {"delivered": "discord", "status": "sent"}
                out = deliver({}, "discord:123:456", "run-6", "wf")
        args = mock.call_args
        assert args[0][1] == "discord:123:456"

    def test_telegram_format_triggers_telegram_delivery(self, tmp_path):
        """delivery='telegram:CHAT_ID' should call _deliver_telegram."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            with patch("plugins.workflow.delivery_router._deliver_telegram") as mock:
                mock.return_value = {"delivered": "telegram", "status": "sent"}
                out = deliver({}, "telegram:999888", "run-7", "wf")
        mock.assert_called_once()
        assert out["delivered"] == "telegram"

    def test_telegram_thread_format(self, tmp_path):
        """delivery='telegram:CHAT:THREAD' should pass thread_id."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            with patch("plugins.workflow.delivery_router._deliver_telegram") as mock:
                mock.return_value = {"delivered": "telegram", "status": "sent"}
                out = deliver({}, "telegram:-100123:456", "run-8", "wf")
        args = mock.call_args
        assert args[0][1] == "telegram:-100123:456"


# ── Discord delivery tests (mocked) ──────────────────────────────

class TestDeliverDiscord:
    """Tests for Discord-specific delivery (mocked hermes send)."""

    def test_deliver_discord_posts_and_returns_metadata(self, tmp_path):
        """Should call hermes send and return discord metadata."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            with patch("plugins.workflow.delivery_router.log") as mock_log:
                # Mock the import of hermes_cli.send_cmd inside _deliver_discord
                mock_send = MagicMock()
                mock_hermes_cli = MagicMock()
                mock_hermes_cli.send_cmd = MagicMock(send=mock_send)
                with patch.dict("sys.modules", {
                    "hermes_cli": mock_hermes_cli,
                    "hermes_cli.send_cmd": mock_hermes_cli.send_cmd,
                }):
                    from plugins.workflow.delivery_router import _deliver_discord
                    out = _deliver_discord(
                        {"node-a": "done"},
                        "discord:123456789",
                        "run-discord-1",
                        "fleet-health",
                    )
        assert out["delivered"] == "discord"
        assert out["channel_id"] == "123456789"
        assert out["status"] == "sent"

    def test_deliver_discord_with_thread(self, tmp_path):
        """Should include thread_id in the result."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            mock_send = MagicMock()
            mock_hermes_cli = MagicMock()
            mock_hermes_cli.send_cmd = MagicMock(send=mock_send)
            with patch.dict("sys.modules", {
                "hermes_cli": mock_hermes_cli,
                "hermes_cli.send_cmd": mock_hermes_cli.send_cmd,
            }):
                from plugins.workflow.delivery_router import _deliver_discord
                out = _deliver_discord(
                    {"a": "done"},
                    "discord:111:222",
                    "run-discord-2",
                    "wf",
                )
        assert out["thread_id"] == "222"
        assert out["channel_id"] == "111"

    def test_deliver_discord_failure_returns_error(self, tmp_path):
        """Failed Discord delivery returns error status (log already written)."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            mock_send = MagicMock(side_effect=RuntimeError("no token"))
            mock_hermes_cli = MagicMock()
            mock_hermes_cli.send_cmd = MagicMock(send=mock_send)
            with patch.dict("sys.modules", {
                "hermes_cli": mock_hermes_cli,
                "hermes_cli.send_cmd": mock_hermes_cli.send_cmd,
            }):
                from plugins.workflow.delivery_router import _deliver_discord
                out = _deliver_discord(
                    {"a": "done"},
                    "discord:999",
                    "run-discord-fail",
                    "wf",
                )
        assert out["delivered"] == "discord"
        assert out["status"] == "failed"
        assert "error" in out


# ── Telegram delivery tests (mocked) ─────────────────────────────

class TestDeliverTelegram:
    """Tests for Telegram-specific delivery (mocked hermes send)."""

    def test_deliver_telegram_posts_and_returns_metadata(self, tmp_path):
        """Should call hermes send and return telegram metadata."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            mock_send = MagicMock()
            mock_hermes_cli = MagicMock()
            mock_hermes_cli.send_cmd = MagicMock(send=mock_send)
            with patch.dict("sys.modules", {
                "hermes_cli": mock_hermes_cli,
                "hermes_cli.send_cmd": mock_hermes_cli.send_cmd,
            }):
                from plugins.workflow.delivery_router import _deliver_telegram
                out = _deliver_telegram(
                    {"a": "done"},
                    "telegram:999888",
                    "run-tg-1",
                    "fleet-health",
                )
        assert out["delivered"] == "telegram"
        assert out["chat_id"] == "999888"
        assert out["status"] == "sent"

    def test_deliver_telegram_with_thread(self, tmp_path):
        """Should include thread_id in the result."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            mock_send = MagicMock()
            mock_hermes_cli = MagicMock()
            mock_hermes_cli.send_cmd = MagicMock(send=mock_send)
            with patch.dict("sys.modules", {
                "hermes_cli": mock_hermes_cli,
                "hermes_cli.send_cmd": mock_hermes_cli.send_cmd,
            }):
                from plugins.workflow.delivery_router import _deliver_telegram
                out = _deliver_telegram(
                    {"a": "done"},
                    "telegram:-100123:456",
                    "run-tg-2",
                    "wf",
                )
        assert out["chat_id"] == "-100123"
        assert out["thread_id"] == "456"

    def test_deliver_telegram_failure_returns_error(self, tmp_path):
        """Failed Telegram delivery returns error status (log already written)."""
        with patch("plugins.workflow.delivery_router.pathlib.Path.home", return_value=tmp_path):
            mock_send = MagicMock(side_effect=RuntimeError("timeout"))
            mock_hermes_cli = MagicMock()
            mock_hermes_cli.send_cmd = MagicMock(send=mock_send)
            with patch.dict("sys.modules", {
                "hermes_cli": mock_hermes_cli,
                "hermes_cli.send_cmd": mock_hermes_cli.send_cmd,
            }):
                from plugins.workflow.delivery_router import _deliver_telegram
                out = _deliver_telegram(
                    {"a": "done"},
                    "telegram:999",
                    "run-tg-fail",
                    "wf",
                )
        assert out["delivered"] == "telegram"
        assert out["status"] == "failed"
        assert "error" in out


# ── Message formatting tests ─────────────────────────────────────

class TestFormatMessage:
    """Tests for the platform message formatter."""

    def test_all_done(self):
        """All done nodes should produce a clean summary."""
        msg = _format_message(
            {"a": "done", "b": "done", "c": "done"},
            "fleet-health",
            "run-1",
        )
        assert "**Workflow: fleet-health**" in msg
        assert "✅ 3/3 done" in msg
        assert "❌" not in msg

    def test_some_failed(self):
        """Failed nodes should show failure emoji."""
        msg = _format_message(
            {"a": "done", "b": "failed", "c": "done"},
            "my-wf",
            "run-2",
        )
        assert "❌ 1 failed" in msg
        assert "`b`: failed" in msg

    def test_mixed_statuses(self):
        """Mixed statuses should all appear in the message."""
        msg = _format_message(
            {"a": "done", "b": "skipped", "c": "timed_out", "d": "blocked"},
            "wf",
            "run-3",
        )
        assert "⏭ 1 skipped" in msg
        assert "`c`: timed_out" in msg
        assert "`d`: blocked" in msg

    def test_empty_result(self):
        """Empty result should still produce a valid message."""
        msg = _format_message({}, "wf", "run-empty")
        assert "0/0 done" in msg

    def test_run_id_in_message(self):
        """The run_id should appear in the message."""
        msg = _format_message({"a": "done"}, "wf", "run-abc-123")
        assert "run-abc-123" in msg
