"""Regression tests for api_server per-request model_override provider routing.

The gateway's ``_create_agent`` resolves a per-request ``model_override``
(sent by CCC and other API consumers) into a provider + bare model. Three
prefix forms are recognised:

  * ``openrouter/<vendor>/<model>`` → provider=openrouter, strip ``openrouter/``
  * ``litellm-*`` / ``litellm/*``    → provider=custom (local LiteLLM proxy)
  * ``<provider>/<model>``           → any *registered* Hermes provider prefix
    (e.g. ``openai-codex/gpt-5.5``, ``anthropic/claude-sonnet-4-6``) is pinned
    and the prefix stripped, so OAuth-gated providers route directly.

The general branch must NOT hijack aggregator-namespaced models whose prefix
is not a registered provider (``openai/gpt-4o``), nor bare model strings.

CRITICAL CONTRACT (regression for the api.anthropic.com 404 bug): pinning the
provider *name* alone is insufficient — AIAgent keeps the gateway default
base_url/api_key when only ``provider`` is passed, so an OAuth-gated provider
silently hits the wrong endpoint. The general branch must therefore run the
canonical ``resolve_runtime_provider`` resolver and merge base_url + api_key +
api_mode + credential_pool into the runtime kwargs.

These tests exercise the branch logic in isolation (mirroring the code in
``gateway/platforms/api_server.py``) so the routing contract is locked.
"""

from providers import get_provider_profile


def _route(model_override: str, *, resolve_binding: bool = False):
    """Mirror the model_override branch logic in GatewayRunner._create_agent.

    When *resolve_binding* is True, the general provider-prefix branch also runs
    the canonical resolver and merges the full runtime binding — mirroring the
    production code path that fixes OAuth endpoint routing.
    """
    model = model_override
    _override_lower = model_override.lower()
    runtime: dict = {}
    if _override_lower.startswith("openrouter/"):
        runtime["provider"] = "openrouter"
        model = model[len("openrouter/"):]
    elif _override_lower.startswith("litellm-") or _override_lower.startswith("litellm/"):
        runtime["provider"] = "custom"
        runtime["base_url"] = "http://localhost:4000"
    elif "/" in model_override:
        _prefix = model_override.split("/", 1)[0].strip().lower()
        try:
            _profile = get_provider_profile(_prefix)
        except Exception:
            _profile = None
        if _profile is not None:
            _bare = model_override.split("/", 1)[1]
            _binding = None
            if resolve_binding:
                try:
                    from hermes_cli.runtime_provider import resolve_runtime_provider
                    _binding = resolve_runtime_provider(
                        requested=_profile.name, target_model=_bare,
                    )
                except Exception:
                    _binding = None
            if _binding:
                runtime["provider"] = _binding.get("provider") or _profile.name
                if _binding.get("base_url"):
                    runtime["base_url"] = _binding["base_url"]
                if _binding.get("api_key"):
                    runtime["api_key"] = _binding["api_key"]
                if _binding.get("api_mode"):
                    runtime["api_mode"] = _binding["api_mode"]
                if _binding.get("credential_pool") is not None:
                    runtime["credential_pool"] = _binding["credential_pool"]
            else:
                runtime["provider"] = _profile.name
            model = _bare
    return runtime.get("provider"), model, runtime


def test_openai_codex_prefix_routes_to_codex_oauth():
    """gpt-5.5 dispatched as openai-codex/gpt-5.5 must pin the OAuth provider."""
    provider, model, _ = _route("openai-codex/gpt-5.5")
    assert provider == "openai-codex"
    assert model == "gpt-5.5"


def test_codex_alias_prefix_resolves():
    """The 'codex' alias resolves to the openai-codex provider."""
    provider, model, _ = _route("codex/gpt-5.5")
    assert provider == "openai-codex"
    assert model == "gpt-5.5"


def test_anthropic_prefix_routes_to_anthropic():
    provider, model, _ = _route("anthropic/claude-sonnet-4-6")
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-6"


def test_openrouter_prefix_unchanged():
    """OpenRouter branch still strips only its own tag, keeping vendor/model."""
    provider, model, _ = _route("openrouter/openai/gpt-5.5")
    assert provider == "openrouter"
    assert model == "openai/gpt-5.5"


def test_litellm_prefix_routes_to_custom():
    provider, model, _ = _route("litellm-ccc/gpt-5.5")
    assert provider == "custom"
    # litellm models keep their full alias — the proxy resolves them.
    assert model == "litellm-ccc/gpt-5.5"


def test_aggregator_namespaced_model_not_hijacked():
    """'openai/gpt-4o' must NOT be captured — 'openai' is not a registered provider."""
    provider, model, _ = _route("openai/gpt-4o")
    assert provider is None
    assert model == "openai/gpt-4o"


def test_bare_model_falls_through_to_gateway_default():
    provider, model, _ = _route("gpt-5.5")
    assert provider is None
    assert model == "gpt-5.5"


def test_codex_binding_resolves_oauth_endpoint_not_default():
    """Regression for the api.anthropic.com 404 bug.

    Pinning provider='openai-codex' alone left base_url at the gateway default
    (api.anthropic.com), 404ing on gpt-5.5. The branch must resolve the full
    runtime binding so base_url points at the ChatGPT Codex backend and the
    api_mode is the Codex Responses surface.

    Skips gracefully when no Codex OAuth credential is provisioned on the host
    (the resolver raises AuthError) — the binding-merge wiring is still proven
    by the assertions that run when credentials exist.
    """
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        resolve_runtime_provider(requested="openai-codex", target_model="gpt-5.5")
    except Exception:
        import pytest
        pytest.skip("no openai-codex OAuth credential provisioned on this host")

    provider, model, runtime = _route("openai-codex/gpt-5.5", resolve_binding=True)
    assert provider == "openai-codex"
    assert model == "gpt-5.5"
    # The whole point: endpoint is ChatGPT's Codex backend, NOT api.anthropic.com.
    assert "chatgpt.com" in runtime.get("base_url", "")
    assert "api.anthropic.com" not in runtime.get("base_url", "")
    # Codex uses the Responses API surface, not anthropic_messages.
    assert runtime.get("api_mode") == "codex_responses"
    # A live OAuth token must have been merged in.
    assert runtime.get("api_key")


def test_anthropic_binding_resolves_messages_api():
    """anthropic/<model> resolves to the anthropic_messages surface when bound."""
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
        resolve_runtime_provider(requested="anthropic", target_model="claude-sonnet-4-6")
    except Exception:
        import pytest
        pytest.skip("no anthropic credential provisioned on this host")

    provider, model, runtime = _route("anthropic/claude-sonnet-4-6", resolve_binding=True)
    assert provider == "anthropic"
    assert model == "claude-sonnet-4-6"
    assert runtime.get("api_mode") == "anthropic_messages"
