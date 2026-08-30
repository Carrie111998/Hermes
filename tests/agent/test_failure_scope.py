"""Failure-scope routing and transport classification tests."""

import logging
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.auxiliary_client import async_call_llm, call_llm, resolve_provider_client
from agent.backend_identity import (
    BackendIdentity,
    FailureScope,
    classify_failure_scope,
    same_credential_surface,
)
from agent.auxiliary_client import (
    _is_connection_error as is_connection_error,
    _is_endpoint_unreachable_error as is_endpoint_unreachable_error,
    _is_timeout_error as is_timeout_error,
    _is_transient_transport_error as is_transient_transport_error,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in (
        "OPENROUTER_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY",
        "OPENAI_MODEL", "LLM_MODEL", "NOUS_INFERENCE_BASE_URL",
        "ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "NVIDIA_API_KEY", "NVIDIA_BASE_URL", "R3_FALLBACK_KEY",
        "R3_MISSING_FALLBACK_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    import agent.auxiliary_client as aux
    aux._aux_unhealthy_until.clear()
    aux._aux_unhealthy_logged_at.clear()
    yield
    aux._aux_unhealthy_until.clear()
    aux._aux_unhealthy_logged_at.clear()


def _patches(client):
    return (
        patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("openrouter", "some-model", None, None, None),
        ),
        patch(
            "agent.auxiliary_client._get_cached_client",
            return_value=(client, "some-model"),
        ),
        patch(
            "agent.auxiliary_client._validate_llm_response",
            side_effect=lambda response, _task, **_kw: response,
        ),
    )


class TestTransportClassification:
    def test_httpx_connect_error_is_endpoint_scoped(self):
        import httpx
        err = httpx.ConnectError("All connection attempts failed")
        assert is_connection_error(err)
        assert is_endpoint_unreachable_error(err)

    def test_httpx_nested_dns_error_is_endpoint_scoped(self):
        import httpx
        err = httpx.ConnectError("[Errno 8] nodename nor servname provided")
        assert is_connection_error(err)
        assert is_endpoint_unreachable_error(err)

    def test_connect_timeout_is_endpoint_scoped(self):
        import httpx
        err = httpx.ConnectTimeout("timed out")
        assert is_endpoint_unreachable_error(err)
        assert classify_failure_scope("endpoint unreachable") is FailureScope.ENDPOINT

    def test_read_timeout_and_reset_are_model_scoped(self):
        import httpx
        assert is_timeout_error(httpx.ReadTimeout("timed out"))
        assert not is_endpoint_unreachable_error(httpx.ReadTimeout("timed out"))
        assert is_connection_error(httpx.ReadError("connection reset by peer"))
        assert not is_endpoint_unreachable_error(httpx.ReadError("connection reset by peer"))

    @pytest.mark.parametrize(
        "error",
        [
            ssl.SSLError("TLS handshake failed"),
            ssl.SSLCertVerificationError("certificate verify failed: unable to get local issuer certificate"),
            ssl.CertificateError("hostname mismatch"),
        ],
    )
    def test_deterministic_tls_failures_are_endpoint_scoped(self, error):
        assert is_connection_error(error)
        assert is_endpoint_unreachable_error(error)

    @pytest.mark.parametrize(
        "error",
        [
            ssl.SSLEOFError("EOF occurred in violation of protocol"),
            ssl.SSLWantReadError("want read"),
            ssl.SSLWantWriteError("want write"),
            ssl.SSLZeroReturnError("TLS/SSL connection has been closed"),
            ssl.SSLError("connection reset by peer during TLS"),
        ],
    )
    def test_transient_tls_failures_are_model_scoped_and_retryable(self, error):
        assert is_connection_error(error)
        assert not is_endpoint_unreachable_error(error)
        assert is_transient_transport_error(error)

    def test_nested_reset_driven_tls_eof_is_model_scoped(self):
        import httpx
        err = httpx.ReadError("connection reset by peer")
        err.__cause__ = ssl.SSLError("EOF occurred in violation of protocol")
        assert is_connection_error(err)
        assert not is_endpoint_unreachable_error(err)
        assert is_transient_transport_error(err)


class TestSyncFallbackScope:
    def test_connection_reset_remains_model_scoped(self, monkeypatch):
        primary = MagicMock(base_url="https://gateway.example/v1")
        primary.chat.completions.create.side_effect = ConnectionResetError(
            "connection reset by peer"
        )
        fallback = MagicMock(base_url="https://gateway.example/v1")
        fallback.chat.completions.create.return_value = {"fallback": True}
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 0)
        p1, p2, p3 = _patches(primary)
        with p1, p2, p3, patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(fallback, "sibling-model", "fallback_chain[0](custom)"),
        ) as chain:
            result = call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert chain.call_args.kwargs["failure_scope"] is FailureScope.MODEL
        assert chain.call_args.kwargs["failed_model"] == "some-model"

    def test_connect_error_is_endpoint_scoped(self, monkeypatch):
        import httpx
        primary = MagicMock(base_url="https://unreachable.example/v1")
        primary.chat.completions.create.side_effect = httpx.ConnectError(
            "All connection attempts failed"
        )
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create.return_value = {"fallback": True}
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 0)
        p1, p2, p3 = _patches(primary)
        with p1, p2, p3, patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(fallback, "fallback-model", "fallback_chain[0](custom)"),
        ) as chain:
            result = call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert chain.call_args.kwargs["failure_scope"] is FailureScope.ENDPOINT
        assert chain.call_args.kwargs["failed_model"] is None

    def test_connect_timeout_is_endpoint_scoped(self, monkeypatch):
        import httpx
        primary = MagicMock(base_url="https://blackhole.example/v1")
        primary.chat.completions.create.side_effect = httpx.ConnectTimeout("timed out")
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create.return_value = {"fallback": True}
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 0)
        p1, p2, p3 = _patches(primary)
        with p1, p2, p3, patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(fallback, "fallback-model", "fallback_chain[0](custom)"),
        ) as chain:
            result = call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert chain.call_args.kwargs["failure_scope"] is FailureScope.ENDPOINT
        assert chain.call_args.kwargs["failed_model"] is None

    def test_exhausted_5xx_reaches_fallback(self, monkeypatch):
        class ServerError(Exception):
            status_code = 500
        primary = MagicMock(base_url="https://busy.example/v1")
        primary.chat.completions.create.side_effect = ServerError("internal server error")
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create.return_value = {"fallback": True}
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 1)
        monkeypatch.setattr("agent.auxiliary_client._TRANSIENT_RETRY_BACKOFF_BASE", 0)
        p1, p2, p3 = _patches(primary)
        with p1, p2, p3, patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(fallback, "fallback-model", "fallback_chain[0](custom)"),
        ) as chain:
            result = call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert primary.chat.completions.create.call_count == 2
        assert chain.called


    def test_tls_certificate_error_is_endpoint_scoped(self, monkeypatch):
        primary = MagicMock(base_url="https://untrusted.example/v1")
        primary.chat.completions.create.side_effect = ssl.SSLCertVerificationError(
            "certificate verify failed"
        )
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create.return_value = {"fallback": True}
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 2)
        monkeypatch.setattr("agent.auxiliary_client._TRANSIENT_RETRY_BACKOFF_BASE", 0)
        p1, p2, p3 = _patches(primary)
        with p1, p2, p3, patch(
            "agent.auxiliary_client._try_configured_fallback_chain",
            return_value=(fallback, "fallback-model", "fallback_chain[0](custom)"),
        ) as chain:
            result = call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert primary.chat.completions.create.call_count == 1
        assert chain.call_args.kwargs["failure_scope"] is FailureScope.ENDPOINT
        assert chain.call_args.kwargs["failed_model"] is None


class TestAsyncFallbackScope:
    def _run_patches(self, primary, fallback):
        return (
            patch(
                "agent.auxiliary_client._resolve_task_provider_model",
                return_value=("openrouter", "some-model", None, None, None),
            ),
            patch("agent.auxiliary_client._get_cached_client", return_value=(primary, "some-model")),
            patch(
                "agent.auxiliary_client._validate_llm_response",
                side_effect=lambda response, _task, **_kw: response,
            ),
            patch(
                "agent.auxiliary_client._try_configured_fallback_chain",
                return_value=(fallback, "fallback-model", "fallback_chain[0](custom)"),
            ),
            patch("agent.auxiliary_client._to_async_client", return_value=(fallback, "fallback-model")),
        )

    @pytest.mark.asyncio
    async def test_connect_error_is_endpoint_scoped(self, monkeypatch):
        import httpx
        primary = MagicMock(base_url="https://unreachable.example/v1")
        primary.chat.completions.create = AsyncMock(
            side_effect=httpx.ConnectError("[Errno 8] nodename nor servname provided")
        )
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create = AsyncMock(return_value={"fallback": True})
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 0)
        p1, p2, p3, p4, p5 = self._run_patches(primary, fallback)
        with p1, p2, p3, p4 as chain, p5:
            result = await async_call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert chain.call_args.kwargs["failure_scope"] is FailureScope.ENDPOINT

    @pytest.mark.asyncio
    async def test_connect_timeout_is_endpoint_scoped(self, monkeypatch):
        import httpx
        primary = MagicMock(base_url="https://blackhole.example/v1")
        primary.chat.completions.create = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create = AsyncMock(return_value={"fallback": True})
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 0)
        p1, p2, p3, p4, p5 = self._run_patches(primary, fallback)
        with p1, p2, p3, p4 as chain, p5:
            result = await async_call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert chain.call_args.kwargs["failure_scope"] is FailureScope.ENDPOINT
        assert chain.call_args.kwargs["failed_model"] is None

    @pytest.mark.asyncio
    async def test_tls_certificate_error_is_endpoint_scoped(self, monkeypatch):
        primary = MagicMock(base_url="https://untrusted.example/v1")
        primary.chat.completions.create = AsyncMock(
            side_effect=ssl.SSLCertVerificationError("certificate verify failed")
        )
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create = AsyncMock(return_value={"fallback": True})
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 2)
        monkeypatch.setattr("agent.auxiliary_client._TRANSIENT_RETRY_BACKOFF_BASE", 0)
        p1, p2, p3, p4, p5 = self._run_patches(primary, fallback)
        with p1, p2, p3, p4 as chain, p5:
            result = await async_call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert primary.chat.completions.create.await_count == 1
        assert chain.call_args.kwargs["failure_scope"] is FailureScope.ENDPOINT
        assert chain.call_args.kwargs["failed_model"] is None

    @pytest.mark.asyncio
    async def test_exhausted_408_reaches_fallback(self, monkeypatch):
        class RequestTimeout(Exception):
            status_code = 408
        primary = MagicMock(base_url="https://busy.example/v1")
        primary.chat.completions.create = AsyncMock(side_effect=RequestTimeout("request timeout"))
        fallback = MagicMock(base_url="https://healthy.example/v1")
        fallback.chat.completions.create = AsyncMock(return_value={"fallback": True})
        monkeypatch.setattr("agent.auxiliary_client._transient_retry_count", lambda: 1)
        p1, p2, p3, p4, p5 = self._run_patches(primary, fallback)
        with p1, p2, p3, p4 as chain, p5, patch("asyncio.sleep", new=AsyncMock()):
            result = await async_call_llm(task="session_search", messages=[{"role": "user", "content": "hi"}])
        assert result == {"fallback": True}
        assert primary.chat.completions.create.call_count == 2
        assert chain.called


class TestFallbackEligibility:
    @staticmethod
    def _entry(model):
        return {"provider": "custom", "model": model, "base_url": "https://gateway.example/v1"}

    def test_endpoint_failure_skips_same_endpoint_sibling(self, monkeypatch):
        from agent.auxiliary_client import _try_configured_fallback_chain
        entry = self._entry("model-b")
        monkeypatch.setattr("agent.auxiliary_client._get_auxiliary_task_config", lambda task: {"fallback_chain": [entry]})
        with patch("agent.auxiliary_client._resolve_fallback_entry", side_effect=AssertionError("must skip")):
            result = _try_configured_fallback_chain(
                task="title_generation", failed_provider="custom", failed_model=None,
                failed_base_url=entry["base_url"], failure_scope=FailureScope.ENDPOINT,
            )
        assert result == (None, None, "")

    def test_timeout_allows_same_endpoint_sibling_model(self, monkeypatch):
        from agent.auxiliary_client import _try_configured_fallback_chain
        entry = self._entry("model-b")
        sibling = MagicMock()
        monkeypatch.setattr("agent.auxiliary_client._get_auxiliary_task_config", lambda task: {"fallback_chain": [entry]})
        with patch("agent.auxiliary_client._resolve_fallback_entry", return_value=(sibling, "model-b")):
            result = _try_configured_fallback_chain(
                task="title_generation", failed_provider="custom", failed_model="model-a",
                failed_base_url=entry["base_url"], failure_scope=FailureScope.MODEL,
            )
        assert result[0] is sibling
        assert result[1:] == ("model-b", "fallback_chain[0](custom)")


class TestCredentialSurfaceRegression:
    def test_same_explicit_url_is_credential_surface_fallback(self):
        failed = BackendIdentity.build(model="model-a", base_url="https://tenant.example/v1")
        candidate = BackendIdentity.build(model="model-b", base_url="https://tenant.example/v1")
        assert same_credential_surface(candidate, failed)

    def test_unknown_provider_and_known_provider_same_url_use_base_fallback(self):
        failed = BackendIdentity.build(provider="", model="model-a", base_url="https://tenant.example/v1")
        candidate = BackendIdentity.build(provider="custom", model="model-b", base_url="https://tenant.example/v1")
        assert same_credential_surface(candidate, failed)

    def test_different_explicit_urls_remain_conservative(self):
        a = BackendIdentity.build(base_url="https://tenant-a.example/v1")
        b = BackendIdentity.build(base_url="https://tenant-b.example/v1")
        assert not same_credential_surface(a, b)


class TestOpenRouterPolicyAdditions:
    def test_resolver_forwards_explicit_free_model_to_gate(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.auxiliary_client._select_pool_entry", return_value=(False, None)), \
             patch("hermes_cli.config.load_config_readonly", return_value={"auxiliary": {"free_only": True}}), \
             patch("agent.auxiliary_client.OpenAI") as mock_openai:
            client = MagicMock()
            mock_openai.return_value = client
            resolved, model = resolve_provider_client("openrouter", model="nvidia/nemotron-3-ultra-550b-a55b:free")
        assert resolved is client
        assert model == "nvidia/nemotron-3-ultra-550b-a55b:free"

    def test_free_only_gate_does_not_mark_openrouter_unhealthy(self, monkeypatch):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.auxiliary_client._select_pool_entry", return_value=(False, None)), \
             patch("hermes_cli.config.load_config_readonly", return_value={"auxiliary": {"free_only": True}}), \
             patch("agent.auxiliary_client._mark_provider_unhealthy") as mark:
            client, model = resolve_provider_client("openrouter", model="google/gemini-3.6-flash")
        assert client is None and model is None
        mark.assert_not_called()

    def test_free_only_gate_reports_policy_not_credentials(self, monkeypatch, caplog):
        monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
        with patch("agent.auxiliary_client._select_pool_entry", return_value=(False, None)), \
             patch("hermes_cli.config.load_config_readonly", return_value={"auxiliary": {"free_only": True}}), \
             caplog.at_level(logging.WARNING, logger="agent.auxiliary_client"):
            resolve_provider_client("openrouter", model="google/gemini-3.6-flash")
        messages = [record.getMessage() for record in caplog.records]
        assert any("free_only" in message and "google/gemini-3.6-flash" in message for message in messages)


class TestTopLevelFallbackIdentity:
    def test_indexed_label_exclusion_and_entry_identity(self, monkeypatch):
        from agent.auxiliary_client import _try_main_fallback_chain
        entries = [
            {"provider": "custom", "model": "model-a", "base_url": "https://tenant-a.example/v1", "api_key": "entry-key-a"},
            {"provider": "custom", "model": "model-b", "base_url": "https://tenant-b.example/v1", "api_key": "entry-key-b"},
        ]
        config = {"fallback_providers": entries}
        first_client = MagicMock(base_url=entries[0]["base_url"])
        second_client = MagicMock(base_url=entries[1]["base_url"])
        with (
            patch("hermes_cli.config.load_config_readonly", return_value=config),
            patch("agent.auxiliary_client._read_main_provider", return_value="main"),
            patch("agent.auxiliary_client._read_main_model", return_value="main-model"),
            patch("agent.auxiliary_client._read_main_base_url", return_value="https://main.example/v1"),
            patch("agent.auxiliary_client._read_main_api_key", return_value="main-key"),
            patch("agent.auxiliary_client._resolve_fallback_entry", side_effect=[(first_client, "model-a"), (second_client, "model-b")]) as resolver,
        ):
            first = _try_main_fallback_chain(task="session_search", failed_provider="other", failed_model="failed-model", failure_scope=FailureScope.MODEL)
            second = _try_main_fallback_chain(task="session_search", failed_provider="other", failed_model="failed-model", failure_scope=FailureScope.MODEL, excluded_labels={first[2]})
        assert first[2] == "fallback_providers[0](custom)"
        assert second[2] == "fallback_providers[1](custom)"
        assert resolver.call_args_list[0].args[0] == entries[0]
        assert resolver.call_args_list[1].args[0] == entries[1]

    def test_top_level_key_env_identity_preserves_credential_sibling(self, monkeypatch):
        from agent.auxiliary_client import _try_main_fallback_chain
        monkeypatch.delenv("R3_FALLBACK_KEY", raising=False)
        entry = {"provider": "custom", "model": "model-b", "base_url": "https://tenant-b.example/v1", "key_env": "R3_FALLBACK_KEY"}
        client = MagicMock(base_url=entry["base_url"])
        with (
            patch("hermes_cli.config.load_config_readonly", return_value={"fallback_providers": [entry]}),
            patch("agent.auxiliary_client._read_main_provider", return_value="main"),
            patch("agent.auxiliary_client._read_main_model", return_value="main-model"),
            patch("agent.auxiliary_client._read_main_base_url", return_value="https://main.example/v1"),
            patch("agent.auxiliary_client._read_main_api_key", return_value="main-key"),
            patch("agent.auxiliary_client._resolve_fallback_entry", return_value=(client, entry["model"])) as resolver,
        ):
            result = _try_main_fallback_chain(task="session_search", failed_provider="custom", failed_model="model-a", failed_base_url="https://tenant-a.example/v1", failed_api_key="failed-key", failure_scope=FailureScope.CREDENTIAL)
        assert result[2] == "fallback_providers[0](custom)"
        assert resolver.call_args.args[0] == entry

    def test_pool_identity_is_provider_and_endpoint_scoped(self):
        from agent.auxiliary_client import _backend_identity_for_entry
        custom = _backend_identity_for_entry({"provider": "custom", "model": "model-a", "base_url": "https://shared.example/v1", "credential_pool": "pool-a"})
        openrouter = _backend_identity_for_entry({"provider": "openrouter", "model": "model-a", "base_url": "https://shared.example/v1", "credential_pool": "pool-a"})
        from agent.backend_identity import same_credential_surface
        assert same_credential_surface(custom, openrouter) is False

    def test_missing_entry_key_env_fails_closed_before_provider_resolution(self, monkeypatch):
        monkeypatch.delenv("R3_MISSING_FALLBACK_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "provider-wide-sentinel")
        with patch("agent.auxiliary_client._create_openai_client") as create_client:
            result = resolve_provider_client("custom", model="model-b", explicit_base_url="https://tenant-b.example/v1", explicit_api_key=None, allow_provider_fallback=False)
        assert result == (None, None)
        create_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_top_level_skips_missing_key_before_exact_sibling(self, monkeypatch):
        from agent.auxiliary_client import _run_fallback_chain_async, _try_main_fallback_chain
        monkeypatch.delenv("R3_MISSING_FALLBACK_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "provider-wide-sentinel")
        entries = [
            {"provider": "custom", "model": "model-missing", "base_url": "https://tenant-a.example/v1", "key_env": "R3_MISSING_FALLBACK_KEY"},
            {"provider": "custom", "model": "model-valid", "base_url": "https://tenant-b.example/v1", "api_key": "entry-key-b"},
        ]
        resolved = MagicMock(base_url=entries[1]["base_url"], api_key="entry-key-b")
        async_client = MagicMock()
        resolver_calls = []
        def resolve(provider, **kwargs):
            resolver_calls.append((provider, kwargs))
            return resolved, kwargs["model"]
        async def call_candidate(*args, **kwargs):
            return {"selected": args[1]}
        with (
            patch("hermes_cli.config.load_config_readonly", return_value={"fallback_providers": entries}),
            patch("agent.auxiliary_client._read_main_provider", return_value="main"),
            patch("agent.auxiliary_client._read_main_model", return_value="main-model"),
            patch("agent.auxiliary_client._read_main_base_url", return_value="https://main.example/v1"),
            patch("agent.auxiliary_client._read_main_api_key", return_value="main-key"),
            patch("agent.auxiliary_client.resolve_provider_client", side_effect=resolve),
            patch("agent.auxiliary_client._to_async_client", return_value=(async_client, "model-valid")),
            patch("agent.auxiliary_client._call_fallback_candidate_async", side_effect=call_candidate),
        ):
            result = await _run_fallback_chain_async(
                _try_main_fallback_chain,
                {"task": "session_search", "failed_provider": "other", "failed_model": "failed-model", "failure_scope": FailureScope.MODEL},
                {"task": "session_search", "model": "model-valid", "messages": []},
            )
        assert result == {"selected": "model-valid"}
        assert [kwargs["model"] for _, kwargs in resolver_calls] == ["model-valid"]
        assert resolver_calls[0][1]["explicit_api_key"] == "entry-key-b"
        assert resolver_calls[0][1]["allow_provider_fallback"] is False
