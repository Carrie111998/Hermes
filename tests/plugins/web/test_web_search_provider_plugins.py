"""Plugin-side tests for the web search provider migration (PR #25182).

Covers:

- All eight bundled plugins (brave-free, ddgs, searxng, exa, parallel,
  tavily, firecrawl, xai) instantiate and self-report the expected
  capabilities + ABC-derived defaults.
- Each plugin's ``is_available()`` correctly reflects env-var presence.
- The web_search_registry resolves an active provider in the documented
  scenarios (explicit config wins ignoring availability, fallback walks
  legacy preference filtered by availability, unknown name falls back).
- Plugin response shapes match the legacy bit-for-bit contract.

Per the dev skill: these tests use *real* imports from the plugin
modules — no mocking of provider classes themselves — so the test
catches drift in the ABC interface, the registry, and the plugin
glue layer simultaneously.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clear_web_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every web-provider env var so is_available() returns False."""
    for k in (
        "BRAVE_SEARCH_API_KEY",
        "SEARXNG_URL",
        "TAVILY_API_KEY",
        "TAVILY_BASE_URL",
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "PARALLEL_SEARCH_MODE",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
        "TOOL_GATEWAY_USER_TOKEN",
        "XAI_API_KEY",
    ):
        monkeypatch.delenv(k, raising=False)


def _ensure_plugins_loaded() -> None:
    """Idempotently load plugins so the registry is populated."""
    from hermes_cli.plugins import _ensure_plugins_discovered

    _ensure_plugins_discovered()


# ---------------------------------------------------------------------------
# Per-plugin discovery + capability flags
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test starts with a clean web-provider env."""
    _clear_web_env(monkeypatch)


class TestBundledPluginsRegister:
    """All eight bundled web plugins discover and register correctly."""

    def test_all_seven_plugins_present_in_registry(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import list_providers

        names = sorted(p.name for p in list_providers())
        assert names == [
            "brave-free",
            "ddgs",
            "exa",
            "firecrawl",
            "parallel",
            "searxng",
            "tavily",
            "xai",
        ]

    @pytest.mark.parametrize(
        "plugin_name,expected_search,expected_extract",
        [
            ("brave-free", True, False),
            ("ddgs", True, False),
            ("searxng", True, False),
            ("exa", True, True),
            ("parallel", True, True),
            ("tavily", True, True),
            ("firecrawl", True, True),
            # xai: search-only via Grok's agentic web_search tool.
            ("xai", True, False),
        ],
    )
    def test_capability_flags_match_spec(
        self,
        plugin_name: str,
        expected_search: bool,
        expected_extract: bool,
    ) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider(plugin_name)
        assert provider is not None, f"plugin {plugin_name!r} not registered"
        assert provider.supports_search() is expected_search
        assert provider.supports_extract() is expected_extract

    @pytest.mark.parametrize(
        "plugin_name",
        ["brave-free", "ddgs", "searxng", "exa", "parallel", "tavily", "firecrawl", "xai"],
    )
    def test_each_plugin_has_name_and_display_name(self, plugin_name: str) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider(plugin_name)
        assert provider is not None
        assert provider.name == plugin_name
        assert provider.display_name  # any non-empty string

    @pytest.mark.parametrize(
        "plugin_name",
        ["brave-free", "ddgs", "searxng", "exa", "parallel", "tavily", "firecrawl", "xai"],
    )
    def test_each_plugin_has_setup_schema(self, plugin_name: str) -> None:
        """``get_setup_schema()`` returns a dict the picker can consume."""
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        provider = get_provider(plugin_name)
        assert provider is not None
        schema = provider.get_setup_schema()
        assert isinstance(schema, dict)
        assert "name" in schema
        assert "env_vars" in schema


# ---------------------------------------------------------------------------
# is_available() behavior
# ---------------------------------------------------------------------------


class TestIsAvailable:
    """Each plugin's ``is_available()`` returns False without env config."""

    def test_brave_free_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("brave-free")
        assert p is not None
        assert p.is_available() is False  # no BRAVE_SEARCH_API_KEY
        monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "real")
        assert p.is_available() is True

    def test_searxng_requires_url(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("searxng")
        assert p is not None
        assert p.is_available() is False
        monkeypatch.setenv("SEARXNG_URL", "http://localhost:8080")
        assert p.is_available() is True

    def test_tavily_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("tavily")
        assert p is not None
        assert p.is_available() is False
        monkeypatch.setenv("TAVILY_API_KEY", "real")
        assert p.is_available() is True

    def test_exa_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("exa")
        assert p is not None
        assert p.is_available() is False
        monkeypatch.setenv("EXA_API_KEY", "real")
        assert p.is_available() is True

    def test_parallel_requires_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("parallel")
        assert p is not None
        assert p.is_available() is False
        monkeypatch.setenv("PARALLEL_API_KEY", "real")
        assert p.is_available() is True

    def test_firecrawl_requires_either_key_or_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("firecrawl")
        assert p is not None
        assert p.is_available() is False

        # Either FIRECRAWL_API_KEY or FIRECRAWL_API_URL lights it up.
        monkeypatch.setenv("FIRECRAWL_API_KEY", "real")
        assert p.is_available() is True
        monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
        monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:3002")
        assert p.is_available() is True

    def test_ddgs_always_available_when_package_importable(self) -> None:
        """DDGS is the always-on fallback — no API key required.

        It may report unavailable if the ``ddgs`` package itself isn't
        installed in the env (legitimate — the plugin's post_setup hook
        triggers pip install on first selection). We only assert that
        is_available() doesn't raise.
        """
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("ddgs")
        assert p is not None
        # Truthy or falsy, just must not raise.
        _ = bool(p.is_available())

    def test_xai_requires_api_key_or_oauth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """xAI needs XAI_API_KEY or OAuth tokens in auth.json."""
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("xai")
        assert p is not None
        assert p.is_available() is False  # no XAI_API_KEY, no auth.json
        monkeypatch.setenv("XAI_API_KEY", "real")
        assert p.is_available() is True


# ---------------------------------------------------------------------------
# Registry resolution semantics (Option B — conservative smart fallback)
# ---------------------------------------------------------------------------


class TestRegistryResolution:
    """``_resolve()`` follows explicit-config + availability-filtered fallback."""

    def test_explicit_configured_provider_returned_even_when_unavailable(
        self,
    ) -> None:
        """Explicit ``web.search_backend`` wins regardless of is_available().

        Without availability filtering on the explicit path, the dispatcher
        would silently switch backends; with this check the dispatcher
        surfaces a precise "FOO_API_KEY is not set" error instead.
        """
        _ensure_plugins_loaded()
        from agent.web_search_registry import _resolve

        # No BRAVE_SEARCH_API_KEY (fixture cleared it).
        result = _resolve("brave-free", capability="search")
        assert result is not None
        assert result.name == "brave-free"
        # Confirm it's the unavailable one — dispatcher will surface
        # a typed credential-missing error to the caller.
        assert result.is_available() is False

    def test_unknown_configured_name_falls_back_to_available_provider(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Typo / uninstalled plugin → walk legacy preference, pick available."""
        _ensure_plugins_loaded()
        from agent.web_search_registry import _resolve

        monkeypatch.setenv("EXA_API_KEY", "real")
        result = _resolve("not-a-real-provider", capability="search")
        # Either ddgs (no-key fallback) or exa (the only available
        # premium provider) — both are valid. The point is the unknown
        # name shouldn't return None when SOMETHING is available.
        assert result is not None
        assert result.is_available() is True

    def test_explicit_search_only_provider_for_extract_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Asking for extract via a search-only backend → fall back.

        ``brave-free`` is search-only (``supports_extract() is False``).
        When the registry resolves it for an extract capability, the
        explicit-config branch rejects it as capability-incompatible
        and the fallback walk picks an extract-capable provider.
        """
        _ensure_plugins_loaded()
        from agent.web_search_registry import _resolve

        monkeypatch.setenv("EXA_API_KEY", "real")
        result = _resolve("brave-free", capability="extract")
        # Should land on exa (only extract-capable available provider).
        assert result is not None
        assert result.supports_extract() is True
        assert result.is_available() is True

    def test_no_config_no_credentials_returns_none(
        self,
    ) -> None:
        """No backend configured AND no available providers → typically None.

        ``ddgs`` is the no-credential fallback; if its ``ddgs`` Python
        package is installed in the test env, ddgs will be picked.
        Otherwise the resolver returns None. Either outcome is correct.
        """
        _ensure_plugins_loaded()
        from agent.web_search_registry import _resolve

        result = _resolve(None, capability="search")
        if result is not None:
            # The only no-credential provider is ddgs; anything else
            # means an env var leaked in.
            assert result.is_available() is True

    def test_fallback_search_excludes_firecrawl_and_uses_existing_priority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_fallback_provider

        monkeypatch.setenv("FIRECRAWL_API_KEY", "fc")
        monkeypatch.setenv("PARALLEL_API_KEY", "parallel")
        result = get_fallback_provider(
            "search", excluded=frozenset({"firecrawl"})
        )
        assert result is not None
        assert result.name == "parallel"

    def test_explicit_firecrawl_is_excluded_but_explicit_non_firecrawl_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_plugins_loaded()
        import agent.web_search_registry as registry

        monkeypatch.setattr(
            registry, "_read_config_key", lambda *path: "firecrawl"
        )
        monkeypatch.setenv("EXA_API_KEY", "exa")
        result = registry.get_fallback_provider(
            "extract", excluded=frozenset({"firecrawl"})
        )
        assert result is not None
        assert result.name == "exa"

    def test_explicit_non_firecrawl_must_be_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_plugins_loaded()
        import agent.web_search_registry as registry

        monkeypatch.setattr(
            registry, "_read_config_key", lambda *path: "parallel"
        )
        monkeypatch.setenv("EXA_API_KEY", "exa")
        result = registry.get_fallback_provider(
            "extract", excluded=frozenset({"firecrawl"})
        )
        assert result is not None
        assert result.name == "exa"

    def test_fallback_returns_none_when_all_capable_providers_are_excluded(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _ensure_plugins_loaded()
        import agent.web_search_registry as registry

        firecrawl = registry.get_provider("firecrawl")
        assert firecrawl is not None
        monkeypatch.setattr(registry, "_providers", {"firecrawl": firecrawl})
        assert (
            registry.get_fallback_provider(
                "extract", excluded=frozenset({"firecrawl"})
            )
            is None
        )

    def test_fallback_rejects_unknown_capability(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_fallback_provider

        with pytest.raises(ValueError, match="Unsupported web capability"):
            get_fallback_provider("browse")


# ---------------------------------------------------------------------------
# Sync-vs-async extract detection
# ---------------------------------------------------------------------------


class TestAsyncExtractDispatch:
    """The dispatcher detects async vs sync extract methods correctly."""

    def test_parallel_extract_is_async(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("parallel")
        assert p is not None
        assert inspect.iscoroutinefunction(p.extract) is True

    def test_firecrawl_extract_is_async(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("firecrawl")
        assert p is not None
        assert inspect.iscoroutinefunction(p.extract) is True

    def test_exa_extract_is_sync(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("exa")
        assert p is not None
        assert inspect.iscoroutinefunction(p.extract) is False

    def test_tavily_extract_is_sync(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("tavily")
        assert p is not None
        assert inspect.iscoroutinefunction(p.extract) is False


# ---------------------------------------------------------------------------
# Error response shape (preserved bit-for-bit from legacy)
# ---------------------------------------------------------------------------


class TestErrorResponseShapes:
    """When credentials are missing, plugins return typed errors, not raises."""

    def test_brave_free_returns_error_dict_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("brave-free")
        assert p is not None
        result = p.search("test", limit=5)
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "error" in result

    def test_searxng_returns_error_dict_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("searxng")
        assert p is not None
        result = p.search("test", limit=5)
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "error" in result

    def test_exa_returns_error_dict_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("exa")
        assert p is not None
        result = p.search("test", limit=5)
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "error" in result

    def test_tavily_returns_error_dict_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("tavily")
        assert p is not None
        result = p.search("test", limit=5)
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "error" in result

    def test_parallel_extract_returns_per_url_errors_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("parallel")
        assert p is not None
        result = asyncio.run(p.extract(["https://example.com"]))
        assert isinstance(result, list)
        assert len(result) == 1
        assert "error" in result[0]
        assert result[0]["url"] == "https://example.com"

    def test_firecrawl_extract_returns_per_url_errors_when_unconfigured(self) -> None:
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("firecrawl")
        assert p is not None
        # firecrawl extract returns [] when the website-policy gate rejects
        # the URL, or a per-URL error dict when the gate passes but the
        # firecrawl client fails. Use a URL the policy allows to make sure
        # we hit the credential-missing path.
        result = asyncio.run(p.extract(["https://example.com"]))
        assert isinstance(result, list)
        if result:  # if anything came back, it should be an error entry
            assert "error" in result[0]

    def test_firecrawl_search_classifies_payment_required_preserving_envelope(
        self,
        monkeypatch,
    ):
        from plugins.web.firecrawl import provider as firecrawl_provider

        class PaymentRequiredError(Exception):
            status_code = 402

        class Client:
            def search(self, **kwargs):
                raise PaymentRequiredError(
                    "Payment Required: Failed to search. Credits exhausted"
                )

        monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: Client())

        result = firecrawl_provider.FirecrawlWebSearchProvider().search("test")

        assert result["success"] is False
        assert "Firecrawl search failed" in result["error"]
        assert result["error_info"] == {
            "code": "provider_credits_exhausted",
            "provider": "firecrawl",
            "scope": "account",
            "retryable": False,
        }

    def test_firecrawl_search_does_not_classify_transient_error_as_credits(
        self,
        monkeypatch,
    ):
        from plugins.web.firecrawl import provider as firecrawl_provider

        class Client:
            def search(self, **kwargs):
                raise TimeoutError("temporary timeout")

        monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: Client())

        result = firecrawl_provider.FirecrawlWebSearchProvider().search("test")

        assert result["success"] is False
        assert "error_info" not in result

    def test_firecrawl_search_run_circuit_blocks_later_network_call(
        self,
        monkeypatch,
    ):
        from agent import firecrawl_run_state as state
        from plugins.web.firecrawl import provider as firecrawl_provider

        calls = []

        class Client:
            def search(self, **kwargs):
                calls.append(kwargs)
                return {"web": []}

        monkeypatch.setattr(
            firecrawl_provider, "_get_firecrawl_client", lambda: Client()
        )
        provider = firecrawl_provider.FirecrawlWebSearchProvider()
        _, token = state.install_firecrawl_run()
        try:
            assert provider.search("before")["success"] is True
            assert len(calls) == 1
            state.record_firecrawl_credits_exhausted()
            blocked = provider.search("after")
            assert len(calls) == 1
            assert blocked["error_info"] == dict(state.CIRCUIT_OPEN_INFO)
        finally:
            state.reset_firecrawl_run(token)

    def test_firecrawl_search_402_opens_shared_run_state(
        self,
        monkeypatch,
    ):
        from agent import firecrawl_run_state as state
        from plugins.web.firecrawl import provider as firecrawl_provider

        class PaymentRequiredError(Exception):
            status_code = 402

        class Client:
            def search(self, **kwargs):
                raise PaymentRequiredError("secret response body must not escape")

        monkeypatch.setattr(
            firecrawl_provider, "_get_firecrawl_client", lambda: Client()
        )
        run, token = state.install_firecrawl_run()
        try:
            result = firecrawl_provider.FirecrawlWebSearchProvider().search("query")
            assert run.circuit_open is True
            assert result["error_info"] == dict(state.CREDITS_EXHAUSTED_INFO)
            assert "secret response body" not in repr(run.first_failure)
        finally:
            state.reset_firecrawl_run(token)

    def test_firecrawl_extract_shared_run_blocks_next_invocation(
        self,
        monkeypatch,
    ):
        from agent import firecrawl_run_state as state
        from plugins.web.firecrawl import provider as firecrawl_provider

        calls = []

        class PaymentRequiredError(Exception):
            status_code = 402

        class Client:
            def scrape(self, *, url, formats):
                calls.append(url)
                raise PaymentRequiredError("secret response")

        monkeypatch.setattr(
            firecrawl_provider, "_get_firecrawl_client", lambda: Client()
        )
        monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)
        provider = firecrawl_provider.FirecrawlWebSearchProvider()
        run, token = state.install_firecrawl_run()
        try:
            first = asyncio.run(provider.extract(["https://example.com/1"]))
            second = asyncio.run(provider.extract(["https://example.com/2"]))
            assert run.circuit_open is True
            assert calls == ["https://example.com/1"]
            assert first[0]["error_info"] == dict(state.CREDITS_EXHAUSTED_INFO)
            assert second[0]["error_info"] == dict(state.CIRCUIT_OPEN_INFO)
        finally:
            state.reset_firecrawl_run(token)

    def test_firecrawl_extract_transient_failure_leaves_shared_run_open_for_calls(
        self,
        monkeypatch,
    ):
        from agent import firecrawl_run_state as state
        from plugins.web.firecrawl import provider as firecrawl_provider

        calls = []

        class Client:
            def scrape(self, *, url, formats):
                calls.append(url)
                raise TimeoutError("temporary timeout")

        monkeypatch.setattr(
            firecrawl_provider, "_get_firecrawl_client", lambda: Client()
        )
        monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)
        provider = firecrawl_provider.FirecrawlWebSearchProvider()
        run, token = state.install_firecrawl_run()
        try:
            asyncio.run(provider.extract(["https://example.com/1"]))
            asyncio.run(provider.extract(["https://example.com/2"]))
            assert run.circuit_open is False
            assert calls == ["https://example.com/1", "https://example.com/2"]
        finally:
            state.reset_firecrawl_run(token)

    def test_firecrawl_extract_opens_invocation_circuit_after_payment_required(
        self,
        monkeypatch,
    ):
        from plugins.web.firecrawl import provider as firecrawl_provider

        calls = []

        class PaymentRequiredError(Exception):
            status_code = 402

        class Client:
            def scrape(self, *, url, formats):
                calls.append(url)
                raise PaymentRequiredError(
                    "Payment Required: Failed to scrape. Credits exhausted"
                )

        monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: Client())
        monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)

        urls = ["https://example.com/1", "https://example.com/2"]
        result = asyncio.run(
            firecrawl_provider.FirecrawlWebSearchProvider().extract(urls)
        )

        assert calls == [urls[0]]
        assert [item["url"] for item in result] == urls
        assert result[0]["error_info"]["code"] == "provider_credits_exhausted"
        assert result[1]["error_info"]["code"] == "provider_circuit_open"
        assert result[1]["error_info"]["scope"] == "account"

    def test_firecrawl_extract_normal_url_failure_does_not_open_circuit(
        self,
        monkeypatch,
    ):
        from plugins.web.firecrawl import provider as firecrawl_provider

        calls = []

        class Client:
            def scrape(self, *, url, formats):
                calls.append(url)
                raise TimeoutError("temporary timeout")

        monkeypatch.setattr(firecrawl_provider, "_get_firecrawl_client", lambda: Client())
        monkeypatch.setattr(firecrawl_provider, "check_website_access", lambda url: None)

        urls = ["https://example.com/1", "https://example.com/2"]
        result = asyncio.run(
            firecrawl_provider.FirecrawlWebSearchProvider().extract(urls)
        )

        assert calls == urls
        assert all("error_info" not in item for item in result)

    def test_firecrawl_config_error_points_paid_users_to_nous_subscription(self, monkeypatch):
        from plugins.web.firecrawl import provider as firecrawl_provider

        monkeypatch.setattr(
            "tools.web_tools.managed_nous_tools_enabled",
            lambda: True,
            raising=False,
        )

        with pytest.raises(ValueError) as exc_info:
            firecrawl_provider._raise_web_backend_configuration_error()

        message = str(exc_info.value)
        assert "With your Nous subscription you can also use the Tool Gateway" in message
        assert "select Nous Subscription as the web provider" in message
        assert "managed Firecrawl web tools is unavailable" not in message

    def test_firecrawl_config_error_uses_entitlement_message_when_not_paid(self, monkeypatch):
        from plugins.web.firecrawl import provider as firecrawl_provider

        monkeypatch.setattr(
            "tools.web_tools.managed_nous_tools_enabled",
            lambda: False,
            raising=False,
        )
        monkeypatch.setattr(
            "tools.web_tools.nous_tool_gateway_unavailable_message",
            lambda capability: f"{capability} denied by test entitlement.",
            raising=False,
        )

        with pytest.raises(ValueError) as exc_info:
            firecrawl_provider._raise_web_backend_configuration_error()

        assert "managed Firecrawl web tools denied by test entitlement" in str(exc_info.value)

    def test_xai_search_returns_error_dict_when_unconfigured(self) -> None:
        """xAI returns a typed error dict (no XAI_API_KEY)."""
        _ensure_plugins_loaded()
        from agent.web_search_registry import get_provider

        p = get_provider("xai")
        assert p is not None
        result = p.search("test", limit=5)
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "error" in result
