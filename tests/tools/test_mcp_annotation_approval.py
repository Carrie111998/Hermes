"""MCP write approval driven only by annotations and Hermes approval mode."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tools import mcp_tool


class _TextBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _ToolResult:
    isError = False
    structuredContent = None

    def __init__(self, text: str = "ok"):
        self.content = [_TextBlock(text)]


def _run_on_private_loop(coro_or_factory, timeout=30):
    del timeout
    coro = coro_or_factory() if callable(coro_or_factory) else coro_or_factory
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.fixture(autouse=True)
def _isolate_annotation_state():
    with patch.dict(mcp_tool._tool_read_only_hints, {}, clear=True):
        yield


def _annotation_tool(name: str, hint=...):
    annotations = (
        None
        if hint is ...
        else SimpleNamespace(readOnlyHint=hint)
    )
    return SimpleNamespace(
        name=name,
        description="",
        inputSchema={},
        annotations=annotations,
    )


def test_annotation_capture_fails_closed_for_missing_or_malformed_hints():
    mcp_tool._record_tool_approval_metadata(
        "pipedream",
        [
            _annotation_tool("read", True),
            _annotation_tool("write", False),
            _annotation_tool("missing"),
            _annotation_tool("truthy", "yes"),
        ],
    )

    assert mcp_tool._tool_read_only_hints["pipedream"] == {
        "read": True,
        "write": False,
        "missing": False,
        "truthy": False,
    }


def test_cached_annotation_metadata_has_live_path_parity():
    cached = mcp_tool._CachedMCPTool(
        "read",
        "",
        {},
        annotations={"readOnlyHint": True},
    )
    missing = mcp_tool._CachedMCPTool("write", "", {})

    mcp_tool._record_tool_approval_metadata("pipedream", [cached, missing])

    assert mcp_tool._tool_read_only_hints["pipedream"] == {
        "read": True,
        "write": False,
    }


def test_annotation_metadata_is_isolated_by_profile_home(tmp_path, monkeypatch):
    from agent import secret_scope
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", True)
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"

    token = set_hermes_home_override(str(home_a))
    try:
        mcp_tool._record_tool_approval_metadata(
            "evaos-pipedream-google_sheets",
            [_annotation_tool("rows", True)],
        )
    finally:
        reset_hermes_home_override(token)

    token = set_hermes_home_override(str(home_b))
    try:
        mcp_tool._record_tool_approval_metadata(
            "evaos-pipedream-google_sheets",
            [_annotation_tool("rows", False)],
        )
    finally:
        reset_hermes_home_override(token)

    assert mcp_tool._tool_read_only_hints[
        (str(home_a.resolve()), "evaos-pipedream-google_sheets")
    ] == {"rows": True}
    assert mcp_tool._tool_read_only_hints[
        (str(home_b.resolve()), "evaos-pipedream-google_sheets")
    ] == {"rows": False}


def test_lazy_cache_registration_restores_annotation_before_tool_handler():
    entry = {
        "tools": [
            {
                "name": "read",
                "description": "",
                "inputSchema": {},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "write",
                "description": "",
                "inputSchema": {},
            },
        ]
    }
    with patch.object(mcp_tool, "_scan_mcp_description", return_value=[]), \
         patch.object(
             mcp_tool,
             "_convert_mcp_schema",
             side_effect=RuntimeError("stop after metadata"),
         ), \
         pytest.raises(RuntimeError, match="stop after metadata"):
        mcp_tool._register_from_cache_sync(
            "pipedream",
            {"auth": "evaos_lease", "app_slug": "google_sheets"},
            entry,
        )

    assert mcp_tool._tool_read_only_hints["pipedream"] == {
        "read": True,
        "write": False,
    }


def test_read_only_hint_bypasses_approval():
    mcp_tool._tool_read_only_hints["pipedream"] = {"search": True}

    with patch("tools.approval.request_elicitation_consent") as consent:
        assert mcp_tool._mcp_tool_approval_check(
            "pipedream", "search", {"query": "quarterly plan"}
        ) is None

    consent.assert_not_called()


def test_manual_approval_accepts_or_denies_once():
    mcp_tool._tool_read_only_hints["pipedream"] = {"send_email": False}

    with patch("tools.approval._get_approval_mode", return_value="manual"), \
         patch("tools.approval.request_elicitation_consent", return_value="accept"):
        assert mcp_tool._mcp_tool_approval_check(
            "pipedream", "send_email", {"to": "owner@example.com"}
        ) is None

    with patch("tools.approval._get_approval_mode", return_value="manual"), \
         patch("tools.approval.request_elicitation_consent", return_value="decline"):
        blocked = mcp_tool._mcp_tool_approval_check(
            "pipedream", "send_email", {"to": "owner@example.com"}
        )

    assert "did not approve" in json.loads(blocked)["error"]


@pytest.mark.parametrize(
    ("verdict", "allowed"),
    [("approve", True), ("deny", False)],
)
def test_smart_mode_uses_native_guardian_without_a_second_prompt(verdict, allowed):
    mcp_tool._tool_read_only_hints["pipedream"] = {"update_row": False}

    with patch("tools.approval._get_approval_mode", return_value="smart"), \
         patch("tools.approval._smart_approve", return_value=verdict) as smart, \
         patch("tools.approval.request_elicitation_consent") as consent:
        result = mcp_tool._mcp_tool_approval_check(
            "pipedream", "update_row", {"row": 7}
        )

    smart.assert_called_once()
    consent.assert_not_called()
    assert (result is None) is allowed


def test_off_mode_and_session_yolo_bypass_write_approval():
    from tools import approval

    mcp_tool._tool_read_only_hints["pipedream"] = {"update_row": False}

    with patch("tools.approval._get_approval_mode", return_value="off"), \
         patch("tools.approval.request_elicitation_consent") as consent:
        assert mcp_tool._mcp_tool_approval_check(
            "pipedream", "update_row", {}
        ) is None
    consent.assert_not_called()

    session_key = "mcp-yolo-test"
    token = approval.set_current_session_key(session_key)
    approval.enable_session_yolo(session_key)
    try:
        with patch("tools.approval._get_approval_mode", return_value="manual"), \
             patch("tools.approval.request_elicitation_consent") as consent:
            assert mcp_tool._mcp_tool_approval_check(
                "pipedream", "update_row", {}
            ) is None
        consent.assert_not_called()
    finally:
        approval.clear_session(session_key)
        approval.reset_current_session_key(token)


@pytest.mark.parametrize(
    "credential_key",
    ["Authorization", "token", "client_secret"],
)
def test_approval_display_redacts_bearer_tokens(credential_key):
    secret = "pdt_fake_bearer_value_that_must_not_escape"
    captured = {}
    mcp_tool._tool_read_only_hints["pipedream"] = {"send_email": False}

    def _capture(message, description, **kwargs):
        captured.update(
            message=message,
            description=description,
            surface=kwargs.get("surface"),
        )
        return "decline"

    with patch("tools.approval._get_approval_mode", return_value="manual"), \
         patch("tools.approval.request_elicitation_consent", side_effect=_capture):
        mcp_tool._mcp_tool_approval_check(
            "pipedream",
            "send_email",
            {"headers": {credential_key: f"Bearer {secret}"}},
        )

    rendered = json.dumps(captured)
    assert secret not in rendered
    assert "redacted-secret" in rendered or "***" in rendered


def test_denial_happens_before_lazy_connect_or_rpc():
    mcp_tool._tool_read_only_hints["pipedream"] = {"send_email": False}
    connect = MagicMock()
    handler = mcp_tool._make_tool_handler("pipedream", "send_email", 5.0)

    with patch("tools.approval._get_approval_mode", return_value="manual"), \
         patch("tools.approval.request_elicitation_consent", return_value="decline"), \
         patch("tools.mcp_tool._get_connected_server_for_call", connect):
        result = handler({"to": "owner@example.com"})

    connect.assert_not_called()
    assert "did not approve" in json.loads(result)["error"]


def test_approved_write_invokes_rpc_once():
    session = SimpleNamespace(call_tool=AsyncMock(return_value=_ToolResult()))
    server = SimpleNamespace(
        session=session,
        _rpc_lock=asyncio.Lock(),
        _pending_call_context=None,
    )
    mcp_tool._tool_read_only_hints["pipedream"] = {"send_email": False}
    handler = mcp_tool._make_tool_handler("pipedream", "send_email", 5.0)

    with patch("tools.approval._get_approval_mode", return_value="manual"), \
         patch("tools.approval.request_elicitation_consent", return_value="accept"), \
         patch("tools.mcp_tool._get_connected_server_for_call", return_value=server), \
         patch("tools.mcp_tool._run_on_mcp_loop", side_effect=_run_on_private_loop):
        result = handler({"to": "owner@example.com"})

    session.call_tool.assert_awaited_once_with(
        "send_email",
        arguments={"to": "owner@example.com"},
    )
    assert json.loads(result) == {"result": "ok"}
