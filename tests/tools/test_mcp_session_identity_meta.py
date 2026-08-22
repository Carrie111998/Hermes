"""Gateway session user_id is attached to MCP tools/call via ``meta``.

The MCP event loop does not inherit session ContextVars, so the handler
snapshots ``HERMES_SESSION_USER_ID`` on the agent thread and passes it as
``session.call_tool(..., meta=...)``. It is not injected into ``arguments``.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool


class _FakeContentBlock:
    def __init__(self, text: str, block_type: str = "text"):
        self.text = text
        self.type = block_type


class _FakeCallToolResult:
    def __init__(self, content, is_error=False, structuredContent=None):
        self.content = content
        self.isError = is_error
        self.structuredContent = structuredContent


def _fake_run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        async def _install_lock_and_run():
            for srv in list(mcp_tool._servers.values()):
                if getattr(srv, "_rpc_lock", None) is None:
                    srv._rpc_lock = asyncio.Lock()
            return await coro
        return loop.run_until_complete(_install_lock_and_run())
    finally:
        loop.close()


@pytest.fixture
def fake_session():
    session = MagicMock()
    session.call_tool = AsyncMock(
        return_value=_FakeCallToolResult(content=[_FakeContentBlock("ok")])
    )
    server = SimpleNamespace(session=session, _rpc_lock=None, _config={})
    with patch.dict(mcp_tool._servers, {"srv": server}), \
         patch("tools.mcp_tool._run_on_mcp_loop",
               side_effect=_fake_run_on_mcp_loop), \
         patch.dict(mcp_tool._server_error_counts, {}, clear=True):
        yield session


class TestMcpSessionIdentityMeta:
    def test_unset_session_returns_none(self):
        from gateway.session_context import reset_session_vars

        reset_session_vars()
        assert mcp_tool._mcp_session_identity_meta() is None

    def test_bound_session_user_id(self):
        from gateway.session_context import reset_session_vars, set_session_vars

        reset_session_vars()
        set_session_vars(user_id="tg-99")
        try:
            assert mcp_tool._mcp_session_identity_meta("srv", {}) == {
                mcp_tool._MCP_SESSION_USER_ID_META_KEY: "tg-99",
            }
        finally:
            reset_session_vars()

    def test_custom_meta_key_from_server_config(self):
        from gateway.session_context import reset_session_vars, set_session_vars

        reset_session_vars()
        set_session_vars(user_id="tg-99")
        try:
            assert mcp_tool._mcp_session_identity_meta("srv", {
                "session_user_id_meta_key": "x-user-id",
            }) == {"x-user-id": "tg-99"}
        finally:
            reset_session_vars()

    def test_reserved_meta_key_falls_back_to_default(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            key = mcp_tool._resolve_session_user_id_meta_key("srv", {
                "session_user_id_meta_key": "mcp.com/user_id",
            })
        assert key == mcp_tool._MCP_SESSION_USER_ID_META_KEY
        assert any("session_user_id_meta_key" in r.message for r in caplog.records)

    def test_blank_user_id_returns_none(self):
        from gateway.session_context import reset_session_vars, set_session_vars

        reset_session_vars()
        set_session_vars(user_id="  ")
        try:
            assert mcp_tool._mcp_session_identity_meta() is None
        finally:
            reset_session_vars()

    def test_handler_passes_meta_not_arguments(self, fake_session):
        from gateway.session_context import reset_session_vars, set_session_vars

        reset_session_vars()
        set_session_vars(user_id="discord-u1")
        try:
            handler = mcp_tool._make_tool_handler("srv", "echo", 30.0)
            raw = handler({"q": "hi"})
        finally:
            reset_session_vars()

        assert json.loads(raw) == {"result": "ok"}
        fake_session.call_tool.assert_awaited_once_with(
            "echo",
            arguments={"q": "hi"},
            meta={mcp_tool._MCP_SESSION_USER_ID_META_KEY: "discord-u1"},
        )

    def test_handler_omits_meta_without_session_user(self, fake_session):
        from gateway.session_context import reset_session_vars

        reset_session_vars()
        handler = mcp_tool._make_tool_handler("srv", "echo", 30.0)
        raw = handler({"q": "hi"})
        assert json.loads(raw) == {"result": "ok"}
        fake_session.call_tool.assert_awaited_once_with(
            "echo",
            arguments={"q": "hi"},
        )

    def test_handler_uses_configured_meta_key(self, fake_session):
        from gateway.session_context import reset_session_vars, set_session_vars

        mcp_tool._servers["srv"]._config = {
            "session_user_id_meta_key": "caller-id",
        }
        reset_session_vars()
        set_session_vars(user_id="feishu-u2")
        try:
            handler = mcp_tool._make_tool_handler("srv", "echo", 30.0)
            raw = handler({"q": "hi"})
        finally:
            reset_session_vars()

        assert json.loads(raw) == {"result": "ok"}
        fake_session.call_tool.assert_awaited_once_with(
            "echo",
            arguments={"q": "hi"},
            meta={"caller-id": "feishu-u2"},
        )
