"""Tests for warn_deprecated_cwd_env_vars() migration warning."""


class TestDeprecatedCwdWarning:
    """Warn when MESSAGING_CWD or TERMINAL_CWD is set in .env."""

    def test_messaging_cwd_triggers_warning(self, monkeypatch, capsys):
        monkeypatch.setenv("MESSAGING_CWD", "/some/path")
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(
            config={},
            env_values={"MESSAGING_CWD": "/some/path"},
        )

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "deprecated" in captured.err.lower()
        assert "config.yaml" in captured.err

    def test_terminal_cwd_triggers_warning_when_config_placeholder(self, monkeypatch, capsys):
        monkeypatch.setenv("TERMINAL_CWD", "/project")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        # config has placeholder cwd → TERMINAL_CWD likely from .env
        warn_deprecated_cwd_env_vars(
            config={"terminal": {"cwd": "."}},
            env_values={"TERMINAL_CWD": "/project"},
        )

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" in captured.err
        assert "deprecated" in captured.err.lower()

    def test_no_warning_when_config_has_explicit_cwd(self, monkeypatch, capsys):
        monkeypatch.setenv("TERMINAL_CWD", "/project")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        # config has explicit cwd → TERMINAL_CWD could be from config bridge
        warn_deprecated_cwd_env_vars(
            config={"terminal": {"cwd": "/project"}},
            env_values={"TERMINAL_CWD": "/project"},
        )

        captured = capsys.readouterr()
        assert "TERMINAL_CWD" not in captured.err

    def test_no_warning_when_env_clean(self, monkeypatch, capsys):
        monkeypatch.delenv("MESSAGING_CWD", raising=False)
        monkeypatch.delenv("TERMINAL_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(config={}, env_values={})

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_both_deprecated_vars_warn(self, monkeypatch, capsys):
        monkeypatch.setenv("MESSAGING_CWD", "/msg/path")
        monkeypatch.setenv("TERMINAL_CWD", "/term/path")

        from hermes_cli.config import warn_deprecated_cwd_env_vars
        warn_deprecated_cwd_env_vars(
            config={},
            env_values={
                "MESSAGING_CWD": "/msg/path",
                "TERMINAL_CWD": "/term/path",
            },
        )

        captured = capsys.readouterr()
        assert "MESSAGING_CWD" in captured.err
        assert "TERMINAL_CWD" in captured.err

    def test_inherited_terminal_cwd_does_not_trigger_warning(
        self, monkeypatch, capsys
    ):
        """The gateway's runtime bridge is not evidence of a deprecated file entry."""
        monkeypatch.setenv("TERMINAL_CWD", "/opt/data")
        monkeypatch.delenv("MESSAGING_CWD", raising=False)

        from hermes_cli.config import warn_deprecated_cwd_env_vars

        warn_deprecated_cwd_env_vars(config={}, env_values={})

        captured = capsys.readouterr()
        assert captured.err == ""

    def test_default_path_reads_actual_env_file(
        self, tmp_path, monkeypatch, capsys
    ):
        env_path = tmp_path / ".env"
        env_path.write_text("TERMINAL_CWD=/legacy/project\n", encoding="utf-8")
        monkeypatch.setattr("hermes_cli.config.get_env_path", lambda: env_path)

        from hermes_cli import config as config_module

        config_module.invalidate_env_cache()
        config_module.warn_deprecated_cwd_env_vars(
            config={"terminal": {"cwd": "."}},
        )

        captured = capsys.readouterr()
        assert "TERMINAL_CWD=/legacy/project" in captured.err
        assert str(env_path) in captured.err
