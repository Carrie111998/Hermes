"""Tests for hermes status custom-pool provider rendering helpers
(regression for the second PR in the Kimi custom-pool auth-status fix
series). The bug: ``hermes_cli.status.show_status`` only iterates a
hardcoded dict of env-var-backed providers; custom pool providers
added via ``hermes auth add --pool custom:`` are invisible in the
"API-Key Providers" block.
"""

from unittest.mock import patch


def test_is_resolved_api_key_rejects_empty():
    from hermes_cli.status import _is_resolved_api_key
    assert _is_resolved_api_key("") is False
    assert _is_resolved_api_key(None) is False


def test_is_resolved_api_key_rejects_unresolved_placeholder():
    """A literal ``${MISSING_ENV_VAR}`` must NOT be treated as a
    resolved api key — same guard as ``hermes auth status``."""
    from hermes_cli.status import _is_resolved_api_key
    assert _is_resolved_api_key("${MISSING_ENV_VAR}") is False
    assert _is_resolved_api_key("${SECRET}") is False


def test_is_resolved_api_key_accepts_real_secret():
    from hermes_cli.status import _is_resolved_api_key
    assert _is_resolved_api_key("sk-real-key-1234567890abcdef") is True
    assert _is_resolved_api_key("kimi-abc-xyz-123") is True


def test_emit_custom_pool_status_skips_when_no_custom_providers(capsys):
    """No custom_providers in config → helper prints nothing."""
    from hermes_cli.status import _emit_custom_pool_status
    with patch("hermes_cli.status.load_config", return_value={}):
        _emit_custom_pool_status()
    out = capsys.readouterr().out
    assert out == ""


def test_emit_custom_pool_status_skips_on_malformed_config(capsys):
    """Malformed custom_providers entry must not crash."""
    from hermes_cli.status import _emit_custom_pool_status
    with patch("hermes_cli.status.load_config",
               return_value={"custom_providers": "not-a-list"}):
        _emit_custom_pool_status()
    out = capsys.readouterr().out
    assert out == ""


def test_emit_custom_pool_status_shows_configured(capsys):
    """A custom pool provider with a resolved api_key must print as
    ✓ configured (custom pool)."""
    from hermes_cli.status import _emit_custom_pool_status
    cfg = {"custom_providers": [{"name": "Moonshot Kimi (international)"}]}
    cp_responses = {
        "custom:Moonshot Kimi (international)": {
            "api_key": "sk-real-key-1234567890abcdef",
        },
    }
    with patch("hermes_cli.status.load_config", return_value=cfg), \
         patch("agent.credential_pool._get_custom_provider_config",
               side_effect=lambda k: cp_responses.get(k, {})):
        _emit_custom_pool_status()
    out = capsys.readouterr().out
    assert "Moonshot Kimi (internati" in out
    assert "✓" in out
    assert "configured (custom pool)" in out


def test_emit_custom_pool_status_reports_unresolved_placeholder(capsys):
    """A custom pool provider whose api_key is a literal
    ``${MISSING_ENV_VAR}`` must print as ✗ missing api_key / env."""
    from hermes_cli.status import _emit_custom_pool_status
    cfg = {"custom_providers": [{"name": "Broken Provider"}]}
    cp_responses = {
        "custom:Broken Provider": {"api_key": "${MISSING_ENV_VAR}"},
    }
    with patch("hermes_cli.status.load_config", return_value=cfg), \
         patch("agent.credential_pool._get_custom_provider_config",
               side_effect=lambda k: cp_responses.get(k, {})):
        _emit_custom_pool_status()
    out = capsys.readouterr().out
    assert "Broken Provider" in out
    assert "✗" in out
    assert "missing api_key / env" in out


def test_emit_custom_pool_status_handles_empty_name(capsys):
    """A custom_providers entry with no name must be skipped silently."""
    from hermes_cli.status import _emit_custom_pool_status
    cfg = {"custom_providers": [{"name": ""}, {"name": "Real One"}]}
    cp_responses = {"custom:Real One": {"api_key": "sk-real-1234"}}
    with patch("hermes_cli.status.load_config", return_value=cfg), \
         patch("agent.credential_pool._get_custom_provider_config",
               side_effect=lambda k: cp_responses.get(k, {})):
        _emit_custom_pool_status()
    out = capsys.readouterr().out
    # Only "Real One" should appear.
    assert "Real One" in out
    assert out.count("custom pool") == 1
