from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_enforcement as ke
import hermes_cli.plugins as plugins_mod


@pytest.fixture(autouse=True)
def reset_enforcement_state():
    ke.reset_all_state()
    ke._CONFIG_CACHE.clear()
    ke._CONFIG_LAST_READ = 0.0
    yield
    ke.reset_all_state()
    ke._CONFIG_CACHE.clear()
    ke._CONFIG_LAST_READ = 0.0


def _create_dispatch_task(home: Path, *, board: str, decision: dict) -> str:
    with kb.connect(board=board) as conn:
        return kb.create_task(
            conn,
            title="dispatch integration",
            assignee="worker-terra",
            dispatch_decision=decision,
        )


def test_durable_route_and_exemption_events_are_verified(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    route = {
        "route": "worker-terra",
        "model": "deepseek-v4-flash",
        "provider": "new-api",
    }
    route_id = _create_dispatch_task(home, board="route-board", decision=route)
    assert ke._verify_dispatch_decision_from_db(
        route_id, "worker-terra", "deepseek-v4-flash", "new-api", None,
        board="route-board",
    )
    assert not ke._verify_dispatch_decision_from_db(
        route_id, "worker-terra", "deepseek-v4-flash", "new-api", None,
        board="wrong-board",
    )

    exemption = {"exemption": "tiny"}
    exemption_id = _create_dispatch_task(
        home, board="exemption-board", decision=exemption,
    )
    assert ke._verify_dispatch_decision_from_db(
        exemption_id, "worker-terra", None, None, "tiny",
        board="exemption-board",
    )
    assert not ke._verify_dispatch_decision_from_db(
        exemption_id, "worker-terra", None, None, "controller_judgment",
        board="exemption-board",
    )


def test_post_hook_uses_result_board_for_durable_readback(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ke.set_enforcement_enabled_for_test(True)

    decision = {
        "route": "worker-terra",
        "model": "deepseek-v4-flash",
        "provider": "new-api",
    }
    task_id = _create_dispatch_task(home, board="explicit-board", decision=decision)
    ke._post_tool_call_enforcement(
        tool_name="kanban_create",
        args={"assignee": "worker-terra", "dispatch_decision": decision},
        result={"success": True, "task_id": task_id, "board": "explicit-board"},
        status="ok",
        session_id="session-a",
    )
    assert ke.dispatch_enforcement_is_established("session-a")
    blocked_same_session = ke._pre_tool_call_enforcement(
        tool_name="terminal", session_id="session-a",
    )
    assert blocked_same_session and blocked_same_session["action"] == "block"
    blocked = ke._pre_tool_call_enforcement(
        tool_name="terminal", session_id="session-b",
    )
    assert blocked and blocked["action"] == "block"


def test_plugin_path_route_blocks_and_scoped_exemption_allows_exact_op(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    bundled = Path(__file__).resolve().parents[2] / "plugins"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    config_path.write_text(
        yaml.safe_dump({
            "plugins": {"enabled": ["kanban-enforcement"]},
            "kanban": {"enforce_dispatch_routing": True},
        }),
        encoding="utf-8",
    )
    ke._CONFIG_LAST_READ = 0.0
    old_manager = plugins_mod._plugin_manager
    try:
        plugins_mod._plugin_manager = plugins_mod.PluginManager()
        plugins_mod.discover_plugins()

        route = {
            "route": "worker-terra",
            "model": "deepseek-v4-flash",
            "provider": "new-api",
        }
        route_id = _create_dispatch_task(home, board="route-board", decision=route)
        plugins_mod.invoke_hook(
            "post_tool_call",
            tool_name="kanban_create",
            args={"assignee": "worker-terra", "dispatch_decision": route},
            result={"success": True, "task_id": route_id, "board": "route-board"},
            status="ok",
            session_id="route-session",
        )
        blocked = plugins_mod.invoke_hook(
            "pre_tool_call",
            tool_name="terminal",
            args={"command": "true"},
            session_id="route-session",
        )
        assert any(item.get("action") == "block" for item in blocked)

        scoped = {
            "exemption": "controller_judgment",
            "allowed_tool": "process",
            "allowed_action": "kill",
            "allowed_uses": 1,
        }
        exempted_id = _create_dispatch_task(home, board="exemption-board", decision=scoped)
        plugins_mod.invoke_hook(
            "post_tool_call",
            tool_name="kanban_create",
            args={"dispatch_decision": scoped},
            result={"success": True, "task_id": exempted_id, "board": "exemption-board"},
            status="ok",
            session_id="exemption-session",
        )
        allowed = plugins_mod.invoke_hook(
            "pre_tool_call",
            tool_name="process",
            args={"action": "kill", "session_id": "p1"},
            session_id="exemption-session",
        )
        assert allowed == []
        blocked_unrelated = plugins_mod.invoke_hook(
            "pre_tool_call",
            tool_name="process",
            args={"action": "write", "session_id": "p1", "data": "hi"},
            session_id="exemption-session",
        )
        assert any(item.get("action") == "block" for item in blocked_unrelated)
    finally:
        plugins_mod._plugin_manager = old_manager


def test_real_plugin_discovery_and_invoke_hook_require_dual_opt_in(
    tmp_path, monkeypatch,
):
    home = tmp_path / ".hermes"
    home.mkdir()
    config_path = home / "config.yaml"
    bundled = Path(__file__).resolve().parents[2] / "plugins"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_BUNDLED_PLUGINS", str(bundled))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    old_manager = plugins_mod._plugin_manager
    try:
        config_path.write_text(
            yaml.safe_dump({
                "plugins": {"enabled": ["kanban-enforcement"]},
                "kanban": {"enforce_dispatch_routing": True},
            }),
            encoding="utf-8",
        )
        ke._CONFIG_LAST_READ = 0.0
        plugins_mod._plugin_manager = plugins_mod.PluginManager()
        plugins_mod.discover_plugins()
        loaded = next(
            item for item in plugins_mod._plugin_manager.list_plugins()
            if item["name"] == "kanban-enforcement"
        )
        assert loaded["enabled"] is True
        results = plugins_mod.invoke_hook(
            "pre_tool_call",
            tool_name="terminal",
            args={"command": "true"},
            session_id="plugin-session",
        )
        assert any(
            isinstance(result, dict) and result.get("action") == "block"
            for result in results
        )

        config_path.write_text(
            yaml.safe_dump({
                "plugins": {"enabled": ["kanban-enforcement"]},
                "kanban": {"enforce_dispatch_routing": False},
            }),
            encoding="utf-8",
        )
        ke._CONFIG_LAST_READ = 0.0
        assert plugins_mod.invoke_hook(
            "pre_tool_call",
            tool_name="terminal",
            args={"command": "true"},
            session_id="plugin-session-off",
        ) == []

        config_path.write_text(
            yaml.safe_dump({
                "plugins": {"enabled": []},
                "kanban": {"enforce_dispatch_routing": True},
            }),
            encoding="utf-8",
        )
        plugins_mod._plugin_manager = plugins_mod.PluginManager()
        plugins_mod.discover_plugins()
        loaded = next(
            item for item in plugins_mod._plugin_manager.list_plugins()
            if item["name"] == "kanban-enforcement"
        )
        assert loaded["enabled"] is False
        assert plugins_mod.invoke_hook(
            "pre_tool_call",
            tool_name="terminal",
            args={"command": "true"},
            session_id="plugin-disabled",
        ) == []
    finally:
        plugins_mod._plugin_manager = old_manager
