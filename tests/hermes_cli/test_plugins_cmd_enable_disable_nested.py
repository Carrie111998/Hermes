"""Tests for nested/alias-normalized enable & disable flows.

Companion to test_plugins_cmd_category_discovery.py. That file covers the
*listing* side of nested category plugins (issue #41066). These tests cover
the *mutation* side: `hermes plugins enable/disable` must resolve a bare name
OR a full path-derived key (e.g. `observability/trace_sink`) to the canonical
registry key and write THAT — the same string PluginManager gates on — so a
nested bundled plugin can actually be toggled.
"""

import sys  # noqa: F401
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _make_plugin_dir(parent: Path, name: str, manifest: dict) -> Path:
    d = parent / name
    d.mkdir(parents=True, exist_ok=True)
    import yaml
    (d / "plugin.yaml").write_text(yaml.dump(manifest), encoding="utf-8")
    (d / "__init__.py").write_text("def register(ctx): pass\n", encoding="utf-8")
    return d


def _make_category_plugin(parent: Path, category: str, name: str, manifest: dict) -> Path:
    return _make_plugin_dir(parent / category, name, manifest)


@pytest.fixture
def nested_plugin_env(tmp_path):
    """A user-plugins dir containing one nested and one flat plugin, with the
    bundled dir pointed at an empty path. Returns the tmp_path."""
    _make_category_plugin(tmp_path, "observability", "trace_sink", {
        "name": "trace_sink", "version": "1.0.0", "description": "trace sink"
    })
    _make_plugin_dir(tmp_path, "disk-cleanup", {
        "name": "disk-cleanup", "version": "1.0.0"
    })
    return tmp_path


# ---------------------------------------------------------------------------
# _resolve_plugin_key
# ---------------------------------------------------------------------------


class TestResolvePluginKey:
    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    def test_full_key_resolves_to_itself(self, mock_user, mock_bundled, nested_plugin_env):
        from hermes_cli.plugins_cmd import _resolve_plugin_key
        mock_user.return_value = nested_plugin_env
        mock_bundled.return_value = nested_plugin_env / "nonexistent"
        assert _resolve_plugin_key("observability/trace_sink") == "observability/trace_sink"


    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    def test_unknown_returns_none(self, mock_user, mock_bundled, nested_plugin_env):
        from hermes_cli.plugins_cmd import _resolve_plugin_key
        mock_user.return_value = nested_plugin_env
        mock_bundled.return_value = nested_plugin_env / "nonexistent"
        assert _resolve_plugin_key("does-not-exist") is None

    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    def test_ambiguous_leaf_name_returns_none(self, mock_user, mock_bundled, tmp_path):
        """Same leaf name under two categories must NOT silently pick one."""
        from hermes_cli.plugins_cmd import _resolve_plugin_key
        _make_category_plugin(tmp_path, "image_gen", "openai", {"name": "image-gen-openai"})
        _make_category_plugin(
            tmp_path, "observability", "openai", {"name": "observability-openai"}
        )
        mock_user.return_value = tmp_path
        mock_bundled.return_value = tmp_path / "nonexistent"
        # Bare "openai" is ambiguous -> None; the full key still resolves.
        assert _resolve_plugin_key("openai") is None
        assert _resolve_plugin_key("image_gen/openai") == "image_gen/openai"


# ---------------------------------------------------------------------------
# cmd_enable / cmd_disable — write the canonical key
# ---------------------------------------------------------------------------


class TestEnableDisableNested:
    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    def test_enable_bare_name_writes_key(
        self, mock_en, mock_dis, mock_save_en, mock_save_dis,
        mock_user, mock_bundled, nested_plugin_env,
    ):
        from hermes_cli.plugins_cmd import cmd_enable
        mock_user.return_value = nested_plugin_env
        mock_bundled.return_value = nested_plugin_env / "nonexistent"

        cmd_enable("trace_sink", allow_tool_override=False)  # bare name

        saved = mock_save_en.call_args[0][0]
        # The canonical key — NOT the bare name — must be persisted, because
        # that is what PluginManager matches when deciding to load.
        assert "observability/trace_sink" in saved
        assert "trace_sink" not in saved or "observability/trace_sink" in saved


    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    def test_enable_unknown_plugin_exits(self, mock_user, mock_bundled, nested_plugin_env):
        from hermes_cli.plugins_cmd import cmd_enable
        mock_user.return_value = nested_plugin_env
        mock_bundled.return_value = nested_plugin_env / "nonexistent"
        with pytest.raises(SystemExit):
            cmd_enable("does-not-exist")

    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    def test_enable_flat_plugin_unchanged(
        self, mock_en, mock_dis, mock_save_en, mock_save_dis,
        mock_user, mock_bundled, nested_plugin_env,
    ):
        """Flat plugins keep writing their bare name (key == name) — no regression."""
        from hermes_cli.plugins_cmd import cmd_enable
        mock_user.return_value = nested_plugin_env
        mock_bundled.return_value = nested_plugin_env / "nonexistent"

        cmd_enable("disk-cleanup", allow_tool_override=False)
        saved = mock_save_en.call_args[0][0]
        assert "disk-cleanup" in saved


# ---------------------------------------------------------------------------
# cmd_enable — built-in tool override consent (issue #29249)
# ---------------------------------------------------------------------------


class TestEnableToolOverrideConsent:
    """Enabling a non-bundled plugin must surface a consent decision about the
    privileged ``allow_tool_override`` capability, and persist the operator's
    choice under ``plugins.entries.<key>.allow_tool_override``."""


    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    @patch("hermes_cli.plugins_cmd._set_plugin_entry_flag")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    def test_interactive_eof_defaults_to_deny(
        self, mock_en, mock_dis, mock_save_en, mock_save_dis, mock_set_flag,
        mock_user, mock_bundled, nested_plugin_env,
    ):
        """Non-interactive stdin (EOFError) must fail closed to deny."""
        from hermes_cli.plugins_cmd import cmd_enable
        mock_user.return_value = nested_plugin_env
        mock_bundled.return_value = nested_plugin_env / "nonexistent"

        with patch("rich.console.Console.input", side_effect=EOFError):
            cmd_enable("disk-cleanup")

        mock_set_flag.assert_called_once_with(
            "disk-cleanup", "allow_tool_override", False
        )

    @patch("hermes_cli.plugins.get_bundled_plugins_dir")
    @patch("hermes_cli.plugins_cmd._plugins_dir")
    @patch("hermes_cli.plugins_cmd._set_plugin_entry_flag")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    def test_bundled_plugin_never_prompts_or_writes_entry(
        self, mock_en, mock_dis, mock_save_en, mock_save_dis, mock_set_flag,
        mock_user, mock_bundled, tmp_path,
    ):
        """Bundled plugins are trusted — no consent prompt, no entry write."""
        from hermes_cli.plugins_cmd import cmd_enable
        # Bundled dir holds the plugin; user dir is empty.
        _make_plugin_dir(tmp_path / "bundled", "trusted_bundled", {
            "name": "trusted_bundled", "version": "1.0.0",
        })
        mock_user.return_value = tmp_path / "empty"
        mock_bundled.return_value = tmp_path / "bundled"

        # Console.input would raise if called — proving no prompt fired.
        with patch("rich.console.Console.input", side_effect=AssertionError("prompted")):
            cmd_enable("trusted_bundled")

        mock_set_flag.assert_not_called()


class TestCompositeMenuWritesCanonicalKey:
    """#40190 follow-up: the interactive `hermes plugins` menu must persist
    the CANONICAL KEY (``web/firecrawl``), never the bare manifest name
    (``web-firecrawl``), so its disabled-list entries stay aligned with what
    ``cmd_enable`` clears and what PluginManager gates on. Writing the bare
    name is what silently vetoed a bundled backend forever (pi314).
    """

    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    def test_fallback_toggle_off_disables_by_key_not_name(
        self, mock_en, mock_save_en, mock_save_dis,
    ):
        from hermes_cli.plugins_cmd import _run_composite_fallback
        from rich.console import Console

        # key differs from the manifest name, mirroring web/firecrawl.
        plugin_keys = ["web/firecrawl"]
        plugin_labels = ["web-firecrawl — firecrawl [bundled]"]
        plugin_selected = {0}

        # Toggle the selected plugin off, then confirm. This proves an actual
        # user change persists the canonical key; merely opening/closing the
        # menu is covered separately as a no-op.
        with patch("builtins.input", side_effect=["1", ""]):
            _run_composite_fallback(
                plugin_keys, plugin_labels, plugin_selected,
                set(), [], Console(), ["web-firecrawl"],
            )

        saved_dis = mock_save_dis.call_args[0][0]
        assert "web/firecrawl" in saved_dis      # canonical key persisted
        assert "web-firecrawl" not in saved_dis   # never the bare name

    def test_cmd_toggle_preselects_effective_status_and_noop_preserves_config(
        self, tmp_path, monkeypatch
    ):
        from hermes_cli import plugins_cmd

        backend = _make_category_plugin(
            tmp_path,
            "image_gen",
            "openai-codex",
            {"name": "openai-codex", "kind": "backend"},
        )
        platform = _make_category_plugin(
            tmp_path,
            "platforms",
            "telegram",
            {"name": "telegram", "kind": "platform"},
        )
        standalone = _make_plugin_dir(
            tmp_path,
            "opt-in",
            {"name": "opt-in", "kind": "standalone"},
        )
        entries = [
            (
                "openai-codex",
                "1.0.0",
                "Codex image backend",
                "bundled",
                backend,
                "image_gen/openai-codex",
            ),
            (
                "telegram",
                "1.0.0",
                "Telegram platform",
                "bundled",
                platform,
                "platforms/telegram",
            ),
            ("opt-in", "1.0.0", "Opt-in", "bundled", standalone, "opt-in"),
            (
                "package-plugin",
                "1.0.0",
                "Entry point",
                "entrypoint",
                "package.module:register",
                "package-plugin",
            ),
        ]
        enabled = {"package-plugin"}
        disabled = {"platforms/telegram"}
        saved_enabled = []
        saved_disabled = []
        observed = {}

        monkeypatch.setattr(plugins_cmd, "_discover_all_plugins", lambda: entries)
        monkeypatch.setattr(plugins_cmd, "_get_enabled_set", lambda: set(enabled))
        monkeypatch.setattr(plugins_cmd, "_get_disabled_set", lambda: set(disabled))
        monkeypatch.setattr(plugins_cmd, "_get_current_memory_provider", lambda: "")
        monkeypatch.setattr(
            plugins_cmd, "_get_current_context_engine", lambda: "compressor"
        )
        monkeypatch.setattr(plugins_cmd, "_save_enabled_set", saved_enabled.append)
        monkeypatch.setattr(plugins_cmd, "_save_disabled_set", saved_disabled.append)
        monkeypatch.setattr(
            plugins_cmd.sys, "stdin", SimpleNamespace(isatty=lambda: True)
        )
        monkeypatch.setitem(sys.modules, "curses", SimpleNamespace())

        def run_fallback(
            _curses,
            keys,
            labels,
            selected,
            disabled_set,
            categories,
            console,
            names,
        ):
            observed["selected"] = set(selected)
            plugins_cmd._run_composite_fallback(
                keys,
                labels,
                selected,
                disabled_set,
                categories,
                console,
                names,
            )

        monkeypatch.setattr(plugins_cmd, "_run_composite_ui", run_fallback)

        # Confirm both sections without changing a checkbox or provider.
        with patch("builtins.input", side_effect=["", ""]):
            plugins_cmd.cmd_toggle()

        # Bundled backend is effectively on; explicit disable wins for the
        # bundled platform; standalone remains opt-in; enabled entry point is on.
        assert observed["selected"] == {0, 3}
        assert saved_enabled == []
        assert saved_disabled == []

        # Toggle the effective default-on backend off through cmd_toggle and
        # confirm the shared persistence path records only that explicit
        # change while retaining the existing entry-point enable and platform
        # disable.
        with patch("builtins.input", side_effect=["1", "", ""]):
            plugins_cmd.cmd_toggle()

        assert saved_enabled == [{"package-plugin"}]
        assert saved_disabled == [
            {"image_gen/openai-codex", "platforms/telegram"}
        ]
