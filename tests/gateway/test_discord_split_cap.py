"""Regression tests for the Discord split-delivery cap (issue #86581).

A degenerate turn can produce tens of thousands of characters.  Without a
ceiling, the adapter posts every 2000-char chunk back-to-back and floods the
channel — the #86581 incident delivered 60,698 chars as 31 messages.  The
cap keeps the first ``MAX_SPLIT_MESSAGES`` chunks and replaces the remainder
with a short notice.
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod
    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

from plugins.platforms.discord.adapter import (  # noqa: E402
    DiscordAdapter,
    _apply_yaml_config,
)


MAX = DiscordAdapter.MAX_MESSAGE_LENGTH
CAP = DiscordAdapter.MAX_SPLIT_MESSAGES


def _make_adapter(*, chunk_indicators=False):
    return DiscordAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"chunk_indicators": chunk_indicators},
        )
    )


def _huge_content(chars: int = 60_000) -> str:
    # Distinct filler — this test is about SIZE, not repetition.
    return " ".join(f"word-{i}-" + "x" * 12 for i in range(chars // 20))


class TestCapSplitChunks:
    def test_default_config_preserves_chunk_indicators(self):
        from hermes_cli.config import DEFAULT_CONFIG

        assert DEFAULT_CONFIG["discord"]["chunk_indicators"] is True

    def test_yaml_config_seeds_chunk_indicator_setting(self):
        seeded = _apply_yaml_config({}, {"chunk_indicators": False})

        assert seeded == {"chunk_indicators": False}

    def test_disabled_chunk_indicators_are_not_rendered(self):
        adapter = _make_adapter(chunk_indicators=False)

        chunks = adapter.split_message("word " * 1000, MAX)

        assert len(chunks) > 1
        assert all(re.search(r"\(\d+/\d+\)\s*$", chunk) is None for chunk in chunks)

    def test_stream_split_respects_disabled_chunk_indicators(self):
        adapter = _make_adapter(chunk_indicators=False)
        consumer = GatewayStreamConsumer(adapter, "555", StreamConsumerConfig())

        chunks = consumer._truncate_for_stream("word " * 1000, MAX, len)

        assert len(chunks) > 1
        assert all(re.search(r"\(\d+/\d+\)\s*$", chunk) is None for chunk in chunks)

    def test_below_cap_unchanged(self):
        adapter = _make_adapter()
        chunks = ["a", "b", "c"]
        assert adapter._cap_split_chunks(chunks) == chunks

    def test_over_cap_keeps_n_minus_1_plus_notice(self):
        adapter = _make_adapter()
        chunks = [f"chunk-{i}-" + "z" * 100 for i in range(40)]
        capped = adapter._cap_split_chunks(chunks)
        assert len(capped) == CAP
        assert capped[0] == chunks[0]
        assert "Response truncated" in capped[-1]
        assert "delivery limit" in capped[-1]
        # The notice itself must stay under Discord's per-message cap.
        assert len(capped[-1]) <= MAX


class TestSendCap:
    @pytest.mark.asyncio
    async def test_standalone_send_respects_disabled_indicators(self, monkeypatch):
        from gateway.config import Platform
        from gateway.platform_registry import platform_registry
        from tools.send_message_tool import _send_to_platform

        sender = AsyncMock(return_value={"success": True, "message_id": "1"})
        entry = SimpleNamespace(max_message_length=MAX, standalone_sender_fn=sender)
        original_get = platform_registry.get

        def fake_get(name):
            return entry if name == "discord" else original_get(name)

        monkeypatch.setattr(platform_registry, "get", fake_get)
        pconfig = SimpleNamespace(
            enabled=True,
            token="***",
            extra={"chunk_indicators": False},
        )

        result = await _send_to_platform(
            Platform.DISCORD,
            pconfig,
            "ch",
            "word " * 1000,
        )

        assert result["success"] is True
        assert sender.await_count >= 3
        assert all(
            re.search(r"\(\d+/\d+\)\s*$", call.args[2]) is None
            for call in sender.await_args_list
        )

    @pytest.mark.asyncio
    async def test_send_caps_split_flood(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        sends = []

        async def fake_send(*, content, reference=None):
            sends.append(content)
            return SimpleNamespace(id=9000 + len(sends))

        channel = SimpleNamespace(id=555, send=AsyncMock(side_effect=fake_send))
        adapter._client = SimpleNamespace(
            get_channel=lambda _cid: channel,
            fetch_channel=AsyncMock(),
        )

        result = await adapter.send("555", _huge_content())

        assert result.success is True
        assert len(sends) == CAP
        assert "Response truncated" in sends[-1]
        assert all(
            re.search(r"\(\d+/\d+\)\s*$", content) is None for content in sends
        )


class TestForumCap:
    @pytest.mark.asyncio
    async def test_send_to_forum_caps_followup_chunks(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        thread_sends = []

        async def fake_thread_send(*, content):
            thread_sends.append(content)
            return SimpleNamespace(id=8000 + len(thread_sends))

        thread_channel = SimpleNamespace(
            id=777, send=AsyncMock(side_effect=fake_thread_send)
        )
        forum_channel = SimpleNamespace(
            id=666,
            type=SimpleNamespace(value=15),
            create_thread=AsyncMock(return_value=SimpleNamespace(
                id=777,
                thread=thread_channel,
                message=SimpleNamespace(id=8000),
            )),
        )

        result = await adapter._send_to_forum(forum_channel, _huge_content())

        assert result.success is True
        # 1 starter message + at most (CAP - 1) follow-up chunks.
        assert len(thread_sends) <= CAP - 1
        assert "Response truncated" in thread_sends[-1]
        assert all(
            re.search(r"\(\d+/\d+\)\s*$", content) is None
            for content in thread_sends
        )


class TestEditOverflowCap:
    @pytest.mark.asyncio
    async def test_edit_overflow_split_capped(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        adapter = _make_adapter()
        edits = []
        sends = []

        async def fake_edit(*, content):
            edits.append(content)

        async def fake_send(*, content, reference=None):
            sends.append(content)
            return SimpleNamespace(id=9000 + len(sends))

        msg = SimpleNamespace(id=42, edit=AsyncMock(side_effect=fake_edit))
        channel = SimpleNamespace(id=555, send=AsyncMock(side_effect=fake_send))

        result = await adapter._edit_overflow_split(channel, msg, "42", _huge_content())

        assert result.success is True
        # 1 in-place edit + at most (CAP - 1) continuation sends.
        assert len(edits) == 1
        assert len(sends) <= CAP - 1
        assert "Response truncated" in (sends[-1] if sends else edits[-1])
        assert all(
            re.search(r"\(\d+/\d+\)\s*$", content) is None
            for content in edits + sends
        )
