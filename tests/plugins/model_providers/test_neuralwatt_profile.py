"""Unit tests for the NeuralWatt provider profile.

Pins the per-model ``reasoning_effort`` wire-shape contract (NEURALWATT
publishes different effort levels and aliases per model on ``/v1/models``),
the per-model max-output cap, preview/variant handling, quota/usage fetch,
and the retryable ``internal_routing_error`` 400 classification.

Catalog verified against the live authenticated /v1/models on 2026-08-21
(22 model ids incl. variants).
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
        assert "reasoning_effort" not in top_level

    # -- deepseek-v4-flash (xhigh→max) --------------------------------
    def test_flash_xhigh_maps_to_max(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="deepseek-v4-flash",
        )
        assert top_level["reasoning_effort"] == "max"

    def test_flash_high_stays_high(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="deepseek-v4-flash",
        )
        assert top_level["reasoning_effort"] == "high"

    def test_flash_none_disables(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"},
            model="deepseek-v4-flash",
        )
        assert top_level["reasoning_effort"] == "none"

    # -- deepseek-v4-pro (xhigh→high — the KEY divergence) ------------
    def test_pro_xhigh_maps_to_high(self, neuralwatt_profile):
        """On pro, xhigh resolves to HIGH (its published alias), not max."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="deepseek-v4-pro",
        )
        assert top_level["reasoning_effort"] == "high"

    def test_pro_max_stays_max(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model="deepseek-v4-pro",
        )
        assert top_level["reasoning_effort"] == "max"

    def test_pro_defaults_to_max_when_enabled(self, neuralwatt_profile):
        """Enabled without an explicit effort → our provider default (max)."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True}, model="deepseek-v4-pro"
        )
        assert top_level["reasoning_effort"] == "max"

    # -- glm-5.2 (xhigh→max, medium→high, minimal→none) ---------------
    def test_glm_xhigh_maps_to_max(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="glm-5.2",
        )
        assert top_level["reasoning_effort"] == "max"

    def test_glm_medium_maps_to_high(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            model="glm-5.2",
        )
        assert top_level["reasoning_effort"] == "high"

    def test_glm_minimal_maps_to_none(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "minimal"},
            model="glm-5.2",
        )
        assert top_level["reasoning_effort"] == "none"

    # -- variants (fast/flex/short) ------------------------------------
    @pytest.mark.parametrize("mid", ["glm-5.2-fast", "glm-5.2-short-fast-flex"])
    def test_glm_fast_variants_honor_contract(self, neuralwatt_profile, mid):
        """-fast variants share the effort vocab; server default off."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"}, model=mid
        )
        assert top_level["reasoning_effort"] == "max"

    def test_flash_flex_variant_resolves(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="deepseek-v4-flash-flex",
        )
        assert top_level["reasoning_effort"] == "max"

    def test_hf_slug_alias_resolves_to_flash(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="deepseek-ai/DeepSeek-V4-Flash",
        )
        assert top_level["reasoning_effort"] == "max"

    def test_canary_pin_folds_to_parent(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="deepseek-v4-flash-0731-canary",
        )
        assert top_level["reasoning_effort"] == "high"

    # -- no-effort-capability models (kimi-k2.7) -----------------------
    @pytest.mark.parametrize(
        "mid", ["kimi-k2.7-code", "kimi-k2.7-code-fast", "kimi-k2.7-code-flex"]
    )
    def test_kimi_k27_never_sends_effort(self, neuralwatt_profile, mid):
        """kimi-k2.7 family: capabilities.reasoning_effort=False — omit."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"}, model=mid
        )
        assert top_level == {}

    # -- qwen native xhigh ---------------------------------------------
    def test_qwen38_keeps_xhigh_verbatim(self, neuralwatt_profile):
        """qwen-3.8-27b serves xhigh natively — do not clamp down."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "xhigh"},
            model="qwen-3.8-27b",
        )
        assert top_level["reasoning_effort"] == "xhigh"

    def test_qwen38_max_aliases_to_xhigh(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model="qwen-3.8-27b",
        )
        assert top_level["reasoning_effort"] == "xhigh"

    def test_qwen36_defaults_to_server_high(self, neuralwatt_profile):
        """qwen3.6-35b has no max — enabled-no-effort → its server high."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True}, model="qwen3.6-35b"
        )
        assert top_level["reasoning_effort"] == "high"

    # -- disabled -------------------------------------------------------
    def test_explicitly_disabled_sends_none(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="deepseek-v4-flash",
        )
        assert top_level["reasoning_effort"] == "none"

    # -- session fingerprint -------------------------------------------
    def test_session_id_emitted_as_user_field(self, neuralwatt_profile):
        """Routing fingerprint: OpenAI user field carries the session id."""
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config=None,
            model="deepseek-v4-flash",
            session_id="abc123-session-hex",
        )
        # f"hermes-{session_id[-32:]}" — whole id is shorter than 32 chars
        assert top_level["user"] == "hermes-abc123-session-hex", top_level

    def test_no_session_id_no_user_field(self, neuralwatt_profile):
        _, top_level = neuralwatt_profile.build_api_kwargs_extras(
            reasoning_config=None, model="deepseek-v4-flash", session_id=""
        )
        assert "user" not in top_level


class TestNeuralWattMaxTokens:
    """Per-model output caps from the live contract."""

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("deepseek-v4-flash", 65536),
            ("deepseek-v4-pro", 393216),
            ("glm-5.2", 65536),  # no published cap → profile floor
            ("gemma-4-31b", 16384),
            ("glm-5.2-short", 32000),
            ("kimi-k3", 65536),
        ],
    )
    def test_per_model_output_caps(self, neuralwatt_profile, model, expected):
        assert neuralwatt_profile.get_max_tokens(model) == expected


class TestNeuralWattCapabilities:
    """glm family has no JSON mode; preview + fallback list semantics."""

    def test_glm_json_mode_false(self, neuralwatt_profile):
        from plugins.model_providers.neuralwatt import _NEURALWATT_STATIC_CONTRACT

        assert _NEURALWATT_STATIC_CONTRACT["glm-5.2"]["json_mode"] is False
        assert _NEURALWATT_STATIC_CONTRACT["deepseek-v4-flash"]["json_mode"] is True

    @pytest.mark.parametrize("preview", ["deepseek-v4-pro", "qwen-3.8-27b"])
    def test_preview_models_flagged(self, neuralwatt_profile, preview):
        assert neuralwatt_profile.is_preview(preview) is True
        assert neuralwatt_profile.is_preview("deepseek-v4-flash") is False

    def test_fallback_chain_flash_pro_glm(self, neuralwatt_profile):
        """Decision 2026-08-21: fallback chain flash → pro → glm-5.2."""
        assert list(neuralwatt_profile.fallback_models) == [
            "deepseek-v4-flash",
            "deepseek-v4-pro",
            "glm-5.2",
        ]


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

    @pytest.mark.parametrize(
        "method,args,expected_suffix",
        [
            ("fetch_sessions", {}, "/usage/sessions?limit=20&offset=0"),
            ("fetch_session_families", {}, "/usage/sessions/families?limit=20"),
            ("fetch_session_detail", {"session_id": "abc"}, "/usage/sessions/abc"),
            ("fetch_requests", {}, "/usage/requests?limit=20"),
        ],
    )
    def test_usage_endpoint_urls(
        self, neuralwatt_profile, monkeypatch, method, args, expected_suffix
    ):
        captured = {}

        class _FakeResp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        import hermes_cli.urllib_security as sec

        def fake_open(req, timeout):
            captured["url"] = req.full_url
            return _FakeResp()

        monkeypatch.setattr(sec, "open_credentialed_url", fake_open, raising=False)
        result = getattr(neuralwatt_profile, method)(
            api_key="sk-test",
            base_url="https://api.neuralwatt.com/v1",
            timeout=5,
            **args,
        )
        assert result == {"ok": True}
        assert captured["url"].endswith(expected_suffix), captured["url"]


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


class TestNeuralWattCommand:
    """The shared /neuralwatt dispatch layers (unit, no live API)."""

    def test_help_lists_subcommands(self, neuralwatt_profile, monkeypatch):
        import hermes_cli.neuralwatt_cmd as ncmd

        monkeypatch.setattr(ncmd, "_profile", lambda: neuralwatt_profile)
        monkeypatch.setattr(
            ncmd, "_runtime_profile_kwargs", lambda: {"api_key": "x", "base_url": "u"}
        )
        out = ncmd.handle_neuralwatt_command("", surface="gateway")
        for token in ("models", "quota", "sessions", "families", "analyze"):
            assert token in out

    def test_unknown_subcommand_message(self, neuralwatt_profile, monkeypatch):
        import hermes_cli.neuralwatt_cmd as ncmd

        monkeypatch.setattr(ncmd, "_profile", lambda: neuralwatt_profile)
        monkeypatch.setattr(
            ncmd, "_runtime_profile_kwargs", lambda: {"api_key": "x", "base_url": "u"}
        )
        out = ncmd.handle_neuralwatt_command("bogus", surface="gateway")
        assert "Unknown subcommand" in out
