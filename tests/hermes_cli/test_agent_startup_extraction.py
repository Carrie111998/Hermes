"""Regression tests for the c11 extraction (wave-1 shard s5, implementer w1b).

``hermes_cli/agent_startup.py`` now owns the agent-startup preparation
helpers that used to live in ``hermes_cli/main.py`` (``_is_tui_chat_launch``,
``_command_has_dedicated_mcp_startup``, ``_should_background_mcp_startup``,
``_prepare_agent_startup``, ``_apply_safe_mode``, ``_set_chat_arg_defaults``,
plus the ``_AGENT_COMMANDS`` / ``_AGENT_SUBCOMMANDS`` constants).  Bodies were
lifted verbatim; ``hermes_cli.main`` re-imports every name so the historical
``hermes_cli.main.<name>`` test-patch surface still resolves.

The ``_prepare_agent_startup`` tests follow the stub style of
``tests/hermes_cli/test_mcp_startup.py`` (sys.modules substitution), keeping
the tests hermetic and fast.
"""

from __future__ import annotations

from argparse import Namespace
from contextlib import nullcontext
import os
import sys
import types

import pytest

from hermes_cli import agent_startup
from hermes_cli import main as main_mod


# ── _is_tui_chat_launch ────────────────────────────────────────────────────


def test_is_tui_chat_launch_flag(monkeypatch):
    monkeypatch.delenv("HERMES_TUI", raising=False)
    assert agent_startup._is_tui_chat_launch(Namespace(tui=True)) is True
    assert agent_startup._is_tui_chat_launch(Namespace(tui=False)) is False


def test_is_tui_chat_launch_env(monkeypatch):
    monkeypatch.setenv("HERMES_TUI", "1")
    assert agent_startup._is_tui_chat_launch(Namespace(tui=False)) is True


# ── _command_has_dedicated_mcp_startup ─────────────────────────────────────


def test_command_has_dedicated_mcp_startup():
    assert agent_startup._command_has_dedicated_mcp_startup(
        Namespace(command="acp", gateway_command=None, cron_command=None)
    ) is True
    assert agent_startup._command_has_dedicated_mcp_startup(
        Namespace(command="gateway", gateway_command="run", cron_command=None)
    ) is True
    assert agent_startup._command_has_dedicated_mcp_startup(
        Namespace(command="cron", gateway_command=None, cron_command="run")
    ) is True
    assert agent_startup._command_has_dedicated_mcp_startup(
        Namespace(command="cron", gateway_command=None, cron_command="tick")
    ) is True
    assert agent_startup._command_has_dedicated_mcp_startup(
        Namespace(command="gateway", gateway_command="stop", cron_command=None)
    ) is False
    assert agent_startup._command_has_dedicated_mcp_startup(
        Namespace(command="chat", gateway_command=None, cron_command=None)
    ) is False


# ── _should_background_mcp_startup ─────────────────────────────────────────


def test_should_background_mcp_startup(monkeypatch):
    monkeypatch.delenv("HERMES_TUI", raising=False)
    assert agent_startup._should_background_mcp_startup(
        Namespace(command="chat", tui=False)
    ) is True
    assert agent_startup._should_background_mcp_startup(
        Namespace(command=None, tui=False)
    ) is True
    assert agent_startup._should_background_mcp_startup(
        Namespace(command="rl", tui=False)
    ) is True
    assert agent_startup._should_background_mcp_startup(
        Namespace(command="chat", tui=True)
    ) is False
    assert agent_startup._should_background_mcp_startup(
        Namespace(command="gateway", tui=False)
    ) is False


# ── _apply_safe_mode ───────────────────────────────────────────────────────


def test_apply_safe_mode_sets_env(monkeypatch):
    for var in ("HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES"):
        monkeypatch.delenv(var, raising=False)
    agent_startup._apply_safe_mode(Namespace(safe_mode=True))
    assert os.environ["HERMES_SAFE_MODE"] == "1"
    assert os.environ["HERMES_IGNORE_USER_CONFIG"] == "1"
    assert os.environ["HERMES_IGNORE_RULES"] == "1"


def test_apply_safe_mode_noop_without_flag(monkeypatch):
    for var in ("HERMES_SAFE_MODE", "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES"):
        monkeypatch.delenv(var, raising=False)
    agent_startup._apply_safe_mode(Namespace(safe_mode=False))
    assert "HERMES_SAFE_MODE" not in os.environ


# ── _set_chat_arg_defaults ─────────────────────────────────────────────────


def test_set_chat_arg_defaults_fills_missing(monkeypatch):
    args = Namespace(command="chat")
    agent_startup._set_chat_arg_defaults(args)
    assert args.query is None
    assert args.model is None
    assert args.provider is None
    assert args.toolsets is None
    assert args.verbose is False
    assert args.resume is None
    assert args.continue_last is None
    assert args.worktree is False


def test_set_chat_arg_defaults_preserves_existing():
    args = Namespace(query="hi", model="gpt5", worktree=True)
    agent_startup._set_chat_arg_defaults(args)
    assert args.query == "hi"
    assert args.model == "gpt5"
    assert args.worktree is True
    assert args.verbose is False


# ── _prepare_agent_startup ─────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("HERMES_YOLO_MODE", "HERMES_SAFE_MODE",
                "HERMES_IGNORE_USER_CONFIG", "HERMES_IGNORE_RULES"):
        monkeypatch.delenv(var, raising=False)
    yield


def _install_agent_startup_stubs(monkeypatch, *, discover_mcp_tools=None):
    """Stub every lazy import inside _prepare_agent_startup (mirrors
    tests/hermes_cli/test_mcp_startup.py)."""
    monkeypatch.setitem(
        sys.modules, "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=lambda: None),
    )
    monkeypatch.setitem(
        sys.modules, "hermes_cli.config",
        types.SimpleNamespace(load_config=lambda: {}),
    )
    monkeypatch.setitem(
        sys.modules, "agent.shell_hooks",
        types.SimpleNamespace(register_from_config=lambda *_a, **_k: None),
    )
    monkeypatch.setitem(
        sys.modules, "agent.outbound_webhooks",
        types.SimpleNamespace(register_from_config=lambda *_a, **_k: None),
    )
    monkeypatch.setitem(
        sys.modules, "tools.mcp_oauth",
        types.SimpleNamespace(suppress_interactive_oauth=lambda: nullcontext()),
    )
    if discover_mcp_tools is not None:
        monkeypatch.setitem(
            sys.modules, "tools.mcp_tool",
            types.SimpleNamespace(discover_mcp_tools=discover_mcp_tools),
        )


def test_prepare_agent_startup_sets_yolo_env_before_discovery(monkeypatch):
    """#60328 contract: HERMES_YOLO_MODE must be set before any plugin/tool
    discovery import path runs."""
    seen = {}

    def _discover_plugins():
        seen["yolo"] = os.environ.get("HERMES_YOLO_MODE")

    _install_agent_startup_stubs(monkeypatch, discover_mcp_tools=lambda: None)
    monkeypatch.setitem(
        sys.modules, "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=_discover_plugins),
    )
    monkeypatch.setitem(
        sys.modules, "hermes_cli.mcp_startup",
        types.SimpleNamespace(start_background_mcp_discovery=lambda **_k: None),
    )

    agent_startup._prepare_agent_startup(
        Namespace(command="chat", tui=False, yolo=True, safe_mode=False,
                  accept_hooks=False, gateway_command=None, cron_command=None,
                  mcp_action=None)
    )
    assert seen["yolo"] == "1"
    assert os.environ["HERMES_YOLO_MODE"] == "1"


def test_prepare_agent_startup_applies_safe_mode(monkeypatch):
    _install_agent_startup_stubs(monkeypatch, discover_mcp_tools=lambda: None)
    monkeypatch.setitem(
        sys.modules, "hermes_cli.mcp_startup",
        types.SimpleNamespace(start_background_mcp_discovery=lambda **_k: None),
    )
    agent_startup._prepare_agent_startup(
        Namespace(command="chat", tui=False, yolo=False, safe_mode=True,
                  accept_hooks=False, gateway_command=None, cron_command=None,
                  mcp_action=None)
    )
    assert os.environ["HERMES_SAFE_MODE"] == "1"


def test_prepare_agent_startup_skips_discovery_for_gateway_run(monkeypatch):
    """gateway run has a dedicated MCP startup path; nothing may be touched
    here except safe-mode/yolo plumbing."""
    calls = {"plugins": 0, "mcp": 0}

    def _discover_plugins():
        calls["plugins"] += 1

    monkeypatch.setitem(
        sys.modules, "hermes_cli.plugins",
        types.SimpleNamespace(discover_plugins=_discover_plugins),
    )
    _install_agent_startup_stubs(monkeypatch, discover_mcp_tools=lambda: calls.__setitem__("mcp", calls["mcp"] + 1))
    monkeypatch.setitem(
        sys.modules, "hermes_cli.mcp_startup",
        types.SimpleNamespace(start_background_mcp_discovery=lambda **_k: calls.__setitem__("mcp", calls["mcp"] + 1)),
    )

    agent_startup._prepare_agent_startup(
        Namespace(command="gateway", gateway_command="run", tui=False,
                  yolo=False, safe_mode=False, accept_hooks=False,
                  cron_command=None, mcp_action=None)
    )
    assert calls == {"plugins": 0, "mcp": 0}


def test_prepare_agent_startup_backgrounds_mcp_for_chat(monkeypatch):
    calls = {"bg": 0}

    def _bg(**_k):
        calls["bg"] += 1

    _install_agent_startup_stubs(monkeypatch)
    monkeypatch.setitem(
        sys.modules, "hermes_cli.mcp_startup",
        types.SimpleNamespace(start_background_mcp_discovery=_bg),
    )
    agent_startup._prepare_agent_startup(
        Namespace(command="chat", tui=False, yolo=False, safe_mode=False,
                  accept_hooks=False, gateway_command=None, cron_command=None,
                  mcp_action=None)
    )
    assert calls["bg"] == 1


# ── re-export contract: hermes_cli.main.<name> still resolves ─────────────


def test_main_reexports_agent_startup_names():
    assert main_mod._is_tui_chat_launch is agent_startup._is_tui_chat_launch
    assert main_mod._command_has_dedicated_mcp_startup is agent_startup._command_has_dedicated_mcp_startup
    assert main_mod._should_background_mcp_startup is agent_startup._should_background_mcp_startup
    assert main_mod._prepare_agent_startup is agent_startup._prepare_agent_startup
    assert main_mod._apply_safe_mode is agent_startup._apply_safe_mode
    assert main_mod._set_chat_arg_defaults is agent_startup._set_chat_arg_defaults
    assert main_mod._AGENT_COMMANDS is agent_startup._AGENT_COMMANDS
    assert main_mod._AGENT_SUBCOMMANDS is agent_startup._AGENT_SUBCOMMANDS


def test_main_patch_surface_for_prepare_agent_startup(monkeypatch):
    """test_yolo_startup_order patches ``hermes_cli.main._prepare_agent_startup``;
    the patch must keep working against the re-exported name."""
    patched = []

    def _spy(args):
        patched.append(args.command)

    monkeypatch.setattr(main_mod, "_prepare_agent_startup", _spy)
    main_mod._prepare_agent_startup(Namespace(command="chat"))
    assert patched == ["chat"]
