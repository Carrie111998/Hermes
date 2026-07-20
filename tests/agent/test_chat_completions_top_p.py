"""[hermes-v2] H-12 regression test: top_p passthrough on chat_completions.

Verifies that the openai-compat chat_completions transport forwards
top_p from the caller config to the API kwargs, both via the
provider-profile path (the standard for registered providers like
minimax/openrouter/etc.) and via the request_overrides path.

Before H-12 the standard path silently dropped top_p — only
temperature was forwarded. M3 (and ollama/step-flash/local 9B
models) accept top_p as a top-level field, so dropping it meant
the operator's top_p config had no effect.

Refs: H-12 (hermes-v2 plan, 2026-07-20).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


# ── helpers ────────────────────────────────────────────────────────────


class _FakeProfile:
    """Minimal stand-in for providers.base.ProviderProfile."""

    def __init__(
        self,
        fixed_temperature=None,
        max_tokens_default=None,
        extras_extra_body=None,
        extras_top_level=None,
    ):
        self.fixed_temperature = fixed_temperature
        self.max_tokens_default = max_tokens_default
        self._extras_body = extras_extra_body or {}
        self._extras_top = extras_top_level or {}

    def prepare_messages(self, messages):
        return messages

    def get_max_tokens(self, model):
        return self.max_tokens_default

    def build_api_kwargs_extras(self, **_):
        return self._extras_body, self._extras_top

    def build_extra_body(self, **_):
        return {}


def _make_transport():
    """Instantiate ChatCompletionsTransport with no live client."""
    from agent.transports.chat_completions import ChatCompletionsTransport
    return ChatCompletionsTransport()


def _params(profile, **overrides):
    p = {
        "timeout": 30.0,
        "max_tokens": 1024,
        "model_lower": "minimax-m3",
        "provider_profile": profile,
        "ollama_num_ctx": None,
        "supports_reasoning": False,
    }
    p.update(overrides)
    return p


def _messages():
    return [{"role": "user", "content": "hello"}]


# ── core regression test ───────────────────────────────────────────────


class TestTopPPassthrough:
    def test_top_p_forwarded_when_caller_provides(self) -> None:
        """Operator sets top_p=0.95 in config → reaches the wire."""
        from providers.base import OMIT_TEMPERATURE
        profile = _FakeProfile(fixed_temperature=OMIT_TEMPERATURE)
        t = _make_transport()
        params = _params(profile, top_p=0.95)
        kwargs = t.build_kwargs(model="MiniMax-M3", messages=_messages(), **params)
        assert kwargs.get("top_p") == 0.95, (
            f"top_p=0.95 must reach the wire; got {kwargs.get('top_p')!r}"
        )

    def test_top_p_omitted_when_caller_does_not_provide(self) -> None:
        """No top_p in config → don't add a default that could surprise
        downstream providers (some reject unknown fields)."""
        from providers.base import OMIT_TEMPERATURE
        profile = _FakeProfile(fixed_temperature=OMIT_TEMPERATURE)
        t = _make_transport()
        params = _params(profile)  # no top_p
        kwargs = t.build_kwargs(model="MiniMax-M3", messages=_messages(), **params)
        assert "top_p" not in kwargs

    def test_top_p_passes_alongside_temperature(self) -> None:
        """Both should reach the wire when caller sets both. Use a
        profile with no fixed_temperature (None) so caller's value
        flows through — OMIT_TEMPERATURE strips temperature entirely."""
        profile = _FakeProfile(fixed_temperature=None)
        t = _make_transport()
        params = _params(profile, temperature=1.0, top_p=0.95)
        kwargs = t.build_kwargs(model="MiniMax-M3", messages=_messages(), **params)
        assert kwargs.get("temperature") == 1.0
        assert kwargs.get("top_p") == 0.95

    def test_top_p_zero_is_kept(self) -> None:
        """top_p=0.0 (deterministic) is a valid value and must not be
        confused with None."""
        from providers.base import OMIT_TEMPERATURE
        profile = _FakeProfile(fixed_temperature=OMIT_TEMPERATURE)
        t = _make_transport()
        params = _params(profile, top_p=0.0)
        kwargs = t.build_kwargs(model="MiniMax-M3", messages=_messages(), **params)
        assert "top_p" in kwargs
        assert kwargs["top_p"] == 0.0

    def test_request_overrides_top_p_still_works(self) -> None:
        """The legacy request_overrides path must still forward top_p
        (regression coverage on the override route itself)."""
        from providers.base import OMIT_TEMPERATURE
        profile = _FakeProfile(fixed_temperature=OMIT_TEMPERATURE)
        t = _make_transport()
        params = _params(profile, request_overrides={"top_p": 0.8})
        kwargs = t.build_kwargs(model="MiniMax-M3", messages=_messages(), **params)
        assert kwargs.get("top_p") == 0.8

    def test_top_p_does_not_break_when_profile_omits_temperature(self) -> None:
        """Profile uses OMIT_TEMPERATURE; caller still gets top_p."""
        from providers.base import OMIT_TEMPERATURE
        profile = _FakeProfile(fixed_temperature=OMIT_TEMPERATURE)
        t = _make_transport()
        params = _params(profile, top_p=0.7)
        kwargs = t.build_kwargs(model="MiniMax-M3", messages=_messages(), **params)
        # Temperature is omitted entirely (profile's call), top_p is set.
        assert "temperature" not in kwargs
        assert kwargs["top_p"] == 0.7
