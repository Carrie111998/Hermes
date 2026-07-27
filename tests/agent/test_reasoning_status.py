"""Reasoning effort must never be silently inert.

The wire gate (``AIAgent._supports_reasoning_extra_body``) is an allowlist of
routes known to accept a reasoning field. Everything else returns False. That
is deliberate — other endpoints answer 400 — but until now an operator who set
``agent.reasoning_effort: medium`` against a self-hosted endpoint got no signal
at all that the setting was doing nothing.

These tests pin three things:
  1. the default gate behaviour (unchanged),
  2. the operator opt-in that makes it transmissible on a known-good endpoint,
  3. that the reported status tells the truth in every combination.
"""

from __future__ import annotations

import pytest

from agent.reasoning_status import (
    configured_effort,
    describe,
    passthrough_override,
    warning_line,
)


# ── the opt-in knob ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [True, "true", "yes", "on", "1", "always", "force"])
def test_passthrough_true_values(raw):
    assert passthrough_override({"agent": {"reasoning_passthrough": raw}}) is True


@pytest.mark.parametrize("raw", [False, "false", "no", "off", "0", "never"])
def test_passthrough_false_values(raw):
    assert passthrough_override({"agent": {"reasoning_passthrough": raw}}) is False


@pytest.mark.parametrize("cfg", [
    None, {}, {"agent": {}}, {"agent": None}, {"agent": {"reasoning_passthrough": None}},
])
def test_passthrough_absent_means_auto_detect(cfg):
    assert passthrough_override(cfg) is None


def test_unrecognised_passthrough_value_degrades_to_auto_detect():
    """A typo must not force a field onto a provider that rejects it."""
    assert passthrough_override({"agent": {"reasoning_passthrough": "hgih"}}) is None


# ── configured effort ────────────────────────────────────────────────────────

def test_configured_effort_reads_agent_section():
    assert configured_effort({"agent": {"reasoning_effort": "medium"}}) == "medium"


@pytest.mark.parametrize("cfg", [
    {}, {"agent": {}}, {"agent": {"reasoning_effort": ""}},
    {"agent": {"reasoning_effort": None}},
    {"agent": {"reasoning_effort": False}},   # explicit "thinking disabled"
])
def test_configured_effort_absent(cfg):
    assert configured_effort(cfg) is None


# ── the status report must tell the truth ────────────────────────────────────

def test_configured_but_unsupported_is_reported_as_not_sent():
    """The production shape: effort set, self-hosted route, gate says no."""
    status = describe(
        configured="medium", supported=False,
        provider="custom:litellm", model="mythos-heavy",
        base_url="http://127.0.0.1:4000/v1", override=None,
    )
    assert status["will_be_sent"] is False
    assert status["effective_effort"] is None, "must not report an effort that is not sent"
    assert status["configured_effort"] == "medium"
    assert "NOT sent" in status["summary"]
    assert "reasoning_passthrough" in status["reason"], "must name the remedy"


def test_supported_route_reports_the_effort_as_live():
    status = describe(
        configured="high", supported=True,
        provider="openrouter", model="anthropic/claude-x",
        base_url="https://openrouter.ai/api/v1", override=None,
    )
    assert status["will_be_sent"] is True
    assert status["effective_effort"] == "high"
    assert "IS sent" in status["summary"]
    assert status["reason"] is None


def test_opt_in_is_named_as_the_reason_it_is_sent():
    status = describe(
        configured="medium", supported=True, provider="custom:litellm",
        base_url="http://127.0.0.1:4000/v1", override=True,
    )
    assert status["will_be_sent"] is True
    assert "opt-in" in status["summary"]


def test_explicit_opt_out_is_reported_distinctly():
    status = describe(
        configured="medium", supported=False, provider="openrouter",
        base_url="https://openrouter.ai/api/v1", override=False,
    )
    assert status["will_be_sent"] is False
    assert "passthrough" in status["reason"]


def test_no_effort_configured_is_not_an_error():
    status = describe(configured=None, supported=False, provider="custom:litellm")
    assert status["will_be_sent"] is False
    assert status["reason"] is None
    assert warning_line(status) is None


# ── the operator warning ─────────────────────────────────────────────────────

def test_warning_fires_only_when_configured_and_dropped():
    dropped = describe(configured="medium", supported=False, provider="custom:litellm")
    live = describe(configured="medium", supported=True, provider="openrouter")

    line = warning_line(dropped)
    assert line is not None and "medium" in line and "NOT be sent" in line
    assert warning_line(live) is None


# ── the gate itself: default unchanged, override honoured ────────────────────

class _FakeAgent:
    """Minimal stand-in exercising the real gate logic via the real method."""

    def __init__(self, base_url: str, provider: str, override):
        self._base_url_lower = base_url.lower()
        self.provider = provider
        self.model = "mythos-heavy"
        self._reasoning_passthrough_cached = override

    _reasoning_passthrough_override = None  # bound below


def _make_gate():
    import run_agent

    return run_agent.AIAgent._supports_reasoning_extra_body, run_agent.AIAgent._reasoning_passthrough_override


def _gate(base_url: str, provider: str, override):
    gate, override_fn = _make_gate()
    agent = _FakeAgent(base_url, provider, override)
    type(agent)._reasoning_passthrough_override = override_fn
    return gate(agent)


def test_selfhosted_route_still_defaults_to_not_sending():
    """Historical behaviour must be preserved when the knob is unset."""
    assert _gate("http://127.0.0.1:4000/v1", "custom:litellm", None) is False


def test_operator_opt_in_enables_a_selfhosted_route():
    assert _gate("http://127.0.0.1:4000/v1", "custom:litellm", True) is True


def test_operator_opt_out_disables_even_a_supported_route():
    """The override must work in both directions, including over Nous Portal."""
    assert _gate("https://inference-api.nousresearch.com/v1", "nous", None) is True
    assert _gate("https://inference-api.nousresearch.com/v1", "nous", False) is False


# ── acceptance is not proof of effect ────────────────────────────────────────

def test_opt_in_sending_is_not_reported_as_proof_of_effect():
    """A route can accept reasoning_effort and ignore it.

    Probing this profile's LiteLLM router (2026-07-27) returned HTTP 200 for
    every effort level while reasoning-token medians stayed non-monotonic
    (minimal 56.5 > high 51.0 > control 53.5 > low 37.0, n=4 each) and the
    control with no field at all already produced reasoning. Reporting "IS
    sent" without that caveat would recreate the silent over-claim this module
    exists to prevent.
    """
    status = describe(
        configured="high", supported=True, provider="custom:litellm",
        base_url="http://127.0.0.1:4000/v1", override=True,
    )
    assert status["will_be_sent"] is True
    assert status["reason"], "opt-in must carry the acceptance-vs-effect caveat"
    assert "not proof of effect" in status["reason"]


def test_auto_detected_route_carries_no_caveat():
    """Only the operator opt-in path is unverified; auto-detected routes are
    ones Hermes already knows honor the field."""
    status = describe(
        configured="high", supported=True, provider="openrouter",
        base_url="https://openrouter.ai/api/v1", override=None,
    )
    assert status["reason"] is None
