"""Unit tests for the custom provider profile's reasoning wiring.

``provider=custom`` covers any OpenAI-compatible endpoint the user points
Hermes at — local Ollama, vLLM, llama.cpp, and hosted reasoning APIs like
GLM-5.2 on Volcengine ARK. Before #57601's salvage, ``CustomProfile`` emitted
nothing when reasoning was *enabled*, so a configured ``reasoning_effort``
was silently dropped for every custom endpoint.

These tests pin the wire-shape contract (the enable cases pass
``supports_reasoning=True``, i.e. the target model declares a thinking
capability; the disable cases do not need it):
  - disabled            → extra_body.think = False
  - enabled + effort    → top-level reasoning_effort (native OpenAI-compat
                          format GLM/ARK expect), passed through verbatim
                          including ``max``/``xhigh``
  - enabled + no effort → nothing emitted (endpoint's server default applies)
  - ollama_num_ctx      → extra_body.options.num_ctx, orthogonal to reasoning

``TestCustomReasoningCapabilityGate`` covers the ``supports_reasoning=False``
side: local Ollama models without the "thinking" capability (e.g.
qwen2.5:7b) must never receive an effort value, since Ollama's
``/v1/chat/completions`` 400s with ``"<model>" does not support thinking``.
Disabling reasoning stays ungated — those same models accept
``reasoning_effort="none"`` and ``think=false`` with HTTP 200.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def custom_profile():
    """Resolve the registered custom profile via the global registry.

    Importing ``model_tools`` triggers plugin discovery, which registers the
    ``custom`` profile. Going through ``get_provider_profile`` keeps the test
    honest — if the registered class is ever downgraded to a plain
    ``ProviderProfile``, the assertions below collapse.
    """
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("custom")
    assert profile is not None, "custom provider profile must be registered"
    return profile


class TestCustomReasoningWireShape:
    """``build_api_kwargs_extras`` produces the correct wire format."""

    def test_no_reasoning_config_emits_nothing(self, custom_profile):
        """Unset reasoning → omit everything so the endpoint's default applies."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, model="glm-5.2"
        )
        assert eb == {}
        assert tl == {}

    def test_disabled_sends_think_false(self, custom_profile):
        """enabled=False → reasoning_effort='none' top-level + think=False.

        Both fields are required: Ollama's /v1/chat/completions silently
        ignores extra_body.think (only /api/chat honours it — ollama#14820)
        but respects top-level reasoning_effort (#25758). think=False stays
        for proxies and the native /api/chat path.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="glm-5.2"
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_effort_none_sends_think_false(self, custom_profile):
        """effort='none' is the disable alias → same dual emission."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "none"}, model="glm-5.2"
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    @pytest.mark.parametrize(
        "effort", ["minimal", "low", "medium", "high", "xhigh", "max"]
    )
    def test_enabled_effort_goes_top_level(self, custom_profile, effort):
        """enabled + effort → TOP-LEVEL reasoning_effort, passed through verbatim.

        GLM-5.2/ARK and OpenAI-compatible reasoning APIs read reasoning_effort
        as a top-level string, not nested in extra_body. ``max`` is GLM's
        native deep-reasoning level and must survive.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort}, model="glm-5.2",
            supports_reasoning=True,
        )
        assert tl == {"reasoning_effort": effort}
        assert "reasoning_effort" not in eb
        assert "think" not in eb


    def test_does_not_force_think_true_on_enable(self, custom_profile):
        """We must never send think=True on enable — it's Ollama-only and
        would 400 on GLM/vLLM endpoints that don't recognize it."""
        eb, _ = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"}, model="glm-5.2",
            supports_reasoning=True,
        )
        assert eb.get("think") is not True


class TestCustomReasoningWithNumCtx:
    """Ollama num_ctx and reasoning are independent and compose."""

    def test_num_ctx_alone(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config=None, ollama_num_ctx=8192, model="qwen3"
        )
        assert eb == {"options": {"num_ctx": 8192}}
        assert tl == {}

    def test_num_ctx_survives_capability_gate_suppressing_reasoning(self, custom_profile):
        """num_ctx must still be emitted even when supports_reasoning=False
        suppresses every reasoning field — the two are wired independently."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            ollama_num_ctx=8192,
            supports_reasoning=False,
            model="qwen2.5:7b",
        )
        assert eb == {"options": {"num_ctx": 8192}}
        assert tl == {}


class TestCustomReasoningCapabilityGate:
    """``supports_reasoning`` gates the ENABLE branch only.

    Reproduces the reported bug: a local Ollama model without the "thinking"
    capability (qwen2.5:7b) must never receive an effort value — Ollama's
    /v1/chat/completions 400s with ``"qwen2.5:7b" does not support thinking``.

    Measured against that endpoint with qwen2.5:7b:

        reasoning_effort="medium" → HTTP 400 (does not support thinking)
        reasoning_effort="none"   → HTTP 200
        think=false               → HTTP 200

    So the rejection is about *enabling* thinking, not about the field being
    present. The disable branch stays ungated accordingly.
    """

    def test_supports_reasoning_false_suppresses_effort(self, custom_profile):
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"},
            supports_reasoning=False,
            model="qwen2.5:7b",
        )
        assert eb == {}
        assert tl == {}

    def test_supports_reasoning_false_still_emits_disable_fields(self, custom_profile):
        """The explicit-disable branch stays ungated.

        Non-thinking models accept the disable fields with HTTP 200 (see the
        class docstring), so gating this branch would buy nothing and would
        silently drop a user's explicit "don't reason" on any route whose
        capability probe is unavailable — leaving a thinking-capable model
        reasoning against instructions.
        """
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False},
            supports_reasoning=False,
            model="qwen2.5:7b",
        )
        assert eb == {"think": False}
        assert tl == {"reasoning_effort": "none"}

    def test_supports_reasoning_true_emits_effort(self, custom_profile):
        """Non-regression: a local Ollama model that DOES declare thinking
        (e.g. deepseek-r1) still gets reasoning_effort wired through."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            supports_reasoning=True,
            model="deepseek-r1",
        )
        assert tl == {"reasoning_effort": "high"}
        assert "think" not in eb

    def test_default_is_false_when_omitted(self, custom_profile):
        """The parameter defaults to False (fail closed) when a caller omits
        it entirely — see the transport-level non-regression test for proof
        every real call site always passes it explicitly."""
        eb, tl = custom_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="qwen2.5:7b",
        )
        assert eb == {}
        assert tl == {}

