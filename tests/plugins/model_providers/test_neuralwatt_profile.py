"""Unit tests for the NeuralWatt provider profile.

Pins the per-model ``reasoning_effort`` wire-shape contract (NEURALWATT
publishes different effort levels and aliases per model on ``/v1/models``),
the per-model max-output cap, quota/usage fetch, and the retryable
``internal_routing_error`` 400 classification.

Live-verified against the authenticated /v1/models catalog on 2026-08-21:
  - deepseek-v4-flash: efforts max/high/none, default none, xhigh→max, out 65536
  - deepseek-v4-pro:   efforts max/high/low/none, default low, xhigh→high, out 393216
  - glm-5.2:           efforts max/high/none, default max, xhigh→max, json_mode False
"""

from __future__ import annotations

import pytest


@pytest.fixture
def neuralwatt_profile():
    """Resolve the registered NeuralWatt profile.

    Going through ``providers.get_provider_profile`` keeps the test honest —
    if someone later replaces the registered class with a plain
    ``ProviderProfile``, every assertion below collapses.
    """
    # ``model_tools`` triggers plugin discovery on import, which is what
    # registers the NeuralWatt profile in the global provider registry.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("neuralwatt")
    assert profile is not None, "neuralwatt provider profile must be registered"
    return profile


class TestNeuralWattReasoningWireShape:
    """``build_api_kwargs_extras`` matches each model's published contract."""

    def test_no_config_omits_effort(self, neuralwatt_profile):
        """No reasoning config → nothing sent; the model's default applies."""
        extra_body, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config=None, model="deepseek-v4-flash"
        )
        assert extra_body == {}
        assert top_level == {}

    # -- deepseek-v4-flash (xhigh→max) --------------------------------
    def test_flash_xhigh_maps_to_max(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="deepseek-v4-flash",
        )
        assert top_level == {"reasoning_effort": "max"}

    def test_flash_high_stays_high(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="deepseek-v4-flash",
        )
        assert top_level == {"reasoning_effort": "high"}

    def test_flash_none_disables(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="deepseek-v4-flash",
        )
        assert top_level == {"reasoning_effort": "none"}

    # -- deepseek-v4-pro (xhigh→high — the KEY divergence) ------------
    def test_pro_xhigh_maps_to_high(self, neuralwatt_profile):
        """On pro, xhigh resolves to HIGH (its published alias), not max."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="deepseek-v4-pro",
        )
        assert top_level == {"reasoning_effort": "high"}

    def test_pro_max_stays_max(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model="deepseek-v4-pro",
        )
        assert top_level == {"reasoning_effort": "max"}

    def test_pro_defaults_to_low(self, neuralwatt_profile):
        """No config → nothing sent; pro server default is low."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config=None, model="deepseek-v4-pro"
        )
        assert top_level == {}

    # -- glm-5.2 (xhigh→max, medium→high, minimal→none) ---------------
    def test_glm_xhigh_maps_to_max(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="glm-5.2",
        )
        assert top_level == {"reasoning_effort": "max"}

    def test_glm_medium_maps_to_high(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model="glm-5.2",
        )
        assert top_level == {"reasoning_effort": "high"}

    def test_glm_minimal_maps_to_none(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "minimal"},
            model="glm-5.2",
        )
        assert top_level == {"reasoning_effort": "none"}

    # -- disabled -------------------------------------------------------
    def test_explicitly_disabled_sends_none(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="deepseek-v4-flash",
        )
        assert top_level == {"reasoning_effort": "none"}

    def test_unknown_model_uses_openai_ladder(self, neuralwatt_profile):
        """No published contract → clamp onto the widest wire vocabulary."""
        expected = {"ultra": "max", "xhigh": "xhigh", "none": "none"}
        for effort, wire in expected.items():
            _, top_level = neuralwatt_profile.build_api_kwargs_extras(
                reasoning_config={"enabled": True, "effort": effort},
                model="some-future-model",
            )
            assert top_level == {"reasoning_effort": wire}, (
                f"effort {effort} → expected {wire}, got {top_level}"
            )


class TestNeuralWattMaxTokens:
    """Per-model output caps from the live contract."""

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("deepseek-v4-flash", 65536),
            ("deepseek-v4-pro", 393216),
            ("glm-5.2", 65536),  # no published cap → profile floor
        ],
    )
    def test_per_model_output_caps(self, neuralwatt_profile, model, expected):
        assert neuralwatt_profile.get_max_tokens(model) == expected


class TestNeuralWattCapabilities:
    """glm-5.2 has no JSON mode; fallback list excludes the preview model."""

    def test_glm_json_mode_false(self, neuralwatt_profile):
        from plugins.model_providers.neuralwatt import _NEURALWATT_STATIC_CONTRACT

        assert _NEURALWATT_STATIC_CONTRACT["glm-5.2"]["json_mode"] is False
        assert _NEURALWATT_STATIC_CONTRACT["deepseek-v4-flash"]["json_mode"] is True

    def test_preview_pro_excluded_from_fallback(self, neuralwatt_profile):
        assert "deepseek-v4-pro" not in neuralwatt_profile.fallback_models
        assert "deepseek-v4-flash" in neuralwatt_profile.fallback_models


class TestNeuralWattQuotaFetch:
    """fetch_quota / fetch_usage resolve the right URL + auth."""

    def test_fetch_usage_path_and_creds(self, neuralwatt_profile, monkeypatch):
        captured = {}

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        import urllib.request

        def fake_open(req, timeout):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return _FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_open)
        # The security wrapper delegates to urllib.request.urlopen.
        import hermes_cli.urllib_security as sec

        monkeypatch.setattr(sec, "open_credentialed_url", fake_open, raising=False)
        result = neuralwatt_profile.fetch_usage(
            api_key="sk-test", base_url="https://api.neuralwatt.com/v1", timeout=5
        )
        assert result == {"ok": True}
        assert captured["url"].endswith("/usage/energy?period_days=30")
        assert captured["auth"] == "Bearer sk-test"

    def test_fetch_quota_requires_key(self, neuralwatt_profile):
        assert neuralwatt_profile.fetch_quota(api_key="", timeout=3) is None


class TestNeuralWattErrorClassification:
    """internal_routing_error is the ONE retryable 400 — provider-gated."""

    @pytest.mark.parametrize(
        "status, code, reason, retryable",
        [
            (400, "internal_routing_error", "server_error", True),
            (429, "rate_limit_exceeded", "rate_limit", True),
            (503, "model_overloaded", "overloaded", True),
            (401, "invalid_api_key", "auth", False),
        ],
    )
    def test_classify_neuralwatt(self, status, code, reason, retryable):
        import openai as _o
        from agent.error_classifier import classify_api_error

        body = {"error": {"message": "test", "code": code, "type": "error"}}
        resp = type(
            "R",
            (),
            {
                "status_code": status,
                "headers": {},
                "request": None,
                "text": "test",
                "json": lambda: body,
            },
        )()
        err = _o.BadRequestError("test", response=resp, body=body)
        r = classify_api_error(err, provider="neuralwatt", model="deepseek-v4-flash")
        assert r.reason.value == reason, f"{status}/{code} → {r.reason.value}"
        assert r.retryable is retryable

    def test_internal_routing_error_gated_to_neuralwatt(self):
        """Same 400 on another provider stays a non-retryable format error."""
        import openai as _o
        from agent.error_classifier import classify_api_error

        body = {"error": {"message": "x", "code": "internal_routing_error"}}
        resp = type(
            "R",
            (),
            {
                "status_code": 400,
                "headers": {},
                "request": None,
                "text": "x",
                "json": lambda: body,
            },
        )()
        err = _o.BadRequestError("x", response=resp, body=body)
        for provider, expected_retryable in [
            ("openrouter", False),
            ("neuralwatt", True),
        ]:
            r = classify_api_error(err, provider=provider, model="m")
            assert r.retryable is expected_retryable, provider
