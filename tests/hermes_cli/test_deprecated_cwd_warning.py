"""Tests for warn_deprecated_cwd_env_vars() migration warning."""


def _make_env(tmp_path, monkeypatch, content: str):
    """Point HERMES_HOME at a temp dir containing the given .env content."""
    (tmp_path / ".env").write_text(content)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, tmp_path, monkeypatch, capsys):
        _make_env(tmp_path, monkeypatch, "MESSAGING_CWD=/some/path\n")
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_both_deprecated_vars_warn(self, tmp_path, monkeypatch, capsys):
        _make_env(
            tmp_path, monkeypatch,
            "MESSAGING_CWD=/msg/path\nTERMINAL_CWD=/term/path\n",
        )
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_programmatic_env_var_does_not_warn(self, tmp_path, monkeypatch, capsys):
        # TERMINAL_CWD in the process env but NOT in .env (set by session
        # restore / worktree launch / gateway) must not trigger the warning.
        _make_env(tmp_path, monkeypatch, "# TERMINAL_CWD=.\n")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert captured.err == ""
