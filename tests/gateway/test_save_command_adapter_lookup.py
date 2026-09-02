"""Regression tests for the /save slash command's adapter lookup (#95744).

``_handle_save_command`` used to call the non-existent ``self.get_adapter()``,
so every ``/save`` crashed with ``AttributeError: 'GatewayRunner' object has
no attribute 'get_adapter'`` before any document was sent. It must use the
same ``self.adapters.get(platform)`` lookup as the rest of the mixin.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin


class _FakeStore:
    async def get_or_create_session(self, source):
        return SimpleNamespace(session_id="save-regression-session")


class _FakeSessionDB:
    async def export_session(self, session_id):
        return {
            "session_id": session_id,
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        }


class _FakeAdapter:
    def __init__(self):
        self.send_document = AsyncMock()


def _fake_runner(adapter):
    return SimpleNamespace(
        adapters={"telegram": adapter} if adapter is not None else {},
        async_session_store=_FakeStore(),
        _session_db=_FakeSessionDB(),
    )


def _save_event():
    return SimpleNamespace(
        get_command_args=lambda: "md",
        source=SimpleNamespace(platform="telegram", chat_id="chat-1"),
    )


@pytest.mark.asyncio
async def test_save_command_sends_document_via_adapter_dict_lookup():
    """A platform with a registered adapter receives the export document."""
    adapter = _FakeAdapter()
    fake = _fake_runner(adapter)

    result = await GatewaySlashCommandsMixin._handle_save_command(fake, _save_event())

    assert result == "Export complete."
    adapter.send_document.assert_awaited_once()
    kwargs = adapter.send_document.await_args.kwargs
    assert kwargs["chat_id"] == "chat-1"
    assert kwargs["file_name"].endswith(".md")


@pytest.mark.asyncio
async def test_save_command_reports_missing_adapter_without_crashing():
    """An unregistered platform takes the graceful 'no adapter' path (#95744).

    Before the fix this raised AttributeError('... has no attribute
    'get_adapter'') instead of returning a usable message.
    """
    fake = _fake_runner(None)

    result = await GatewaySlashCommandsMixin._handle_save_command(fake, _save_event())

    assert result == "Platform adapter not found to send the document."
