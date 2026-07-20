"""Tests for Vertex AI runtime-provider resolution and profile registration.

Covers: provider-profile registration + aliases, alias canonicalization,
resolve_runtime_provider(vertex) minting an OAuth token, and the friendly
AuthError when credentials can't be resolved. No network calls.
"""

from __future__ import annotations

import pytest








def test_resolve_runtime_provider_raises_autherror_when_unresolved(monkeypatch):
    import agent.vertex_adapter as va
    from hermes_cli import runtime_provider as rp
    from hermes_cli.auth import AuthError

    monkeypatch.setattr(va, "get_vertex_config", lambda: (None, None))
    with pytest.raises(AuthError) as exc:
        rp.resolve_runtime_provider(requested="vertex")
    msg = str(exc.value)
    assert "OAuth2" in msg
    assert "not a static API key" in msg


def test_vertex_registered_in_provider_registry():
    """PROVIDER_REGISTRY (hermes_cli.auth) is what agent/auxiliary_client.py's
    resolve_provider_client() looks up before dispatching on auth_type. Without
    an entry here, the ``elif pconfig.auth_type == "vertex":`` branch there is
    unreachable dead code — every auxiliary Vertex call (vision, title
    generation, MoA reference/aggregator slots, ...) fails at the
    ``pconfig is None`` guard before ever reaching it."""
    from hermes_cli.auth import PROVIDER_REGISTRY

    cfg = PROVIDER_REGISTRY.get("vertex")
    assert cfg is not None
    assert cfg.auth_type == "vertex"


def test_vertex_registered_in_hermes_overlays():
    """hermes_cli.providers.get_provider("vertex") backs
    _preserve_provider_with_base_url() in agent/auxiliary_client.py, which
    decides whether a MoA slot's resolved Vertex (base_url, api_key) pair
    keeps its "vertex" provider identity or silently collapses to "custom" —
    losing the identity _refresh_provider_credentials() needs to re-mint an
    expired OAuth2 token on a 401."""
    from hermes_cli.providers import get_provider

    resolved = get_provider("vertex")
    assert resolved is not None
    assert resolved.auth_type == "vertex"


# ---------------------------------------------------------------------------
# Claude-on-Vertex: dual-path routing (Anthropic Messages vs OpenAI-compat).
# ---------------------------------------------------------------------------

def test_claude_on_vertex_routes_to_anthropic_messages(monkeypatch):
    """A Claude model on the vertex provider must route through the
    AnthropicVertex SDK path (api_mode=anthropic_messages), carrying the
    google-auth Credentials object and project/region — NOT a static token."""
    import agent.vertex_adapter as va
    from hermes_cli import runtime_provider as rp

    class _Creds:
        token = "ya29.TOKEN"

    fake_creds = _Creds()
    monkeypatch.setattr(
        va, "get_vertex_anthropic_config",
        lambda *a, **k: (fake_creds, "my-proj", "us-east5"),
    )
    rt = rp.resolve_runtime_provider(
        requested="vertex", target_model="claude-sonnet-4-5@20250929",
    )
    assert rt["provider"] == "vertex"
    assert rt["api_mode"] == "anthropic_messages"
    assert rt["vertex_anthropic"] is True
    assert rt["vertex_project_id"] == "my-proj"
    assert rt["region"] == "us-east5"
    assert rt["vertex_credentials"] is fake_creds
    # regional base_url shape (Anthropic SDK appends the rawPredict path itself)
    assert rt["base_url"] == "https://us-east5-aiplatform.googleapis.com/v1"


def test_claude_on_vertex_global_region_base_url(monkeypatch):
    import agent.vertex_adapter as va
    from hermes_cli import runtime_provider as rp

    monkeypatch.setattr(
        va, "get_vertex_anthropic_config",
        lambda *a, **k: (object(), "my-proj", "global"),
    )
    rt = rp.resolve_runtime_provider(
        requested="vertex", target_model="claude-opus-4-1@20250805",
    )
    assert rt["base_url"] == "https://aiplatform.googleapis.com/v1"


def test_gemini_on_vertex_still_uses_openai_compat(monkeypatch):
    """Non-Claude models must keep the OpenAI-compat (chat_completions) path."""
    import agent.vertex_adapter as va
    from hermes_cli import runtime_provider as rp

    monkeypatch.setattr(
        va, "get_vertex_config",
        lambda: ("ya29.TOKEN", "https://aiplatform.googleapis.com/v1beta1/projects/p/locations/global/endpoints/openapi"),
    )
    rt = rp.resolve_runtime_provider(
        requested="vertex", target_model="gemini-2.5-flash",
    )
    assert rt["api_mode"] == "chat_completions"
    assert rt.get("vertex_anthropic") is None
    assert rt["api_key"] == "ya29.TOKEN"


def test_claude_on_vertex_raises_autherror_when_unresolved(monkeypatch):
    import agent.vertex_adapter as va
    from hermes_cli import runtime_provider as rp
    from hermes_cli.auth import AuthError

    monkeypatch.setattr(va, "get_vertex_anthropic_config", lambda *a, **k: (None, None, None))
    with pytest.raises(AuthError) as exc:
        rp.resolve_runtime_provider(requested="vertex", target_model="claude-sonnet-4-5@20250929")
    assert "Claude" in str(exc.value)


def test_build_anthropic_vertex_client_shape():
    """The AnthropicVertex client must be built with self-refreshing creds,
    max_retries=0 (hermes owns retry), and NO 1M-context beta."""
    pytest.importorskip("anthropic")
    from unittest.mock import MagicMock
    from agent.anthropic_adapter import build_anthropic_vertex_client

    creds = MagicMock()
    client = build_anthropic_vertex_client("my-proj", "us-east5", credentials=creds)
    assert type(client).__name__ == "AnthropicVertex"
    assert client.project_id == "my-proj"
    assert client.region == "us-east5"
    assert client.max_retries == 0
    beta = client._custom_headers.get("anthropic-beta", "")
    assert "context-1m" not in beta
    assert "interleaved-thinking-2025-05-14" in beta


# ── /model picker visibility (list_authenticated_providers) ─────────────────
#
# Vertex has auth_type "vertex" and env_vars=() — no API key to detect — so
# without a dedicated credential check (mirroring bedrock's aws_sdk special
# case) the picker omits the provider row entirely whenever vertex isn't the
# configured model.provider. Regression: switching model.provider to `moa`
# made "Google Vertex AI" vanish from the desktop picker despite working ADC.

def test_picker_lists_vertex_when_credentials_present(monkeypatch):
    import agent.vertex_adapter as va
    from hermes_cli import model_switch as ms

    monkeypatch.setattr(va, "has_vertex_credentials", lambda: True)
    rows = ms.list_authenticated_providers(current_provider="moa")
    vertex_rows = [r for r in rows if r.get("slug") == "vertex"]
    assert vertex_rows, "vertex row missing from picker despite credentials"
    models = vertex_rows[0].get("models") or []
    assert "claude-fable-5" in models


def test_picker_hides_vertex_without_credentials(monkeypatch):
    import agent.vertex_adapter as va
    from hermes_cli import model_switch as ms

    monkeypatch.setattr(va, "has_vertex_credentials", lambda: False)
    rows = ms.list_authenticated_providers(current_provider="moa")
    assert not [r for r in rows if r.get("slug") == "vertex"]


def test_vertex_explicitly_configured_via_config_section(monkeypatch):
    """A `vertex:` config section with project_id is the explicit opt-in
    signal (vertex has no API key for check 3 to find)."""
    import hermes_cli.auth as auth
    import hermes_cli.config as config

    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})
    monkeypatch.setattr(
        config, "load_config",
        lambda *a, **k: {"model": {"provider": "moa"}, "vertex": {"project_id": "my-proj"}},
    )
    monkeypatch.delenv("VERTEX_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert auth.is_provider_explicitly_configured("vertex") is True


def test_vertex_not_explicitly_configured_when_unset(monkeypatch):
    import hermes_cli.auth as auth
    import hermes_cli.config as config

    monkeypatch.setattr(auth, "_load_auth_store", lambda: {})
    monkeypatch.setattr(
        config, "load_config",
        lambda *a, **k: {"model": {"provider": "moa"}},
    )
    monkeypatch.delenv("VERTEX_CREDENTIALS_PATH", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert auth.is_provider_explicitly_configured("vertex") is False
