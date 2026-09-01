"""Tests for Discord thread participation persistence.

Verifies that _threads (ThreadParticipationTracker) survives adapter restarts by
being persisted to ~/.hermes/discord_threads.json.
"""

import json
import os
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


class TestDiscordThreadPersistence:
    """Thread IDs are saved to disk and reloaded on init."""

    def _make_adapter(self, tmp_path):
        """Build a minimal DiscordAdapter with HERMES_HOME pointed at tmp_path."""
        from gateway.config import PlatformConfig
        from plugins.platforms.discord.adapter import DiscordAdapter

        config = PlatformConfig(enabled=True, token="test-token")
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            return DiscordAdapter(config=config)

    def test_starts_empty_when_no_state_file(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        assert "$nonexistent" not in adapter._threads

    def test_track_thread_persists_to_disk(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter._threads.mark("111")
            adapter._threads.mark("222")

        state_file = tmp_path / "discord_threads.json"
        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert set(saved) == {"111", "222"}

    def test_threads_survive_restart(self, tmp_path):
        """Threads tracked by one adapter instance are visible to the next."""
        adapter1 = self._make_adapter(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter1._threads.mark("aaa")
            adapter1._threads.mark("bbb")

        adapter2 = self._make_adapter(tmp_path)
        assert "aaa" in adapter2._threads
        assert "bbb" in adapter2._threads

    def test_public_snapshot_is_unbounded_for_discord(self, tmp_path):
        adapter = self._make_adapter(tmp_path)
        with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
            adapter._threads.mark("111")
            adapter._threads.mark("222")

        assert adapter._threads._max_tracked is None
        assert adapter.participating_thread_ids() == ("111", "222")

    @pytest.mark.asyncio
    async def test_lightweight_metadata_refresh_skips_message_history(self):
        from plugins.platforms.discord.adapter import DiscordAdapter

        created_at = datetime(2026, 8, 1, tzinfo=timezone.utc)

        class FakeThread:
            id = 123
            parent_id = 456
            guild = SimpleNamespace(id=789)
            name = "Tracked"
            last_message_id = None
            auto_archive_duration = 10080
            archived = False

            def __init__(self):
                self.created_at = created_at
                self.history_calls = 0

            def history(self, **_kwargs):
                self.history_calls += 1

                async def messages():
                    if False:
                        yield None

                return messages()

        channel = FakeThread()
        adapter = object.__new__(DiscordAdapter)
        adapter._client = SimpleNamespace(
            user=SimpleNamespace(id=1),
            get_channel=lambda _thread_id: channel,
        )

        metadata = await adapter.resolve_thread_metadata(
            "123", include_activity_history=False
        )

        assert metadata["accessible"] is True
        assert metadata["parent_channel_id"] == "456"
        assert metadata["last_hermes_activity_at"] == created_at
        assert channel.history_calls == 0

    @pytest.mark.asyncio
    async def test_delivery_target_validation_checks_permissions_and_send(self):
        from plugins.platforms.discord.adapter import DiscordAdapter

        target = SimpleNamespace(
            id=999,
            guild=SimpleNamespace(me=SimpleNamespace(id=1)),
            parent_id=None,
            send=AsyncMock(),
            permissions_for=lambda _member: SimpleNamespace(
                view_channel=True,
                read_messages=True,
                send_messages=True,
            ),
        )
        adapter = object.__new__(DiscordAdapter)
        adapter._client = SimpleNamespace(
            user=SimpleNamespace(id=1),
            get_channel=lambda _channel_id: target,
        )

        assert await adapter.validate_delivery_target("999") == {
            "ok": True,
            "channel_id": "999",
        }

