"""Integration tests for the startup Telegram outbox drain.

Covers the review ask on the durable-outbox PR: a send interrupted before
completion (recorded as pending in the outbox WAL) must actually be re-sent
once the gateway starts and the Telegram adapter reports connected — not
merely recorded. Pre-seeds an isolated HERMES_HOME, runs GatewayRunner.start()
with a stubbed Telegram connect, and observes the resend.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _isolated_outbox_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a scratch dir so tests never touch the real
    ~/.hermes/state/telegram-outbox.jsonl (same pattern as
    tests/tools/test_send_message_telegram_outbox.py)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    yield


class StubAdapter(BasePlatformAdapter):
    def __init__(self, *, platform=Platform.TELEGRAM):
        super().__init__(PlatformConfig(enabled=True, token="test"), platform)

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return SendResult(success=True, message_id="1")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


def _make_runner(tmp_path):
    """Minimal GatewayRunner via object.__new__ (mirrors
    tests/gateway/test_platform_reconnect.py)."""
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="test")},
        sessions_dir=tmp_path,
    )
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._exit_reason = None
    runner._exit_with_failure = False
    runner._exit_cleanly = False
    runner._failed_platforms = {}
    runner.adapters = {}
    runner.delivery_router = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._honcho_managers = {}
    runner._honcho_configs = {}
    runner._shutdown_all_gateway_honcho = lambda: None
    runner._background_tasks = set()
    runner.session_store = MagicMock()
    runner.hooks = MagicMock()
    runner.hooks.loaded_hooks = []
    runner.hooks.emit = AsyncMock()
    runner._suspend_stuck_loop_sessions = MagicMock(return_value=0)
    runner._update_runtime_status = MagicMock()
    runner._update_platform_runtime_status = MagicMock()
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner._send_update_notification = AsyncMock(return_value=True)
    runner._send_restart_notification = AsyncMock()
    runner._create_adapter = MagicMock(
        side_effect=lambda platform, _config: StubAdapter(platform=platform)
    )
    runner._connect_adapter_with_timeout = AsyncMock(return_value=True)
    return runner


def _start_patches():
    """The environment patches GatewayRunner.start() needs in a unit context
    (same set as tests/gateway/test_platform_reconnect.py)."""

    def fake_create_task(coro):
        coro.close()
        return MagicMock()

    return [
        patch("gateway.status.write_runtime_status"),
        patch("hermes_cli.plugins.discover_plugins"),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("agent.shell_hooks.register_from_config"),
        patch(
            "tools.process_registry.process_registry.recover_from_checkpoint",
            return_value=0,
        ),
        patch(
            "gateway.channel_directory.build_channel_directory",
            new=AsyncMock(return_value={"platforms": {}}),
        ),
        patch("gateway.run.asyncio.create_task", side_effect=fake_create_task),
    ]


class TestStartupOutboxDrain:
    @pytest.mark.asyncio
    async def test_pending_entry_is_resent_after_start(self, tmp_path):
        """Pre-seeded pending outbox entry is re-sent once telegram connects."""
        from tools.telegram_outbox import (
            _outbox_path,
            outbox_append,
            outbox_pending_entries,
        )

        outbox_append(chat_id="414606068", message="interrupted send", thread_id=None)
        assert len(outbox_pending_entries()) == 1
        # Backdate the entry past the drain's grace window (fresh entries are
        # deliberately skipped as likely in-flight; this one simulates a send
        # interrupted before a reboot, i.e. genuinely old).
        path = _outbox_path()
        entry = json.loads(path.read_text().strip())
        entry["created_at"] -= 600
        path.write_text(json.dumps(entry, ensure_ascii=False) + "\n")

        sent_payloads = []

        def fake_send_message_tool(payload):
            sent_payloads.append(payload)
            return json.dumps({"success": True, "message_id": "99"})

        runner = _make_runner(tmp_path)
        patches = _start_patches()
        for p in patches:
            p.start()
        try:
            with patch(
                "tools.send_message_tool.send_message_tool",
                side_effect=fake_send_message_tool,
            ):
                assert await runner.start() is True
                drain_task = getattr(runner, "_telegram_outbox_drain_task", None)
                assert drain_task is not None, "drain was not scheduled on connect"
                await drain_task
        finally:
            for p in patches:
                p.stop()

        assert len(sent_payloads) == 1
        assert sent_payloads[0]["message"] == "interrupted send"
        assert sent_payloads[0]["target"] == "telegram:414606068"
        # The drain's own resend must not re-append (it sets _skip_outbox).
        assert sent_payloads[0].get("_skip_outbox") is True
        assert outbox_pending_entries() == []

    @pytest.mark.asyncio
    async def test_empty_outbox_start_is_clean(self, tmp_path):
        """No pending entries: drain runs, sends nothing, start unaffected."""
        sent_payloads = []

        def fake_send_message_tool(payload):  # pragma: no cover - must not fire
            sent_payloads.append(payload)
            return json.dumps({"success": True})

        runner = _make_runner(tmp_path)
        patches = _start_patches()
        for p in patches:
            p.start()
        try:
            with patch(
                "tools.send_message_tool.send_message_tool",
                side_effect=fake_send_message_tool,
            ):
                assert await runner.start() is True
                drain_task = getattr(runner, "_telegram_outbox_drain_task", None)
                assert drain_task is not None
                await drain_task
        finally:
            for p in patches:
                p.stop()

        assert sent_payloads == []

    @pytest.mark.asyncio
    async def test_drain_is_not_doubled_while_running(self, tmp_path):
        """A reconnect landing mid-drain must not start a second drain task."""
        runner = _make_runner(tmp_path)
        release = asyncio.Event()

        async def _slow_drain():
            await release.wait()

        runner._telegram_outbox_drain_task = asyncio.ensure_future(_slow_drain())
        first = runner._telegram_outbox_drain_task
        runner._schedule_telegram_outbox_drain()
        assert runner._telegram_outbox_drain_task is first
        release.set()
        await first

    @pytest.mark.asyncio
    async def test_drain_failure_does_not_break_start(self, tmp_path):
        """outbox_drain raising must be swallowed (entries stay pending)."""
        runner = _make_runner(tmp_path)
        with patch(
            "tools.telegram_outbox.outbox_drain",
            side_effect=RuntimeError("boom"),
        ):
            runner._schedule_telegram_outbox_drain()
            await runner._telegram_outbox_drain_task  # must not raise

    @pytest.mark.asyncio
    async def test_not_scheduled_during_shutdown(self, tmp_path):
        """A connect finishing during shutdown must not start a drain.

        Keyed on _shutdown_event, not _running — start() only flips _running
        after the connect loop, so a _running guard would have silently
        disabled the cold-start drain (caught in review)."""
        runner = _make_runner(tmp_path)
        runner._shutdown_event.set()
        runner._schedule_telegram_outbox_drain()
        assert getattr(runner, "_telegram_outbox_drain_task", None) is None

    @pytest.mark.asyncio
    async def test_scheduled_on_cold_start_before_running_flag(self, tmp_path):
        """Cold start schedules the drain even while _running is still False
        (real start() ordering: connect loop runs before _running=True)."""
        runner = _make_runner(tmp_path)
        runner._running = False  # what start() actually looks like mid-loop
        with patch(
            "tools.telegram_outbox.outbox_drain",
            return_value={"attempted": 0, "sent": 0, "dropped_stale": 0, "still_pending": 0},
        ):
            runner._schedule_telegram_outbox_drain()
            task = getattr(runner, "_telegram_outbox_drain_task", None)
            assert task is not None
            await task

    @pytest.mark.asyncio
    async def test_task_is_owned_by_background_registry(self, tmp_path):
        """The drain task joins _background_tasks (shutdown sweep) and leaves
        it when done."""
        runner = _make_runner(tmp_path)
        with patch(
            "tools.telegram_outbox.outbox_drain",
            return_value={"attempted": 0, "sent": 0, "dropped_stale": 0, "still_pending": 0},
        ):
            runner._schedule_telegram_outbox_drain()
            task = runner._telegram_outbox_drain_task
            assert task in runner._background_tasks
            await task
            await asyncio.sleep(0)  # let the done_callback run
            assert task not in runner._background_tasks


class TestDrainWalSemantics:
    """WAL-level behaviors the gateway wiring relies on (review High #1)."""

    def test_concurrent_append_survives_compaction(self):
        """An append landing while the drain is sending must survive the
        compaction rewrite (fresh-read-under-lock, not snapshot replace)."""
        from tools import telegram_outbox as ob

        ob.outbox_append(chat_id="1", message="old entry")
        path = ob._outbox_path()
        entry = json.loads(path.read_text().strip())
        entry["created_at"] -= 600
        path.write_text(json.dumps(entry, ensure_ascii=False) + "\n")

        def send_and_append_concurrently(chat_id, message, thread_id):
            # Simulates a live sender appending mid-drain.
            ob.outbox_append(chat_id="2", message="landed mid-drain")
            return True

        summary = ob.outbox_drain(send_fn=send_and_append_concurrently, grace_seconds=0.0)
        # grace=0 means the mid-drain append is drain-eligible next pass but
        # was appended after this drain's snapshot — it must NOT be lost.
        assert summary["sent"] == 1
        remaining = ob.outbox_pending_entries()
        assert [e["message"] for e in remaining] == ["landed mid-drain"]

    def test_grace_window_skips_fresh_entries(self):
        from tools import telegram_outbox as ob

        ob.outbox_append(chat_id="1", message="fresh in-flight send")
        sends = []
        summary = ob.outbox_drain(send_fn=lambda *a: sends.append(a) or True)
        assert summary["attempted"] == 0
        assert sends == []
        # Still pending — a later drain (past the window) will pick it up.
        assert len(ob.outbox_pending_entries()) == 1

    def test_max_items_bounds_attempts(self):
        from tools import telegram_outbox as ob

        path = ob._outbox_path()
        lines = []
        for i in range(3):
            ob.outbox_append(chat_id=str(i), message=f"m{i}")
        entries = [json.loads(l) for l in path.read_text().splitlines()]
        for e in entries:
            e["created_at"] -= 600
            lines.append(json.dumps(e, ensure_ascii=False))
        path.write_text("\n".join(lines) + "\n")

        summary = ob.outbox_drain(send_fn=lambda *a: True, grace_seconds=0, max_items=2)
        assert summary["attempted"] == 2
        assert len(ob.outbox_pending_entries()) == 1
