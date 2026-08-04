"""First-class Kiro ACP provider contracts."""

from hermes_cli.models import (
    CANONICAL_PROVIDERS,
    _PROVIDER_MODELS,
    normalize_provider,
)
from hermes_cli.providers import HERMES_OVERLAYS
from hermes_cli import model_setup_flows


def test_kiro_acp_is_visible_in_canonical_provider_picker():
    entry = next(provider for provider in CANONICAL_PROVIDERS if provider.slug == "kiro-acp")

    assert entry.label == "Kiro ACP"
    assert "kiro-cli acp" in entry.tui_desc


def test_kiro_acp_defaults_to_claude_sonnet_5():
    assert _PROVIDER_MODELS["kiro-acp"] == [
        "claude-sonnet-5",
        "claude-opus-5",
    ]


def test_kiro_aliases_normalize_to_kiro_acp():
    assert normalize_provider("kiro") == "kiro-acp"
    assert normalize_provider("kiro-cli") == "kiro-acp"


def test_kiro_acp_overlay_uses_external_process_transport():
    overlay = HERMES_OVERLAYS["kiro-acp"]

    assert overlay.auth_type == "external_process"
    assert overlay.base_url_override == "acp://kiro"


def test_kiro_model_flow_persists_selected_model_and_provider(monkeypatch):
    saved = {}
    config = {}

    monkeypatch.setattr(
        "hermes_cli.auth.get_external_process_provider_status",
        lambda provider: {
            "configured": True,
            "resolved_command": "/usr/local/bin/kiro-cli",
            "base_url": "acp://kiro",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.auth.resolve_external_process_provider_credentials",
        lambda provider: {
            "provider": provider,
            "command": "/usr/local/bin/kiro-cli",
            "base_url": "acp://kiro",
            "args": ["acp"],
        },
    )
    monkeypatch.setattr(
        "hermes_cli.auth._prompt_model_selection",
        lambda models, **kwargs: "claude-sonnet-5",
    )
    monkeypatch.setattr(
        "hermes_cli.auth._save_model_choice",
        lambda model: saved.setdefault("model_choice", model),
    )
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: config)
    monkeypatch.setattr(
        "hermes_cli.config.save_config",
        lambda value: saved.setdefault("config", value.copy()),
    )
    monkeypatch.setattr("hermes_cli.auth.deactivate_provider", lambda: None)

    flow = getattr(model_setup_flows, "_model_flow_kiro_acp", None)
    assert callable(flow)
    flow({}, "")

    assert saved["model_choice"] == "claude-sonnet-5"
    assert saved["config"]["model"] == {
        "provider": "kiro-acp",
        "default": "claude-sonnet-5",
        "base_url": "acp://kiro",
        "api_mode": "chat_completions",
    }
