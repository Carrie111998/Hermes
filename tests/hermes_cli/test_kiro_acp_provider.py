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


def test_kiro_acp_defaults_to_claude_opus_5():
    assert _PROVIDER_MODELS["kiro-acp"] == [
        "claude-opus-5",
        "claude-sonnet-5",
    ]


def test_kiro_aliases_normalize_to_kiro_acp():
    assert normalize_provider("kiro") == "kiro-acp"
    assert normalize_provider("kiro-cli") == "kiro-acp"
    assert normalize_provider("kiro-agent") == "kiro-acp"


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
        lambda models, **kwargs: "claude-opus-5",
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

    assert saved["model_choice"] == "claude-opus-5"
    assert saved["config"]["model"] == {
        "provider": "kiro-acp",
        "default": "claude-opus-5",
        "base_url": "acp://kiro",
        "api_mode": "chat_completions",
    }


def test_runtime_kiro_acp_never_empty_command_or_unusable_creds(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.auth.shutil.which",
        lambda command: f"/usr/local/bin/{command}",
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.load_pool",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("credential pool must not run for kiro-acp")
        ),
    )
    from hermes_cli.runtime_provider import resolve_runtime_provider

    result = resolve_runtime_provider(requested="kiro-acp", target_model="claude-opus-5")
    assert result["command"]
    assert result["args"] == ["acp", "--model", "claude-opus-5"]
    assert "--trust-all-tools" not in result["args"]
    assert "No usable credentials" not in str(result)


def test_aux_client_builds_kiro_acp_client(monkeypatch):
    from agent.auxiliary_client import _normalize_aux_provider, resolve_provider_client
    from agent.kiro_acp_client import KiroACPClient

    assert _normalize_aux_provider("kiro") == "kiro-acp"
    assert _normalize_aux_provider("kiro-cli") == "kiro-acp"

    monkeypatch.setattr(
        "hermes_cli.auth.resolve_external_process_provider_credentials",
        lambda provider: {
            "provider": "kiro-acp",
            "api_key": "kiro-acp",
            "base_url": "acp://kiro",
            "command": "/usr/local/bin/kiro-cli",
            "args": ["acp"],
        },
    )
    client, model = resolve_provider_client("kiro-acp", model="claude-opus-5")
    assert isinstance(client, KiroACPClient)
    assert model == "claude-opus-5"
    assert "--model" in client._acp_args
    assert "claude-opus-5" in client._acp_args


# ---------------------------------------------------------------------------
# /model picker visibility (list_authenticated_providers / list_picker_providers)
# ---------------------------------------------------------------------------
#
# The in-session picker (cli.py ``/model``) builds its menu from
# ``build_models_payload`` → ``list_authenticated_providers``. Section 2 of
# that function historically gated only api_key / oauth / aws_sdk / vertex,
# so ``external_process`` providers (kiro-acp, copilot-acp) were silently
# dropped even when ``kiro-cli`` was on PATH and the session was already
# running on Kiro ACP.


def _kiro_status(*, configured: bool):
    return {
        "configured": configured,
        "logged_in": configured,
        "provider": "kiro-acp",
        "name": "Kiro ACP",
        "command": "kiro-cli",
        "args": ["acp"],
        "resolved_command": "/usr/local/bin/kiro-cli" if configured else None,
        "base_url": "acp://kiro",
    }


def test_kiro_acp_appears_in_picker_when_cli_configured(monkeypatch):
    """Configured kiro-cli must surface a first-class Kiro ACP picker row."""
    from hermes_cli.inventory import build_models_payload, load_picker_context
    from hermes_cli.model_switch import list_authenticated_providers, list_picker_providers

    monkeypatch.setattr(
        "hermes_cli.auth.get_external_process_provider_status",
        lambda provider: _kiro_status(configured=True)
        if provider == "kiro-acp"
        else {"configured": False, "logged_in": False},
    )

    rows = list_authenticated_providers(
        current_provider="openrouter",
        max_models=50,
    )
    kiro = next((p for p in rows if p["slug"] == "kiro-acp"), None)
    assert kiro is not None, f"kiro-acp missing from authenticated picker: {[p['slug'] for p in rows]}"
    assert kiro["name"] == "Kiro ACP"
    assert kiro["models"] == ["claude-opus-5", "claude-sonnet-5"]
    assert kiro["total_models"] == 2

    picker = list_picker_providers(current_provider="openrouter", max_models=50)
    picker_kiro = next((p for p in picker if p["slug"] == "kiro-acp"), None)
    assert picker_kiro is not None, "list_picker_providers must not drop kiro-acp"
    assert picker_kiro["models"] == ["claude-opus-5", "claude-sonnet-5"]

    ctx = load_picker_context().with_overrides(current_provider="openrouter")
    menu = build_models_payload(ctx)["providers"]
    assert any(p["slug"] == "kiro-acp" for p in menu), (
        f"kiro-acp missing from /model menu: {[p['slug'] for p in menu]}"
    )


def test_kiro_acp_hidden_from_picker_when_cli_missing(monkeypatch):
    """No kiro-cli (and not the current provider) → no Kiro ACP row."""
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setattr(
        "hermes_cli.auth.get_external_process_provider_status",
        lambda provider: _kiro_status(configured=False),
    )

    rows = list_authenticated_providers(
        current_provider="openrouter",
        max_models=50,
    )
    assert not any(p["slug"] == "kiro-acp" for p in rows)


def test_kiro_acp_stays_in_picker_when_it_is_the_current_provider(monkeypatch):
    """A live kiro-acp session must remain selectable even if PATH lookup fails."""
    from hermes_cli.model_switch import list_authenticated_providers

    monkeypatch.setattr(
        "hermes_cli.auth.get_external_process_provider_status",
        lambda provider: _kiro_status(configured=False),
    )

    rows = list_authenticated_providers(
        current_provider="kiro-acp",
        current_model="claude-opus-5",
        max_models=50,
    )
    kiro = next((p for p in rows if p["slug"] == "kiro-acp"), None)
    assert kiro is not None, "current kiro-acp session must stay in the picker"
    assert kiro["is_current"] is True
    assert "claude-opus-5" in kiro["models"]
