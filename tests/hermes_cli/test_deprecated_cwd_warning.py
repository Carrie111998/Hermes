"""Tests for warn_deprecated_cwd_env_vars() migration warning.

The warning claims a key was "found in .env", so it must read the on-disk
.env — not ``os.environ``.  ``cli.py`` force-exports ``terminal.cwd`` into
``os.environ["TERMINAL_CWD"]`` on every CLI start (rewriting it to
``os.getcwd()`` for the local backend), so a process-env read reported the
config bridge's own value as a user-authored .env entry on every clean
install.
"""

import pytest


@pytest.fixture
def fake_dotenv(monkeypatch):
    """Patch the on-disk .env loader with a caller-supplied mapping."""
    def _install(mapping):
        import hermes_cli.config as cfg
        monkeypatch.setattr(cfg, "load_env", lambda: dict(mapping))
    return _install


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, fake_dotenv, capsys):
        fake_dotenv({"MESSAGING_CWD": "/some/path"})

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_both_deprecated_vars_warn(self, fake_dotenv, capsys):
        fake_dotenv({"MESSAGING_CWD": "/msg/path", "TERMINAL_CWD": "/term/path"})

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_terminal_cwd_in_dotenv_warns_even_with_explicit_config_cwd(
        self, fake_dotenv, capsys
    ):
        """A real .env entry shadows config.yaml — report it either way.

        The old code suppressed this whenever config.yaml named an explicit
        path, which hid the one case that genuinely is split-brain.
        """
        fake_dotenv({"TERMINAL_CWD": "/from/dotenv"})

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={"terminal": {"cwd": "/from/config"}})

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" in captured.err

    # ── Regression: the config bridge must not look like a user .env entry ──

    def test_bridged_process_env_does_not_warn(self, fake_dotenv, monkeypatch, capsys):
        """THE BUG: config.yaml at its default ``cwd: .`` warned on every start.

        cli.py sets ``os.environ["TERMINAL_CWD"] = os.getcwd()`` for the local
        backend, then the warning reported that value as "found in .env" —
        naming a file the user never edited and a value appearing nowhere in it.
        """
        fake_dotenv({})
        monkeypatch.setenv("TERMINAL_CWD", "/current/working/dir")
        monkeypatch.setenv("MESSAGING_CWD", "/also/bridged")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={"terminal": {"cwd": "."}})

        assert capsys.readouterr().err == ""

    def test_commented_out_dotenv_line_does_not_warn(self, fake_dotenv, capsys):
        """``# TERMINAL_CWD=.`` ships in .env.example; load_env skips comments."""
        fake_dotenv({})  # load_env() never yields commented-out keys

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={"terminal": {"cwd": "."}})

        assert capsys.readouterr().err == ""

    def test_blank_value_does_not_warn(self, fake_dotenv, capsys):
        """``TERMINAL_CWD=`` with an empty value is not a configured path."""
        fake_dotenv({"TERMINAL_CWD": "   ", "MESSAGING_CWD": ""})

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={})

        assert capsys.readouterr().err == ""

    def test_unreadable_dotenv_is_silent(self, monkeypatch, capsys):
        """A broken .env read must not spray a warning at startup."""
        import hermes_cli.config as cfg

        def _boom():
            raise OSError("permission denied")

        monkeypatch.setattr(cfg, "load_env", _boom)
        cfg.warn_deprecated_cwd_env_vars(config={})

        assert capsys.readouterr().err == ""
