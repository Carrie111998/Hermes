"""Regression tests for /model support of config.yaml custom_providers.

The terminal `hermes model` flow already exposes `custom_providers`, but the
shared slash-command pipeline (`/model` in CLI/gateway/Telegram) historically
only looked at `providers:`.
"""

import hermes_cli.providers as providers_mod
import pytest
from hermes_cli.model_switch import list_authenticated_providers, switch_model
from hermes_cli.providers import resolve_provider_full


_MOCK_VALIDATION = {
    "accepted": True,
    "persist": True,
    "recognized": True,
    "message": None,
}


@pytest.fixture(autouse=True)
def _disable_live_custom_provider_model_probe(monkeypatch):
    """Keep custom-provider picker fixtures independent of local model servers."""
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        "hermes_cli.models.cached_provider_model_ids", lambda *_a, **_kw: []
    )
    monkeypatch.setattr(
        "hermes_cli.models.provider_model_ids", lambda *_a, **_kw: []
    )


def test_list_authenticated_providers_includes_custom_providers(monkeypatch):
    """No-args /model menus should include saved custom_providers entries."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(providers_mod, "HERMES_OVERLAYS", {})
    monkeypatch.setattr("hermes_cli.models.fetch_api_models", lambda *a, **k: [])

    providers = list_authenticated_providers(
        current_provider="openai-codex",
        user_providers={},
        custom_providers=[
            {
                "name": "Local (127.0.0.1:4141)",
                "base_url": "http://127.0.0.1:4141/v1",
                "model": "rotator-openrouter-coding",
            }
        ],
        max_models=50,
    )

    assert any(
        p["slug"] == "custom:local-(127.0.0.1:4141)"
        and p["name"] == "Local (127.0.0.1:4141)"
        and p["models"] == ["rotator-openrouter-coding"]
        and p["api_url"] == "http://127.0.0.1:4141/v1"
        for p in providers
    )








def test_is_routing_aggregator_excludes_flat_namespace_resellers():
    """opencode-go / opencode-zen stay ``is_aggregator=True`` (model-switch
    relies on it to search their flat bare-name catalog), but they are NOT
    routing aggregators — their models are first-party, so the picker dedup
    must not strip them. (#47077)"""
    # Still aggregators for model-switch flat-catalog resolution.
    assert providers_mod.is_aggregator("opencode-go") is True
    assert providers_mod.is_aggregator("opencode-zen") is True
    # But NOT routing aggregators for picker-dedup purposes.
    assert providers_mod.is_routing_aggregator("opencode-go") is False
    assert providers_mod.is_routing_aggregator("opencode-zen") is False
    # True routers and custom proxies remain routing aggregators.
    assert providers_mod.is_routing_aggregator("openrouter") is True
    assert providers_mod.is_routing_aggregator("custom:litellm") is True
    assert providers_mod.is_routing_aggregator("not-a-provider") is False


def test_picker_selection_resolves_named_custom_provider_model_id(monkeypatch):
    """Picker prefixes must not leak into a named custom provider API model id."""
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "api_key": "test-key",
            "base_url": "https://token.sensenova.cn/v1",
            "api_mode": "chat_completions",
        },
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: _MOCK_VALIDATION,
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities",
        lambda *a, **k: None,
    )

    result = switch_model(
        raw_input="sensenova/deepseek-v4-flash",
        current_provider="openai-codex",
        current_model="gpt-5.4",
        explicit_provider="custom:sensenova",
        user_providers={},
        custom_providers=[
            {
                "name": "sensenova",
                "base_url": "https://token.sensenova.cn/v1",
                "models": [
                    {"id": "deepseek-v4-flash", "name": "deepseek-v4-flash"}
                ],
            }
        ],
    )

    assert result.success is True
    assert result.target_provider == "custom:sensenova"
    assert result.new_model == "deepseek-v4-flash"








# ─────────────────────────────────────────────────────────────────────────────
# #9210: group custom_providers by (base_url, api_key) in /model picker
# ─────────────────────────────────────────────────────────────────────────────


def test_list_authenticated_providers_bare_custom_slug_recovers(monkeypatch):
    """Regression for #17478: when a prior failed switch left the bare
    literal "custom" in model.provider, the picker must NOT propagate
    that broken slug. It must fall back to the canonical
    ``custom:<name>`` form so the picker stays usable."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(providers_mod, "HERMES_OVERLAYS", {})

    providers = list_authenticated_providers(
        current_provider="custom",
        current_base_url="http://localhost:11434/v1",
        user_providers={},
        custom_providers=[
            {"name": "Ollama — GLM 5.1", "base_url": "http://localhost:11434/v1",
             "api_key": "ollama", "model": "glm-5.1"},
        ],
        max_models=50,
    )

    matches = [p for p in providers if p.get("is_user_defined")]
    assert len(matches) == 1
    group = matches[0]
    # Canonical slug, NOT the bare "custom" that caused #17478
    assert group["slug"] == "custom:ollama"
    assert group["is_current"] is True




def test_custom_providers_uses_live_models_for_multi_model_endpoint(monkeypatch):
    """Custom providers with api_key + base_url should prefer live /models.

    Custom providers (section 4 of list_authenticated_providers) point at
    gateways like Bifrost that expose hundreds of models.  Reading only the
    static ``models:`` dict from config.yaml leaves the /model picker with
    a stale subset.  Live discovery fills the picker with all available
    models from the endpoint.
    """
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})

    calls = []

    def fake_fetch_api_models(api_key, base_url, **kwargs):
        calls.append((api_key, base_url, kwargs))
        return ["gateway-model-a", "gateway-model-b", "gateway-model-c"]

    monkeypatch.setattr("hermes_cli.models.fetch_api_models", fake_fetch_api_models)

    custom_providers = [
        {
            "name": "my-gateway",
            "api_key": "sk-gateway-key",
            "base_url": "https://gateway.example.com/v1",
            "model": "gateway-model-a",
            "models": {
                "gateway-model-a": {"context_length": 128000},
                "gateway-model-b": {"context_length": 128000},
            },
        }
    ]

    providers = list_authenticated_providers(
        current_provider="openrouter",
        current_base_url="https://openrouter.ai/api/v1",
        custom_providers=custom_providers,
        max_models=50,
    )

    gateway_prov = next(
        (
            p
            for p in providers
            if p.get("api_url") == "https://gateway.example.com/v1"
        ),
        None,
    )

    assert gateway_prov is not None, "Custom provider group not found in results"
    assert calls == [
        ("sk-gateway-key", "https://gateway.example.com/v1", {"headers": None})
    ], "fetch_api_models must be called with the custom provider's credentials"
    assert gateway_prov["models"] == [
        "gateway-model-a",
        "gateway-model-b",
        "gateway-model-c",
    ], "Live models must replace the static subset"
    assert gateway_prov["total_models"] == 3


def test_same_endpoint_different_extra_headers_not_collapsed(monkeypatch):
    """Entries sharing (api_url, credential, api_mode) but declaring different
    extra_headers must NOT collapse into one picker row — each is a distinct
    header-authenticated endpoint (e.g. per-tenant routing behind one proxy)
    and must probe /models with its own headers."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})

    calls = []

    def fake_fetch_api_models(api_key, base_url, **kwargs):
        calls.append((api_key, base_url, kwargs.get("headers")))
        # Return a per-tenant model list keyed by the routing header so we can
        # assert each row got its OWN probe rather than a shared one.
        tenant = (kwargs.get("headers") or {}).get("X-Tenant", "none")
        return [f"model-{tenant}"]

    monkeypatch.setattr("hermes_cli.models.fetch_api_models", fake_fetch_api_models)

    providers = list_authenticated_providers(
        current_provider="openrouter",
        current_base_url="https://openrouter.ai/api/v1",
        custom_providers=[
            {
                "name": "Proxy Tenant A",
                "api_key": "shared-key",
                "base_url": "http://localhost:8081/v1",
                "extra_headers": {"X-Tenant": "a"},
            },
            {
                "name": "Proxy Tenant B",
                "api_key": "shared-key",
                "base_url": "http://localhost:8081/v1",
                "extra_headers": {"X-Tenant": "b"},
            },
        ],
        max_models=50,
    )

    rows = [
        p for p in providers if p.get("api_url") == "http://localhost:8081/v1"
    ]
    # Two distinct rows, not one collapsed row.
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}: {rows}"

    # Each tenant was probed with its OWN header set (order-independent).
    assert ("shared-key", "http://localhost:8081/v1", {"X-Tenant": "a"}) in calls
    assert ("shared-key", "http://localhost:8081/v1", {"X-Tenant": "b"}) in calls

    # Each row surfaces the model list its own headers unlocked.
    models_by_row = {tuple(r["models"]) for r in rows}
    assert models_by_row == {("model-a",), ("model-b",)}






def test_resolve_custom_provider_passes_key_env():
    """resolve_custom_provider should propagate key_env into api_key_env_vars.

    Regression: previously api_key_env_vars was always (), silently dropping
    the configured env var and causing 401s on every request.
    """
    from hermes_cli.providers import resolve_custom_provider

    resolved = resolve_custom_provider(
        "custom:token-plan",
        custom_providers=[
            {
                "name": "token-plan",
                "base_url": "https://token-plan-sgp.xiaomimimo.com/v1",
                "key_env": "XIAOMI_MIMO_API_KEY",
                "model": "mimo-v2-pro",
            }
        ],
    )

    assert resolved is not None
    assert resolved.api_key_env_vars == ("XIAOMI_MIMO_API_KEY",)
    assert resolved.base_url == "https://token-plan-sgp.xiaomimimo.com/v1"


def test_discovered_models_auto_saved_to_cache(monkeypatch):
    """Discovered models are persisted to config so ``discover_models: false``
    has a populated cache on the next read (#65652).

    When a successful probe returns live models, ``_save_discovered_models_to_config``
    must be called with the provider's base_url and the discovered model list.
    """
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr("hermes_cli.providers.HERMES_OVERLAYS", {})

    save_calls = []

    def fake_fetch_api_models(api_key, base_url, **kwargs):
        return ["discovered-a", "discovered-b", "discovered-c"]

    monkeypatch.setattr("hermes_cli.models.fetch_api_models", fake_fetch_api_models)
    monkeypatch.setattr(
        "hermes_cli.model_switch._save_discovered_models_to_config",
        lambda api_url, model_ids: save_calls.append((api_url, model_ids)),
    )

    custom_providers = [
        {
            "name": "my-gateway",
            "api_key": "***",
            "base_url": "https://gateway.example.com/v1",
            "discover_models": True,
            "model": "only-model",
            "models": {"only-model": {"context_length": 128000}},
        }
    ]

    providers = list_authenticated_providers(
        current_provider="my-gateway",
        current_base_url="https://gateway.example.com/v1",
        custom_providers=custom_providers,
        max_models=50,
        probe_custom_providers=True,
    )

    assert len(save_calls) == 1, (
        "_save_discovered_models_to_config must be called after a successful probe"
    )
    assert save_calls[0][0] == "https://gateway.example.com/v1"
    assert save_calls[0][1] == ["discovered-a", "discovered-b", "discovered-c"]

    gateway_prov = next(
        (p for p in providers if p.get("api_url") == "https://gateway.example.com/v1"),
        None,
    )
    assert gateway_prov is not None
    assert gateway_prov["models"] == ["discovered-a", "discovered-b", "discovered-c"]




def test_save_discovered_models_preserves_dict_form(monkeypatch):
    """``_save_discovered_models_to_config`` must not replace a dict-form
    ``models`` mapping (per-model metadata like ``context_length``) with
    a flat list of strings (#67841)."""
    from hermes_cli.model_switch import _save_discovered_models_to_config

    save_calls = []

    def fake_save(config):
        save_calls.append(dict(config))

    monkeypatch.setattr("hermes_cli.config.save_config", fake_save)
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "custom_providers": [
                {
                    "name": "my-gateway",
                    "base_url": "https://gateway.example.com/v1",
                    "models": {
                        "configured-model": {"context_length": 8192},
                    },
                }
            ]
        },
    )

    # Dict-form models must NOT be overwritten by discovered models
    _save_discovered_models_to_config(
        "https://gateway.example.com/v1",
        ["configured-model", "discovered-model"],
    )
    assert save_calls == [], (
        "Dict-form models must not be replaced with a flat list"
    )


def test_shared_url_different_display_names_are_separate_rows(monkeypatch):
    """Multiple custom_providers entries sharing base_url + api_key + api_mode
    but with *different* display-name prefixes (e.g. a proxy fronting
    cerebras, groq and perplexity at one URL) must each get their own picker
    row, not collapse into one."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(providers_mod, "HERMES_OVERLAYS", {})
    # Stub live discovery so the test is deterministic regardless of network.
    monkeypatch.setattr(
        "hermes_cli.models.fetch_api_models",
        lambda api_key, base_url, **kwargs: [],
    )

    providers = list_authenticated_providers(
        current_provider="openrouter",
        current_base_url="https://openrouter.ai/api/v1",
        user_providers={},
        custom_providers=[
            {"name": "Cerebras", "base_url": "https://proxy.example.com/v1",
             "api_key": "proxy-key", "model": "llama-4-scout"},
            {"name": "Groq", "base_url": "https://proxy.example.com/v1",
             "api_key": "proxy-key", "model": "llama-4-scout"},
            {"name": "Perplexity", "base_url": "https://proxy.example.com/v1",
             "api_key": "proxy-key", "model": "sonar-pro"},
        ],
        max_models=50,
    )

    custom = [p for p in providers if p.get("is_user_defined")]
    names = sorted(p["name"] for p in custom)
    assert names == ["Cerebras", "Groq", "Perplexity"], (
        f"expected three separate rows, got {names}"
    )
    # Each row carries only its own model (no cross-contamination).
    by_name = {p["name"]: p["models"] for p in custom}
    assert by_name["Cerebras"] == ["llama-4-scout"]
    assert by_name["Groq"] == ["llama-4-scout"]
    assert by_name["Perplexity"] == ["sonar-pro"]


def test_excluded_providers_hides_builtin_row(monkeypatch):
    """``excluded_providers`` must hide a built-in provider row that would
    otherwise surface when its credentials are present."""
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(providers_mod, "HERMES_OVERLAYS", {})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")

    baseline = list_authenticated_providers(
        current_provider="openrouter",
        current_base_url="https://openrouter.ai/api/v1",
        user_providers={},
        custom_providers=[],
        max_models=50,
    )
    assert any(p["slug"] == "openrouter" for p in baseline), (
        "sanity: openrouter row must appear when OPENROUTER_API_KEY is set"
    )

    filtered = list_authenticated_providers(
        current_provider="openrouter",
        current_base_url="https://openrouter.ai/api/v1",
        user_providers={},
        custom_providers=[],
        max_models=50,
        excluded_providers=["openrouter"],
    )
    assert not any(p["slug"] == "openrouter" for p in filtered), (
        "excluded_providers=['openrouter'] must hide the openrouter row"
    )


# ---------------------------------------------------------------------------
# Same-provider bare custom: keep session endpoint (no OpenRouter fallthrough)
# ---------------------------------------------------------------------------


def _clear_provider_env(monkeypatch) -> None:
    for key in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_BASE_URL",
        "OPENAI_API_KEY",
        "CUSTOM_BASE_URL",
        "CUSTOM_API_KEY",
        "HERMES_INFERENCE_PROVIDER",
    ):
        monkeypatch.delenv(key, raising=False)


def test_same_provider_bare_custom_keeps_session_endpoint(tmp_path, monkeypatch):
    """Same-provider /model on bare custom must not fall through to OpenRouter.

    Without a trustworthy config model.base_url, resolve_runtime_provider("custom")
    defaults to OpenRouter. Session-only custom endpoints must keep current_*.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: _MOCK_VALIDATION,
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    session_url = "https://my-private-llm.example.com/v1"
    session_key = "sk-session-custom-key"
    result = switch_model(
        raw_input="model-b",
        current_provider="custom",
        current_model="model-a",
        current_api_key=session_key,
        current_base_url=session_url,
    )

    assert result.success is True
    assert result.base_url.rstrip("/") == session_url
    assert result.api_key == session_key
    assert "openrouter.ai" not in (result.base_url or "").lower()


def test_same_provider_bare_custom_passes_session_creds_to_resolve(
    tmp_path, monkeypatch
):
    """Bare-custom same-provider resolve must receive explicit session URL/key."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: _MOCK_VALIDATION,
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    captured: dict = {}

    def _fake_resolve(**kwargs):
        captured.update(kwargs)
        return {
            "provider": "custom",
            "api_key": kwargs.get("explicit_api_key") or "",
            "base_url": kwargs.get("explicit_base_url") or "",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_resolve
    )

    session_url = "https://my-private-llm.example.com/v1"
    session_key = "sk-session-custom-key"
    result = switch_model(
        raw_input="model-b",
        current_provider="custom",
        current_model="model-a",
        current_api_key=session_key,
        current_base_url=session_url,
    )

    assert result.success is True
    assert captured.get("explicit_base_url") == session_url
    assert captured.get("explicit_api_key") == session_key
    assert captured.get("requested") == "custom"


def test_same_provider_named_provider_does_not_force_session_url(
    tmp_path, monkeypatch
):
    """Non-custom same-provider switches must not pin explicit session URL.

    OpenCode and similar providers still need resolve-time base_url adjustments.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: _MOCK_VALIDATION,
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    captured: dict = {}

    def _fake_resolve(**kwargs):
        captured.update(kwargs)
        return {
            "provider": "opencode-go",
            "api_key": "sk-rotated",
            "base_url": "https://api.opencode-go.com/v2",
            "api_mode": "chat_completions",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_resolve
    )

    result = switch_model(
        raw_input="kimi-k2.5",
        current_provider="opencode-go",
        current_model="mimo-v2.5",
        current_api_key="sk-old",
        current_base_url="https://api.opencode-go.com/v1",
    )

    assert result.success is True
    assert "explicit_base_url" not in captured or captured.get("explicit_base_url") in (
        None,
        "",
    )
    assert result.base_url == "https://api.opencode-go.com/v2"
    assert result.api_key == "sk-rotated"


def test_same_provider_empty_resolve_does_not_refill_from_session(
    tmp_path, monkeypatch
):
    """Empty resolver output must not be refilled from current_* here.

    Broad empty-result credential preservation is owned by #44502. This PR
    only blocks non-empty OpenRouter fallthrough for bare custom/local via
    explicit_*; non-custom same-provider switches keep resolver values as-is.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: _MOCK_VALIDATION,
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **kwargs: {
            "provider": "opencode-go",
            "api_key": "",
            "base_url": "",
            "api_mode": "",
        },
    )

    result = switch_model(
        raw_input="kimi-k2.5",
        current_provider="opencode-go",
        current_model="mimo-v2.5",
        current_api_key="sk-session-key",
        current_base_url="https://api.opencode-go.com/v1",
    )

    assert result.success is True
    assert result.api_key != "sk-session-key"
    assert (result.base_url or "") != "https://api.opencode-go.com/v1"


def test_same_provider_named_custom_adopts_rotated_config_creds(
    tmp_path, monkeypatch
):
    """Named custom:* same-provider /model must re-resolve config rotation.

    Unlike bare custom, custom:relay has a durable config identity. Stale
    session URL/key must not pin over a rotated configured endpoint/key.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        "custom_providers:\n"
        "- name: relay\n"
        "  base_url: https://replacement.example/v1\n"
        "  api_key: rotated-config-key\n"
        "  model: model-a\n"
    )
    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model",
        lambda *a, **k: _MOCK_VALIDATION,
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    captured: dict = {}
    from hermes_cli.runtime_provider import resolve_runtime_provider as _real_resolve

    def _spy_resolve(**kwargs):
        captured.update(kwargs)
        return _real_resolve(**kwargs)

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _spy_resolve
    )

    result = switch_model(
        raw_input="model-b",
        current_provider="custom:relay",
        current_model="model-a",
        current_api_key="old-session-key",
        current_base_url="https://retired.example/v1",
        custom_providers=[
            {
                "name": "relay",
                "base_url": "https://replacement.example/v1",
                "api_key": "rotated-config-key",
                "model": "model-a",
            }
        ],
    )

    assert result.success is True
    assert "explicit_base_url" not in captured or captured.get("explicit_base_url") in (
        None,
        "",
    )
    assert "explicit_api_key" not in captured or captured.get("explicit_api_key") in (
        None,
        "",
    )
    assert result.base_url.rstrip("/") == "https://replacement.example/v1"
    assert result.api_key == "rotated-config-key"


def test_same_provider_direct_alias_does_not_send_session_key_to_alias_host(
    tmp_path, monkeypatch
):
    """Direct-alias host change must not reuse the prior session credential.

    Bare-custom same-provider switches preserve session URL/key to avoid
    OpenRouter fallthrough. When a same-provider direct alias then replaces
    only base_url, that preserved key must not be validated or returned for
    the alias host.
    """
    import hermes_cli.model_switch as ms
    from hermes_cli.model_switch import DirectAlias

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)

    session_url = "https://endpoint-a.example/v1"
    session_key = "sk-endpoint-a-private"
    alias_url = "https://endpoint-b.example/v1"

    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {
            "alias-b": DirectAlias("model-on-b", "custom", alias_url),
        },
    )
    monkeypatch.setattr(
        ms,
        "resolve_alias",
        lambda raw, prov: ("custom", "model-on-b", "alias-b"),
    )

    validation_calls: list[dict] = []

    def _capture_validate(model_name, provider, **kwargs):
        validation_calls.append(
            {
                "model_name": model_name,
                "provider": provider,
                "api_key": kwargs.get("api_key"),
                "base_url": kwargs.get("base_url"),
            }
        )
        return _MOCK_VALIDATION

    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model", _capture_validate
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    result = switch_model(
        raw_input="alias-b",
        current_provider="custom",
        current_model="model-a",
        current_api_key=session_key,
        current_base_url=session_url,
    )

    assert result.success is True
    assert result.base_url.rstrip("/") == alias_url.rstrip("/")
    assert result.api_key != session_key
    assert validation_calls, "expected validate_requested_model to run"
    for call in validation_calls:
        assert (call["base_url"] or "").rstrip("/") == alias_url.rstrip("/")
        assert call["api_key"] != session_key


def test_same_provider_direct_alias_resolves_authenticated_endpoint_b_key(
    tmp_path, monkeypatch
):
    """URL-changing direct alias must resolve endpoint B's configured key.

    Clearing A's session credential is required, but immediately forcing
    no-key-required rejects authenticated aliases (e.g. host-derived vendor
    keys / Ollama Cloud) even when B's key is configured.
    """
    import hermes_cli.model_switch as ms
    from hermes_cli.model_switch import DirectAlias

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)

    session_url = "https://endpoint-a.example/v1"
    session_key = "sk-endpoint-a-private"
    alias_url = "https://api.deepseek.com/v1"
    endpoint_b_key = "sk-endpoint-b-deepseek"

    monkeypatch.setenv("DEEPSEEK_API_KEY", endpoint_b_key)
    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {
            "alias-b": DirectAlias("deepseek-chat", "custom", alias_url),
        },
    )
    monkeypatch.setattr(
        ms,
        "resolve_alias",
        lambda raw, prov: ("custom", "deepseek-chat", "alias-b"),
    )

    validation_calls: list[dict] = []

    def _capture_validate(model_name, provider, **kwargs):
        validation_calls.append(
            {
                "model_name": model_name,
                "provider": provider,
                "api_key": kwargs.get("api_key"),
                "base_url": kwargs.get("base_url"),
            }
        )
        return _MOCK_VALIDATION

    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model", _capture_validate
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    result = switch_model(
        raw_input="alias-b",
        current_provider="custom",
        current_model="model-a",
        current_api_key=session_key,
        current_base_url=session_url,
    )

    assert result.success is True
    assert result.base_url.rstrip("/") == alias_url.rstrip("/")
    assert result.api_key == endpoint_b_key
    assert result.api_key != session_key
    assert result.api_key != "no-key-required"
    assert validation_calls, "expected validate_requested_model to run"
    for call in validation_calls:
        assert (call["base_url"] or "").rstrip("/") == alias_url.rstrip("/")
        assert call["api_key"] == endpoint_b_key


def test_same_provider_direct_alias_resolves_ollama_cloud_key(
    tmp_path, monkeypatch
):
    """Authenticated Ollama Cloud aliases must pick up OLLAMA_API_KEY for B."""
    import hermes_cli.model_switch as ms
    from hermes_cli.model_switch import DirectAlias

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _clear_provider_env(monkeypatch)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    session_url = "https://endpoint-a.example/v1"
    session_key = "sk-endpoint-a-private"
    alias_url = "https://ollama.com/v1"
    ollama_key = "sk-ollama-cloud-b"

    monkeypatch.setenv("OLLAMA_API_KEY", ollama_key)
    monkeypatch.setattr(
        ms,
        "DIRECT_ALIASES",
        {
            "cloud-llama": DirectAlias("llama3.2", "custom", alias_url),
        },
    )
    monkeypatch.setattr(
        ms,
        "resolve_alias",
        lambda raw, prov: ("custom", "llama3.2", "cloud-llama"),
    )

    validation_calls: list[dict] = []

    def _capture_validate(model_name, provider, **kwargs):
        validation_calls.append(
            {
                "api_key": kwargs.get("api_key"),
                "base_url": kwargs.get("base_url"),
            }
        )
        return _MOCK_VALIDATION

    monkeypatch.setattr(
        "hermes_cli.models.validate_requested_model", _capture_validate
    )
    monkeypatch.setattr("hermes_cli.model_switch.get_model_info", lambda *a, **k: None)
    monkeypatch.setattr(
        "hermes_cli.model_switch.get_model_capabilities", lambda *a, **k: None
    )

    result = switch_model(
        raw_input="cloud-llama",
        current_provider="custom",
        current_model="model-a",
        current_api_key=session_key,
        current_base_url=session_url,
    )

    assert result.success is True
    assert result.base_url.rstrip("/") == alias_url.rstrip("/")
    assert result.api_key == ollama_key
    assert result.api_key != session_key
    assert validation_calls
    for call in validation_calls:
        assert call["api_key"] == ollama_key
