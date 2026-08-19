"""Tests for hermes tools disable/enable/list command (backend)."""
import json
import os
import subprocess
import sys
from argparse import Namespace
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gateway.platform_registry import platform_registry
from hermes_cli.tools_config import (
    _diagnostic_memory_provider,
    _diagnostic_context_engine,
    _known_tool_platforms,
    _print_tools_diagnostics,
    build_tools_diagnostics,
    tools_diagnose_command,
    tools_disable_enable_command,
)


# ── Built-in toolset disable ────────────────────────────────────────────────


class TestToolsDisableBuiltin:

    def test_disable_removes_toolset_from_platform(self):
        config = {"platform_toolsets": {"cli": ["web", "memory", "terminal"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(Namespace(tools_action="disable", names=["web"], platform="cli"))
        saved = mock_save.call_args[0][0]
        assert "web" not in saved["platform_toolsets"]["cli"]
        assert "memory" in saved["platform_toolsets"]["cli"]


# ── Built-in toolset enable ─────────────────────────────────────────────────


# ── MCP tool disable ────────────────────────────────────────────────────────


class TestToolsDisableMcp:


    def test_disable_unknown_server_prints_error(self, capsys):
        config = {"mcp_servers": {}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config"):
            tools_disable_enable_command(
                Namespace(tools_action="disable", names=["unknown:tool"], platform="cli")
            )
        out = capsys.readouterr().out
        assert "MCP server 'unknown' not found in config" in out


# ── MCP tool enable ──────────────────────────────────────────────────────────


# ── Mixed targets ────────────────────────────────────────────────────────────


# ── List output ──────────────────────────────────────────────────────────────


class TestToolsList:


    def test_list_shows_mcp_excluded_tools(self, capsys):
        config = {
            "mcp_servers": {"github": {"tools": {"exclude": ["create_issue"]}}},
        }
        with patch("hermes_cli.tools_config.load_config", return_value=config):
            tools_disable_enable_command(Namespace(tools_action="list", platform="cli"))
        out = capsys.readouterr().out
        assert "github" in out
        assert "create_issue" in out


# ── Diagnostics ──────────────────────────────────────────────────────────────


class TestToolsDiagnose:

    def test_known_platforms_include_disposable_manager_registrations(self):
        manager = MagicMock()
        manager._plugin_platform_names = {"late-probe-platform"}
        with patch("hermes_cli.plugins.discover_plugins"), patch(
            "hermes_cli.plugins.get_plugin_manager", return_value=manager
        ), patch.object(platform_registry, "registered_names", return_value=set()):
            assert "late-probe-platform" in _known_tool_platforms()

    def test_unknown_platform_json_is_structured_and_fails(self, capsys):
        args = Namespace(platform="definitely-invalid", json=True)

        result = tools_diagnose_command(args)

        assert result == 2
        payload = json.loads(capsys.readouterr().out)
        assert payload["error"]["code"] == "unknown_platform"
        assert payload["error"]["platform"] == "definitely-invalid"
        assert "cli" in payload["error"]["valid_platforms"]

    def test_unknown_platform_human_error_uses_stderr(self, capsys):
        result = tools_diagnose_command(
            Namespace(platform="definitely-invalid", json=False)
        )

        captured = capsys.readouterr()
        assert result == 2
        assert captured.out == ""
        assert "Unknown platform" in captured.err

    def test_json_output_isolated_from_provider_stdout(self, capsys):
        def _noisy_build(_config, _platform):
            print("provider-noise")
            return {"platform": "cli", "ok": True}

        with patch(
            "hermes_cli.tools_config.load_config_for_diagnostics", return_value={}
        ), patch(
            "hermes_cli.tools_config._known_tool_platforms", return_value={"cli"}
        ), patch(
            "hermes_cli.tools_config.build_tools_diagnostics",
            side_effect=_noisy_build,
        ):
            result = tools_diagnose_command(Namespace(platform="cli", json=True))

        captured = capsys.readouterr()
        assert result == 0
        assert json.loads(captured.out) == {"platform": "cli", "ok": True}
        assert "provider-noise" not in captured.out
        assert captured.err == ""

    def test_human_output_isolated_from_provider_terminal_controls(self, capsys):
        diag = {
            "platform": "cli",
            "resolution": {},
            "enabled_toolsets": [],
            "config_sources": {},
            "tools_visible": [],
            "tool_search": {},
            "provider_tools": {},
            "filtered": [],
            "conflicts": [],
        }

        def _noisy_build(_config, _platform):
            print("\x1b]0;provider-controlled\x07")
            return diag

        with patch(
            "hermes_cli.tools_config.load_config_for_diagnostics", return_value={}
        ), patch(
            "hermes_cli.tools_config._known_tool_platforms", return_value={"cli"}
        ), patch(
            "hermes_cli.tools_config.build_tools_diagnostics",
            side_effect=_noisy_build,
        ):
            result = tools_diagnose_command(Namespace(platform="cli", json=False))

        captured = capsys.readouterr()
        assert result == 0
        assert captured.out.startswith("Tool diagnostics (cli):")
        assert "provider-controlled" not in captured.out
        assert "\x1b" not in captured.out
        assert "\x07" not in captured.out
        assert captured.err == ""

    def test_output_isolates_raw_descriptors_and_inherited_children(self, capfd):
        import os
        import subprocess
        import sys

        diag = {
            "platform": "cli",
            "resolution": {},
            "enabled_toolsets": [],
            "config_sources": {},
            "tools_visible": [],
            "tool_search": {},
            "provider_tools": {},
            "filtered": [],
            "conflicts": [],
        }

        def _noisy_build(_config, _platform):
            os.write(1, b"fd-stdout-noise\n")
            os.write(2, b"fd-stderr-noise\n")
            subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import os; os.write(1, b'child-stdout-noise\\n'); "
                    "os.write(2, b'child-stderr-noise\\n')",
                ],
                check=True,
            )
            return diag

        with patch(
            "hermes_cli.tools_config.load_config_for_diagnostics", return_value={}
        ), patch(
            "hermes_cli.tools_config._known_tool_platforms", return_value={"cli"}
        ), patch(
            "hermes_cli.tools_config.build_tools_diagnostics",
            side_effect=_noisy_build,
        ):
            assert tools_diagnose_command(Namespace(platform="cli", json=True)) == 0
            json_output = capfd.readouterr()
            assert json.loads(json_output.out) == diag
            assert json_output.err == ""

            assert tools_diagnose_command(Namespace(platform="cli", json=False)) == 0
            human_output = capfd.readouterr()
            assert human_output.out.startswith("Tool diagnostics (cli):")
            assert human_output.err == ""

        combined = json_output.out + human_output.out
        assert "fd-stdout-noise" not in combined
        assert "fd-stderr-noise" not in combined
        assert "child-stdout-noise" not in combined
        assert "child-stderr-noise" not in combined

    def test_memory_diagnostics_never_register_provider_skills(self):
        with patch(
            "plugins.memory.load_memory_provider", return_value=None
        ) as load_provider:
            _diagnostic_memory_provider(
                {"memory": {"provider": "stub-memory"}}
            )

        load_provider.assert_called_once_with(
            "stub-memory", register_skills=False
        )

    def test_context_diagnostics_never_register_plugin_commands(self):
        with patch(
            "plugins.context_engine.load_context_engine", return_value=None
        ) as load_engine, patch(
            "hermes_cli.plugins.get_plugin_context_engine", return_value=None
        ):
            _diagnostic_context_engine(
                {"context": {"engine": "stub-context"}}
            )

        load_engine.assert_called_once_with(
            "stub-context", register_commands=False
        )

    def test_context_engine_diagnostics_rejects_uncopyable_plugin_singleton(self):
        class _UncopyableEngine:
            name = "uncopyable"

            def __deepcopy__(self, memo):
                raise TypeError("contains a lock")

            def get_tool_schemas(self):
                return [{"name": "should_not_be_visible"}]

        with patch(
            "plugins.context_engine.load_context_engine", return_value=None
        ), patch(
            "hermes_cli.plugins.get_plugin_context_engine",
            return_value=_UncopyableEngine(),
        ):
            provider, schemas, error = _diagnostic_context_engine(
                {"context": {"engine": "uncopyable"}}
            )

        assert provider == "uncopyable"
        assert schemas == []
        assert error == "engine cannot be copied safely"

    def test_build_diagnostics_reports_visible_and_disabled_tools(self):
        config = {
            "platform_toolsets": {"cli": ["file"]},
            "tools": {"tool_search": {"enabled": "off"}},
        }

        diag = build_tools_diagnostics(config, "cli")

        assert diag["platform"] == "cli"
        assert diag["resolution"]["phase"] == "pre_startup"
        assert "file" in diag["enabled_toolsets"]
        assert "read_file" in diag["tools_visible"]
        assert {
            "tool": "terminal",
            "toolset": "terminal",
            "reason": "toolset disabled",
        } in diag["filtered"]

    def test_composite_disabled_toolset_reports_policy_reason(self):
        config = {
            "platform_toolsets": {"cli": ["file", "terminal", "web"]},
            "agent": {"disabled_toolsets": ["debugging"]},
            "tools": {"tool_search": {"enabled": "off"}},
        }

        diag = build_tools_diagnostics(config, "cli")
        reasons = {
            row["tool"]: row["reason"]
            for row in diag["filtered"]
            if row["tool"] in {"read_file", "terminal", "web_search"}
        }

        assert reasons == {
            "read_file": "toolset disabled",
            "terminal": "toolset disabled",
            "web_search": "toolset disabled",
        }

    def test_build_diagnostics_reports_external_memory_and_context_tools(self):
        config = {
            "memory": {"provider": "stub-memory"},
            "context": {"engine": "stub-context"},
            "platform_toolsets": {
                "cli": ["file", "memory", "context_engine"]
            },
            "tools": {"tool_search": {"enabled": "off"}},
        }
        memory_schema = {
            "name": "stub_memory_recall",
            "description": "Recall",
            "parameters": {"type": "object", "properties": {}},
        }
        context_schema = {
            "name": "stub_context_expand",
            "description": "Expand",
            "parameters": {"type": "object", "properties": {}},
        }

        with patch(
            "hermes_cli.tools_config._diagnostic_memory_provider",
            return_value=("stub-memory", [memory_schema], None),
        ), patch(
            "hermes_cli.tools_config._diagnostic_context_engine",
            return_value=("stub-context", [context_schema], None),
        ):
            diag = build_tools_diagnostics(config, "cli")

        assert "stub_memory_recall" in diag["tools_visible"]
        assert "stub_context_expand" in diag["tools_visible"]
        assert diag["provider_tools"]["memory"]["injected"] == 1
        assert diag["provider_tools"]["context_engine"]["injected"] == 1

    def test_duplicate_external_schema_is_a_top_level_conflict(self):
        config = {
            "platform_toolsets": {"cli": ["file", "memory"]},
            "tools": {"tool_search": {"enabled": "off"}},
        }
        duplicate = {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {}},
        }
        unique = {
            "name": "memory_unique",
            "parameters": {"type": "object", "properties": {}},
        }

        with patch(
            "hermes_cli.tools_config._diagnostic_memory_provider",
            return_value=("stub-memory", [duplicate, unique], None),
        ), patch(
            "hermes_cli.tools_config._diagnostic_context_engine",
            return_value=("compressor", [], None),
        ):
            diag = build_tools_diagnostics(config, "cli")

        assert {
            "tool": "read_file",
            "reason": "duplicate tool name",
            "source": "memory",
        } in diag["conflicts"]
        assert (
            diag["provider_tools"]["memory"]["skipped_reason"]
            == "duplicate tool name"
        )

    def test_plugin_registration_conflicts_are_reported_before_dedup(self):
        config = {
            "platform_toolsets": {"cli": ["file"]},
            "tools": {"tool_search": {"enabled": "off"}},
        }
        conflict = {
            "tool": "read_file",
            "reason": "plugin tool overrides existing registry tool",
            "source": "stub-plugin",
        }

        with patch(
            "hermes_cli.plugins.get_diagnostic_plugin_conflicts",
            return_value=[conflict],
            create=True,
        ):
            diag = build_tools_diagnostics(config, "cli")

        assert conflict in diag["conflicts"]

    def test_in_process_diagnose_preserves_cold_registry_state(self, tmp_path):
        code = r'''
import contextlib
import io
import json
from argparse import Namespace
from hermes_cli.tools_config import tools_diagnose_command
from tools.registry import registry

before = {
    "generation": registry._generation,
    "names": registry.get_all_tool_names(),
    "aliases": registry.get_registered_toolset_aliases(),
}
with contextlib.redirect_stdout(io.StringIO()):
    rc = tools_diagnose_command(Namespace(platform="cli", json=True))
after = {
    "generation": registry._generation,
    "names": registry.get_all_tool_names(),
    "aliases": registry.get_registered_toolset_aliases(),
}
print(json.dumps({"rc": rc, "same": before == after}))
'''
        env = {
            **os.environ,
            "HOME": str(tmp_path),
            "HERMES_HOME": str(tmp_path / "missing-home"),
            "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )

        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {"rc": 0, "same": True}

    def test_human_renderer_escapes_provider_control_characters(self, capsys):
        diag = {
            "platform": "cli",
            "resolution": {"phase": "pre_startup", "detail": "projection"},
            "enabled_toolsets": ["file"],
            "disabled_toolsets": [],
            "config_sources": {},
            "tools_visible": ["evil\x1b[31m\nFORGED"],
            "tool_search": {},
            "provider_tools": {},
            "filtered": [],
            "conflicts": [],
        }

        _print_tools_diagnostics(diag)

        out = capsys.readouterr().out
        assert "\x1b" not in out
        assert "\nFORGED" not in out
        assert r"\x1b" in out
        assert r"\nFORGED" in out

    def test_build_diagnostics_reports_external_family_toolset_gates(self):
        config = {
            "memory": {"provider": "stub-memory"},
            "context": {"engine": "stub-context"},
            "platform_toolsets": {"cli": ["file"]},
            "agent": {
                "disabled_toolsets": '["memory", "context_engine"]'
            },
            "tools": {"tool_search": {"enabled": "off"}},
        }
        memory_schema = {"name": "gated_memory", "parameters": {}}
        context_schema = {"name": "gated_context", "parameters": {}}

        with patch(
            "hermes_cli.tools_config._diagnostic_memory_provider",
            return_value=("stub-memory", [memory_schema], None),
        ), patch(
            "hermes_cli.tools_config._diagnostic_context_engine",
            return_value=("stub-context", [context_schema], None),
        ):
            diag = build_tools_diagnostics(config, "cli")

        assert "gated_memory" not in diag["tools_visible"]
        assert "gated_context" not in diag["tools_visible"]
        assert diag["provider_tools"]["memory"]["skipped_reason"] == "toolset disabled"
        assert diag["provider_tools"]["context_engine"]["skipped_reason"] == "toolset disabled"

    def test_plugin_provider_counts_owned_names_not_shared_toolset_members(self):
        from tools.registry import registry

        tool_name = "diagnose_owned_plugin_tool"
        registry.register(
            name=tool_name,
            handler=lambda args, **kwargs: "{}",
            schema={
                "name": tool_name,
                "description": "Plugin-owned file helper",
                "parameters": {"type": "object", "properties": {}},
            },
            toolset="file",
        )
        config = {
            "platform_toolsets": {"cli": ["file"]},
            "tools": {"tool_search": {"enabled": "off"}},
        }

        try:
            with patch(
                "hermes_cli.tools_config._get_plugin_toolset_keys",
                return_value={"file"},
            ), patch(
                "hermes_cli.plugins.get_plugin_tool_names",
                return_value={tool_name},
                create=True,
            ):
                diag = build_tools_diagnostics(config, "cli")
        finally:
            registry.deregister(tool_name)

        plugins = diag["provider_tools"]["plugins"]
        assert plugins["schemas"] == 1
        assert plugins["injected"] == 1
        assert plugins["toolsets"] == ["file"]

    def test_build_diagnostics_reports_activated_tool_search(self):
        from tools.registry import registry

        tool_name = "diagnose_deferred_tool"
        toolset = "mcp-diagnose-test"
        registry.register(
            name=tool_name,
            handler=lambda args, **kwargs: "{}",
            schema={
                "name": tool_name,
                "description": "Deferred diagnostic tool",
                "parameters": {"type": "object", "properties": {}},
            },
            toolset=toolset,
        )
        try:
            config = {
                "platform_toolsets": {"cli": [toolset]},
                "tools": {"tool_search": {"enabled": "on"}},
            }
            with patch(
                "hermes_cli.tools_config._get_platform_tools",
                return_value={toolset},
            ):
                diag = build_tools_diagnostics(config, "cli")
        finally:
            registry.deregister(tool_name)

        assert diag["tool_search"]["activated"] is True
        assert tool_name not in diag["tools_visible"]
        assert {"tool_search", "tool_describe", "tool_call"}.issubset(
            diag["tools_visible"]
        )
        assert {
            "tool": tool_name,
            "toolset": toolset,
            "reason": "deferred by tool search",
        } in diag["filtered"]

    def test_build_diagnostics_does_not_publish_or_populate_global_state(
        self, monkeypatch
    ):
        import model_tools
        import tools.registry as registry_module

        monkeypatch.setattr(
            model_tools, "_last_resolved_tool_names", ["runtime_tool"]
        )
        model_tools._clear_tool_defs_cache()
        check_cache_before = dict(registry_module._check_fn_cache)
        last_good_before = dict(registry_module._check_fn_last_good)
        config = {
            "platform_toolsets": {"cli": ["file"]},
            "tools": {"tool_search": {"enabled": "off"}},
        }
        original_config = deepcopy(config)

        build_tools_diagnostics(config, "cli")

        assert config == original_config
        assert model_tools._last_resolved_tool_names == ["runtime_tool"]
        assert model_tools._tool_defs_cache == {}
        assert registry_module._check_fn_cache == check_cache_before
        assert registry_module._check_fn_last_good == last_good_before

    def test_diagnose_command_does_not_mutate_config_caches(
        self, tmp_path, monkeypatch, capsys
    ):
        from hermes_cli import config as config_mod
        from hermes_cli import managed_scope
        from hermes_cli import plugins as plugins_mod

        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        (hermes_home / "config.yaml").write_text("agent:\n  max_turns: 7\n")
        managed_home = tmp_path / "managed"
        managed_home.mkdir()
        (managed_home / "config.yaml").write_text("agent:\n  max_turns: 9\n")
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_home))
        before = (
            deepcopy(config_mod._LOAD_CONFIG_CACHE),
            deepcopy(config_mod._RAW_CONFIG_CACHE),
            deepcopy(config_mod._LAST_EXPANDED_CONFIG_BY_PATH),
            deepcopy(config_mod._CONFIG_PARSE_WARNED),
            deepcopy(managed_scope._CONFIG_CACHE),
            dict(plugins_mod._plugin_managers_by_home),
            plugins_mod._plugin_manager,
        )
        assert tools_diagnose_command(Namespace(platform="cli", json=True)) == 0
        json.loads(capsys.readouterr().out)

        assert config_mod._LOAD_CONFIG_CACHE == before[0]
        assert config_mod._RAW_CONFIG_CACHE == before[1]
        assert config_mod._LAST_EXPANDED_CONFIG_BY_PATH == before[2]
        assert config_mod._CONFIG_PARSE_WARNED == before[3]
        assert managed_scope._CONFIG_CACHE == before[4]
        assert plugins_mod._plugin_managers_by_home == before[5]
        assert plugins_mod._plugin_manager is before[6]

    def test_diagnose_fresh_process_does_not_create_runtime_state(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        env = dict(os.environ)
        env["HERMES_HOME"] = str(hermes_home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("HERMES_PROFILE", None)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "tools",
                "diagnose",
                "--json",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        diagnostics = json.loads(result.stdout)
        assert "session_search" in diagnostics["tools_visible"]
        assert not hermes_home.exists()

    def test_diagnose_invalid_platform_fresh_process_is_zero_write(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env["HERMES_HOME"] = str(hermes_home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("HERMES_PROFILE", None)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "tools",
                "diagnose",
                "--platform",
                "definitely-invalid",
                "--json",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 2, result.stderr
        assert json.loads(result.stdout)["error"]["code"] == "unknown_platform"
        assert result.stderr == ""
        assert not hermes_home.exists()

    def test_diagnose_malformed_config_is_zero_write_and_machine_safe(
        self, tmp_path
    ):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        config_path = hermes_home / "config.yaml"
        config_path.write_text("platform_toolsets: [unterminated\n")
        before_tree = {
            str(path.relative_to(hermes_home)): (
                path.read_bytes() if path.is_file() else None
            )
            for path in hermes_home.rglob("*")
        }
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env["HERMES_HOME"] = str(hermes_home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("HERMES_PROFILE", None)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "tools",
                "diagnose",
                "--json",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        after_tree = {
            str(path.relative_to(hermes_home)): (
                path.read_bytes() if path.is_file() else None
            )
            for path in hermes_home.rglob("*")
        }

        assert result.returncode == 0
        json.loads(result.stdout)
        assert result.stderr == ""
        assert after_tree == before_tree

    def test_diagnose_existing_env_is_byte_and_tree_read_only(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        hermes_home.mkdir()
        env_path = hermes_home / ".env"
        env_path.write_bytes("NOUS_API_KEY=opaque\n".encode("utf-16-le"))
        before_bytes = env_path.read_bytes()
        before_stat = env_path.stat()
        before_tree = {
            str(path.relative_to(hermes_home)) for path in hermes_home.rglob("*")
        }
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env["HERMES_HOME"] = str(hermes_home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("HERMES_PROFILE", None)

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "hermes_cli.main",
                "tools",
                "diagnose",
                "--json",
            ],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        json.loads(result.stdout)
        after_stat = env_path.stat()
        after_tree = {
            str(path.relative_to(hermes_home)) for path in hermes_home.rglob("*")
        }
        assert result.stderr == ""
        assert env_path.read_bytes() == before_bytes
        assert after_stat.st_mode == before_stat.st_mode
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
        assert after_tree == before_tree

    def test_diagnose_existing_nous_auth_is_byte_and_tree_read_only(self, tmp_path):
        hermes_home = tmp_path / "hermes"
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env["HERMES_HOME"] = str(hermes_home)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.pop("HERMES_PROFILE", None)
        for name in (
            "NOUS_API_KEY",
            "NOUS_INFERENCE_API_KEY",
            "NOUS_INFERENCE_BASE_URL",
        ):
            env.pop(name, None)

        command = [
            sys.executable,
            "-m",
            "hermes_cli.main",
            "tools",
            "diagnose",
            "--platform",
            "cli",
            "--json",
        ]
        repo_root = str(Path(__file__).resolve().parents[2])

        hermes_home.mkdir(parents=True)
        auth_path = hermes_home / "auth.json"
        auth_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "active_provider": "nous",
                    "providers": {
                        "nous": {
                            "access_token": "opaque-access-token",
                            "refresh_token": "refresh-token",
                            "inference_base_url": (
                                "https://inference-api.nousresearch.com/v1"
                            ),
                        }
                    },
                },
                indent=2,
            )
            + "\n"
        )
        auth_path.chmod(0o600)
        before_bytes = auth_path.read_bytes()
        before_stat = auth_path.stat()
        before_tree = {
            str(path.relative_to(hermes_home)) for path in hermes_home.rglob("*")
        }

        result = subprocess.run(
            command,
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        json.loads(result.stdout)
        after_stat = auth_path.stat()
        after_tree = {
            str(path.relative_to(hermes_home)) for path in hermes_home.rglob("*")
        }
        assert auth_path.read_bytes() == before_bytes
        assert after_stat.st_mode == before_stat.st_mode
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
        assert after_tree == before_tree


# ── Validation ───────────────────────────────────────────────────────────────


class TestToolsValidation:


    def test_mixed_valid_and_invalid_applies_valid_only(self):
        config = {"platform_toolsets": {"cli": ["web", "memory"]}}
        with patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as mock_save:
            tools_disable_enable_command(
                Namespace(tools_action="disable", names=["web", "bad_toolset"], platform="cli")
            )
        saved = mock_save.call_args[0][0]
        assert "web" not in saved["platform_toolsets"]["cli"]
        assert "memory" in saved["platform_toolsets"]["cli"]


@pytest.mark.parametrize("action", ["list", "enable", "disable"])
def test_tools_action_accepts_deferred_plugin_without_materializing(action, capsys):
    platform = "deferred-tools-test"
    loader = MagicMock()
    configured_tools = ["memory", "web"] if action == "disable" else ["memory"]
    config = {"platform_toolsets": {platform: configured_tools}}
    args = Namespace(tools_action=action, platform=platform)
    if action != "list":
        args.names = ["web"]

    def discover_deferred_platform():
        platform_registry.register_deferred(platform, loader)

    try:
        with patch(
            "hermes_cli.plugins.discover_plugins",
            side_effect=discover_deferred_platform,
        ) as discover, \
             patch("hermes_cli.tools_config.load_config", return_value=config), \
             patch("hermes_cli.tools_config.save_config") as save:
            tools_disable_enable_command(args)

        out = capsys.readouterr().out
        assert "Unknown platform" not in out
        discover.assert_called()
        loader.assert_not_called()
        if action == "list":
            assert f"Built-in toolsets ({platform}):" in out
            save.assert_not_called()
        else:
            save.assert_called()
            saved_tools = save.call_args.args[0]["platform_toolsets"][platform]
            assert ("web" in saved_tools) is (action == "enable")
    finally:
        platform_registry.unregister(platform)
