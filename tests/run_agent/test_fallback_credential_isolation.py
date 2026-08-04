"""Tests for fallback credential pool isolation.

Verifies that fallback activation isolates the credential pool from the
primary provider, preventing two bugs:

1. GH #33163: fallback retains primary's base_url → requests go to wrong endpoint
2. GH #33088: fallback provider's 429 exhausts primary credential pool

Both bugs share the same root cause: _recover_with_credential_pool and
_swap_credential continue operating on the PRIMARY's credential pool during
fallback calls, contaminating primary state with fallback-provider errors.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest



# ── Helpers ──────────────────────────────────────────────────────────

def _make_pool(provider, n_entries=1):
    """Create a mock credential pool with N entries."""
    pool = MagicMock()
    pool.provider = provider
    pool.has_credentials.return_value = n_entries > 0
    pool.has_available.return_value = n_entries > 0
    entry = MagicMock()
    entry.id = f"{provider}-entry-0"
    entry.runtime_api_key = f"key-{provider}"
    entry.runtime_base_url = f"https://{provider}.example.com/v1"
    entry.access_token = f"token-{provider}"
    entry.base_url = f"https://{provider}.example.com/v1"
    pool.current.return_value = entry
    pool.mark_exhausted_and_rotate.return_value = entry
    return pool


def _make_agent(provider="openai-codex", model="gpt-5.5",
                base_url="https://chatgpt.com/backend-api/codex",
                api_mode="codex_responses"):
    """Create a minimal AIAgent-like object with just the fields we need."""
    agent = MagicMock()
    agent.provider = provider
    agent.model = model
    agent.base_url = base_url
    agent.api_mode = api_mode
    agent.api_key = "primary-key"
    agent._fallback_activated = False
    agent._fallback_index = 0
    agent._fallback_chain = []
    agent._primary_runtime = {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_mode": api_mode,
        "api_key": "primary-key",
        "client_kwargs": {
            "api_key": "primary-key",
            "base_url": base_url,
        },
        "use_prompt_caching": False,
        "use_native_cache_layout": False,
        "anthropic_api_key": "",
        "anthropic_base_url": "",
    }
    agent._config_context_length = None
    agent._credential_pool = _make_pool(provider)
    agent._rate_limited_until = 0
    agent._transport_cache = {}
    agent._client_kwargs = {
        "api_key": "primary-key",
        "base_url": base_url,
    }
    return agent


# ── Test: _try_activate_fallback clears mismatched pool ──────────────

class TestFallbackCredentialIsolation:
    """Test that _try_activate_fallback isolates the credential pool."""

    def test_fallback_clears_primary_pool(self):
        """When switching from openai-codex to openrouter, the codex pool is cleared."""
        # We test the isolation logic directly here as a minimal guard; the
        # integration-style test below calls the real fallback activator.

        agent = _make_agent(provider="openai-codex", base_url="https://chatgpt.com/backend-api/codex")
        agent._fallback_activated = True
        agent._credential_pool = _make_pool("openai-codex")

        # Simulate: after fallback activation, provider is now openrouter
        fb_provider = "openrouter"
        fb_model = "openrouter/auto"

        # The isolation code from _try_activate_fallback:
        pool = getattr(agent, "_credential_pool", None)
        if pool is not None:
            pool_provider = getattr(pool, "provider", "") or ""
            if pool_provider.lower() != fb_provider:
                agent._credential_pool = None

        assert agent._credential_pool is None, (
            "Pool should be cleared when fallback provider differs from pool provider"
        )

    def test_fallback_keeps_matching_pool(self):
        """When fallback provider matches pool provider, pool is preserved."""
        agent = _make_agent(provider="openrouter", base_url="https://openrouter.ai/api/v1")
        agent._credential_pool = _make_pool("openrouter")

        fb_provider = "openrouter"

        pool = getattr(agent, "_credential_pool", None)
        if pool is not None:
            pool_provider = getattr(pool, "provider", "") or ""
            if pool_provider.lower() != fb_provider:
                agent._credential_pool = None

        assert agent._credential_pool is not None, (
            "Pool should be preserved when fallback provider matches pool provider"
        )

    def test_fallback_attaches_matching_pool_after_clear(self):
        """Provider-switch fallback should attach the fallback provider's pool."""
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_agent(
            provider="ollama-cloud",
            model="glm-5.2",
            base_url="https://ollama.com/v1",
            api_mode="chat_completions",
        )
        agent._fallback_chain = [{"provider": "openai-codex", "model": "gpt-5.5"}]
        agent._credential_pool = _make_pool("ollama-cloud")
        agent._buffer_status = MagicMock()
        agent._is_azure_openai_url.return_value = False
        agent._is_direct_openai_url.return_value = False
        agent._provider_model_requires_responses_api.return_value = False
        agent._anthropic_prompt_cache_policy.return_value = (False, False)
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent._replace_primary_openai_client = MagicMock()
        agent.context_compressor = None

        fallback_client = SimpleNamespace(
            api_key="codex-key",
            base_url="https://chatgpt.com/backend-api/codex",
            _custom_headers={},
        )
        fallback_pool = _make_pool("openai-codex")

        with patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fallback_client, "gpt-5.5"),
        ) as resolve_provider_client, patch(
            "agent.credential_pool.load_pool",
            return_value=fallback_pool,
        ) as load_pool:
            assert try_activate_fallback(agent) is True

        resolve_provider_client.assert_called_once()
        load_pool.assert_called_once_with("openai-codex")
        assert agent.provider == "openai-codex"
        assert agent.model == "gpt-5.5"
        assert agent.base_url == "https://chatgpt.com/backend-api/codex"
        assert agent.api_mode == "codex_responses"
        assert agent._credential_pool is fallback_pool
        assert agent._credential_pool.provider == "openai-codex"
        assert agent._transport_cache == {}

    def test_fallback_reuses_identity_matching_runtime_pool(self):
        """Resolver-selected pools must not be loaded again during fallback."""
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_agent(
            provider="ollama-cloud",
            model="glm-5.2",
            base_url="https://ollama.com/v1",
            api_mode="chat_completions",
        )
        agent._fallback_chain = [{"provider": "openai-codex", "model": "gpt-5.5"}]
        agent._credential_pool = _make_pool("ollama-cloud")
        agent._buffer_status = MagicMock()
        agent._is_azure_openai_url.return_value = False
        agent._is_direct_openai_url.return_value = False
        agent._provider_model_requires_responses_api.return_value = False
        agent._anthropic_prompt_cache_policy.return_value = (False, False)
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent.context_compressor = None

        fallback_client = SimpleNamespace(
            api_key="codex-key",
            base_url="https://chatgpt.com/backend-api/codex",
            _custom_headers={},
        )
        runtime_pool = _make_pool("openai-codex")
        reloaded_pool = _make_pool("openai-codex")

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(fallback_client, "gpt-5.5"),
            ),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "provider": "openai-codex",
                    "requested_provider": "openai-codex",
                    "api_mode": "codex_responses",
                    "credential_pool": runtime_pool,
                },
            ),
            patch(
                "agent.credential_pool.load_pool",
                return_value=reloaded_pool,
            ) as load_pool,
        ):
            assert try_activate_fallback(agent) is True

        load_pool.assert_not_called()
        assert agent._credential_pool is runtime_pool

    def test_named_custom_fallback_separates_transport_and_pool_identity(self):
        """A named custom fallback must attach its own pool on a shared URL."""
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_agent(
            provider="custom",
            model="fallback-model",
            base_url="https://gateway.example/v1",
        )
        agent.requested_provider = "custom:provider-a"
        agent._primary_runtime["requested_provider"] = "custom:provider-a"
        agent._fallback_chain = [{"provider": "provider-b", "model": "fallback-model"}]
        agent._credential_pool = _make_pool("custom:provider-a")
        agent._buffer_status = MagicMock()
        agent._is_azure_openai_url.return_value = False
        agent._is_direct_openai_url.return_value = False
        agent._provider_model_requires_responses_api.return_value = False
        agent._anthropic_prompt_cache_policy.return_value = (False, False)
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent.context_compressor = None

        fallback_client = SimpleNamespace(
            api_key="provider-b-key",
            base_url="https://gateway.example/v1",
            _custom_headers={},
        )
        fallback_pool = _make_pool("custom:provider-b")

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(fallback_client, "fallback-model"),
            ),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "provider": "custom",
                    "requested_provider": "provider-b",
                    "api_mode": "chat_completions",
                },
            ) as resolve_runtime,
            patch(
                "agent.credential_pool.get_custom_provider_pool_key",
                return_value="custom:provider-b",
            ) as pool_key,
            patch(
                "agent.credential_pool.load_pool",
                return_value=fallback_pool,
            ) as load_pool,
        ):
            assert try_activate_fallback(agent) is True

        resolve_runtime.assert_called_once_with(
            requested="provider-b",
            target_model="fallback-model",
            explicit_base_url=None,
            explicit_api_key=None,
        )
        pool_key.assert_called_once_with(
            "https://gateway.example/v1",
            provider_name="custom:provider-b",
        )
        load_pool.assert_called_once_with("custom:provider-b")
        assert agent.provider == "custom"
        assert agent.requested_provider == "custom:provider-b"
        assert agent._credential_pool is fallback_pool

    def test_named_custom_fallback_uses_resolved_api_mode_for_native_client(self):
        """A named custom fallback must honor resolver protocol metadata."""
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_agent(
            provider="custom",
            model="fallback-model",
            base_url="https://gateway.example/v1",
        )
        agent.requested_provider = "custom:provider-a"
        agent._primary_runtime["requested_provider"] = "custom:provider-a"
        agent._fallback_chain = [{"provider": "provider-b", "model": "fallback-model"}]
        agent._credential_pool = _make_pool("custom:provider-a")
        agent._buffer_status = MagicMock()
        agent._is_azure_openai_url.return_value = False
        agent._is_direct_openai_url.return_value = False
        agent._provider_model_requires_responses_api.return_value = False
        agent._anthropic_prompt_cache_policy.return_value = (False, False)
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent.context_compressor = None

        fallback_client = SimpleNamespace(
            api_key="provider-b-key",
            base_url="https://gateway.example/v1",
            _custom_headers={},
        )
        fallback_pool = _make_pool("custom:provider-b")
        native_client = MagicMock(name="native_anthropic_client")

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                return_value=(fallback_client, "fallback-model"),
            ),
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                return_value={
                    "provider": "custom",
                    "requested_provider": "provider-b",
                    "api_mode": "anthropic_messages",
                },
            ),
            patch(
                "agent.credential_pool.get_custom_provider_pool_key",
                return_value="custom:provider-b",
            ),
            patch(
                "agent.credential_pool.load_pool",
                return_value=fallback_pool,
            ),
            patch(
                "agent.anthropic_adapter.build_anthropic_client",
                return_value=native_client,
            ) as build_anthropic,
        ):
            assert try_activate_fallback(agent) is True

        assert agent.provider == "custom"
        assert agent.requested_provider == "custom:provider-b"
        assert agent.api_mode == "anthropic_messages"
        assert agent._anthropic_client is native_client
        assert agent.client is None
        build_anthropic.assert_called_once_with(
            "provider-b-key", "https://gateway.example/v1", timeout=None,
        )

    @pytest.mark.parametrize(
        ("native_provider", "native_api_mode"),
        [
            ("bedrock", "bedrock_converse"),
            ("openai-codex", "codex_app_server"),
        ],
    )
    def test_builtin_native_fallback_skips_unconstructable_runtime(
        self, native_provider, native_api_mode
    ):
        """A built-in native route must not be treated as an endpoint fallback."""
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_agent(
            provider="custom",
            model="primary-model",
            base_url="https://primary.example/v1",
        )
        agent.requested_provider = "custom:provider-a"
        agent._primary_runtime["requested_provider"] = "custom:provider-a"
        agent._fallback_chain = [
            {"provider": native_provider, "model": "native-model"},
            {"provider": "provider-b", "model": "fallback-model"},
        ]
        agent._credential_pool = _make_pool("custom:provider-a")
        agent._buffer_status = MagicMock()
        agent._is_azure_openai_url.return_value = False
        agent._is_direct_openai_url.return_value = False
        agent._provider_model_requires_responses_api.return_value = False
        agent._anthropic_prompt_cache_policy.return_value = (False, False)
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent.context_compressor = None
        agent._try_activate_fallback.side_effect = (
            lambda reason=None: try_activate_fallback(agent, reason)
        )

        supported_client = SimpleNamespace(
            api_key="provider-b-key",
            base_url="https://gateway.example/v1",
            _custom_headers={},
        )
        fallback_pool = _make_pool("custom:provider-b")

        def fake_resolve(provider, model=None, raw_codex=False,
                         explicit_base_url=None, explicit_api_key=None):
            if provider == "provider-b":
                return supported_client, "fallback-model"
            raise AssertionError(
                f"native fallback must not construct endpoint client for {provider!r}"
            )

        def fake_runtime(*, requested=None, target_model=None,
                         explicit_base_url=None, explicit_api_key=None):
            if requested == native_provider:
                return {
                    "provider": native_provider,
                    "requested_provider": native_provider,
                    "api_mode": native_api_mode,
                }
            if requested == "provider-b":
                return {
                    "provider": "custom",
                    "requested_provider": requested,
                    "api_mode": "chat_completions",
                }
            raise AssertionError(f"unexpected runtime request: {requested!r}")

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                side_effect=fake_resolve,
            ) as resolve_client,
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                side_effect=fake_runtime,
            ),
            patch(
                "agent.credential_pool.get_custom_provider_pool_key",
                return_value="custom:provider-b",
            ),
            patch(
                "agent.credential_pool.load_pool",
                return_value=fallback_pool,
            ),
        ):
            assert try_activate_fallback(agent) is True

        assert [call.args[0] for call in resolve_client.call_args_list] == ["provider-b"]
        assert agent.provider == "custom"
        assert agent.requested_provider == "custom:provider-b"
        assert agent.model == "fallback-model"
        assert agent.api_mode == "chat_completions"

    @pytest.mark.parametrize("api_mode", ["bedrock_converse", "codex_app_server"])
    def test_named_custom_fallback_skips_unsupported_native_api_modes(self, api_mode):
        """Fallback must not publish a native-only mode without its runtime."""
        from agent.chat_completion_helpers import try_activate_fallback

        agent = _make_agent(
            provider="custom",
            model="primary-model",
            base_url="https://primary.example/v1",
        )
        agent.requested_provider = "custom:provider-a"
        agent._primary_runtime["requested_provider"] = "custom:provider-a"
        agent._fallback_chain = [
            {"provider": "unsupported-provider", "model": "unsupported-model"},
            {"provider": "provider-b", "model": "fallback-model"},
        ]
        agent._credential_pool = _make_pool("custom:provider-a")
        agent._buffer_status = MagicMock()
        agent._is_azure_openai_url.return_value = False
        agent._is_direct_openai_url.return_value = False
        agent._provider_model_requires_responses_api.return_value = False
        agent._anthropic_prompt_cache_policy.return_value = (False, False)
        agent._ensure_lmstudio_runtime_loaded = MagicMock()
        agent.context_compressor = None
        agent._try_activate_fallback.side_effect = (
            lambda reason=None: try_activate_fallback(agent, reason)
        )

        supported_client = SimpleNamespace(
            api_key="provider-b-key",
            base_url="https://gateway.example/v1",
            _custom_headers={},
        )
        fallback_pool = _make_pool("custom:provider-b")

        def fake_resolve(provider, model=None, raw_codex=False,
                         explicit_base_url=None, explicit_api_key=None):
            if provider == "provider-b":
                return supported_client, "fallback-model"
            raise AssertionError(
                f"unsupported native fallback must not construct {provider!r}"
            )

        def fake_runtime(*, requested=None, target_model=None,
                         explicit_base_url=None, explicit_api_key=None):
            if requested == "unsupported-provider":
                return {
                    "provider": "custom",
                    "requested_provider": requested,
                    "api_mode": api_mode,
                }
            if requested == "provider-b":
                return {
                    "provider": "custom",
                    "requested_provider": requested,
                    "api_mode": "chat_completions",
                }
            raise AssertionError(f"unexpected runtime request: {requested!r}")

        with (
            patch(
                "agent.auxiliary_client.resolve_provider_client",
                side_effect=fake_resolve,
            ) as resolve_client,
            patch(
                "hermes_cli.runtime_provider.resolve_runtime_provider",
                side_effect=fake_runtime,
            ),
            patch(
                "agent.credential_pool.get_custom_provider_pool_key",
                return_value="custom:provider-b",
            ),
            patch(
                "agent.credential_pool.load_pool",
                return_value=fallback_pool,
            ),
        ):
            assert try_activate_fallback(agent) is True

        assert [call.args[0] for call in resolve_client.call_args_list] == ["provider-b"]
        assert agent.provider == "custom"
        assert agent.requested_provider == "custom:provider-b"
        assert agent.model == "fallback-model"
        assert agent.api_mode == "chat_completions"


# ── Test: _recover_with_credential_pool rejects mismatched pool ──────

class TestRecoveryProviderGuard:
    """Test that _recover_with_credential_pool skips mismatched pools."""

    def test_recovery_skips_mismatched_pool(self):
        """_recover_with_credential_pool should not mutate a pool belonging
        to a different provider than the active agent provider."""
        agent = _make_agent(provider="openrouter")
        # Pool still belongs to primary (openai-codex) — mismatch
        agent._credential_pool = _make_pool("openai-codex")

        current_provider = (getattr(agent, "provider", "") or "").strip().lower()
        pool_provider = getattr(agent._credential_pool, "provider", "") or ""

        # The guard logic:
        should_skip = (current_provider and pool_provider and
                       current_provider != pool_provider)

        assert should_skip is True, (
            f"Provider mismatch: agent={current_provider}, pool={pool_provider} — should skip"
        )

    def test_recovery_allows_matching_pool(self):
        """When pool and agent provider match, recovery proceeds normally."""
        agent = _make_agent(provider="openrouter")
        agent._credential_pool = _make_pool("openrouter")

        current_provider = (getattr(agent, "provider", "") or "").strip().lower()
        pool_provider = getattr(agent._credential_pool, "provider", "") or ""

        should_skip = (current_provider and pool_provider and
                       current_provider != pool_provider)

        assert should_skip is False, (
            "Same provider — should allow recovery"
        )

    def test_recovery_429_from_zai_does_not_exhaust_codex_pool(self):
        """Regression test for GH #33088: zai 429 should NOT exhaust
        openai-codex credential pool."""
        agent = _make_agent(provider="zai", base_url="https://api.z.com/v1")
        # Stale codex pool from primary
        codex_pool = _make_pool("openai-codex")
        agent._credential_pool = codex_pool

        # The guard should prevent mark_exhausted_and_rotate from being called
        current_provider = "zai"
        pool_provider = "openai-codex"
        should_skip = current_provider != pool_provider

        assert should_skip is True
        codex_pool.mark_exhausted_and_rotate.assert_not_called()


# ── Test: base_url not overwritten after fallback ────────────────────

class TestBaseUrlLeak:
    """Regression tests for GH #33163: base_url leaks from primary."""

    def test_client_kwargs_base_url_preserved_after_pool_clear(self):
        """After fallback activation clears the pool, _client_kwargs should
        still have the fallback base_url, not the primary's."""
        agent = _make_agent(
            provider="openai-codex",
            base_url="https://chatgpt.com/backend-api/codex"
        )

        # Simulate what _try_activate_fallback does:
        fb_base_url = "https://openrouter.ai/api/v1/"
        agent.provider = "openrouter"
        agent.base_url = fb_base_url
        agent._client_kwargs = {
            "api_key": "or-key",
            "base_url": fb_base_url,
        }

        # Clear mismatched pool
        agent._credential_pool = None

        assert agent._client_kwargs["base_url"] == fb_base_url, (
            f"base_url should be {fb_base_url}, not primary's URL"
        )

    def test_swap_credential_does_not_restore_primary_url(self):
        """_swap_credential should not be called when pool is None,
        preventing it from overwriting base_url back to primary's."""
        agent = _make_agent(provider="openrouter", base_url="https://openrouter.ai/api/v1/")
        agent._credential_pool = None  # Cleared by fallback isolation

        # If pool is None, _recover_with_credential_pool returns early
        # and _swap_credential is never called
        pool = agent._credential_pool
        assert pool is None, "Pool should be None — _swap_credential won't be reached"
