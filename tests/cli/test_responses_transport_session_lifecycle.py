"""Classic CLI lifecycle coverage for Codex Responses transports."""

from types import SimpleNamespace

import cli as cli_mod
from hermes_cli.model_switch import ModelSwitchResult


def _stub_cli():
    return SimpleNamespace(
        agent=None,
        model="gpt-5-codex",
        provider="openai-codex",
        requested_provider="openai-codex",
        api_key="token",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        responses_transport="sse",
        _session_responses_transport_override=None,
        _explicit_api_key=None,
        _explicit_base_url=None,
        _pending_model_switch_note=None,
        conversation_history=[],
    )


def _switch_result(transport="websocket-cached"):
    return ModelSwitchResult(
        success=True,
        new_model="gpt-5.4-codex",
        target_provider="openai-codex",
        api_key="token",
        base_url="https://chatgpt.com/backend-api/codex",
        api_mode="codex_responses",
        responses_transport=transport,
        provider_label="ChatGPT Codex",
    )


def _patch_display(monkeypatch):
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.resolve_display_context_length",
        lambda *_args, **_kwargs: None,
    )


def test_session_model_switch_installs_transport_override(monkeypatch):
    _patch_display(monkeypatch)
    stub = _stub_cli()

    cli_mod.HermesCLI._apply_model_switch_result(
        stub, _switch_result(), persist_global=False
    )

    assert stub.responses_transport == "websocket-cached"
    assert (
        stub._session_responses_transport_override == "websocket-cached"
    )


def test_global_model_switch_persists_transport_and_clears_override(monkeypatch):
    _patch_display(monkeypatch)
    writes = {}
    monkeypatch.setattr(
        cli_mod, "save_config_value", lambda key, value: writes.__setitem__(key, value)
    )
    monkeypatch.setattr(
        cli_mod.HermesCLI,
        "_clear_persisted_context_for_model_switch",
        lambda *_args, **_kwargs: None,
    )
    stub = _stub_cli()
    stub._session_responses_transport_override = "sse"

    cli_mod.HermesCLI._apply_model_switch_result(
        stub, _switch_result(), persist_global=True
    )

    assert writes["model.responses_transport"] == "websocket-cached"
    assert stub._session_responses_transport_override is None
