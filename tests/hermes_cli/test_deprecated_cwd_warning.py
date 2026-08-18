"""Tests for warn_deprecated_cwd_env_vars() migration warning."""


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err


    def test_both_deprecated_vars_warn(self, monkeypatch, capsys):
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_local_backend_dot_cwd_does_not_warn_when_bridged(self, monkeypatch, capsys):
        """When terminal.cwd is '.' and TERMINAL_CWD is bridged from os.getcwd()
        (and not in .env), no false-positive warning is emitted (#89016)."""
        import os
        from hermes_cli import config as config_mod

        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.setenv("TERMINAL_CWD", os.getcwd())
        monkeypatch.setattr(config_mod, "load_env", lambda: {})

        config_mod.warn_deprecated_cwd_env_vars(config={"terminal": {"cwd": "."}})
        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err
        assert captured.err == ""

    def test_dot_env_terminal_cwd_warns(self, monkeypatch, capsys):
        """When TERMINAL_CWD is actually in .env, warning is emitted."""
        import os
        from hermes_cli import config as config_mod

        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.setenv("TERMINAL_CWD", os.getcwd())
        monkeypatch.setattr(config_mod, "load_env", lambda: {"TERMINAL_CWD": os.getcwd()})

        config_mod.warn_deprecated_cwd_env_vars(config={"terminal": {"cwd": "."}})
        captured = capsys.readouterr()
        assert "TERMINAL_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
