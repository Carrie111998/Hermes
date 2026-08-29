"""Behavior contracts for the ``approvals.model_config_confirm`` gate (#97652).

Agents can silently rewrite model-routing config (``model.*``,
``delegation.model/provider/base_url``, ``auxiliary.*.model/provider/base_url``)
via ``hermes config set`` — including when ``approvals.mode: off``.  This gate
forces *agent-initiated* (terminal-tool-spawned) writes to those keys through a
confirm step: a y/N prompt in an interactive TTY, or a hard refusal (exit
non-zero, no hang) in the non-interactive context the agent runs in.

Human CLI / programmatic writes carry no ``HERMES_AGENT_INITIATED`` marker and
pass through untouched.  These are behavior contracts, not snapshots.
"""

from __future__ import annotations

import argparse
import os

import pytest

from hermes_cli.config import (
    DEFAULT_CONFIG,
    _is_model_routing_key,
    config_command,
    set_config_value,
)


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir; default to the non-agent case."""
    (tmp_path / ".env").touch()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Tests default to a HUMAN/programmatic write (no agent marker) unless a
    # test opts in by setting HERMES_AGENT_INITIATED.
    monkeypatch.delenv("HERMES_AGENT_INITIATED", raising=False)
    return tmp_path


def _read_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    return cfg_path.read_text() if cfg_path.exists() else ""


def _load(tmp_path):
    import yaml

    return yaml.safe_load(_read_config(tmp_path)) or {}


# ---------------------------------------------------------------------------
# Default config key
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    def test_default_config_has_the_key(self):
        approvals = DEFAULT_CONFIG.get("approvals")
        assert isinstance(approvals, dict)
        assert "model_config_confirm" in approvals

    def test_default_is_true(self):
        # New installs confirm agent-initiated model-routing rewrites — a
        # silent billing decision must not be the enabled-by-default behavior.
        assert DEFAULT_CONFIG["approvals"]["model_config_confirm"] is True


# ---------------------------------------------------------------------------
# Key classification (behavior contract, not a frozen list)
# ---------------------------------------------------------------------------


class TestModelRoutingKeyClassification:
    @pytest.mark.parametrize(
        "key",
        [
            "model",
            "model.default",
            "model.provider",
            "model.base_url",
            "model.context_length",
            "delegation.model",
            "delegation.provider",
            "delegation.base_url",
            "auxiliary.summarize.model",
            "auxiliary.summarize.provider",
            "auxiliary.summarize.base_url",
        ],
    )
    def test_model_routing_keys_are_recognized(self, key):
        assert _is_model_routing_key(key) is True

    @pytest.mark.parametrize(
        "key",
        [
            "terminal.backend",
            "skills.enabled",
            "cron.interval",
            # auxiliary.<name>.<field> is a routing key only for the three
            # routing fields; tuning params are not billing-relevant.
            "auxiliary.summarize.max_tokens",
            "auxiliary.summarize.enabled",
            "custom.enabled",
        ],
    )
    def test_non_model_routing_keys_are_not_recognized(self, key):
        assert _is_model_routing_key(key) is False


# ---------------------------------------------------------------------------
# Human / programmatic writes are unaffected
# ---------------------------------------------------------------------------


class TestHumanWriteUnaffected:
    def test_human_write_to_model_key_allowed(self, _isolated_hermes_home):
        set_config_value("model.default", "anthropic/claude-sonnet-4")
        assert _load(_isolated_hermes_home)["model"]["default"] == "anthropic/claude-sonnet-4"

    def test_human_write_to_delegation_model_allowed(self, _isolated_hermes_home):
        set_config_value("delegation.model", "gpt-4o")
        assert _load(_isolated_hermes_home)["delegation"]["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# Agent-initiated non-interactive writes are REFUSED (no hang, exit non-zero)
# ---------------------------------------------------------------------------


class TestAgentWriteRefused:
    def test_agent_write_to_model_default_refused(self, _isolated_hermes_home, monkeypatch, capsys):
        # Seed a valid value first (human write, no marker).
        set_config_value("model.default", "upstage/solar-pro4")
        before = _read_config(_isolated_hermes_home)

        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        with pytest.raises(SystemExit) as exc:
            set_config_value("model.default", "deepseek/deepseek-v4-flash")
        assert exc.value.code != 0

        err = capsys.readouterr().err
        assert "model.default" in err
        # The refusal must instruct the agent to surface the change to the user.
        assert "surface" in err.lower()

        # Config must be unchanged — the silent rewrite became a refusal.
        assert _read_config(_isolated_hermes_home) == before

    def test_agent_write_to_non_model_key_allowed(self, _isolated_hermes_home, monkeypatch):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        set_config_value("terminal.backend", "docker")
        assert _load(_isolated_hermes_home)["terminal"]["backend"] == "docker"

    def test_config_command_agent_write_refuses(self, _isolated_hermes_home, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        args = argparse.Namespace(
            config_command="set", key="delegation.model", value="gpt-4o", force=False
        )
        with pytest.raises(SystemExit) as exc:
            config_command(args)
        assert exc.value.code != 0


# ---------------------------------------------------------------------------
# User-directed writes bypass the gate (inline escape hatch)
# ---------------------------------------------------------------------------


class TestUserDirectedBypass:
    def test_confirm_model_change_bypasses_gate(self, _isolated_hermes_home, monkeypatch):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        set_config_value("delegation.model", "gpt-4o", confirm_model_change=True)
        assert _load(_isolated_hermes_home)["delegation"]["model"] == "gpt-4o"

    def test_bypass_still_emits_visible_notice(self, _isolated_hermes_home, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        set_config_value("delegation.model", "gpt-4o", confirm_model_change=True)
        out = capsys.readouterr().out
        assert "delegation.model" in out
        assert "changed" in out.lower()

    def test_config_command_honors_confirm_model_change(
        self, _isolated_hermes_home, monkeypatch
    ):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        args = argparse.Namespace(
            config_command="set",
            key="delegation.model",
            value="gpt-4o",
            force=False,
            confirm_model_change=True,
        )
        config_command(args)
        assert _load(_isolated_hermes_home)["delegation"]["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# Approvals.model_config_confirm=false silences the gate (but keeps the notice)
# ---------------------------------------------------------------------------


class TestGateDisabled:
    def test_gate_disabled_allows_agent_write(self, _isolated_hermes_home, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        # Not a routing key, so this write is allowed and persists the opt-out.
        set_config_value("approvals.model_config_confirm", "false")
        set_config_value("delegation.model", "gpt-4o")
        cfg = _load(_isolated_hermes_home)
        assert cfg["approvals"]["model_config_confirm"] is False
        assert cfg["delegation"]["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# Interactive TTY prompting (destructive-slash style y/N)
# ---------------------------------------------------------------------------


class TestInteractivePrompt:
    def test_interactive_approve_allows_and_notices(
        self, _isolated_hermes_home, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        monkeypatch.setattr("hermes_cli.config._is_interactive_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        set_config_value("delegation.model", "gpt-4o")
        assert _load(_isolated_hermes_home)["delegation"]["model"] == "gpt-4o"
        out = capsys.readouterr().out
        assert "delegation.model" in out
        assert "changed" in out.lower()

    def test_interactive_decline_refuses(self, _isolated_hermes_home, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        monkeypatch.setattr("hermes_cli.config._is_interactive_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        with pytest.raises(SystemExit) as exc:
            set_config_value("delegation.model", "gpt-4o")
        assert exc.value.code != 0
        assert "delegation" not in _read_config(_isolated_hermes_home)
