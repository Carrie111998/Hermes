"""Tests for profile-scoped terminal env resolution (``_runtime_terminal_env``).

A multiplex gateway serves many profiles from one process, but terminal
settings are read from ``os.environ`` (TERMINAL_*). ``_runtime_terminal_env``
overlays the ACTIVE profile's ``terminal.*`` config onto a private copy of the
process env during a scoped turn, so every profile resolves its own backend
without mutating global state; single-profile processes keep the historical
process-environment path.
"""

import os

import pytest

import tools.terminal_tool as terminal_tool


@pytest.fixture()
def _profile_home(tmp_path):
    """A profile home whose config selects a non-default terminal backend."""
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    (profile_home / "config.yaml").write_text(
        "terminal:\n  backend: ssh\n  timeout: 42\n",
        encoding="utf-8",
    )
    from hermes_cli import config as hermes_config

    hermes_config._LOAD_CONFIG_CACHE.clear()
    hermes_config._RAW_CONFIG_CACHE.clear()
    return profile_home


class TestRuntimeTerminalEnv:
    def test_scoped_turn_overlays_profile_config(self, monkeypatch, _profile_home):
        """During a multiplexed profile turn the profile's terminal config wins
        over the process-global env — without mutating ``os.environ``."""
        from agent.secret_scope import (
            is_multiplex_active,
            reset_secret_scope,
            set_multiplex_active,
            set_secret_scope,
        )
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        monkeypatch.setenv("TERMINAL_ENV", "local")
        # Exported value for a setting the profile does NOT configure — the
        # overlay must preserve it.
        monkeypatch.setenv("TERMINAL_SSH_HOST", "exported.example")

        previous = is_multiplex_active()
        set_multiplex_active(True)
        home_token = set_hermes_home_override(str(_profile_home))
        secret_token = set_secret_scope({})
        try:
            runtime_env = terminal_tool._runtime_terminal_env()
            config = terminal_tool._get_env_config()
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
            set_multiplex_active(previous)

        assert runtime_env["TERMINAL_ENV"] == "ssh"
        assert runtime_env["TERMINAL_SSH_HOST"] == "exported.example"
        assert config["env_type"] == "ssh"
        assert config["timeout"] == 42
        assert config["ssh_host"] == "exported.example"
        # The overlay is private: the process env still selects local.
        assert os.environ["TERMINAL_ENV"] == "local"

    def test_scoped_turn_sees_profile_dotenv_terminal_settings(
        self, monkeypatch, _profile_home
    ):
        """A TERMINAL_* setting living only in the profile's ``.env`` (carried
        by the bound secret scope, never in the process env) must reach the
        overlay. Three-layer precedence: os.environ < profile ``.env`` <
        config.yaml ``terminal.*``."""
        from agent.secret_scope import (
            build_profile_secret_scope,
            is_multiplex_active,
            reset_secret_scope,
            set_multiplex_active,
            set_secret_scope,
        )
        from hermes_constants import (
            reset_hermes_home_override,
            set_hermes_home_override,
        )

        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.delenv("TERMINAL_SSH_HOST", raising=False)
        # Same key in the process env AND the profile .env — the .env wins.
        monkeypatch.setenv("TERMINAL_SSH_USER", "process-user")
        (_profile_home / ".env").write_text(
            # ssh_host: only in .env (the substantive case).
            "TERMINAL_SSH_HOST=dotenv.example\n"
            "TERMINAL_SSH_USER=dotenv-user\n"
            # timeout: .env says 99, config.yaml says 42 — config still wins.
            "TERMINAL_TIMEOUT=99\n",
            encoding="utf-8",
        )

        previous = is_multiplex_active()
        set_multiplex_active(True)
        home_token = set_hermes_home_override(str(_profile_home))
        secret_token = set_secret_scope(build_profile_secret_scope(_profile_home))
        try:
            runtime_env = terminal_tool._runtime_terminal_env()
            config = terminal_tool._get_env_config()
        finally:
            reset_secret_scope(secret_token)
            reset_hermes_home_override(home_token)
            set_multiplex_active(previous)

        assert runtime_env["TERMINAL_SSH_HOST"] == "dotenv.example"
        assert runtime_env["TERMINAL_SSH_USER"] == "dotenv-user"
        assert runtime_env["TERMINAL_TIMEOUT"] == "42"
        assert config["ssh_host"] == "dotenv.example"
        assert config["ssh_user"] == "dotenv-user"
        assert config["timeout"] == 42
        # The overlay never leaks into the process environment.
        assert "TERMINAL_SSH_HOST" not in os.environ
        assert os.environ["TERMINAL_SSH_USER"] == "process-user"
        assert os.environ["TERMINAL_ENV"] == "local"

    def test_multiplex_without_scope_keeps_process_env(self, monkeypatch, _profile_home):
        """Multiplex active but no bound secret scope (e.g. process-level
        startup work) keeps the historical process-environment path."""
        from agent.secret_scope import is_multiplex_active, set_multiplex_active

        monkeypatch.setenv("TERMINAL_ENV", "local")
        # Pin the one-shot config bridge so this test only observes the
        # process env, not the sandboxed config backfill.
        monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)

        previous = is_multiplex_active()
        set_multiplex_active(True)
        try:
            runtime_env = terminal_tool._runtime_terminal_env()
        finally:
            set_multiplex_active(previous)

        assert runtime_env["TERMINAL_ENV"] == "local"

    def test_single_profile_path_reads_process_environment(self, monkeypatch):
        """Single-profile processes see exactly the process env (a private
        copy — mutating the result must not touch ``os.environ``)."""
        monkeypatch.setenv("TERMINAL_ENV", "local")
        monkeypatch.setenv("TERMINAL_TIMEOUT", "77")
        monkeypatch.setattr(terminal_tool, "_terminal_config_bridge_attempted", True)

        runtime_env = terminal_tool._runtime_terminal_env()

        assert runtime_env["TERMINAL_ENV"] == "local"
        assert runtime_env["TERMINAL_TIMEOUT"] == "77"
        runtime_env["TERMINAL_ENV"] = "mutated"
        assert os.environ["TERMINAL_ENV"] == "local"
