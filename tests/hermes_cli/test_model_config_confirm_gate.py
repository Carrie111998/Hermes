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
    _emit_model_change_notice,
    _emit_model_routing_refusal,
    _is_model_routing_key,
    config_command,
    set_config_value,
)
from tools.environments.base import (
    AGENT_INITIATED_ENV,
    agent_initiated_command,
    stamp_agent_initiated,
)
from tools.environments.local import build_subprocess_env


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
            "model.api_key",
            "delegation",
            "delegation.model",
            "delegation.provider",
            "delegation.base_url",
            "auxiliary",
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
# The gate NEVER prompts (PTY-hang) — an agent-marked process must not wait
# on stdin, even when a Hermes terminal-tool PTY run gives it a real TTY.
# ---------------------------------------------------------------------------


class TestNeverPrompts:
    def test_pty_run_refuses_without_prompt(
        self, _isolated_hermes_home, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        # Simulate a PTY: stdin is a real TTY.
        monkeypatch.setattr("hermes_cli.config._is_interactive_tty", lambda: True)
        # If the gate ever called input(), it would hang until the 600s timeout
        # — fail loudly instead so the regression is caught instantly.
        def _no_input(*a, **k):
            raise AssertionError("gate must never call input() for an agent write")

        monkeypatch.setattr("builtins.input", _no_input)
        with pytest.raises(SystemExit) as exc:
            set_config_value("delegation.model", "gpt-4o")
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert "delegation.model" in err
        # Refused: config unchanged (no silent rewrite).
        assert "delegation" not in _read_config(_isolated_hermes_home)

    def test_agent_write_refused_even_when_tty_answer_is_yes(
        self, _isolated_hermes_home, monkeypatch
    ):
        # A write under a PTY must NOT prompt-and-allow just because the human
        # would have typed 'y' — prompting is for humans only, and a human write
        # never carries the agent marker.
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        monkeypatch.setattr("hermes_cli.config._is_interactive_tty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        with pytest.raises(SystemExit) as exc:
            set_config_value("delegation.model", "gpt-4o")
        assert exc.value.code != 0
        assert "delegation" not in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# Secret exposure: model.* includes model.api_key (a credential in config.yaml),
# so the gate notice/refusal must redact credential-shaped values.
# ---------------------------------------------------------------------------


class TestSecretRedaction:
    def test_notice_redacts_secret_value(self, capsys):
        secret = "sk-live-abcdefgh123456789"
        _emit_model_change_notice("model.api_key", None, secret)
        out = capsys.readouterr().out
        assert secret not in out
        assert "model.api_key" in out

    def test_refusal_redacts_secret_value(self, capsys):
        secret = "sk-live-abcdefgh123456789"
        _emit_model_routing_refusal("model.api_key", None, secret)
        err = capsys.readouterr().err
        assert secret not in err
        assert "model.api_key" in err

    def test_agent_write_to_model_api_key_does_not_leak_secret(
        self, _isolated_hermes_home, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        secret = "sk-live-abcdefgh123456789"
        with pytest.raises(SystemExit) as exc:
            set_config_value("model.api_key", secret)
        assert exc.value.code != 0
        err = capsys.readouterr().err
        assert secret not in err

    def test_notice_does_not_mask_non_secret_value(self, capsys):
        # Ordinary routing values (model/provider) are shown in full.
        _emit_model_change_notice("delegation.model", "gpt-4o", "gpt-5")
        out = capsys.readouterr().out
        assert "gpt-4o" in out
        assert "gpt-5" in out


# ---------------------------------------------------------------------------
# Parent-section writes: a --force write to the whole `delegation`/`auxiliary`
# section replaces the routing block, so the bare section keys are gate-worthy.
# ---------------------------------------------------------------------------


class TestParentSectionWriteGated:
    def test_agent_force_write_to_delegation_section_refused(
        self, _isolated_hermes_home, monkeypatch, capsys
    ):
        set_config_value("delegation.model", "gpt-4o")
        before = _read_config(_isolated_hermes_home)

        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        with pytest.raises(SystemExit) as exc:
            set_config_value("delegation", "gpt-4o", force=True)
        assert exc.value.code != 0
        assert _read_config(_isolated_hermes_home) == before

    def test_agent_force_write_to_auxiliary_section_refused(
        self, _isolated_hermes_home, monkeypatch
    ):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        with pytest.raises(SystemExit) as exc:
            set_config_value("auxiliary", "gpt-4o", force=True)
        assert exc.value.code != 0
        assert "auxiliary" not in _read_config(_isolated_hermes_home)


# ---------------------------------------------------------------------------
# The refusal message must not hand the agent a copy-paste bypass command.
# ---------------------------------------------------------------------------


class TestRefusalMessage:
    def test_refusal_tells_agent_to_surface_but_no_command(self, _isolated_hermes_home, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_AGENT_INITIATED", "1")
        with pytest.raises(SystemExit) as exc:
            set_config_value("delegation.model", "gpt-4o")
        assert exc.value.code != 0
        err = capsys.readouterr().err
        # Keeps the "surface to the user" direction and the flag name.
        assert "surface" in err.lower()
        assert "user" in err.lower()
        assert "confirm-model-change" in err.lower()
        # But drops the copy-paste command that taught the agent to bypass.
        assert "hermes config set" not in err


# ---------------------------------------------------------------------------
# Agent-initiated marker coverage: every Hermes-spawned child env / command
# path (local, scratch, docker, ssh, modal, daytona, vercel) must carry it so a
# nested `hermes config set` is gated — not only the local builder.
# ---------------------------------------------------------------------------


class TestAgentMarkerCoverage:
    def test_build_subprocess_env_non_scrub_stamps_marker(self):
        env = build_subprocess_env(
            scrub_secrets=False, inherit_profile_home=False, base={"FOO": "bar"}
        )
        assert env.get("HERMES_AGENT_INITIATED") == "1"

    def test_build_subprocess_env_scrub_stamps_marker(self):
        env = build_subprocess_env(scrub_secrets=True, base={"FOO": "bar"})
        assert env.get("HERMES_AGENT_INITIATED") == "1"

    def test_stamp_agent_initiated_helper(self):
        env = {}
        stamp_agent_initiated(env)
        assert env[AGENT_INITIATED_ENV] == "1"

    def test_agent_initiated_command_prefix(self):
        # The bash prefix is how remote/container backends (ssh, modal, daytona,
        # vercel_sandbox) mark a child they can't pass an env dict to.
        prefixed = agent_initiated_command("echo hi")
        assert prefixed.startswith(f"export {AGENT_INITIATED_ENV}=1; ")
        assert "echo hi" in prefixed

    def test_docker_env_builder_includes_marker(self):
        # Unit-level: build the docker exec env args without a docker daemon.
        import tools.environments.docker as docker_mod

        d = docker_mod.DockerEnvironment.__new__(docker_mod.DockerEnvironment)
        d._env = docker_mod._normalize_env_dict({"FOO": "bar"})
        d._forward_env = docker_mod._normalize_forward_env_names([])
        d._init_unset_passthrough_names = ()
        # Replicate the single line DockerEnvironment.__init__ adds (#97652).
        stamp_agent_initiated(d._env)
        args = d._build_init_env_args()
        assert "HERMES_AGENT_INITIATED=1" in args

