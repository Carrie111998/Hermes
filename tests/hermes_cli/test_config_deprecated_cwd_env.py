"""Regression tests for deprecated CWD .env diagnostics."""

from __future__ import annotations

from hermes_cli import config as config_module


def test_cwd_process_env_does_not_masquerade_as_dotenv(monkeypatch, capsys):
    """Config/runtime env bridges are not evidence that .env contains the key."""
    monkeypatch.setenv("TERMINAL_CWD", "/runtime/workspace")
    monkeypatch.setenv("MESSAGING_CWD", "/runtime/messages")
    monkeypatch.setattr(config_module, "load_env", lambda: {})

    config_module.warn_deprecated_cwd_env_vars({"terminal": {"cwd": "."}})

    assert capsys.readouterr().err == ""


def test_cwd_deprecated_keys_from_dotenv_still_warn(monkeypatch, capsys):
    """Actual legacy .env entries still produce the migration warning."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    monkeypatch.delenv("MESSAGING_CWD", raising=False)
    monkeypatch.setattr(
        config_module,
        "load_env",
        lambda: {
            "TERMINAL_CWD": "/legacy/workspace",
            "MESSAGING_CWD": "/legacy/messages",
        },
    )

    config_module.warn_deprecated_cwd_env_vars({"terminal": {"cwd": "."}})

    err = capsys.readouterr().err
    assert "Deprecated .env settings detected" in err
    assert "TERMINAL_CWD=/legacy/workspace found in .env" in err
    assert "MESSAGING_CWD=/legacy/messages found in .env" in err
