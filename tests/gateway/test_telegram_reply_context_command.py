import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


class _Stream:
    def __init__(self, data=b""):
        self.data = data

    async def read(self, limit):
        return self.data[:limit]


class _Process:
    def __init__(self, stdout=b"", stderr=b"", returncode=0):
        self.stdout = _Stream(stdout)
        self.stderr = _Stream(stderr)
        self._final_returncode = returncode
        self.returncode = None
        self.killed = False
        self.waited = False

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        self.returncode = -9 if self.killed else self._final_returncode
        return self.returncode


def _event():
    return SimpleNamespace(
        text="hello",
        reply_to_message_id="7",
        message_id="8",
        channel_context=None,
        source=SimpleNamespace(
            chat_id="-1004362586179",
            thread_id="5",
            chat_type="group",
        ),
    )


@pytest.mark.asyncio
async def test_reply_context_command_enriches_channel_context(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/contaminated/repo/venv")
    monkeypatch.setenv("PYTHONHOME", "/contaminated/python")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "reply_context_command": "/runtime/python /scripts/context.py",
                "reply_context_timeout": 5,
                "reply_context_depth": 30,
                "reply_context_neighbors": 5,
                "reply_context_max_messages": 100,
                "reply_context_max_chars": 80000,
            },
        )
    )
    proc = _Process(
        stdout=json.dumps({"ok": True, "context": "bounded context"}).encode(),
        returncode=0,
    )
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)) as spawn:
        event = await adapter._enrich_reply_context(_event())

    assert event.channel_context == "bounded context"
    argv = spawn.await_args.args
    env = spawn.await_args.kwargs["env"]
    assert argv[:2] == ("/runtime/python", "/scripts/context.py")
    assert "PYTHONPATH" not in env
    assert "PYTHONHOME" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "--depth" in argv and argv[argv.index("--depth") + 1] == "30"
    assert "--neighbors" in argv and argv[argv.index("--neighbors") + 1] == "5"
    assert "--max-messages" in argv and argv[argv.index("--max-messages") + 1] == "100"


@pytest.mark.asyncio
async def test_reply_context_failure_falls_back_to_native_reply():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"reply_context_command": "/runtime/python /scripts/context.py"},
        )
    )
    event = _event()
    proc = _Process(stdout=b'{"ok": false}', returncode=2)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await adapter._enrich_reply_context(event)
    assert result is event
    assert result.channel_context is None


@pytest.mark.asyncio
async def test_reply_context_output_flood_is_killed_and_reaped():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={
                "reply_context_command": "/runtime/python /scripts/context.py",
                "reply_context_max_chars": 1000,
            },
        )
    )
    proc = _Process(stdout=b"x" * 20_000)
    with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)):
        result = await adapter._enrich_reply_context(_event())
    assert result.channel_context is None
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_reply_context_timeout_kills_and_reaps_process():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"reply_context_command": "/runtime/python /scripts/context.py"},
        )
    )
    proc = _Process(stdout=b"{}")
    with (
        patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=proc)),
        patch("asyncio.wait_for", new=AsyncMock(side_effect=asyncio.TimeoutError)),
    ):
        result = await adapter._enrich_reply_context(_event())
    assert result.channel_context is None
    assert proc.killed is True
    assert proc.waited is True


@pytest.mark.asyncio
async def test_reply_context_is_skipped_without_reply_or_outside_groups():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"reply_context_command": "/runtime/python /scripts/context.py"},
        )
    )
    event = _event()
    event.reply_to_message_id = None
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
        result = await adapter._enrich_reply_context(event)
    assert result is event
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_reply_context_is_skipped_for_gateway_commands():
    adapter = TelegramAdapter(
        PlatformConfig(
            enabled=True,
            token="***",
            extra={"reply_context_command": "/runtime/python /scripts/context.py"},
        )
    )
    event = _event()
    event.text = "/reset"
    with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as spawn:
        result = await adapter._enrich_reply_context(event)
    assert result is event
    spawn.assert_not_awaited()
