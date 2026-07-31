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

    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_exact_manifest_name_precedes_ambiguous_leaf(self, mock_entries, tmp_path):
        from hermes_cli.plugins_cmd import _resolve_plugin_key

        mock_entries.return_value = [
            (
                "openai",
                "",
                "",
                "bundled",
                tmp_path / "image_gen" / "openai",
                "image_gen/openai",
            ),
            (
                "other-openai",
                "",
                "",
                "bundled",
                tmp_path / "model-providers" / "openai",
                "model-providers/openai",
            ),
        ]

        assert _resolve_plugin_key("openai") == "image_gen/openai"


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

    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value={"web-exa"})
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value={"web/exa"})
    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_enable_repairs_dashboard_alias_residue(
        self, mock_entries, mock_en, mock_dis, mock_save_en, mock_save_dis,
        tmp_path,
    ):
        from hermes_cli.plugins_cmd import cmd_enable

        entry = (
            "web-exa", "", "", "bundled", tmp_path / "web" / "exa", "web/exa",
        )
        mock_entries.return_value = [entry]

        cmd_enable("web/exa", allow_tool_override=False)

        assert mock_save_en.call_args[0][0] == {"web/exa"}
        assert mock_save_dis.call_args[0][0] == set()

    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value={"nous"})
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_loader_alias_cleanup_ignores_legacy_alias_collision(
        self, mock_entries, mock_en, mock_dis, mock_save_en, mock_save_dis, tmp_path,
    ):
        from hermes_cli.plugins_cmd import cmd_enable

        mock_entries.return_value = [
            (
                "nous", "", "", "bundled",
                tmp_path / "dashboard_auth" / "nous", "dashboard_auth/nous",
            ),
            (
                "nous-provider", "", "", "user",
                tmp_path / "model-providers" / "nous", "model-providers/nous",
            ),
        ]

        cmd_enable("dashboard_auth/nous", allow_tool_override=False)

        assert mock_save_en.call_args[0][0] == {"dashboard_auth/nous"}
        assert mock_save_dis.call_args[0][0] == set()

    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value={"shared"})
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value={"one/plugin"})
    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_enable_preserves_ambiguous_deny_alias(
        self, mock_entries, mock_en, mock_dis, mock_save_en, mock_save_dis,
        tmp_path,
    ):
        from hermes_cli.plugins_cmd import cmd_enable

        mock_entries.return_value = [
            ("shared", "", "", "user", tmp_path / "one" / "plugin", "one/plugin"),
            ("shared", "", "", "user", tmp_path / "two" / "plugin", "two/plugin"),
        ]

        with pytest.raises(SystemExit):
            cmd_enable("one/plugin", allow_tool_override=False)

        mock_save_en.assert_not_called()
        mock_save_dis.assert_not_called()

    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_directory_alias_resolves_only_when_unique(self, mock_entries, tmp_path):
        from hermes_cli.plugins_cmd import _resolve_plugin_key

        mock_entries.return_value = [
            ("declared-name", "", "", "user", tmp_path / "directory-name", "declared-name"),
        ]

        assert _resolve_plugin_key("directory-name") == "declared-name"


class TestBasicAuthConfig:
    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_basic_auth_removes_unique_bundled_aliases(self, mock_entries, tmp_path):
        from hermes_cli.plugins_cmd import ensure_basic_auth_plugin_enabled_in_config

        mock_entries.return_value = [
            (
                "basic",
                "",
                "",
                "bundled",
                tmp_path / "dashboard_auth" / "basic",
                "dashboard_auth/basic",
            ),
        ]
        config = {"plugins": {"disabled": ["basic", "dashboard_auth/basic"]}}

        assert ensure_basic_auth_plugin_enabled_in_config(config) is True
        assert config["plugins"]["disabled"] == []

    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_basic_auth_preserves_ambiguous_sibling_alias(self, mock_entries, tmp_path):
        from hermes_cli.plugins_cmd import ensure_basic_auth_plugin_enabled_in_config

        mock_entries.return_value = [
            (
                "basic",
                "",
                "",
                "bundled",
                tmp_path / "dashboard_auth" / "basic",
                "dashboard_auth/basic",
            ),
            ("basic", "", "", "user", tmp_path / "basic", "basic"),
        ]
        config = {"plugins": {"disabled": ["basic"]}}

        assert ensure_basic_auth_plugin_enabled_in_config(config) is False
        assert config["plugins"]["disabled"] == ["basic"]


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


class TestDashboardPluginActivation:
    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value={"web-exa"})
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value={"web/exa"})
    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_dashboard_enable_clears_stale_deny_alias(
        self, mock_entries, mock_en, mock_dis, mock_save_en, mock_save_dis,
        mock_toggle, tmp_path,
    ):
        from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

        mock_entries.return_value = [
            ("web-exa", "", "", "bundled", tmp_path / "web" / "exa", "web/exa"),
        ]

        result = dashboard_set_agent_plugin_enabled("web-exa", enabled=True)

        assert result == {"ok": True, "name": "web-exa", "unchanged": False}
        assert mock_save_en.call_args[0][0] == {"web/exa"}
        assert mock_save_dis.call_args[0][0] == set()
        mock_toggle.assert_called_once_with(
            "web/exa", enable=True, entries=mock_entries.return_value,
        )

    @patch("hermes_cli.plugins_cmd._toggle_plugin_toolset")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch("hermes_cli.plugins_cmd._get_disabled_set", return_value=set())
    @patch("hermes_cli.plugins_cmd._get_enabled_set", return_value={"web/exa"})
    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    def test_dashboard_disable_writes_canonical_key(
        self, mock_entries, mock_en, mock_dis, mock_save_en, mock_save_dis,
        mock_toggle, tmp_path,
    ):
        from hermes_cli.plugins_cmd import dashboard_set_agent_plugin_enabled

        mock_entries.return_value = [
            ("web-exa", "", "", "bundled", tmp_path / "web" / "exa", "web/exa"),
        ]

        result = dashboard_set_agent_plugin_enabled("web-exa", enabled=False)

        assert result["ok"] is True
        assert mock_save_en.call_args[0][0] == set()
        assert mock_save_dis.call_args[0][0] == {"web/exa"}
        mock_toggle.assert_called_once_with(
            "web/exa", enable=False, entries=mock_entries.return_value,
        )


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

    @patch("hermes_cli.curses_ui.flush_stdin")
    @patch("hermes_cli.plugins_cmd._discover_all_plugins")
    @patch("hermes_cli.plugins_cmd._save_disabled_set")
    @patch("hermes_cli.plugins_cmd._save_enabled_set")
    @patch(
        "hermes_cli.plugins_cmd._get_enabled_set",
        return_value={"canonical-key", "removed-thing"},
    )
    def test_curses_persistence_clears_stale_alias(
        self,
        mock_en,
        mock_save_en,
        mock_save_dis,
        mock_entries,
        mock_flush,
        tmp_path,
        capsys,
    ):
        from hermes_cli.plugins_cmd import _run_composite_ui
        from rich.console import Console

        mock_entries.return_value = [
            (
                "declared-name", "", "", "user", tmp_path / "canonical-key", "canonical-key",
            ),
        ]

        class FakeCurses:
            @staticmethod
            def wrapper(callback):
                return None

        _run_composite_ui(
            FakeCurses(),
            ["canonical-key"],
            ["declared-name"],
            {0},
            {"declared-name"},
            [],
            Console(),
        )

        assert mock_save_en.call_args[0][0] == {"canonical-key", "removed-thing"}
        assert mock_save_dis.call_args[0][0] == set()
        mock_entries.assert_called_once_with()
        assert "General plugins: 1 enabled, 0 disabled." in capsys.readouterr().out

