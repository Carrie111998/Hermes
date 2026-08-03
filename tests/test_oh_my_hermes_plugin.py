"""Smoke tests for the vendored oh-my-hermes plugin bridge."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "oh-my-hermes" / "__init__.py"
BUNDLE = ROOT / "vendor" / "oh-my-hermes" / "src" / "plugin_bundle"
SKILLS = ROOT / "vendor" / "oh-my-hermes" / "skills"


class StubContext:
    def __init__(self) -> None:
        self.tools = []
        self.hooks = []
        self.commands = []

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools.append((name, toolset, schema, handler))

    def register_hook(self, name, callback):
        self.hooks.append(name)

    def register_cli_command(self, **kwargs):
        self.commands.append(kwargs["name"])


def load_plugin():
    spec = importlib.util.spec_from_file_location("test_oh_my_hermes_plugin", PLUGIN)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_register_soft_skips_upstream_when_submodule_missing(monkeypatch):
    """CI shallow checkouts omit vendor/oh-my-hermes — must not raise."""
    plugin = load_plugin()
    monkeypatch.setattr(plugin, "submodule_ready", lambda: False)
    context = StubContext()

    plugin.register(context)

    assert context.tools == []
    assert context.hooks == []
    assert "oh-my-hermes" in context.commands


@pytest.mark.skipif(
    not BUNDLE.is_dir(),
    reason="vendor/oh-my-hermes submodule not checked out",
)
def test_plugin_registers_upstream_tools_hooks_and_cli():
    plugin = load_plugin()
    context = StubContext()
    plugin.register(context)

    assert len(context.tools) == 10
    assert {tool[1] for tool in context.tools} == {"omh"}
    assert {tool[0] for tool in context.tools} == {
        "omh_capabilities",
        "omh_context",
        "omh_gather_evidence",
        "omh_hud",
        "omh_interact",
        "omh_memory",
        "omh_probe",
        "omh_recommend",
        "omh_role",
        "omh_status",
    }
    assert {"on_session_end", "pre_llm_call", "pre_tool_call"}.issubset(context.hooks)
    assert "oh-my-hermes" in context.commands


@pytest.mark.skipif(
    not SKILLS.is_dir(),
    reason="vendor/oh-my-hermes submodule not checked out",
)
def test_submodule_contains_workflow_skills():
    plugin = load_plugin()
    skills = plugin._upstream_skills_root()
    assert skills.is_dir()
    assert (skills / "omh-routing" / "SKILL.md").is_file()
