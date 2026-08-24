"""Regression tests for issue #92817 — Z.ai Coding Plan vision routing.

Before the fix, ``resolve_vision_provider_client`` carried two hardcoded Z.ai
assumptions that broke vision for every Coding Plan subscriber:

1. **Auto mode**: ``_PROVIDER_VISION_MODELS["zai"] = "glm-5v-turbo"`` — a
   static, endpoint-blind default. Coding Plan subscriptions do not include
   glm-5v-turbo, so the call returned ``429 code 1311`` and the chain
   silently degraded to the text-only main model → ``400 code 1210``
   (``messages.content.type is invalid``).
2. **Explicit mode** (``auxiliary.vision.provider: zai`` with no
   ``base_url``): the fallback URL list pinned vision to the *general* API
   endpoints, which are billed independently of the Coding Plan → ``429
   code 1113`` (insufficient balance for a valid plan key) → same 1210.

Measured live with a Coding Plan key (issue table): ``glm-4.5v`` on the
coding endpoint ``https://api.z.ai/api/coding/paas/v4`` returns 200 OK and
is included in the plan — the routing just never sent it there.

The fix makes vision routing endpoint-aware:

- the zai provider profile owns its vision default via
  ``default_vision_model()`` (coding endpoint → ``glm-4.5v``, general API →
  ``glm-5v-turbo``), replacing the hardcoded dict entry;
- auto mode reuses the live main-runtime endpoint so vision hits the same
  billing pool as the main model;
- explicit-provider mode prepends the main-runtime endpoint (rewriting the
  Anthropic wire to its OpenAI-wire coding sibling) before the general-API
  fallback list, and defaults to a vision-capable model instead of the
  text-only aux default (``glm-4.5-flash``).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def zai_profile():
    """Resolve the registered Z.AI profile through the real discovery path."""
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("zai")
    assert profile is not None, "zai provider profile must be registered"
    return profile


# ── The profile owns the endpoint-aware default ─────────────────────────────


class TestZaiProfileVisionDefault:
    """``default_vision_model()`` picks a model the endpoint actually serves."""

    def test_coding_endpoint_returns_glm45v(self, zai_profile):
        assert (
            zai_profile.default_vision_model(
                "https://api.z.ai/api/coding/paas/v4"
            )
            == "glm-4.5v"
        )

    def test_bigmodel_cn_coding_endpoint_returns_glm45v(self, zai_profile):
        assert (
            zai_profile.default_vision_model(
                "https://open.bigmodel.cn/api/coding/paas/v4"
            )
            == "glm-4.5v"
        )

    def test_anthropic_wire_coding_endpoint_returns_glm45v(self, zai_profile):
        assert (
            zai_profile.default_vision_model("https://api.z.ai/api/anthropic")
            == "glm-4.5v"
        )

    def test_general_endpoint_keeps_glm5v_turbo(self, zai_profile):
        assert (
            zai_profile.default_vision_model("https://api.z.ai/api/paas/v4")
            == "glm-5v-turbo"
        )

    def test_no_base_url_keeps_glm5v_turbo(self, zai_profile):
        assert zai_profile.default_vision_model(None) == "glm-5v-turbo"

    def test_lookalike_host_not_treated_as_zai(self, zai_profile):
        url = "https://api.z.ai.evil.example.com/api/coding/paas/v4"
        assert zai_profile.default_vision_model(url) == "glm-5v-turbo"

    def test_path_marker_alone_not_coding(self, zai_profile):
        url = "https://gateway.example.com/api.z.ai/api/coding/paas/v4"
        assert zai_profile.default_vision_model(url) == "glm-5v-turbo"


class TestResolveProviderVisionDefault:
    """``_resolve_provider_vision_default`` honours the endpoint context."""

    def test_zai_coding_endpoint(self):
        from agent.auxiliary_client import _resolve_provider_vision_default

        assert (
            _resolve_provider_vision_default(
                "zai", base_url="https://api.z.ai/api/coding/paas/v4"
            )
            == "glm-4.5v"
        )

    def test_zai_general_endpoint(self):
        from agent.auxiliary_client import _resolve_provider_vision_default

        assert _resolve_provider_vision_default("zai") == "glm-5v-turbo"


# ── Auto mode: endpoint-aware model + endpoint reuse ────────────────────────


class TestVisionAutoZaiEndpointAware:
    def _run_auto(self, runtime_base_url):
        fake_client = MagicMock(name="zai_vision_client")
        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fake_client, None),
        ) as mock_resolve:
            from agent.auxiliary_client import resolve_vision_provider_client

            provider, client, model = resolve_vision_provider_client(
                main_runtime={
                    "provider": "zai",
                    "model": "glm-5.3",
                    "base_url": runtime_base_url,
                    "api_key": "zai-runtime-key",
                    "api_mode": "chat_completions",
                }
            )
        return provider, client, model, mock_resolve

    def test_coding_runtime_uses_glm45v_on_coding_endpoint(self):
        """Auto vision on a coding-plan main runtime must reuse the runtime
        endpoint and send the coding-served vision model, not glm-5v-turbo."""
        provider, client, model, mock_resolve = self._run_auto(
            "https://api.z.ai/api/coding/paas/v4"
        )

        assert provider == "zai"
        assert client is not None
        assert model == "glm-4.5v"
        assert mock_resolve.call_args.args[1] == "glm-4.5v"
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs.get("explicit_base_url") == "https://api.z.ai/api/coding/paas/v4"
        assert kwargs.get("explicit_api_key") == "zai-runtime-key"
        assert kwargs.get("api_mode") == "chat_completions"

    def test_general_runtime_keeps_glm5v_turbo(self):
        """General-API users keep glm-5v-turbo (still the static default),
        but the vision call now reuses the live main endpoint."""
        provider, _client, model, mock_resolve = self._run_auto(
            "https://api.z.ai/api/paas/v4"
        )

        assert provider == "zai"
        assert model == "glm-5v-turbo"
        assert mock_resolve.call_args.args[1] == "glm-5v-turbo"
        assert (
            mock_resolve.call_args.kwargs.get("explicit_base_url")
            == "https://api.z.ai/api/paas/v4"
        )

    def test_bigmodel_coding_runtime_uses_glm45v_on_coding_endpoint(self):
        """The BigModel CN host routes exactly like api.z.ai in auto mode —
        both hosts of the pair must drive the same endpoint reuse."""
        provider, client, model, mock_resolve = self._run_auto(
            "https://open.bigmodel.cn/api/coding/paas/v4"
        )

        assert provider == "zai"
        assert client is not None
        assert model == "glm-4.5v"
        assert mock_resolve.call_args.args[1] == "glm-4.5v"
        kwargs = mock_resolve.call_args.kwargs
        assert (
            kwargs.get("explicit_base_url")
            == "https://open.bigmodel.cn/api/coding/paas/v4"
        )
        assert kwargs.get("explicit_api_key") == "zai-runtime-key"
        assert kwargs.get("api_mode") == "chat_completions"


# ── Explicit mode: prepend the main-runtime endpoint ────────────────────────


class TestVisionExplicitZaiEndpointAware:
    def _run_explicit(self, main_runtime, task_model=None, read_main_provider="zai"):
        fake_client = MagicMock(name="zai_vision_client")
        calls = []

        def fake_gcc(
            provider,
            model,
            async_mode,
            base_url=None,
            api_key=None,
            api_mode=None,
            main_runtime=None,
            is_vision=False,
            task=None,
        ):
            calls.append(
                {
                    "provider": provider,
                    "model": model,
                    "base_url": base_url,
                    "api_key": api_key,
                    "api_mode": api_mode,
                }
            )
            return (fake_client, model)

        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("zai", task_model, None, None, None),
        ), patch(
            "agent.auxiliary_client._get_cached_client", side_effect=fake_gcc
        ), patch(
            "agent.auxiliary_client._read_main_provider",
            return_value=read_main_provider,
        ):
            from agent.auxiliary_client import resolve_vision_provider_client

            provider, client, model = resolve_vision_provider_client(
                provider="zai", main_runtime=main_runtime
            )
        return provider, client, model, calls

    def test_coding_runtime_prepends_coding_endpoint_with_glm45v(self):
        """Explicit ``provider: zai`` with no base_url must try the live
        coding-plan endpoint first — the general API bills the key wrong."""
        provider, client, model, calls = self._run_explicit(
            {
                "provider": "zai",
                "model": "glm-5.3",
                "base_url": "https://api.z.ai/api/coding/paas/v4",
                "api_key": "zai-runtime-key",
                "api_mode": "chat_completions",
            }
        )

        assert provider == "zai"
        assert client is not None
        assert model == "glm-4.5v"
        assert calls[0]["base_url"] == "https://api.z.ai/api/coding/paas/v4"
        assert calls[0]["model"] == "glm-4.5v"
        assert calls[0]["api_key"] == "zai-runtime-key"
        assert calls[0]["api_mode"] == "chat_completions"

    def test_anthropic_wire_runtime_rewritten_to_coding_openai_wire(self):
        """A main runtime on the /api/anthropic wire maps to the OpenAI-wire
        coding endpoint for vision (max_tokens/1210-safe)."""
        provider, _client, model, calls = self._run_explicit(
            {
                "provider": "zai",
                "model": "glm-5.3",
                "base_url": "https://api.z.ai/api/anthropic",
                "api_key": "zai-runtime-key",
                "api_mode": "anthropic_messages",
            }
        )

        assert provider == "zai"
        assert model == "glm-4.5v"
        assert calls[0]["base_url"] == "https://api.z.ai/api/coding/paas/v4"
        assert calls[0]["model"] == "glm-4.5v"
        assert calls[0]["api_mode"] == "chat_completions"

    def test_bigmodel_anthropic_wire_runtime_rewritten_to_coding_openai_wire(self):
        """Both Z.AI hosts' /api/anthropic form must map to the OpenAI-wire
        coding endpoint — not just api.z.ai (#92817 review point 3)."""
        provider, _client, model, calls = self._run_explicit(
            {
                "provider": "zai",
                "model": "glm-5.3",
                "base_url": "https://open.bigmodel.cn/api/anthropic",
                "api_key": "zai-runtime-key",
                "api_mode": "anthropic_messages",
            }
        )

        assert provider == "zai"
        assert model == "glm-4.5v"
        assert (
            calls[0]["base_url"] == "https://open.bigmodel.cn/api/coding/paas/v4"
        )
        assert calls[0]["model"] == "glm-4.5v"
        assert calls[0]["api_mode"] == "chat_completions"

    def test_non_zai_main_runtime_keeps_general_urls_and_vision_default(self):
        """When main is not zai, the general-API list remains — but the
        model default becomes the vision model, not the text-only aux
        default (glm-4.5-flash)."""
        provider, _client, model, calls = self._run_explicit(
            {
                "provider": "deepseek",
                "model": "deepseek-v4",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "ds-key",
            },
            read_main_provider="deepseek",
        )

        assert provider == "zai"
        assert model == "glm-5v-turbo"
        assert calls[0]["base_url"] == "https://open.bigmodel.cn/api/paas/v4"
        assert calls[0]["model"] == "glm-5v-turbo"

    def test_explicit_model_wins_over_endpoint_default(self):
        """An explicit ``auxiliary.vision.model`` is user intent — keep it
        even when it differs from the endpoint-aware default."""
        provider, _client, model, calls = self._run_explicit(
            {
                "provider": "zai",
                "model": "glm-5.3",
                "base_url": "https://api.z.ai/api/coding/paas/v4",
                "api_key": "zai-runtime-key",
                "api_mode": "chat_completions",
            },
            task_model="glm-5v-turbo",
        )

        assert provider == "zai"
        assert model == "glm-5v-turbo"
        assert calls[0]["base_url"] == "https://api.z.ai/api/coding/paas/v4"
        assert calls[0]["model"] == "glm-5v-turbo"


# ── Auto mode: explicit custom vision config wins over the Z.ai runtime ─────


class TestVisionAutoCustomPrecedence:
    def test_explicit_custom_vision_base_wins_over_zai_main_runtime(self):
        """An explicit custom vision base_url in config is user intent — a
        Z.ai-family main endpoint must not silently clobber it (#92817 review
        point 1)."""
        fake_client = MagicMock(name="custom_vision_client")
        with patch(
            "agent.auxiliary_client._resolve_task_provider_model",
            return_value=("auto", None, None, None, None),
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fake_client, None),
        ) as mock_resolve, patch(
            "agent.auxiliary_client._read_main_provider",
            return_value="custom:zai",
        ), patch(
            "agent.auxiliary_client._read_main_model",
            return_value="glm-5.3",
        ), patch(
            "agent.auxiliary_client._read_main_base_url",
            return_value="https://api.z.ai/api/coding/paas/v4",
        ), patch(
            "agent.auxiliary_client._resolve_custom_runtime",
            return_value=(
                "https://gateway.example.com/v1",
                "gateway-key",
                "chat_completions",
            ),
        ):
            from agent.auxiliary_client import resolve_vision_provider_client

            provider, client, model = resolve_vision_provider_client(
                main_runtime={}
            )

        assert provider == "custom:zai"
        assert client is not None
        kwargs = mock_resolve.call_args.kwargs
        assert kwargs.get("explicit_base_url") == "https://gateway.example.com/v1"
        assert kwargs.get("explicit_api_key") == "gateway-key"
        assert kwargs.get("api_mode") == "chat_completions"


# ── Host facts live in the zai plugin; aux delegates to the profile ─────────


class TestZaiProfileHostCheck:
    """``ZaiProfile.is_zai_host_url`` — the single source aux delegates to."""

    def test_profile_accepts_both_hosts(self, zai_profile):
        assert zai_profile.is_zai_host_url("https://api.z.ai/api/paas/v4") is True
        assert (
            zai_profile.is_zai_host_url("https://open.bigmodel.cn/api/anthropic")
            is True
        )

    def test_profile_rejects_lookalike_and_path_markers(self, zai_profile):
        assert (
            zai_profile.is_zai_host_url("https://api.z.ai.evil.example.com/api/paas/v4")
            is False
        )
        assert (
            zai_profile.is_zai_host_url(
                "https://gateway.example.com/api.z.ai/api/paas/v4"
            )
            is False
        )
        assert zai_profile.is_zai_host_url(None) is False


class TestAuxZaiHostCheckDelegation:
    """aux ``_is_zai_host_url`` asks the registered zai profile and only falls
    back to its local host pair when the plugin is not loaded."""

    def test_aux_accepts_both_hosts(self, zai_profile):
        from agent.auxiliary_client import _is_zai_host_url

        assert _is_zai_host_url("https://api.z.ai/api/paas/v4") is True
        assert _is_zai_host_url("https://open.bigmodel.cn/api/anthropic") is True

    def test_aux_rejects_lookalike_and_path_markers(self, zai_profile):
        from agent.auxiliary_client import _is_zai_host_url

        assert (
            _is_zai_host_url("https://api.z.ai.evil.example.com/api/paas/v4")
            is False
        )
        assert (
            _is_zai_host_url(
                "https://gateway.example.com/api.z.ai/api/paas/v4"
            )
            is False
        )
        assert _is_zai_host_url("") is False
        assert _is_zai_host_url(None) is False

    def test_aux_falls_back_to_local_pair_without_profile(self):
        from agent.auxiliary_client import _is_zai_host_url

        with patch("providers.get_provider_profile", return_value=None):
            assert _is_zai_host_url("https://api.z.ai/api/paas/v4") is True
            assert _is_zai_host_url("https://open.bigmodel.cn/api/paas/v4") is True
            assert _is_zai_host_url("https://api.z.ai.evil.example.com/x") is False


# ── Fallback logging names the endpoint it degraded to ──────────────────────


class TestMainFallbackLogNamesEndpoint:
    def test_fallback_log_includes_main_endpoint(self, caplog):
        fake_client = MagicMock(name="main_agent_client")
        with patch(
            "agent.auxiliary_client._read_main_provider", return_value="zai"
        ), patch(
            "agent.auxiliary_client._read_main_model", return_value="glm-5.3"
        ), patch(
            "agent.auxiliary_client._read_main_base_url",
            return_value="https://api.z.ai/api/coding/paas/v4",
        ), patch(
            "agent.auxiliary_client.resolve_provider_client",
            return_value=(fake_client, "glm-5.3"),
        ):
            from agent.auxiliary_client import _try_main_agent_model_fallback

            with caplog.at_level(logging.INFO, logger="agent.auxiliary_client"):
                client, model, label = _try_main_agent_model_fallback(
                    "zai-vision", task="vision", reason="rate limit"
                )

        assert client is fake_client
        assert model == "glm-5.3"
        assert label == "main-agent(zai)"
        assert "glm-5.3" in caplog.text
        assert "https://api.z.ai/api/coding/paas/v4" in caplog.text
