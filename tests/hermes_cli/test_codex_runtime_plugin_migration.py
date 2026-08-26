"""Tests for the codex MCP plugin migration helper."""

from __future__ import annotations


import pytest

try:
    import tomllib
except ImportError:
    import tomli as tomllib  # type: ignore[no-redef]

from hermes_cli.codex_runtime_plugin_migration import (
    MIGRATION_MARKER,
    MIGRATION_END_MARKER,
    _build_hermes_tools_mcp_entry,
    _find_unmanaged_mcp_servers,
    _format_toml_value,
    _looks_like_table_header,
    _looks_like_test_tempdir,
    _parse_toml_table_header,
    _reconcile_unmanaged_tables,
    _strip_existing_managed_block,
    _strip_unmanaged_plugin_tables,
    _translate_one_server,
    migrate,
    migrate_codex_config,
    render_codex_toml_section,
)


# ---- per-server translation ----

class TestTranslateOneServer:
    def test_stdio_basic(self):
        cfg, skipped = _translate_one_server("filesystem", {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"FOO": "bar"},
        })
        assert cfg == {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            "env": {"FOO": "bar"},
        }
        assert skipped == []


    def test_enabled_true_omitted(self):
        cfg, _ = _translate_one_server("x", {"command": "y", "enabled": True})
        assert "enabled" not in cfg  # codex defaults to true


    def test_unknown_keys_warned(self):
        cfg, skipped = _translate_one_server("x", {
            "command": "y",
            "totally_made_up_key": "value",
        })
        assert "totally_made_up_key" not in cfg
        assert any("totally_made_up_key" in s for s in skipped)


# ---- TOML rendering ----

class TestTomlValueFormatter:







    def test_atomic_write_no_temp_leak_on_success(self, tmp_path):
        """The atomic-write path uses tempfile.mkstemp + rename. On
        success the temp file should not be left behind."""
        migrate({"mcp_servers": {"x": {"command": "y"}}},
                codex_home=tmp_path,
                discover_plugins=False,
                expose_hermes_tools=False,
                default_permission_profile=None)
        # config.toml should exist
        assert (tmp_path / "config.toml").exists()
        # And no .config.toml.* temp files left behind
        leftover = [p.name for p in tmp_path.iterdir()
                    if p.name.startswith(".config.toml.")]
        assert leftover == [], f"temp file leaked after migration: {leftover}"

    def test_atomic_write_cleanup_on_rename_failure(self, tmp_path, monkeypatch):
        """If rename fails partway through (out of disk, permissions,
        crash), the temp file must be cleaned up. Otherwise repeated
        failed migrations would pile up .config.toml.* files."""
        from pathlib import Path as _Path
        original_replace = _Path.replace

        def failing_replace(self, target):
            raise OSError("simulated disk full")

        monkeypatch.setattr(_Path, "replace", failing_replace)
        report = migrate(
            {"mcp_servers": {"x": {"command": "y"}}},
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=False,
            default_permission_profile=None,
        )
        # Error surfaced
        assert any("simulated disk full" in e for e in report.errors)
        # And no leaked temp file
        leftover = [p.name for p in tmp_path.iterdir()
                    if p.name.startswith(".config.toml.")]
        assert leftover == [], f"temp files leaked: {leftover}"



class TestRenderToml:

    def test_empty_servers_emits_placeholder(self):
        out = render_codex_toml_section({})
        assert "no MCP servers" in out

    def test_servers_sorted_alphabetically(self):
        out = render_codex_toml_section({
            "zoo": {"command": "z"},
            "alpha": {"command": "a"},
            "middle": {"command": "m"},
        })
        # Find the section header positions and confirm order
        a_pos = out.find("[mcp_servers.alpha]")
        m_pos = out.find("[mcp_servers.middle]")
        z_pos = out.find("[mcp_servers.zoo]")
        assert 0 < a_pos < m_pos < z_pos

    def test_server_with_args_and_env(self):
        out = render_codex_toml_section({
            "fs": {
                "command": "npx",
                "args": ["-y", "filesystem"],
                "env": {"PATH": "/usr/bin"},
            }
        })
        assert "[mcp_servers.fs]" in out
        assert 'command = "npx"' in out
        assert 'args = ["-y", "filesystem"]' in out
        # Env emitted as inline table
        assert 'env = { PATH = "/usr/bin" }' in out


# ---- existing-block stripping ----

class TestStripExistingManagedBlock:
    def test_no_managed_block_unchanged(self):
        text = "[other]\nfoo = 1\n"
        assert _strip_existing_managed_block(text) == text


    def test_preserves_user_content_above_managed_block(self):
        text = (
            "[model]\n"
            'name = "gpt-5.5"\n'
            "\n"
            f"{MIGRATION_MARKER}\n"
            "[mcp_servers.fs]\n"
            'command = "x"\n'
        )
        out = _strip_existing_managed_block(text)
        assert "[model]" in out
        assert 'name = "gpt-5.5"' in out
        assert "mcp_servers.fs" not in out


# ---- end-to-end migrate(, expose_hermes_tools=False) ----

class TestMigrate:



    def test_plugin_discovery_writes_plugin_blocks(self, tmp_path, monkeypatch):
        """Discovered curated plugins land as [plugins."<name>@<marketplace>"]
        blocks. This is what OpenClaw calls 'migrate native codex plugins.'"""
        from hermes_cli import codex_runtime_plugin_migration as crpm

        def fake_query(codex_home=None, timeout=8.0):
            return [
                {"name": "google-calendar", "marketplace": "openai-curated",
                 "enabled": True},
                {"name": "github", "marketplace": "openai-curated",
                 "enabled": True},
            ], None
        monkeypatch.setattr(crpm, "_query_codex_plugins", fake_query)

        report = migrate({}, codex_home=tmp_path, discover_plugins=True)
        text = (tmp_path / "config.toml").read_text()
        assert '[plugins."github@openai-curated"]' in text
        assert '[plugins."google-calendar@openai-curated"]' in text
        assert "enabled = true" in text
        assert "google-calendar@openai-curated" in report.migrated_plugins
        assert "github@openai-curated" in report.migrated_plugins


    def test_plugin_discovery_failure_non_fatal(self, tmp_path, monkeypatch):
        """If codex isn't installed or RPC fails, MCP migration still
        completes. The error surfaces in the report but doesn't abort."""
        from hermes_cli import codex_runtime_plugin_migration as crpm

        def fake_query_fails(codex_home=None, timeout=8.0):
            return [], "codex CLI not available"
        monkeypatch.setattr(crpm, "_query_codex_plugins", fake_query_fails)

        report = migrate({"mcp_servers": {"x": {"command": "y"}}},
                         codex_home=tmp_path, discover_plugins=True, expose_hermes_tools=False)
        assert report.written
        assert report.migrated == ["x"]
        assert report.plugin_query_error == "codex CLI not available"
        assert report.migrated_plugins == []







    def test_full_migration_round_trip(self, tmp_path):
        hermes_cfg = {
            "mcp_servers": {
                "filesystem": {
                    "command": "npx",
                    "args": ["-y", "@modelcontextprotocol/server-filesystem"],
                },
                "github": {
                    "url": "https://api.github.com/mcp",
                    "headers": {"Authorization": "Bearer x"},
                },
            }
        }
        report = migrate(hermes_cfg, codex_home=tmp_path, expose_hermes_tools=False)
        assert report.written
        text = (tmp_path / "config.toml").read_text()
        assert "[mcp_servers.filesystem]" in text
        assert "[mcp_servers.github]" in text
        assert 'command = "npx"' in text
        assert 'url = "https://api.github.com/mcp"' in text




    def test_preserves_user_mcp_server_outside_managed_block(self, tmp_path):
        """Quirk #6: when a user adds their own MCP server entry directly
        to ~/.codex/config.toml outside Hermes' managed block, re-running
        migration must preserve it. Tested both above and below the
        managed block."""
        target = tmp_path / "config.toml"
        target.write_text(
            "[mcp_servers.user-above]\n"
            'command = "/usr/bin/above-server"\n'
            'args = ["--above"]\n'
        )
        # First migrate — adds managed block below user content
        migrate({"mcp_servers": {"hermes-mcp": {"command": "npx"}}},
                codex_home=tmp_path, discover_plugins=False,
                expose_hermes_tools=False)
        text = target.read_text()
        assert "user-above" in text, "user MCP server above managed block got nuked"
        assert 'command = "/usr/bin/above-server"' in text

        # Append another user entry below the managed block
        target.write_text(
            text + "\n[mcp_servers.user-below]\ncommand = \"below-server\"\n"
        )
        # Re-migrate — both should survive
        migrate({"mcp_servers": {"hermes-mcp": {"command": "npx"}}},
                codex_home=tmp_path, discover_plugins=False,
                expose_hermes_tools=False)
        final = target.read_text()
        assert "user-above" in final
        assert "user-below" in final
        # And our managed block is still there with the new content
        assert "[mcp_servers.hermes-mcp]" in final



    def test_summary_reports_migration_count(self, tmp_path):
        report = migrate({
            "mcp_servers": {"a": {"command": "x"}, "b": {"command": "y"}}
        }, codex_home=tmp_path, expose_hermes_tools=False)
        summary = report.summary()
        assert "Migrated 2 MCP server(s)" in summary
        assert "- a" in summary
        assert "- b" in summary

    def test_multiline_array_at_top_level_not_split(self, tmp_path):
        """Top-level multi-line arrays like `trusted_pairs = [ ['a', 'b'] ]` must
        not be falsely identified as table headers by `_insert_managed_block_at_top_level`."""
        target = tmp_path / "config.toml"
        target.write_text("""\
model = "gpt-5.6-sol"
trusted_pairs = [
    ["user1", "read"],
    ["user2", "write"],
]

[features]
memories = true
""")
        report = migrate({"mcp_servers": {"mcp1": {"command": "cmd1"}}},
                         codex_home=tmp_path, discover_plugins=False,
                         expose_hermes_tools=False)
        assert report.written
        assert report.errors == []
        loaded = tomllib.loads(target.read_text())
        assert loaded["trusted_pairs"] == [["user1", "read"], ["user2", "write"]]
        assert loaded["features"]["memories"] is True
        assert loaded["mcp_servers"]["mcp1"]["command"] == "cmd1"

    def test_multiline_array_with_single_element_last_line_at_top_level(self, tmp_path):
        """Single-element last lines in multiline arrays like `['write']` must not
        be confused with TOML table headers."""
        target = tmp_path / "config.toml"
        target.write_text("""\
model = "gpt-5.6-sol"
args = [
    ["a"],
    ["write"]
]

[features]
memories = true
""")
        report = migrate({"mcp_servers": {"mcp1": {"command": "cmd1"}}},
                         codex_home=tmp_path, discover_plugins=False,
                         expose_hermes_tools=False)
        assert report.written
        assert report.errors == []
        loaded = tomllib.loads(target.read_text())
        assert loaded["args"] == [["a"], ["write"]]
        assert loaded["features"]["memories"] is True

    def test_migrate_preserves_bare_mcp_servers_with_inline_servers(self, tmp_path):
        """Bare [mcp_servers] with user inline configs must not be swallowed."""
        target = tmp_path / "config.toml"
        target.write_text("""\
[mcp_servers]
user-inline = { command = "/bin/user-tool" }
""")
        report = migrate(
            {"mcp_servers": {"hermes-mcp": {"command": "npx"}}},
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=False,
        )
        assert report.written
        assert report.errors == []
        loaded = tomllib.loads(target.read_text())
        assert loaded["mcp_servers"]["user-inline"]["command"] == "/bin/user-tool"
        assert loaded["mcp_servers"]["hermes-mcp"]["command"] == "npx"


# ---- Bug B: duplicate [plugins.X] tables ----


class TestStripUnmanagedPluginTables:
    """Regression tests for issue #26250 Bug B.

    When codex itself writes ``[plugins."<name>@<marketplace>"]`` tables
    (via the user running ``codex plugins enable`` directly), re-running
    ``hermes codex-runtime migrate`` would re-emit them inside the managed
    block and the resulting duplicate-table-header would crash codex.
    """

    def test_strips_plugin_tables_outside_managed_block(self):
        text = (
            'model = "gpt-5.5"\n'
            "\n"
            "[mcp_servers.user-thing]\n"
            'command = "x"\n'
            "\n"
            '[plugins."tasks@openai-curated"]\n'
            "enabled = true\n"
            "\n"
            '[plugins."web-search@openai-curated"]\n'
            "enabled = true\n"
            "\n"
            "[features]\n"
            "terminal_resize_reflow = true\n"
        )
        stripped = _strip_unmanaged_plugin_tables(text)
        assert "[plugins." not in stripped
        # Non-plugin content preserved
        assert "[mcp_servers.user-thing]" in stripped
        assert "[features]" in stripped
        assert "terminal_resize_reflow = true" in stripped


    def test_multi_line_array_in_plugin_table_does_not_leak(self):
        """A multi-line TOML array inside a [plugins.X] table whose
        continuation lines start with ``[`` (e.g. nested arrays) must NOT
        prematurely exit the strip region — otherwise array fragments
        leak into top-level output and produce invalid TOML on the next
        codex startup. Regression guard for #26260 review.
        """
        text = (
            '[plugins."tasks@openai-curated"]\n'
            "allowed = [\n"
            '  "a",\n'
            '  ["nested"],\n'
            "]\n"
            "[features]\n"
            "x = 1\n"
        )
        stripped = _strip_unmanaged_plugin_tables(text)
        # Everything inside the plugin table — including the multi-line
        # array's continuation lines starting with `[` — should be gone.
        assert '["nested"]' not in stripped
        assert "allowed" not in stripped
        # Sibling user table survives intact.
        assert "[features]" in stripped
        assert "x = 1" in stripped
        # Result is still valid TOML.
        import tomllib
        tomllib.loads(stripped)

    def test_migrate_dedups_codex_owned_plugin_tables(self, tmp_path, monkeypatch):
        """End-to-end: codex's pre-existing [plugins.X] tables get replaced by
        the managed block's re-emission rather than duplicated."""
        target = tmp_path / "config.toml"
        target.write_text(
            "[mcp_servers.user-server]\n"
            'command = "x"\n'
            "\n"
            '[plugins."tasks@openai-curated"]\n'
            "enabled = true\n"
        )

        # Simulate codex's plugin/list reporting the same plugin tasks@openai-curated.
        def fake_query(codex_home=None, timeout=8.0):
            return (
                [{"name": "tasks", "marketplace": "openai-curated", "enabled": True}],
                None,
            )

        monkeypatch.setattr(
            "hermes_cli.codex_runtime_plugin_migration._query_codex_plugins",
            fake_query,
        )
        migrate({}, codex_home=tmp_path, discover_plugins=True, expose_hermes_tools=False)
        new_text = target.read_text()
        # Only ONE [plugins."tasks@openai-curated"] header should remain — inside
        # the managed block — not the original outside-the-block copy.
        assert new_text.count('[plugins."tasks@openai-curated"]') == 1
        # And the surviving one is inside our managed section.
        managed_start = new_text.index(MIGRATION_MARKER)
        managed_end = new_text.index(MIGRATION_END_MARKER)
        plugin_idx = new_text.index('[plugins."tasks@openai-curated"]')
        assert managed_start < plugin_idx < managed_end
        # File parses cleanly as TOML (the original duplicate-key error is gone).
        import tomllib
        tomllib.loads(new_text)


# ---- Bug C: HERMES_HOME tempdir leak into ~/.codex/config.toml ----


class TestHermesHomeLeakGuard:
    """Regression tests for issue #26250 Bug C.

    Previously ``_build_hermes_tools_mcp_entry()`` read ``HERMES_HOME``
    directly from ``os.environ``, so a pytest ``monkeypatch.setenv`` would
    leak a transient tempdir path into the user's real ``~/.codex/config.toml``
    once codex spawned the hermes-tools MCP subprocess.
    """




    def test_real_hermes_home_propagates(self, monkeypatch, tmp_path):
        """A legitimate HERMES_HOME (not a tempdir path) DOES propagate so the
        MCP subprocess sees the same config as the parent CLI."""
        # Use a path that looks real — under /Users or /home, not /var/folders.
        # We can't easily create one in the test, so just use a stable path
        # outside any tempdir-detector needle. The detector checks for tempdir
        # markers, not for path existence.
        real_path = "/Users/alice/.hermes"
        monkeypatch.setenv("HERMES_HOME", real_path)
        entry = _build_hermes_tools_mcp_entry()
        env = entry.get("env", {})
        assert env.get("HERMES_HOME") == real_path

    def test_unset_hermes_home_omits_env_key(self, monkeypatch):
        """When HERMES_HOME is unset in the environment, the MCP entry MUST
        NOT bake in a resolved-default path. The codex subprocess should
        inherit whatever HERMES_HOME its launcher (systemd, gateway, shell)
        sets at runtime, rather than being pinned to migrate-time defaults.
        Regression guard for issue #26250 follow-up review."""
        monkeypatch.delenv("HERMES_HOME", raising=False)
        entry = _build_hermes_tools_mcp_entry()
        env = entry.get("env", {})
        assert "HERMES_HOME" not in env, (
            f"HERMES_HOME should not be set when env var is unset, got: "
            f"{env.get('HERMES_HOME')!r}"
        )


# ---- Robust Table Parser & Sub-table Reconciliation Tests ----


class TestParseTomlTableHeader:
    def test_bare_keys(self):
        assert _parse_toml_table_header("[features]") == (("features",), False)
        assert _parse_toml_table_header("[mcp_servers.filesystem]") == (("mcp_servers", "filesystem"), False)

    def test_quoted_keys(self):
        assert _parse_toml_table_header('[mcp_servers."second-brain"]') == (("mcp_servers", "second-brain"), False)
        assert _parse_toml_table_header("[mcp_servers.'second-brain']") == (("mcp_servers", "second-brain"), False)
        assert _parse_toml_table_header('[mcp_servers."foo.bar".env]') == (("mcp_servers", "foo.bar", "env"), False)
        assert _parse_toml_table_header(r'[mcp_servers."nested \"quote\""]') == (("mcp_servers", 'nested "quote"'), False)

    def test_array_of_tables(self):
        assert _parse_toml_table_header('[[plugins."linear@openai-curated"]]') == (("plugins", "linear@openai-curated"), True)

    def test_whitespace_and_comments(self):
        assert _parse_toml_table_header("  [  mcp_servers  .  'second-brain'  .  env  ]  # comment with ] bracket ") == (
            ("mcp_servers", "second-brain", "env"), False
        )

    def test_non_header_lines(self):
        assert _parse_toml_table_header("key = [1, 2, 3]") is None
        assert _parse_toml_table_header("  ['array_item'],  ") is None
        assert _parse_toml_table_header("# [commented_header]") is None
        assert _parse_toml_table_header("") is None

    def test_unicode_and_escape_sequences(self):
        assert _parse_toml_table_header(r'[mcp_servers."valid\u0020name"]') == (("mcp_servers", "valid name"), False)
        assert _parse_toml_table_header(r'[mcp_servers."valid\U00000020name"]') == (("mcp_servers", "valid name"), False)
        assert _parse_toml_table_header(r'[mcp_servers."invalid\uZZZZ"]') is None
        assert _parse_toml_table_header(r'[mcp_servers."invalid\x20"]') is None

    def test_trailing_dot_and_malformed(self):
        assert _parse_toml_table_header("[mcp_servers.]") is None
        assert _parse_toml_table_header("[mcp_servers..foo]") is None
        assert _parse_toml_table_header("[mcp_servers. .foo]") is None


class TestReconcileUnmanagedTables:
    def test_cascading_subtable_reconciliation(self):
        toml_text = """\
[projects]
active = true

[mcp_servers.second-brain]
command = "node"
args = ["/path/index.js"]

[mcp_servers.second-brain.env]
FOO = "1"

[mcp_servers.second-brain.settings]
mode = "auto"

[mcp_servers.node_repl]
command = "/path/node_repl"

[mcp_servers.node_repl.env]
BAR = "2"
"""
        reconciled = _reconcile_unmanaged_tables(toml_text, {"second-brain"})
        assert "second-brain" not in reconciled
        assert "[mcp_servers.second-brain.env]" not in reconciled
        assert "[mcp_servers.second-brain.settings]" not in reconciled
        # User-owned node_repl and its sub-table must be preserved intact
        assert "[mcp_servers.node_repl]" in reconciled
        assert "[mcp_servers.node_repl.env]" in reconciled
        assert 'BAR = "2"' in reconciled
        assert "[projects]" in reconciled

    def test_strips_bare_mcp_servers_header(self):
        toml_text = """\
[projects]
active = true

[mcp_servers]

[mcp_servers.second-brain]
command = "node"
"""
        reconciled = _reconcile_unmanaged_tables(toml_text, {"second-brain"})
        assert "[mcp_servers]" not in reconciled
        assert "second-brain" not in reconciled
        assert "[projects]" in reconciled

    def test_bare_mcp_servers_with_inline_servers_preserved(self):
        toml_text = """\
[projects]
active = true

[mcp_servers]
my-custom = { command = "node", args = ["app.js"] }

[mcp_servers.second-brain]
command = "node"
"""
        reconciled = _reconcile_unmanaged_tables(toml_text, {"second-brain"})
        assert "[mcp_servers]" in reconciled
        assert 'my-custom = { command = "node", args = ["app.js"] }' in reconciled
        assert "second-brain" not in reconciled
        assert "[projects]" in reconciled

    def test_multiline_array_inside_table_does_not_break_swallow(self):
        toml_text = """\
[mcp_servers.second-brain]
args = [
    ["a"],
    ["write"]
]
command = "node"

[features]
memories = true
"""
        reconciled = _reconcile_unmanaged_tables(toml_text, {"second-brain"})
        assert "second-brain" not in reconciled
        assert '["write"]' not in reconciled
        assert "[features]" in reconciled
        assert "memories = true" in reconciled


class TestMigrateConflictPolicies:
    def test_replace_with_managed_default(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("""\
model = "gpt-5.6-sol"

[mcp_servers.second-brain]
command = "old-node"
args = ["/old/path.js"]

[mcp_servers.second-brain.env]
OLD_KEY = "old_val"

[mcp_servers.user_custom]
command = "custom-bin"
""")
        hermes_cfg = {
            "mcp_servers": {
                "second-brain": {
                    "command": "new-node",
                    "args": ["/new/path.js"],
                }
            }
        }
        report = migrate(
            hermes_cfg,
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=False,
            default_permission_profile=None,
            conflict_policy="replace_with_managed",
        )
        assert report.written is True
        assert report.errors == []
        assert "second-brain" in report.reconciled_user_servers

        import tomllib
        loaded = tomllib.loads(config_path.read_text())
        assert loaded["mcp_servers"]["second-brain"]["command"] == "new-node"
        assert loaded["mcp_servers"]["user_custom"]["command"] == "custom-bin"
        assert "OLD_KEY" not in str(config_path.read_text())
        assert config_path.read_text().count("[mcp_servers.second-brain]") == 1

    def test_preserve_user_policy(self, tmp_path):
        config_path = tmp_path / "config.toml"
        config_path.write_text("""\
model = "gpt-5.6-sol"

[mcp_servers.second-brain]
command = "user-node"
args = ["/user/path.js"]

[mcp_servers.second-brain.env]
USER_ENV = "1"
""")
        hermes_cfg = {
            "mcp_servers": {
                "second-brain": {
                    "command": "hermes-node",
                    "args": ["/hermes/path.js"],
                },
                "new-server": {
                    "command": "new-bin",
                }
            }
        }
        report = migrate(
            hermes_cfg,
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=False,
            default_permission_profile=None,
            conflict_policy="preserve_user",
        )
        assert report.written is True
        assert report.errors == []
        assert "second-brain" in report.preserved_user_servers

        import tomllib
        loaded = tomllib.loads(config_path.read_text())
        # Preserved user-owned command
        assert loaded["mcp_servers"]["second-brain"]["command"] == "user-node"
        assert loaded["mcp_servers"]["new-server"]["command"] == "new-bin"
        assert config_path.read_text().count("[mcp_servers.second-brain]") == 1

    def test_invalid_conflict_policy_fails_fast(self, tmp_path):
        report = migrate(
            {"mcp_servers": {"x": {"command": "y"}}},
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=False,
            conflict_policy="invalid_policy_name",
        )
        assert report.written is False
        assert any("unrecognized conflict_policy" in e for e in report.errors)
        assert not (tmp_path / "config.toml").exists()


class TestTomlSyntaxValidationGuard:
    def test_tomllib_guard_aborts_write_on_invalid_toml(self, tmp_path, monkeypatch):
        from hermes_cli import codex_runtime_plugin_migration as crpm
        # Simulate a bug in rendering that would produce broken TOML
        monkeypatch.setattr(crpm, "render_codex_toml_section", lambda *a, **kw: "invalid toml = [ unclosed\n")
        report = crpm.migrate(
            {"mcp_servers": {"x": {"command": "y"}}},
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=False,
            default_permission_profile=None,
        )
        assert report.written is False
        assert any("syntax validation" in e for e in report.errors)
        assert not (tmp_path / "config.toml").exists()


class TestEndToEndRealWorldRepro:
    def test_duplicate_second_brain_reproduction_and_idempotency(self, tmp_path):
        """Replay the exact failure scenario where ~/.codex/config.toml had an
        existing [mcp_servers.second-brain] in unmanaged section and another in
        managed section. Migration must deduplicate it into a valid config and
        remain completely idempotent upon successive runs."""
        config_path = tmp_path / "config.toml"
        config_path.write_text("""\
model = "gpt-5.6-sol"
model_reasoning_effort = "ultra"
service_tier = "priority"
notify = ["/Applications/SkyComputerUseClient", "turn-ended"]
sandbox_mode = "workspace-write"
approval_policy = "never"
model_provider = "custom"

# managed by hermes-agent — `hermes codex-runtime migrate` regenerates this section

default_permissions = ":workspace"

[mcp_servers.hermes-tools]
command = "/venv/bin/python3"
args = ["-m", "agent.transports.hermes_tools_mcp_server"]
env = { HERMES_QUIET = "1", HERMES_REDACT_SECRETS = "true" }

[mcp_servers.second-brain]
command = "node"
args = ["/Users/mac/second-brain-mcp/index.js"]

# end hermes-agent managed section

[plugins]

[projects."/Users/mac/personal-dev"]
trust_level = "trusted"

[features]
memories = true

[mcp_servers]

[mcp_servers.second-brain]
command = "node"
args = ["/Users/mac/second-brain-mcp/index.js"]

[mcp_servers.node_repl]
command = "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"
startup_timeout_sec = 120

[mcp_servers.node_repl.env]
NODE_REPL_TIMEOUT = "1000"
""")

        hermes_cfg = {
            "mcp_servers": {
                "second-brain": {
                    "command": "node",
                    "args": ["/Users/mac/second-brain-mcp/index.js"],
                }
            }
        }

        # Run 1: Migrate
        report1 = migrate_codex_config(
            hermes_cfg,
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=True,
            default_permission_profile=":workspace",
        )
        assert report1.written is True
        assert report1.errors == []
        assert "second-brain" in report1.reconciled_user_servers

        import tomllib
        content1 = config_path.read_text()
        loaded1 = tomllib.loads(content1)
        assert content1.count("[mcp_servers.second-brain]") == 1
        assert content1.count("[mcp_servers.node_repl]") == 1
        assert content1.count("[mcp_servers.node_repl.env]") == 1
        assert loaded1["mcp_servers"]["second-brain"]["command"] == "node"
        assert loaded1["mcp_servers"]["node_repl"]["command"] == "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node_repl"
        assert loaded1["features"]["memories"] is True
        assert loaded1["projects"]["/Users/mac/personal-dev"]["trust_level"] == "trusted"

        # Run 2: Idempotent re-run
        report2 = migrate_codex_config(
            hermes_cfg,
            codex_home=tmp_path,
            discover_plugins=False,
            expose_hermes_tools=True,
            default_permission_profile=":workspace",
        )
        assert report2.written is True
        assert report2.errors == []
        content2 = config_path.read_text()
        assert content2 == content1

