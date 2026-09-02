"""Tests for MCP tool structuredContent preservation."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool


class _FakeContentBlock:
    """Minimal content block with .text and .type attributes."""

    def __init__(self, text: str, block_type: str = "text"):
        self.text = text
        self.type = block_type


class _FakeCallToolResult:
    """Minimal CallToolResult stand-in.

    Uses camelCase ``structuredContent`` / ``isError`` to match the real
    MCP SDK Pydantic model (``mcp.types.CallToolResult``).
    """

    def __init__(self, content, is_error=False, structuredContent=None, meta=None):
        self.content = content
        self.isError = is_error
        self.structuredContent = structuredContent
        # Real SDK exposes the wire ``_meta`` field as ``.meta`` (Pydantic alias).
        self.meta = meta


def _fake_run_on_mcp_loop(coro_or_factory, timeout=30):
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    """Run an MCP coroutine directly in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        # `_rpc_lock` must be created inside the loop that awaits it, or asyncio
        # raises "attached to a different loop". Build it here and attach it to
        # whatever fake server is currently registered under _servers.
        async def _install_lock_and_run():
            for srv in list(mcp_tool._servers.values()):
                if getattr(srv, "_rpc_lock", None) is None:
                    srv._rpc_lock = asyncio.Lock()
            return await coro
        return loop.run_until_complete(_install_lock_and_run())
    finally:
        loop.close()


@pytest.fixture
def _patch_mcp_server():
    """Patch _servers and the MCP event loop so _make_tool_handler can run."""
    fake_session = MagicMock()
    # `_rpc_lock` is acquired by _make_tool_handler's call path (mcp_tool.py
    # ~L2008) to serialize JSON-RPC against the server — build it inside the
    # fresh loop that _fake_run_on_mcp_loop spins up, not at fixture import.
    fake_server = SimpleNamespace(session=fake_session, _rpc_lock=None)
    with patch.dict(mcp_tool._servers, {"test-server": fake_server}), \
         patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_fake_run_on_mcp_loop):
        yield fake_session


class TestStructuredContentPreservation:
    """Ensure structuredContent from CallToolResult is forwarded."""

    def test_text_only_result(self, _patch_mcp_server):
        """When no structuredContent, result is text-only (existing behaviour)."""
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("hello")],
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        raw = handler({})
        data = json.loads(raw)
        assert data == {"result": "hello"}


    def test_structured_content_none_falls_back_to_text(self, _patch_mcp_server):
        """When structuredContent is explicitly None, fall back to text."""
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                structuredContent=None,
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        raw = handler({})
        data = json.loads(raw)
        assert data == {"result": "done"}

    def test_empty_text_with_structured_content(self, _patch_mcp_server):
        """When content blocks are empty but structuredContent exists."""
        session = _patch_mcp_server
        payload = {"status": "ok", "data": [1, 2, 3]}
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[],
                structuredContent=payload,
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        raw = handler({})
        data = json.loads(raw)
        assert data["result"] == payload


class TestMetaPassthrough:
    """Server ``_meta`` is surfaced, minus protocol-reserved keys.

    Ported from MoonshotAI/kimi-code#2596/#2600.
    """

    def test_vendor_meta_passes_through(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={"com.example/handoff": {"url": "https://x"}},
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["result"] == "done"
        assert data["_meta"] == {"com.example/handoff": {"url": "https://x"}}

    def test_reserved_meta_keys_dropped(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={
                    "modelcontextprotocol.io/progress": 1,
                    "tools.mcp.com/trace": "x",
                    "com.example.mcp/vendor": "keep",  # trailing mcp label = vendor ns
                    "unprefixed": "keep",
                },
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["_meta"] == {
            "com.example.mcp/vendor": "keep",
            "unprefixed": "keep",
        }

    def test_all_reserved_meta_omits_field(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={"mcp.io/internal": True},
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {"result": "done"}

    def test_meta_with_structured_content(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("txt")],
                structuredContent={"ok": True},
                meta={"com.example/k": "v"},
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {
            "result": "txt",
            "structuredContent": {"ok": True},
            "_meta": {"com.example/k": "v"},
        }

    def test_non_serializable_meta_dropped(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta={"com.example/obj": object()},
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {"result": "done"}

    def test_non_dict_meta_ignored(self, _patch_mcp_server):
        session = _patch_mcp_server
        session.call_tool = AsyncMock(
            return_value=_FakeCallToolResult(
                content=[_FakeContentBlock("done")],
                meta="not-a-dict",
            )
        )
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data == {"result": "done"}


class _ValidatingFakeSession:
    """Mimics ``mcp.ClientSession``: ``call_tool`` revalidates the result
    against the tool's advertised outputSchema and raises RuntimeError on a
    violation — discarding a result whose ``content`` blocks are usable
    (#101330)."""

    def __init__(self, result, invalid=True):
        self._result = result
        self._invalid = invalid
        self.call_count = 0

        async def _strict_validate(name, res):
            if self._invalid:
                raise RuntimeError(
                    f"Invalid structured content returned by tool {name}: "
                    "'locale' is a required property"
                )

        self.validate_tool_result = _strict_validate

    def call_tool(self, name, arguments=None, **kwargs):
        async def _coro():
            self.call_count += 1
            await self.validate_tool_result(name, self._result)
            return self._result

        return _coro()


@pytest.fixture
def _patch_validating_server():
    """Register a schema-validating fake session under the real handler path."""
    session = _ValidatingFakeSession(
        _FakeCallToolResult(
            content=[_FakeContentBlock("usable payload")],
            structuredContent={"ok": False},
        )
    )
    fake_server = SimpleNamespace(session=session, _rpc_lock=None)
    with (
        patch.dict(mcp_tool._servers, {"test-server": fake_server}),
        patch(
            "tools.mcp_tool._run_on_mcp_loop",
            side_effect=_fake_run_on_mcp_loop,
        ),
        patch.dict(mcp_tool._server_error_counts, {}, clear=True),
    ):
        yield session


class TestOutputSchemaTolerance:
    """A schema-invalid structuredContent must degrade, not fail the call
    or trip the per-server circuit breaker (#101330)."""

    def test_schema_violation_serves_content(self, _patch_validating_server):
        """content blocks survive an outputSchema validation failure."""
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["result"] == "usable payload"

    def test_schema_violation_does_not_trip_breaker(self, _patch_validating_server):
        """Past the threshold (3), calls still reach the server — the RPC
        completed, so the server is not 'unreachable'."""
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        for _ in range(4):
            data = json.loads(handler({}))
            assert data["result"] == "usable payload"
        assert _patch_validating_server.call_count == 4
        assert mcp_tool._server_error_counts.get("test-server", 0) == 0

    def test_validation_failure_warns_once_per_tool(
        self, _patch_validating_server, caplog
    ):
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        with caplog.at_level("WARNING", logger="tools.mcp_tool"):
            handler({})
            handler({})
        warnings = [
            r
            for r in caplog.records
            if "violates its own outputSchema" in r.getMessage()
        ]
        assert len(warnings) == 1

    def test_valid_structured_content_unaffected(self, _patch_validating_server):
        """When validation passes, the tolerant wrapper is a no-op."""
        _patch_validating_server._invalid = False
        handler = mcp_tool._make_tool_handler("test-server", "my-tool", 30.0)
        data = json.loads(handler({}))
        assert data["result"] == "usable payload"
        assert data["structuredContent"] == {"ok": False}

    def test_install_is_idempotent(self):
        session = _ValidatingFakeSession(_FakeCallToolResult(content=[]))
        mcp_tool._install_schema_tolerant_validation(session, "srv")
        wrapped = session.validate_tool_result
        mcp_tool._install_schema_tolerant_validation(session, "srv")
        assert session.validate_tool_result is wrapped


class TestReservedMetaKeyPredicate:
    def test_reserved_prefixes(self):
        assert mcp_tool._is_reserved_mcp_meta_key("modelcontextprotocol.io/x")
        assert mcp_tool._is_reserved_mcp_meta_key("mcp.dev/x")
        assert mcp_tool._is_reserved_mcp_meta_key("tools.mcp.com/x")

    def test_vendor_and_unprefixed_not_reserved(self):
        assert not mcp_tool._is_reserved_mcp_meta_key("com.example.mcp/x")  # trailing label
        assert not mcp_tool._is_reserved_mcp_meta_key("com.example/x")
        assert not mcp_tool._is_reserved_mcp_meta_key("plain-key")
        assert not mcp_tool._is_reserved_mcp_meta_key("/leading-slash")
