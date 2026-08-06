"""P0 regression tests for TUI plugin-command session identity."""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def server():
    import tui_gateway.server as mod

    mod._sessions.clear()
    yield mod
    mod._sessions.clear()


def _handler(calls):
    def handler(args, *, session_id=""):
        calls.append((args, session_id))
        return "ok"

    return handler


def test_command_dispatch_passes_persistent_session_key(server):
    calls = []
    server._sessions["ui-sid"] = {"session_key": "persistent-session-key"}
    handler = _handler(calls)

    with (
        patch.object(server, "_load_cfg", return_value={"quick_commands": {}}),
        patch("hermes_cli.plugins.get_plugin_command_handler", return_value=handler),
        patch("hermes_cli.plugins.resolve_plugin_command_result", side_effect=lambda value: value),
    ):
        response = server._methods["command.dispatch"](
            "request-1",
            {"name": "kimi-mode", "arg": "status", "session_id": "ui-sid"},
        )

    assert response["result"]["output"] == "ok"
    assert calls == [("status", "persistent-session-key")]


def test_slash_exec_passes_persistent_session_key(server):
    calls = []
    server._sessions["ui-sid"] = {
        "session_key": "persistent-session-key",
        "slash_worker": None,
        "agent": None,
    }
    handler = _handler(calls)

    with (
        patch("hermes_cli.plugins.get_plugin_command_handler", return_value=handler),
        patch("hermes_cli.plugins.resolve_plugin_command_result", side_effect=lambda value: value),
    ):
        response = server._methods["slash.exec"](
            "request-2",
            {"command": "/kimi-mode status", "session_id": "ui-sid"},
        )

    assert response["result"]["output"] == "ok"
    assert calls == [("status", "persistent-session-key")]


def test_command_dispatch_uses_request_id_when_session_is_missing(server):
    calls = []
    handler = _handler(calls)

    with (
        patch.object(server, "_load_cfg", return_value={"quick_commands": {}}),
        patch("hermes_cli.plugins.get_plugin_command_handler", return_value=handler),
        patch("hermes_cli.plugins.resolve_plugin_command_result", side_effect=lambda value: value),
    ):
        response = server._methods["command.dispatch"](
            "request-fallback",
            {"name": "kimi-mode", "arg": "status", "session_id": "ui-sid"},
        )

    assert response["result"]["output"] == "ok"
    assert calls == [("status", "request-fallback")]
