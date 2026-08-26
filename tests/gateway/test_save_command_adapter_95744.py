"""Regression for issue #95744: /save crashed with
'GatewayRunner' object has no attribute 'get_adapter'.

_handle_save_command called a non-existent self.get_adapter(platform)
method. The correct, established pattern -- used elsewhere in the same
file for the identical "resolve this platform's adapter" need -- is
self.adapters.get(platform), guarded for a runner that hasn't set
.adapters at all.
"""

import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _bootstrap(monkeypatch, tmp_path):
    """Minimal GatewayRunner setup sufficient to reach the adapter
    resolution line inside _handle_save_command, following the
    established pattern from test_42039_duplicate_user_message.py."""
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    config = GatewayConfig()
    runner = gateway_run.GatewayRunner(config)

    session_entry = SessionEntry(
        session_key="agent:main:telegram:group:-1001:12345",
        session_id="sess-save",
        created_at=None,
        updated_at=None,
        platform=Platform.TELEGRAM,
        chat_type="group",
    )
    fake_facade = MagicMock()
    fake_facade._store = runner.session_store
    fake_facade.get_or_create_session = AsyncMock(return_value=session_entry)
    runner._async_session_store = fake_facade

    runner._session_db = MagicMock()
    runner._session_db.export_session = AsyncMock(
        return_value={"messages": [{"role": "user", "content": "hi"}]}
    )

    return runner


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        user_id="12345",
    )


def _event(text="/save"):
    return MessageEvent(text=text, source=_source(), message_id="msg-save")


@pytest.mark.asyncio
async def test_save_command_does_not_raise_attribute_error(monkeypatch, tmp_path):
    """The exact reported crash: /save must not raise AttributeError
    resolving the platform adapter."""
    runner = _bootstrap(monkeypatch, tmp_path)
    mock_adapter = MagicMock()
    mock_adapter.send_document = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: mock_adapter}

    result = await runner._handle_save_command(_event("/save json"))

    assert result == "Export complete."
    mock_adapter.send_document.assert_called_once()


@pytest.mark.asyncio
async def test_save_command_reports_missing_adapter_gracefully(monkeypatch, tmp_path):
    """A platform with no registered adapter must fail gracefully (a
    plain message), not with an AttributeError or KeyError."""
    runner = _bootstrap(monkeypatch, tmp_path)
    runner.adapters = {}  # no adapter registered for TELEGRAM

    result = await runner._handle_save_command(_event("/save json"))

    assert result == "Platform adapter not found to send the document."


@pytest.mark.asyncio
async def test_save_command_handles_runner_with_no_adapters_attribute(
    monkeypatch, tmp_path
):
    """Defensive case matching the established getattr(self, "adapters",
    None) guard. GatewayRunner.__init__ always sets self.adapters = {},
    so this scenario can't occur naturally -- deleting it explicitly
    constructs the edge case the guard exists to protect against, and
    confirms the expression itself doesn't assume the attribute is
    present."""
    runner = _bootstrap(monkeypatch, tmp_path)
    del runner.adapters

    result = await runner._handle_save_command(_event("/save json"))

    assert result == "Platform adapter not found to send the document."
