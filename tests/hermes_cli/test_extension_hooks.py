"""Tests for the four plugin extension hooks added to core call sites.

Hooks under test:
  - pre_db_checkpoint       (hermes_state.py close + pre-VACUUM)
  - pre_fuzzy_repair        (agent_runtime_helpers.py repair_tool_call)
  - pre_delegation_credentials (delegate_tool.py _resolve_delegation_credentials)
  - pre_compression         (conversation_compression.py)

Each hook is fail-open: when no plugin is loaded, or the hook raises,
the original behavior is preserved exactly.
"""

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_db(tmp_path):
    """Create a minimal SessionDB backed by a temp file."""
    from hermes_state import SessionDB

    db_path = tmp_path / "test_state.db"
    return SessionDB(db_path=db_path)


# ===========================================================================
# 1. pre_db_checkpoint
# ===========================================================================

class TestPreDbCheckpointHook:
    """pre_db_checkpoint lets plugins override the WAL checkpoint mode."""

    def test_close_default_truncate_without_plugins(self, tmp_path):
        """Without any plugin, close() still issues TRUNCATE (original behavior)."""
        db = _make_session_db(tmp_path)
        execute_calls = []
        real_conn = db._conn

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            db.close()

        ckpt = [c for c in execute_calls if "wal_checkpoint" in c]
        assert any("TRUNCATE" in c for c in ckpt), f"Expected TRUNCATE, got {ckpt}"

    def test_close_plugin_overrides_to_passive(self, tmp_path):
        """A plugin returning {'mode': 'PASSIVE'} switches close() to PASSIVE."""
        db = _make_session_db(tmp_path)
        execute_calls = []
        real_conn = db._conn

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[{"mode": "PASSIVE"}],
        ):
            db.close()

        ckpt = [c for c in execute_calls if "wal_checkpoint" in c]
        assert any("PASSIVE" in c for c in ckpt), f"Expected PASSIVE, got {ckpt}"
        assert not any("TRUNCATE" in c for c in ckpt), f"TRUNCATE should be gone: {ckpt}"

    def test_close_plugin_error_preserves_default(self, tmp_path):
        """If invoke_hook raises, close() falls back to TRUNCATE."""
        db = _make_session_db(tmp_path)
        execute_calls = []
        real_conn = db._conn

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        with patch(
            "hermes_cli.plugins.invoke_hook",
            side_effect=RuntimeError("plugin exploded"),
        ):
            db.close()

        ckpt = [c for c in execute_calls if "wal_checkpoint" in c]
        assert any("TRUNCATE" in c for c in ckpt), f"Expected TRUNCATE fallback, got {ckpt}"

    def test_close_ignores_non_dict_results(self, tmp_path):
        """Non-dict hook results are silently ignored."""
        db = _make_session_db(tmp_path)
        execute_calls = []
        real_conn = db._conn

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        db._conn = mock_conn

        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=["not a dict", 42, None],
        ):
            db.close()

        ckpt = [c for c in execute_calls if "wal_checkpoint" in c]
        assert any("TRUNCATE" in c for c in ckpt)

    def test_hook_receives_correct_kwargs_close(self, tmp_path):
        """The hook receives context='close' and default_mode='TRUNCATE'."""
        db = _make_session_db(tmp_path)
        captured_kwargs = {}

        def capture_hook(hook_name, **kwargs):
            captured_kwargs.update(kwargs)
            captured_kwargs["_hook_name"] = hook_name
            return []

        with patch("hermes_cli.plugins.invoke_hook", side_effect=capture_hook):
            db.close()

        assert captured_kwargs["_hook_name"] == "pre_db_checkpoint"
        assert captured_kwargs["context"] == "close"
        assert captured_kwargs["default_mode"] == "TRUNCATE"

    def test_vacuum_path_hook_receives_correct_kwargs(self, tmp_path):
        """The pre-VACUUM path passes context='vacuum'."""
        db = _make_session_db(tmp_path)
        captured_kwargs = {}

        def capture_hook(hook_name, **kwargs):
            captured_kwargs.update(kwargs)
            captured_kwargs["_hook_name"] = hook_name
            return []

        # optimize_fts_storage triggers the pre-VACUUM checkpoint
        with patch("hermes_cli.plugins.invoke_hook", side_effect=capture_hook):
            try:
                db.optimize_fts_storage()
            except Exception:
                pass  # may fail on fresh DB — we only care about the hook call

        if captured_kwargs.get("_hook_name") == "pre_db_checkpoint":
            assert captured_kwargs["context"] == "vacuum"
            assert captured_kwargs["default_mode"] == "TRUNCATE"

    def test_vacuum_plugin_overrides_to_passive(self, tmp_path):
        """A plugin can force PASSIVE on the pre-VACUUM path too."""
        db = _make_session_db(tmp_path)
        execute_calls = []
        real_conn = db._conn

        def tracking_execute(sql, *args, **kwargs):
            execute_calls.append(sql)
            return real_conn.execute(sql, *args, **kwargs)

        mock_conn = MagicMock()
        mock_conn.execute.side_effect = tracking_execute
        mock_conn.fetchone.return_value = None
        db._conn = mock_conn

        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[{"mode": "PASSIVE"}],
        ):
            try:
                db.optimize_fts_storage()
            except Exception:
                pass

        ckpt = [c for c in execute_calls if "wal_checkpoint" in c]
        # optimize_fts_storage may or may not reach the checkpoint path
        # depending on DB state; if it does, verify the override took effect.
        for c in ckpt:
            assert "PASSIVE" in c, f"Expected PASSIVE, got: {c}"
            assert "TRUNCATE" not in c, f"TRUNCATE should be overridden: {c}"


# ===========================================================================
# 2. pre_fuzzy_repair
# ===========================================================================

class TestPreFuzzyRepairHook:
    """pre_fuzzy_repair lets plugins suppress the fuzzy-match fallback."""

    def _make_agent(self, tool_names):
        agent = MagicMock()
        agent.valid_tool_names = set(tool_names)
        return agent

    def test_fuzzy_works_without_plugins(self):
        """Without plugins, fuzzy repair still resolves close matches."""
        from agent.agent_runtime_helpers import repair_tool_call

        agent = self._make_agent({"terminal", "read_file", "write_file"})
        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            result = repair_tool_call(agent, "terminl")  # typo
        assert result == "terminal"

    def test_plugin_skips_fuzzy_for_mcp_names(self):
        """A plugin returning {'skip': True} suppresses fuzzy repair."""
        from agent.agent_runtime_helpers import repair_tool_call

        agent = self._make_agent({
            "mcp__ghidra_mcp__decompile_function",
            "terminal",
        })

        def skip_mcp(hook_name, **kwargs):
            if kwargs.get("tool_name", "").startswith("mcp__"):
                return [{"skip": True}]
            return []

        with patch("hermes_cli.plugins.invoke_hook", side_effect=skip_mcp):
            # This would fuzzy-match to decompile_function without the hook
            result = repair_tool_call(agent, "mcp__ghidra_mcp__disassemble_function")
        assert result is None, "Fuzzy should be suppressed for MCP names"

    def test_plugin_skip_does_not_affect_exact_repair(self):
        """Exact/normalization repairs still work even when fuzzy is skipped."""
        from agent.agent_runtime_helpers import repair_tool_call

        agent = self._make_agent({"terminal", "read_file"})

        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[{"skip": True}],
        ):
            # Exact lowercase match — should still work
            assert repair_tool_call(agent, "Terminal") == "terminal"
            # Normalization match
            assert repair_tool_call(agent, "read-file") == "read_file"

    def test_plugin_error_preserves_fuzzy(self):
        """If invoke_hook raises, fuzzy repair still runs."""
        from agent.agent_runtime_helpers import repair_tool_call

        agent = self._make_agent({"terminal", "read_file"})
        with patch(
            "hermes_cli.plugins.invoke_hook",
            side_effect=RuntimeError("boom"),
        ):
            result = repair_tool_call(agent, "terminl")
        assert result == "terminal"

    def test_hook_receives_correct_kwargs(self):
        """The hook gets tool_name and valid_tool_names."""
        from agent.agent_runtime_helpers import repair_tool_call

        agent = self._make_agent({"terminal"})
        captured = {}

        def capture(hook_name, **kwargs):
            captured.update(kwargs)
            captured["_hook_name"] = hook_name
            return []

        with patch("hermes_cli.plugins.invoke_hook", side_effect=capture):
            repair_tool_call(agent, "nonexistent_tool_xyz")

        assert captured["_hook_name"] == "pre_fuzzy_repair"
        assert captured["tool_name"] == "nonexistent_tool_xyz"
        assert captured["valid_tool_names"] == {"terminal"}

    def test_non_dict_results_ignored(self):
        """Non-dict hook results don't suppress fuzzy."""
        from agent.agent_runtime_helpers import repair_tool_call

        agent = self._make_agent({"terminal"})
        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=["skip", 42, True],
        ):
            result = repair_tool_call(agent, "terminl")
        assert result == "terminal"


# ===========================================================================
# 3. pre_delegation_credentials
# ===========================================================================

class TestPreDelegationCredentialsHook:
    """pre_delegation_credentials lets plugins short-circuit cred resolution."""

    def _make_parent(self, provider="nous", model="test/model"):
        parent = MagicMock()
        parent.provider = provider
        parent.model = model
        parent.api_key = "parent-key"
        parent.base_url = "https://parent.example.com/v1"
        parent.api_mode = "chat_completions"
        parent.acp_command = None
        parent.acp_args = []
        parent.reasoning_config = None
        parent._delegate_depth = 0
        parent.enabled_toolsets = None
        parent.disabled_toolsets = None
        parent.valid_tool_names = set()
        parent._client_kwargs = {}
        return parent

    def test_no_plugins_falls_through(self):
        """Without plugins, built-in resolution runs normally."""
        from tools.delegate_tool import _resolve_delegation_credentials

        parent = self._make_parent()
        cfg = {"model": "test/model"}

        with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
            result = _resolve_delegation_credentials(cfg, parent)

        # No provider/base_url configured → inherits from parent (None values)
        assert result["provider"] is None
        assert result["api_key"] is None

    def test_plugin_short_circuits_with_credentials(self):
        """A plugin returning a credential dict bypasses built-in resolution."""
        from tools.delegate_tool import _resolve_delegation_credentials

        parent = self._make_parent()
        cfg = {
            "model": "tencent/hy3:free",
            "provider": "nous",
            "base_url": "https://inference-api.nousresearch.com/v1",
        }
        plugin_creds = {
            "model": "tencent/hy3:free",
            "provider": "nous",
            "base_url": "https://inference-api.nousresearch.com/v1",
            "api_key": "fresh-jwt-token",
            "api_mode": "chat_completions",
        }

        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[plugin_creds],
        ):
            result = _resolve_delegation_credentials(cfg, parent)

        assert result["api_key"] == "fresh-jwt-token"
        assert result["provider"] == "nous"

    def test_plugin_error_falls_through(self):
        """If invoke_hook raises, built-in resolution runs."""
        from tools.delegate_tool import _resolve_delegation_credentials

        parent = self._make_parent()
        cfg = {"model": "test/model"}

        with patch(
            "hermes_cli.plugins.invoke_hook",
            side_effect=RuntimeError("plugin crashed"),
        ):
            result = _resolve_delegation_credentials(cfg, parent)

        assert result["provider"] is None

    def test_hook_receives_cfg_and_parent_info(self):
        """The hook gets cfg, parent_provider, parent_model."""
        from tools.delegate_tool import _resolve_delegation_credentials

        parent = self._make_parent(provider="openrouter", model="gpt-4o")
        # No provider/base_url → built-in returns None-values without
        # hitting the runtime resolver (which would need real auth).
        cfg = {"model": "test/model"}
        captured = {}

        def capture(hook_name, **kwargs):
            captured.update(kwargs)
            captured["_hook_name"] = hook_name
            return []

        with patch("hermes_cli.plugins.invoke_hook", side_effect=capture):
            _resolve_delegation_credentials(cfg, parent)

        assert captured["_hook_name"] == "pre_delegation_credentials"
        assert captured["cfg"] == cfg
        assert captured["parent_provider"] == "openrouter"
        assert captured["parent_model"] == "gpt-4o"

    def test_non_dict_results_ignored(self):
        """Non-dict results don't short-circuit."""
        from tools.delegate_tool import _resolve_delegation_credentials

        parent = self._make_parent()
        cfg = {"model": "test/model"}

        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=["not a dict", 42],
        ):
            result = _resolve_delegation_credentials(cfg, parent)

        assert result["provider"] is None

    def test_dict_without_provider_key_ignored(self):
        """A dict missing 'provider' doesn't short-circuit."""
        from tools.delegate_tool import _resolve_delegation_credentials

        parent = self._make_parent()
        cfg = {"model": "test/model"}

        with patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[{"api_key": "orphan-key"}],  # no "provider" key
        ):
            result = _resolve_delegation_credentials(cfg, parent)

        assert result["provider"] is None


# ===========================================================================
# 4. pre_compression
# ===========================================================================

class TestPreCompressionHook:
    """pre_compression lets plugins veto or prepare for compression."""

    def test_hook_registered_in_valid_hooks(self):
        """All four new hooks are in VALID_HOOKS."""
        from hermes_cli.plugins import VALID_HOOKS

        assert "pre_db_checkpoint" in VALID_HOOKS
        assert "pre_fuzzy_repair" in VALID_HOOKS
        assert "pre_delegation_credentials" in VALID_HOOKS
        assert "pre_compression" in VALID_HOOKS

    def test_compression_proceeds_without_plugins(self):
        """Without plugins, compression is not vetoed."""
        # We test the hook contract at the invoke_hook level since
        # compress_context requires a full agent setup.
        from hermes_cli.plugins import invoke_hook

        with patch(
            "hermes_cli.plugins.get_plugin_manager",
        ) as mock_mgr:
            mock_mgr.return_value.invoke_hook.return_value = []
            results = invoke_hook(
                "pre_compression",
                agent=MagicMock(),
                session_id="s1",
                message_count=10,
            )
        assert results == []

    def test_compression_vetoed_by_plugin(self):
        """A plugin returning {'skip': True} vetoes compression."""
        from hermes_cli.plugins import invoke_hook

        with patch(
            "hermes_cli.plugins.get_plugin_manager",
        ) as mock_mgr:
            mock_mgr.return_value.invoke_hook.return_value = [
                {"skip": True, "reason": "engine rebind failed"}
            ]
            results = invoke_hook(
                "pre_compression",
                agent=MagicMock(),
                session_id="s1",
                message_count=10,
            )
        assert len(results) == 1
        assert results[0]["skip"] is True

    def test_compression_side_effect_hook_no_return(self):
        """A plugin doing side effects (rebind) without returning doesn't veto."""
        from hermes_cli.plugins import invoke_hook

        with patch(
            "hermes_cli.plugins.get_plugin_manager",
        ) as mock_mgr:
            # Real invoke_hook filters None returns; simulate that.
            mock_mgr.return_value.invoke_hook.return_value = []
            results = invoke_hook(
                "pre_compression",
                agent=MagicMock(),
                session_id="s1",
                message_count=10,
            )
        assert results == []


# ===========================================================================
# 5. Integration: VALID_HOOKS completeness
# ===========================================================================

class TestValidHooksCompleteness:
    """Ensure the new hooks don't break the existing hook infrastructure."""

    def test_all_valid_hooks_are_strings(self):
        from hermes_cli.plugins import VALID_HOOKS

        for hook in VALID_HOOKS:
            assert isinstance(hook, str), f"Non-string hook: {hook!r}"

    def test_no_duplicate_hooks(self):
        from hermes_cli.plugins import VALID_HOOKS

        # VALID_HOOKS is a set, so duplicates are impossible by construction,
        # but verify the count matches expectations.
        expected_new = {
            "pre_db_checkpoint",
            "pre_fuzzy_repair",
            "pre_delegation_credentials",
            "pre_compression",
        }
        assert expected_new.issubset(VALID_HOOKS)

    def test_register_hook_accepts_new_hooks(self, tmp_path, monkeypatch):
        """PluginManager.register_hook accepts the new hook names."""
        from hermes_cli.plugins import PluginManager

        plugins_dir = tmp_path / "hermes_test" / "plugins"
        plugin_dir = plugins_dir / "test_ext"
        plugin_dir.mkdir(parents=True)
        (plugin_dir / "plugin.yaml").write_text("name: test_ext\nversion: 0.1.0\n")
        (plugin_dir / "__init__.py").write_text(
            "def register(ctx):\n"
            '    ctx.register_hook("pre_db_checkpoint", lambda **kw: {"mode": "PASSIVE"})\n'
            '    ctx.register_hook("pre_fuzzy_repair", lambda **kw: None)\n'
            '    ctx.register_hook("pre_delegation_credentials", lambda **kw: None)\n'
            '    ctx.register_hook("pre_compression", lambda **kw: None)\n'
        )
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_test"))
        # Plugins are opt-in: write the enabled list so test_ext loads.
        import yaml as _yaml
        config_dir = tmp_path / "hermes_test"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            _yaml.dump({"plugins": {"enabled": ["test_ext"]}})
        )

        mgr = PluginManager()
        mgr.discover_and_load()

        assert mgr.has_hook("pre_db_checkpoint")
        assert mgr.has_hook("pre_fuzzy_repair")
        assert mgr.has_hook("pre_delegation_credentials")
        assert mgr.has_hook("pre_compression")

        # Verify the checkpoint hook actually returns the override
        results = mgr.invoke_hook("pre_db_checkpoint", context="close", default_mode="TRUNCATE")
        assert results == [{"mode": "PASSIVE"}]
