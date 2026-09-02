"""Tests for warn_deprecated_cwd_env_vars() migration warning."""

import io


def _write_env(monkeypatch, tmp_path, content):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / ".env").write_text(content, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    return hermes_home


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_process_environment_does_not_trigger_warning(
        self, monkeypatch, tmp_path, capsys
    ):
        _write_env(monkeypatch, tmp_path, "# TERMINAL_CWD=.\n")
        monkeypatch.setenv("MESSAGING_CWD", "/process/message-path")
        monkeypatch.setenv("TERMINAL_CWD", "/process/terminal-path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars()

        assert capsys.readouterr().err == ""

    def test_both_deprecated_vars_in_dotenv_warn(
        self, monkeypatch, tmp_path, capsys
    ):
        _write_env(
            monkeypatch,
            tmp_path,
            "MESSAGING_CWD=/msg/path\nTERMINAL_CWD=/term/path\n",
        )
        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars()

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_dotenv_terminal_cwd_warns_with_explicit_config(
        self, monkeypatch, tmp_path, capsys
    ):
        hermes_home = _write_env(
            monkeypatch, tmp_path, "TERMINAL_CWD=/legacy/path\n"
        )
        (hermes_home / "config.yaml").write_text(
            "terminal:\n  cwd: /current/path\n", encoding="utf-8"
        )

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars()

        assert "TERMINAL_CWD=/legacy/path" in capsys.readouterr().err

    def test_commented_and_empty_dotenv_values_do_not_warn(
        self, monkeypatch, tmp_path, capsys
    ):
        _write_env(
            monkeypatch,
            tmp_path,
            "# MESSAGING_CWD=/commented\nTERMINAL_CWD=\n",
        )
        monkeypatch.setenv("TERMINAL_CWD", "/process/bridge")

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars()

        assert capsys.readouterr().err == ""

    def test_dotenv_read_failure_is_silent(self, monkeypatch, capsys):
        import hermes_cli.config as config_module

        def raise_read_error():
            raise OSError("permission denied")

        monkeypatch.setattr(config_module, "load_env", raise_read_error)

        config_module.warn_deprecated_cwd_env_vars()

        assert capsys.readouterr().err == ""

    class _FakeTtyStderr:
        """A stderr that claims to be a TTY and records what it is written."""

        def __init__(self):
            self._buf = io.StringIO()

        def isatty(self):
            return True

        def write(self, s):
            self._buf.write(s)
            return len(s)

        def getvalue(self):
            return self._buf.getvalue()

    def test_color_emitted_on_tty_without_no_color(self, monkeypatch, tmp_path):
        _write_env(monkeypatch, tmp_path, "TERMINAL_CWD=/legacy/path\n")
        monkeypatch.delenv("NO_COLOR", raising=False)

        fake = self._FakeTtyStderr()
        monkeypatch.setattr("sys.stderr", fake)

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars()

        out = fake.getvalue()
        assert "TERMINAL_CWD" in out
        assert "\033[33m" in out  # yellow warning mark

    def test_empty_no_color_disables_color_on_tty(self, monkeypatch, tmp_path):
        # Per the NO_COLOR spec (https://no-color.org/), the mere presence
        # of NO_COLOR — even with an empty value — disables color.
        _write_env(monkeypatch, tmp_path, "TERMINAL_CWD=/legacy/path\n")
        monkeypatch.setenv("NO_COLOR", "")

        fake = self._FakeTtyStderr()
        monkeypatch.setattr("sys.stderr", fake)

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars()

        out = fake.getvalue()
        assert "TERMINAL_CWD" in out
        assert "\033[" not in out  # no ANSI escapes leak

    def test_no_color_nonempty_disables_color_on_tty(self, monkeypatch, tmp_path):
        _write_env(monkeypatch, tmp_path, "TERMINAL_CWD=/legacy/path\n")
        monkeypatch.setenv("NO_COLOR", "1")

        fake = self._FakeTtyStderr()
        monkeypatch.setattr("sys.stderr", fake)

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars()

        out = fake.getvalue()
        assert "TERMINAL_CWD" in out
        assert "\033[" not in out
