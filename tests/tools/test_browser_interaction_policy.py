from __future__ import annotations

import json


def test_default_policy_is_isolated(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_INTERACTION", raising=False)
    from agent.browser_interaction_policy import visible_browser_allowed

    assert visible_browser_allowed() is False


def test_explicit_visible_policy_allows_foreground_session(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_INTERACTION", "visible")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    from agent.browser_interaction_policy import visible_browser_allowed

    assert visible_browser_allowed() is True


def test_delegated_child_cannot_inherit_visible_policy(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_INTERACTION", "visible")
    from agent.browser_interaction_policy import visible_browser_allowed
    from agent.delegation_context import delegated_child_context

    with delegated_child_context("child-session"):
        assert visible_browser_allowed() is False


def test_kanban_worker_cannot_inherit_visible_policy(monkeypatch):
    monkeypatch.setenv("HERMES_BROWSER_INTERACTION", "visible")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_policy")
    from agent.browser_interaction_policy import visible_browser_allowed

    assert visible_browser_allowed() is False


def test_computer_use_browser_actions_refused_without_visible_policy(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_INTERACTION", raising=False)
    from tools.computer_use.tool import handle_computer_use

    result = json.loads(handle_computer_use({"action": "cua_browser_prepare", "pid": 42}))
    assert result["code"] == "visible_browser_policy"


def test_terminal_blocks_gui_browser_launch_but_allows_fetch_and_headless(monkeypatch):
    monkeypatch.delenv("HERMES_BROWSER_INTERACTION", raising=False)
    from agent.browser_interaction_policy import blocked_visible_browser_command

    assert blocked_visible_browser_command("open https://example.com")
    assert blocked_visible_browser_command("open -a 'Google Chrome' https://example.com")
    assert blocked_visible_browser_command("google-chrome https://example.com")
    assert blocked_visible_browser_command("python -m webbrowser https://example.com")
    assert blocked_visible_browser_command("curl -fsSL https://example.com") is None
    assert blocked_visible_browser_command("chromium --headless https://example.com") is None
