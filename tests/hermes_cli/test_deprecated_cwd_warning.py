"""Tests for warn_deprecated_cwd_env_vars() migration warning."""


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def _write_dotenv(self, monkeypatch, text):
        """Write ``text`` to the sandboxed HERMES_HOME/.env and reload env."""
        from hermes_constants import get_hermes_home

        env_path = get_hermes_home() / ".env"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(text)

    def test_messaging_cwd_triggers_warning(self, monkeypatch, capsys):
        self._write_dotenv(monkeypatch, "MESSAGING_CWD=/some/path\n")
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_both_deprecated_vars_warn(self, monkeypatch, capsys):
        self._write_dotenv(
            monkeypatch, "MESSAGING_CWD=/msg/path\nTERMINAL_CWD=/term/path\n"
        )
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_inherited_terminal_cwd_does_not_warn(self, monkeypatch, capsys):
        """TERMINAL_CWD inherited from a parent process must not warn.

        Hermes itself exports TERMINAL_CWD for its children (config bridge,
        desktop app injection); a nested ``hermes`` inherits it.  The warning
        claims the value is "found in .env", so it must only fire when the
        key is actually defined in .env.
        """
        # No .env entry — env value is inherited/bridged, not from .env.
        self._write_dotenv(monkeypatch, "SOME_OTHER_KEY=1\n")
        monkeypatch.setenv("TERMINAL_CWD", "/home/user")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err

    def test_inherited_messaging_cwd_does_not_warn(self, monkeypatch, capsys):
        """MESSAGING_CWD exported in a shell (not .env) must not warn."""
        self._write_dotenv(monkeypatch, "SOME_OTHER_KEY=1\n")
        monkeypatch.setenv("MESSAGING_CWD", "/exported/in/shell")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" not in captured.err
