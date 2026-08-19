"""Tests for warn_deprecated_cwd_env_vars() migration warning."""

import os

import pytest


@pytest.fixture
def env_file(monkeypatch, tmp_path):
    """Point load_env() at a throwaway .env file and reset its memo cache."""
    import hermes_cli.config as config

    env_path = tmp_path / ".env"
    monkeypatch.setattr(config, "get_env_path", lambda: env_path)
    monkeypatch.setattr(config, "_env_cache", None)
    return env_path


def _call_warning():
    from hermes_cli.config import warn_deprecated_cwd_env_vars
    return warn_deprecated_cwd_env_vars(config={})


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, env_file, monkeypatch, capsys):
        # The deprecation warning must fire only when the var is really
        # present in the on-disk .env (not merely bridged into os.environ).
        env_file.write_text("MESSAGING_CWD=/some/path\n", encoding="utf-8")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        _call_warning()

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_terminal_cwd_in_env_file_triggers_warning(self, env_file, capsys):
        env_file.write_text("TERMINAL_CWD=/term/path\n", encoding="utf-8")

        _call_warning()

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" in captured.err
        assert "deprecated" in captured.err.lower()

    def test_both_deprecated_vars_warn(self, env_file, capsys):
        env_file.write_text(
            "MESSAGING_CWD=/msg/path\nTERMINAL_CWD=/term/path\n",
            encoding="utf-8",
        )

        _call_warning()

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_bridged_env_var_does_not_warn(self, env_file, monkeypatch, capsys):
        """Regression: the config bridge injects terminal.cwd -> TERMINAL_CWD
        into os.environ even when terminal.cwd is the default "." / "auto".
        That bridged value must NOT be reported as a deprecated .env entry.
        """
        # Empty .env file — nothing deprecated on disk.
        env_file.write_text("# nothing here\n", encoding="utf-8")
        # Simulate the bridge: TERMINAL_CWD present in the process env only.
        monkeypatch.setenv("TERMINAL_CWD", os.getcwd())
        monkeypatch.setenv("MESSAGING_CWD", "/bridged/msg")

        _call_warning()

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err
        assert "MESSAGING_CWD" not in captured.err
        assert "deprecated" not in captured.err.lower()
