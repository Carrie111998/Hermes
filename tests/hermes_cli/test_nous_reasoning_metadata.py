"""Tests for Nous Portal reasoning-capability metadata.

The Portal serves OpenRouter's catalog schema, so it reuses
``parse_openrouter_reasoning_capabilities`` and the same cache-only tri-state
contract. What is Portal-specific: it 403s a catalog read that arrives
without a User-Agent, and its ``reasoning.mandatory`` flag is what decides
whether a disable can be sent at all.
"""

import pytest


def _mock_response(body: bytes):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return body

    return _Resp()


_CATALOG = (
    b'{"data": ['
    b'{"id": "deepseek/deepseek-v4-pro", "supported_parameters": ["reasoning", "tools"],'
    b' "reasoning": {"mandatory": false, "supported_efforts": ["xhigh", "high"]}},'
    b'{"id": "arcee-ai/trinity-large-thinking", "supported_parameters": ["reasoning"],'
    b' "reasoning": {"mandatory": true}}'
    b']}'
)


_NATIVE_ANTHROPIC_CATALOG = (
    b'{"data": ['
    b'{"id": "anthropic/claude-fable-5", "supported_parameters": []},'
    b'{"id": "anthropic/claude-fable-5:batch",'
    b' "supported_parameters": ["reasoning", "reasoning_effort"],'
    b' "reasoning": {"mandatory": true, "supported_efforts": ["max", "xhigh", "high"]}},'
    b'{"id": "anthropic/claude-opus-5", "supported_parameters": []},'
    b'{"id": "anthropic/claude-opus-5:batch",'
    b' "supported_parameters": ["reasoning", "reasoning_effort"],'
    b' "reasoning": {"mandatory": false, "supported_efforts": ["max", "high", "low"]}},'
    b'{"id": "anthropic/claude-haiku-4.5", "supported_parameters": []},'
    b'{"id": "anthropic/claude-haiku-4.5:batch",'
    b' "supported_parameters": ["reasoning"]},'
    b'{"id": "other/model", "supported_parameters": []},'
    b'{"id": "other/model:batch", "supported_parameters": ["reasoning"]}'
    b']}'
)


@pytest.fixture
def cold_cache(monkeypatch):
    """A freshly started process that has never mirrored a catalog to disk."""
    import hermes_cli.models as models_mod

    monkeypatch.setattr(models_mod, "_nous_reasoning_caps_cache", None)
    monkeypatch.setattr(models_mod, "_nous_reasoning_caps_failed_at", None)
    monkeypatch.setattr(models_mod, "_nous_caps_disk_checked", False)
    monkeypatch.setattr(models_mod, "_nous_caps_warm_started", False)
    models_mod._reasoning_caps_disk_path().unlink(missing_ok=True)
    return models_mod


class TestNousModelReasoningCapabilities:
    def test_fetch_parses_mandatory_flag(self, cold_cache, monkeypatch):
        from hermes_cli.models import nous_model_reasoning_capabilities

        monkeypatch.setattr(
            cold_cache, "_urlopen_model_catalog_request",
            lambda req, *, timeout: _mock_response(_CATALOG),
        )
        optional = nous_model_reasoning_capabilities(
            "deepseek/deepseek-v4-pro", allow_fetch=True
        )
        assert optional["supports_reasoning"] is True
        assert optional["mandatory"] is False
        assert optional["supported_efforts"] == ["xhigh", "high"]

        mandatory = nous_model_reasoning_capabilities("arcee-ai/trinity-large-thinking")
        assert mandatory["mandatory"] is True

    def test_native_anthropic_base_uses_batch_reasoning_contract(
        self, cold_cache, monkeypatch
    ):
        """Empty base metadata must not hide native Messages effort controls."""
        from hermes_cli.models import nous_model_reasoning_capabilities

        monkeypatch.setattr(
            cold_cache,
            "_urlopen_model_catalog_request",
            lambda req, *, timeout: _mock_response(_NATIVE_ANTHROPIC_CATALOG),
        )

        fable = nous_model_reasoning_capabilities(
            "anthropic/claude-fable-5", allow_fetch=True
        )
        assert fable == {
            "supports_reasoning": True,
            "supported_efforts": ["max", "xhigh", "high"],
            "mandatory": True,
        }

        opus = nous_model_reasoning_capabilities("anthropic/claude-opus-5")
        assert opus["supports_reasoning"] is True
        assert opus["mandatory"] is False

    def test_native_anthropic_fallback_is_scoped_to_supported_adapter_models(
        self, cold_cache, monkeypatch
    ):
        """Do not borrow batch metadata for Haiku or non-Anthropic routes."""
        from hermes_cli.models import nous_model_reasoning_capabilities

        monkeypatch.setattr(
            cold_cache,
            "_urlopen_model_catalog_request",
            lambda req, *, timeout: _mock_response(_NATIVE_ANTHROPIC_CATALOG),
        )
        nous_model_reasoning_capabilities(
            "anthropic/claude-fable-5", allow_fetch=True
        )

        assert nous_model_reasoning_capabilities(
            "anthropic/claude-haiku-4.5"
        ) == {"supports_reasoning": False}
        assert nous_model_reasoning_capabilities("other/model") == {
            "supports_reasoning": False
        }

    def test_catalog_read_sends_user_agent(self, cold_cache, monkeypatch):
        """The Portal 403s an anonymous catalog read."""
        from hermes_cli.models import nous_model_reasoning_capabilities

        seen = []

        def _capture(req, *, timeout):
            seen.append(req)
            return _mock_response(_CATALOG)

        monkeypatch.setattr(cold_cache, "_urlopen_model_catalog_request", _capture)
        nous_model_reasoning_capabilities("deepseek/deepseek-v4-pro", allow_fetch=True)

        # urllib title-cases header names.
        assert seen[0].get_header("User-agent")

    def test_catalog_read_follows_the_configured_endpoint(
        self, cold_cache, monkeypatch
    ):
        """Capabilities come from the deployment we actually talk to.

        Pinned to production, a staging profile would take its
        reasoning-mandatory verdicts from a different deployment's catalog.
        """
        from hermes_cli.models import nous_catalog_url

        monkeypatch.setenv(
            "NOUS_INFERENCE_BASE_URL", "https://staging.nousresearch.com/v1"
        )
        assert nous_catalog_url() == "https://staging.nousresearch.com/v1/models"

        monkeypatch.delenv("NOUS_INFERENCE_BASE_URL")
        assert nous_catalog_url().endswith("/v1/models")

    def test_unlisted_and_empty_models_return_none(self, cold_cache, monkeypatch):
        from hermes_cli.models import nous_model_reasoning_capabilities

        monkeypatch.setattr(
            cold_cache, "_urlopen_model_catalog_request",
            lambda req, *, timeout: _mock_response(_CATALOG),
        )
        assert nous_model_reasoning_capabilities("private/route", allow_fetch=True) is None
        assert nous_model_reasoning_capabilities("") is None
        assert nous_model_reasoning_capabilities(None) is None

    def test_cache_only_by_default_never_fetches(self, cold_cache, monkeypatch):
        from hermes_cli.models import nous_model_reasoning_capabilities

        def _boom(req, *, timeout):
            raise AssertionError("hot path must not fetch")

        monkeypatch.setattr(cold_cache, "_urlopen_model_catalog_request", _boom)
        assert nous_model_reasoning_capabilities("deepseek/deepseek-v4-pro") is None

    def test_unreachable_catalog_rate_limits_refetch(self, cold_cache, monkeypatch):
        from hermes_cli.models import nous_model_reasoning_capabilities

        calls = {"n": 0}

        def _boom(req, *, timeout):
            calls["n"] += 1
            raise OSError("offline")

        monkeypatch.setattr(cold_cache, "_urlopen_model_catalog_request", _boom)
        assert nous_model_reasoning_capabilities("a/b", allow_fetch=True) is None
        assert nous_model_reasoning_capabilities("a/b", allow_fetch=True) is None
        # Second call inside the failure TTL must not re-fetch.
        assert calls["n"] == 1

    def test_openrouter_cache_is_independent(self, cold_cache, monkeypatch):
        """Two catalogs, two caches — a Portal fetch must not answer for OpenRouter."""
        from hermes_cli.models import (
            nous_model_reasoning_capabilities,
            openrouter_model_reasoning_capabilities,
        )

        monkeypatch.setattr(cold_cache, "_openrouter_reasoning_caps_cache", None)
        monkeypatch.setattr(cold_cache, "_openrouter_reasoning_caps_failed_at", None)
        monkeypatch.setattr(
            cold_cache, "_urlopen_model_catalog_request",
            lambda req, *, timeout: _mock_response(_CATALOG),
        )
        assert nous_model_reasoning_capabilities(
            "deepseek/deepseek-v4-pro", allow_fetch=True
        ) is not None
        assert openrouter_model_reasoning_capabilities("deepseek/deepseek-v4-pro") is None
