"""Regression tests for explicit provider detection."""


def test_provider_env_value_is_read_live_from_dotenv(monkeypatch, tmp_path):
    """Provider pickers see credentials added to .env without a restart."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    from hermes_cli.auth import is_provider_explicitly_configured

    assert is_provider_explicitly_configured("anthropic") is False

    (hermes_home / ".env").write_text(
        "ANTHROPIC_API_KEY=dotenv-key-for-test\n",
        encoding="utf-8",
    )

    assert is_provider_explicitly_configured("anthropic") is True
