"""Tests for the agent.claude_agent_sdk config block.

The claude-agent-sdk provider reads its behavioural flags exclusively from
config.yaml, so the canonical defaults must be registered in DEFAULT_CONFIG.
The example file alone does not make them real config options for
default-driven config tooling.

Adapted from upstream PR #65982 commit 10659717722effd9bb7738423a2399770f50a428.
"""

from __future__ import annotations

from pathlib import Path

from hermes_cli.config import DEFAULT_CONFIG


class TestClaudeAgentSdkDefaults:
    def test_default_config_has_the_block(self):
        agent = DEFAULT_CONFIG.get("agent")
        assert isinstance(agent, dict)
        assert "claude_agent_sdk" in agent

    def test_canonical_defaults(self):
        # Subscription-only is invariant runtime behavior, not a configurable
        # escape hatch. Additional directory access is opt-in and empty by
        # default.
        assert DEFAULT_CONFIG["agent"]["claude_agent_sdk"] == {
            "streaming": False,
            "append_file": "",
            "add_dirs": [],
            "native_read_only": False,
        }

    def test_example_and_default_source_document_the_native_security_boundary(self):
        root = Path(__file__).resolve().parents[2]
        example_text = (root / "cli-config.yaml.example").read_text()
        example_block = example_text.split("  # claude_agent_sdk:", 1)[1].split(
            "  # Inactivity timeout", 1
        )[0]
        default_text = (root / "hermes_cli" / "config.py").read_text()
        default_block = default_text.split('"claude_agent_sdk": {', 1)[1].split(
            "        },", 1
        )[0]

        def normalize_comments(value):
            return " ".join(value.replace("#", " ").split())

        for documented_block in (
            normalize_comments(example_block),
            normalize_comments(default_block),
        ):
            assert "add_dirs" in documented_block
            assert "not a read-only mount" in documented_block
            assert "default auto/acceptEdits" in documented_block
            assert "native edits and common filesystem mutations" in documented_block
            assert (
                "Removing Hermes MCP file/terminal tools does not remove Claude native tools"
                in documented_block
            )
            assert (
                "read-oriented headless profiles must use native_read_only: true"
                in documented_block
            )
            assert "HERMES_TERMINAL_SECURITY_MODE=approval-required" in documented_block
            assert "user/project/local Claude settings" in documented_block
            assert "only Read, Glob, and Grep natively" in documented_block
            assert (
                "unavailable rather than interactively approvable" in documented_block
            )
        assert "native_read_only: false" in example_block


class TestUserConfigMerge:
    """Existing config gets defaults while explicit user values survive."""

    def _load(self, tmp_path, monkeypatch, user_cfg):
        import importlib

        import yaml

        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(yaml.safe_dump(user_cfg))

        monkeypatch.setenv("HERMES_HOME", str(home))
        import hermes_cli.config as cfg_mod

        importlib.reload(cfg_mod)
        return cfg_mod.load_config()

    def test_config_without_block_gets_defaults(self, tmp_path, monkeypatch):
        cfg = self._load(tmp_path, monkeypatch, {"agent": {"max_turns": 5}})
        assert cfg["agent"]["claude_agent_sdk"] == {
            "streaming": False,
            "append_file": "",
            "add_dirs": [],
            "native_read_only": False,
        }
        assert cfg["agent"]["max_turns"] == 5

    def test_explicit_user_values_survive_merge(self, tmp_path, monkeypatch):
        cfg = self._load(
            tmp_path,
            monkeypatch,
            {
                "agent": {
                    "claude_agent_sdk": {
                        "streaming": True,
                        "native_read_only": True,
                    }
                }
            },
        )
        assert cfg["agent"]["claude_agent_sdk"]["streaming"] is True
        assert "allow_metered_key" not in cfg["agent"]["claude_agent_sdk"]
        assert cfg["agent"]["claude_agent_sdk"]["append_file"] == ""
        assert cfg["agent"]["claude_agent_sdk"]["add_dirs"] == []
        assert cfg["agent"]["claude_agent_sdk"]["native_read_only"] is True
