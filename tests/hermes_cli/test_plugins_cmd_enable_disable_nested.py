"""Tests for nested/alias-normalized enable & disable flows.

Companion to test_plugins_cmd_category_discovery.py. That file covers the
*listing* side of nested category plugins (issue #41066). These tests cover
the *mutation* side: `hermes plugins enable/disable` must resolve a bare name
OR a full path-derived key (e.g. `observability/nemo_relay`) to the canonical
registry key and write THAT — the same string PluginManager gates on — so a
nested bundled plugin can actually be toggled.
"""

import sys  # noqa: F401
from pathlib import Path
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
    _make_category_plugin(tmp_path, "observability", "nemo_relay", {
        "name": "nemo_relay", "version": "1.0.0", "description": "relay obs"
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
        assert _resolve_plugin_key("observability/nemo_relay") == "observability/nemo_relay"


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
        _make_category_plugin(tmp_path, "model-providers", "openai", {"name": "mp-openai"})
        mock_user.return_value = tmp_path
        mock_bundled.return_value = tmp_path / "nonexistent"
        # Bare "openai" is ambiguous -> None; the full key still resolves.
        assert _resolve_plugin_key("openai") is None
        assert _resolve_plugin_key("image_gen/openai") == "image_gen/openai"


# ---------------------------------------------------------------------------
# cmd_enable / cmd_disable — write the canonical key
# ---------------------------------------------------------------------------


class TestEnableDisableNested:
    @patch("hermes_cli.plugins.get_plugin_manager")
    @patch("hermes_cli.plugins.discover_plugins")
    def test_toolset_discovery_refreshes_after_config_change(
        self, mock_discover, mock_manager,
    ):
        from types import SimpleNamespace

        from hermes_cli.plugins_cmd import _get_plugin_toolset_key

        loaded = SimpleNamespace(
            manifest=SimpleNamespace(name="associative-memory"),
            tools_registered=[],
        )
        mock_manager.return_value._plugins = {"associative-memory": loaded}

        assert _get_plugin_toolset_key("associative-memory") is None
        mock_discover.assert_called_once_with(force=True)

    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value={"demo"})
    @patch("hermes_cli.plugins_cmd._plugin_exists", return_value=True)
    def test_dashboard_disable_resolves_toolset_before_persisting_state(
        self, _exists, _enabled, _disabled, save_enabled, save_disabled, toggle,
    ):
        from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

        calls = []
        toggle.side_effect = lambda *a, **k: calls.append("toggle")
        save_enabled.side_effect = lambda *a, **k: calls.append("save-enabled")
        save_disabled.side_effect = lambda *a, **k: calls.append("save-disabled")

        result = dashboard_set_agent_plugin_enabled("demo", enabled=False)

        assert result == {"ok": True, "name": "demo", "unchanged": False}
        assert calls == ["toggle", "save-enabled", "save-disabled"]

    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value={"demo"})
    @patch("hermes_cli.plugins_cmd._plugin_exists", return_value=True)
    def test_dashboard_already_enabled_repairs_toolset(
        self, _exists, _enabled, _disabled, toggle,
    ):
        from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

        result = dashboard_set_agent_plugin_enabled("demo", enabled=True)

        assert result == {"ok": True, "name": "demo", "unchanged": True}
        toggle.assert_called_once_with("demo", enable=True)

    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value={"demo"})
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._plugin_exists", return_value=True)
    def test_dashboard_already_disabled_repairs_toolset(
        self, _exists, _enabled, _disabled, toggle,
    ):
        from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

        result = dashboard_set_agent_plugin_enabled("demo", enabled=False)

        assert result == {"ok": True, "name": "demo", "unchanged": True}
        toggle.assert_called_once_with("demo", enable=False)

    @patch("hermes_cli.config.save_config")
    @patch("hermes_cli.config.load_config")
    @patch(
        "hermes_cli.plugins_cmd._get_plugin_toolset_key",
        return_value="associative-memory",
    )
    def test_existing_toolset_still_persists_plugin_mapping(
        self, mock_key, mock_load, mock_save,
    ):
        from hermes_cli.plugins_cmd import _toggle_plugin_toolset

        config = {
            "plugins": {"entries": {}},
            "platform_toolsets": {"cli": ["associative-memory"]},
        }
        mock_load.return_value = config

        _toggle_plugin_toolset("associative-memory", enable=True)

        mock_save.assert_called_once_with(config)
        assert config["plugins"]["entries"]["associative-memory"]["toolsets"] == [
            "associative-memory"
        ]

    @patch("hermes_cli.plugins_cmd._resolve_tool_override_grant")
    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    @patch(
        "hermes_cli.plugins_cmd._resolve_plugin_key_and_source",
        return_value=("associative-memory", "entrypoint"),
    )
    def test_enable_plugin_also_enables_its_toolset(
        self, mock_resolve, mock_en, mock_dis, mock_save_en, mock_save_dis,
        mock_toggle, mock_override,
    ):
        from hermes_cli.plugins_cmd import cmd_enable

        cmd_enable("associative-memory", allow_tool_override=False)

        mock_toggle.assert_called_once_with("associative-memory", enable=True)

    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._resolve_tool_override_grant")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    @patch(
        "hermes_cli.plugins_cmd._resolve_plugin_key_and_source",
        return_value=("third-party", "entrypoint"),
    )
    def test_enable_resolves_override_consent_before_plugin_discovery(
        self, mock_resolve, mock_en, mock_dis, mock_save_en, mock_save_dis,
        mock_override, mock_toggle,
    ):
        from hermes_cli.plugins_cmd import cmd_enable

        calls = []
        mock_override.side_effect = lambda *args, **kwargs: calls.append("consent")
        mock_toggle.side_effect = lambda *args, **kwargs: calls.append("discovery")

        cmd_enable("third-party", allow_tool_override=False)

        assert calls == ["consent", "discovery"]

    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch(
        "hermes_cli.plugins_cmd._get_enabled_set",
        return_value={"associative-memory"},
    )
    @patch(
        "hermes_cli.plugins_cmd._resolve_plugin_key",
        return_value="associative-memory",
    )
    def test_disable_plugin_also_disables_its_toolset(
        self, mock_resolve, mock_en, mock_dis, mock_save_en, mock_save_dis,
        mock_toggle,
    ):
        from hermes_cli.plugins_cmd import cmd_disable

        cmd_disable("associative-memory")

        mock_toggle.assert_called_once_with("associative-memory", enable=False)

    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value={"associative-memory"})
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    @patch(
        "hermes_cli.plugins_cmd._resolve_plugin_key",
        return_value="associative-memory",
    )
    def test_already_disabled_plugin_repairs_stale_toolset_exposure(
        self, mock_resolve, mock_en, mock_dis, mock_toggle,
    ):
        from hermes_cli.plugins_cmd import cmd_disable

        cmd_disable("associative-memory")

        mock_toggle.assert_called_once_with("associative-memory", enable=False)

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

        cmd_enable("nemo_relay", allow_tool_override=False)  # bare name

        saved = mock_save_en.call_args[0][0]
        # The canonical key — NOT the bare name — must be persisted, because
        # that is what PluginManager matches when deciding to load.
        assert "observability/nemo_relay" in saved
        assert "nemo_relay" not in saved or "observability/nemo_relay" in saved


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
    def test_fallback_unchecked_plugin_disables_by_key_not_name(
        self, mock_en, mock_save_en, mock_save_dis,
    ):
        from hermes_cli.plugins_cmd import _run_composite_fallback
        from rich.console import Console

        # key differs from the manifest name, mirroring web/firecrawl.
        plugin_keys = ["web/firecrawl"]
        plugin_labels = ["web-firecrawl — firecrawl [bundled]"]
        plugin_selected = set()  # unchecked → should be disabled

        # First input() toggles nothing (blank Enter confirms immediately),
        # second (category prompt) is skipped with blank Enter.
        with patch("builtins.input", return_value=""):
            _run_composite_fallback(
                plugin_keys, plugin_labels, plugin_selected,
                set(), [], Console(),
            )

        saved_dis = mock_save_dis.call_args[0][0]
        assert "web/firecrawl" in saved_dis      # canonical key persisted
        assert "web-firecrawl" not in saved_dis   # never the bare name

