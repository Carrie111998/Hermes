"""Tests for warn_deprecated_cwd_env_vars() migration warning."""


class TestDeprecatedCwdWarning:
    """Warn only when MESSAGING_CWD or TERMINAL_CWD is actually set in .env."""

    def _make_env_file(self, tmp_path, monkeypatch, content):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        (tmp_path / ".env").write_text(content)

    def test_messaging_cwd_triggers_warning(self, tmp_path, monkeypatch, capsys):
        self._make_env_file(tmp_path, monkeypatch, "MESSAGING_CWD=/some/path\n")
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err


    def test_both_deprecated_vars_warn(self, tmp_path, monkeypatch, capsys):
        self._make_env_file(
            tmp_path,
            monkeypatch,
            "MESSAGING_CWD=/msg/path\nTERMINAL_CWD=/term/path\n",
        )
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err


    def test_runtime_env_var_without_env_file_entry_is_silent(
        self, tmp_path, monkeypatch, capsys
    ):
        """Hermes exports TERMINAL_CWD itself at runtime (gateway, session
        restore, cron).  A process env var with no matching .env line must
        NOT be blamed on .env — this was a false-positive warning on every
        startup for users with a clean .env."""
        self._make_env_file(
            tmp_path, monkeypatch, "# TERMINAL_CWD=.\nOTHER_KEY=value\n"
        )
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert captured.err == ""


    def test_export_prefix_in_env_file_still_warns(
        self, tmp_path, monkeypatch, capsys
    ):
        self._make_env_file(
            tmp_path, monkeypatch, "export TERMINAL_CWD=/term/path\n"
        )
        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" in captured.err
