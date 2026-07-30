"""Telnyx provider wiring tests.

The profile registers through ``plugins/model-providers/telnyx/`` and every
downstream surface (picker, env-var injection, doctor hints, URL→provider
inference, credential resolution) derives from the registry. These tests pin
the derived wiring plus the two hand-edited surfaces in
``agent/model_metadata.py``: the ``telnyx:`` model prefix and the
per-1M-token pricing conversion.
"""

from __future__ import annotations

from decimal import Decimal


class TestTelnyxPickerRegistration:
    def test_canonical_providers_include_telnyx(self):
        from hermes_cli.models import CANONICAL_PROVIDERS, _PROVIDER_LABELS

        assert "telnyx" in {p.slug for p in CANONICAL_PROVIDERS}
        assert _PROVIDER_LABELS.get("telnyx") == "Telnyx"


class TestTelnyxConfigRegistry:
    def test_optional_env_vars_include_telnyx(self):
        from hermes_cli.config import OPTIONAL_ENV_VARS

        assert "TELNYX_API_KEY" in OPTIONAL_ENV_VARS
        entry = OPTIONAL_ENV_VARS["TELNYX_API_KEY"]
        assert entry["category"] == "provider"
        assert entry["password"] is True


class TestTelnyxDoctor:
    def test_provider_env_hints_include_telnyx(self):
        from hermes_cli.doctor import _PROVIDER_ENV_HINTS

        assert "TELNYX_API_KEY" in _PROVIDER_ENV_HINTS


class TestTelnyxAuth:
    def test_credentials_resolve_from_env(self, monkeypatch):
        from hermes_cli.auth import resolve_api_key_provider_credentials

        monkeypatch.setenv("TELNYX_API_KEY", "KEY_test_value")
        creds = resolve_api_key_provider_credentials("telnyx")
        assert creds["api_key"] == "KEY_test_value"
        assert creds["base_url"] == "https://api.telnyx.com/v2/ai/openai"


class TestTelnyxModelMetadata:
    def test_provider_prefix_registered_and_stripped(self):
        from agent.model_metadata import _PROVIDER_PREFIXES, _strip_provider_prefix

        assert "telnyx" in _PROVIDER_PREFIXES
        assert (
            _strip_provider_prefix("telnyx:moonshotai/Kimi-K3")
            == "moonshotai/Kimi-K3"
        )

    def test_url_infers_telnyx(self):
        from agent.model_metadata import _infer_provider_from_url

        assert (
            _infer_provider_from_url("https://api.telnyx.com/v2/ai/openai")
            == "telnyx"
        )


class TestTelnyxPricingUnitConversion:
    """Telnyx quotes pricing as per-1M-token strings (``unit: "1M_tokens"``).

    The generic alias walk in ``_extract_pricing`` would read those figures
    as per-token — a 1,000,000× overcharge — so the unit-tagged branch must
    convert before the generic path can see them.
    """

    PAYLOAD = {
        "id": "moonshotai/Kimi-K3",
        "context_length": 1000000,
        "pricing": {
            "input": "2.700000",
            "output": "13.500000",
            "cached_prompt": "0.270000",
            "currency": "USD",
            "unit": "1M_tokens",
        },
    }

    def test_per_million_strings_become_per_token(self):
        from agent.model_metadata import _extract_pricing

        pricing = _extract_pricing(self.PAYLOAD)
        assert float(pricing["prompt"]) == 2.7 / 1_000_000
        assert float(pricing["completion"]) == 13.5 / 1_000_000
        assert float(pricing["cache_read"]) == 0.27 / 1_000_000

    def test_never_returns_raw_per_million_figures(self):
        """The regression this branch prevents: the generic alias walk
        matching ``input``/``output`` and returning per-1M values as
        per-token."""
        from agent.model_metadata import _extract_pricing

        pricing = _extract_pricing(self.PAYLOAD)
        assert float(pricing["prompt"]) < 1e-3
        assert float(pricing["completion"]) < 1e-3

    def test_empty_pricing_dict_yields_no_entry(self):
        """Proxied routes like openai/gpt-4o-mini ship ``pricing: {}`` —
        no fabricated zeros, no crash."""
        from agent.model_metadata import _extract_pricing

        assert _extract_pricing({"id": "openai/gpt-4o-mini", "pricing": {}}) == {}

    def test_round_trips_through_cost_tracking(self):
        """End-to-end invariant: the per-1M figure Telnyx quotes is the
        per-1M figure the cost tracker bills."""
        from agent.model_metadata import _extract_pricing
        from agent.usage_pricing import _pricing_entry_from_metadata

        metadata = {"moonshotai/Kimi-K3": {"pricing": _extract_pricing(self.PAYLOAD)}}
        entry = _pricing_entry_from_metadata(
            metadata,
            "moonshotai/Kimi-K3",
            source_url="https://api.telnyx.com/v2/ai/openai/models",
            pricing_version="test",
        )
        assert entry is not None
        assert entry.input_cost_per_million == Decimal("2.7")
        assert entry.output_cost_per_million == Decimal("13.5")
        assert entry.cache_read_cost_per_million == Decimal("0.27")
