"""Tests for the ai-firstify plugin.

Covers ``plugins/ai-firstify/``:

  * The bundled skill tree is present and complete (SKILL.md, the six
    reference files, the three per-mode playbooks, and the validate script).
  * ``register()`` wires up the ``ai-firstify:ai-firstify`` skill and the
    ``/ai-firstify`` slash command against a stub context.
  * The slash handler parses modes/aliases and points at on-disk reference
    paths (plugin skills cannot serve sub-files via ``skill_view``).
  * Bundled-plugin discovery + skill registration via
    ``PluginManager.discover_and_load``, and end-to-end resolution through
    ``skill_view``.
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    yield hermes_home


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _plugin_dir() -> Path:
    return _repo_root() / "plugins" / "ai-firstify"


def _skill_dir() -> Path:
    return _plugin_dir() / "skills" / "ai-firstify"


def _load_plugin_init():
    """Import the plugin __init__.py in isolation (no config/manager glue)."""
    plugin_dir = _plugin_dir()
    if "hermes_plugins" not in sys.modules:
        ns = types.ModuleType("hermes_plugins")
        ns.__path__ = []
        sys.modules["hermes_plugins"] = ns
    spec = importlib.util.spec_from_file_location(
        "hermes_plugins.ai_firstify",
        plugin_dir / "__init__.py",
        submodule_search_locations=[str(plugin_dir)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "hermes_plugins.ai_firstify"
    mod.__path__ = [str(plugin_dir)]
    sys.modules["hermes_plugins.ai_firstify"] = mod
    spec.loader.exec_module(mod)
    return mod


class _StubCtx:
    """Minimal stand-in for the PluginContext passed to ``register()``."""

    def __init__(self):
        self.skills: dict = {}
        self.commands: dict = {}

    def register_skill(self, name, path, description=""):
        assert isinstance(path, Path) and path.exists(), f"missing SKILL.md: {path}"
        self.skills[name] = {"path": path, "description": description}

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands[name] = {
            "handler": handler,
            "description": description,
            "args_hint": args_hint,
        }


# ---------------------------------------------------------------------------
# Bundled skill tree
# ---------------------------------------------------------------------------

class TestSkillTree:
    def test_skill_md_present(self):
        assert (_skill_dir() / "SKILL.md").is_file()

    def test_all_reference_files_present(self):
        refs = _skill_dir() / "references"
        expected = {
            "principles.md",
            "patterns.md",
            "anti-patterns.md",
            "skill-architecture.md",
            "project-structure.md",
            "assessment-rubric.md",
            "mode-audit.md",
            "mode-reengineer.md",
            "mode-bootstrap.md",
        }
        present = {p.name for p in refs.glob("*.md")}
        assert expected <= present, expected - present

    def test_validate_script_present(self):
        assert (_skill_dir() / "scripts" / "validate-report.sh").is_file()

    def test_licensing_present(self):
        assert (_plugin_dir() / "LICENSE").is_file()
        assert (_plugin_dir() / "NOTICE").is_file()
        assert (_plugin_dir() / "plugin.yaml").is_file()


# ---------------------------------------------------------------------------
# register() + slash handler
# ---------------------------------------------------------------------------

class TestRegistration:
    def test_register_wires_skill_and_command(self):
        mod = _load_plugin_init()
        ctx = _StubCtx()
        mod.register(ctx)
        assert "ai-firstify" in ctx.skills
        assert ctx.skills["ai-firstify"]["path"] == _skill_dir() / "SKILL.md"
        assert "ai-firstify" in ctx.commands

    def test_help_lists_all_modes(self):
        mod = _load_plugin_init()
        ctx = _StubCtx()
        mod.register(ctx)
        out = ctx.commands["ai-firstify"]["handler"]("help")
        for mode in ("audit", "reengineer", "bootstrap"):
            assert mode in out

    @pytest.mark.parametrize(
        "raw,expected_mode",
        [
            ("", "audit"),
            ("audit", "audit"),
            ("review", "audit"),
            ("reengineer", "reengineer"),
            ("re-engineer", "reengineer"),
            ("fix", "reengineer"),
            ("bootstrap", "bootstrap"),
            ("start", "bootstrap"),
        ],
    )
    def test_mode_aliases(self, raw, expected_mode):
        mod = _load_plugin_init()
        ctx = _StubCtx()
        mod.register(ctx)
        out = ctx.commands["ai-firstify"]["handler"](raw)
        assert f"{expected_mode} mode" in out
        # Points at the real on-disk playbook for the resolved mode.
        assert str(_skill_dir() / "references" / f"mode-{expected_mode}.md") in out

    def test_target_is_echoed(self):
        mod = _load_plugin_init()
        ctx = _StubCtx()
        mod.register(ctx)
        out = ctx.commands["ai-firstify"]["handler"]("audit ./some/path")
        assert "./some/path" in out


# ---------------------------------------------------------------------------
# End-to-end discovery through the real PluginManager
# ---------------------------------------------------------------------------

class TestPluginDiscovery:
    def test_loads_and_registers_skill(self, _isolate_env):
        import yaml

        config = {"plugins": {"enabled": ["ai-firstify"]}}
        (_isolate_env / "config.yaml").write_text(yaml.safe_dump(config))

        for k in list(sys.modules):
            if k.startswith(("hermes_plugins", "hermes_cli.plugins")):
                del sys.modules[k]

        from hermes_cli.plugins import _ensure_plugins_discovered

        mgr = _ensure_plugins_discovered(force=True)
        assert "ai-firstify" in set(getattr(mgr, "_plugins", {}))

        skill_md = mgr.find_plugin_skill("ai-firstify:ai-firstify")
        assert skill_md is not None and skill_md.exists()
        assert "ai-firstify" in mgr.list_plugin_skills("ai-firstify")

    def test_skill_view_serves_bundled_skill(self, _isolate_env):
        import yaml

        config = {"plugins": {"enabled": ["ai-firstify"]}}
        (_isolate_env / "config.yaml").write_text(yaml.safe_dump(config))

        for k in list(sys.modules):
            if k.startswith(("hermes_plugins", "hermes_cli.plugins")):
                del sys.modules[k]

        from hermes_cli.plugins import _ensure_plugins_discovered
        from tools.skills_tool import skill_view

        _ensure_plugins_discovered(force=True)
        result = json.loads(skill_view("ai-firstify:ai-firstify"))
        assert result.get("success") is True
        assert result.get("name") == "ai-firstify:ai-firstify"
        assert "AI-Firstify" in (result.get("content") or "")
