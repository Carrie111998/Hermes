"""Codex event-idle watchdog must honor the reasoning-model floor.

Regression for xAI Grok on ``codex_responses`` / xai-oauth:

After the first SSE frame arrives, Grok with ``reasoning_effort=high|xhigh``
can go silent for minutes while thinking. The context-tier idle defaults
(12 / 60 / 120 / 180s) killed those healthy mid-think streams with:

  Codex stream produced no SSE events for 120s after first byte

before the non-stream fallback (and its own reasoning floor) ever ran.
``resolve_codex_event_idle_timeout`` raises the *implicit* default to the
reasoning floor; explicit ``HERMES_CODEX_EVENT_STALE_TIMEOUT_SECONDS`` still
wins (including 0 to disable).
"""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "model,default,expected",
    [
        ("grok-4.5", 120.0, 600.0),
        ("x-ai/grok-4.5", 120.0, 600.0),
        ("grok-build-0.1", 60.0, 600.0),
        ("nvidia/nemotron-3-ultra-550b-a55b", 120.0, 600.0),
        ("gpt-4o", 120.0, 120.0),  # non-reasoning, no elevated effort: unchanged
        ("x-ai/grok-4", 120.0, 120.0),  # bare grok-4 still not on model floor
    ],
)
def test_implicit_default_raises_for_reasoning_models(model, default, expected):
    from agent.chat_completion_helpers import resolve_codex_event_idle_timeout

    timeout, enabled = resolve_codex_event_idle_timeout(
        default_seconds=default,
        env_raw=None,
        model=model,
    )
    assert enabled is True
    assert timeout == expected


def test_elevated_reasoning_effort_raises_even_for_non_allowlisted_model():
    """Silence floor is effort-driven, not SKU-driven.

    gpt-4o is intentionally off the model allowlist, but xhigh still
    produces multi-minute silent thinking on many providers — the Codex
    event-idle watchdog must not kill at 120s.
    """
    from agent.chat_completion_helpers import resolve_codex_event_idle_timeout

    timeout, enabled = resolve_codex_event_idle_timeout(
        default_seconds=120.0,
        env_raw=None,
        model="gpt-4o",
        reasoning_config={"enabled": True, "effort": "xhigh"},
    )
    assert enabled is True
    assert timeout == 900.0

    timeout_high, _ = resolve_codex_event_idle_timeout(
        default_seconds=120.0,
        env_raw=None,
        model="some-unknown-chat-model",
        reasoning_config="high",
    )
    assert timeout_high == 600.0


def test_explicit_env_wins_over_reasoning_floor():
    from agent.chat_completion_helpers import resolve_codex_event_idle_timeout

    timeout, enabled = resolve_codex_event_idle_timeout(
        default_seconds=120.0,
        env_raw="45",
        model="grok-4.5",
    )
    assert enabled is True
    assert timeout == 45.0


def test_explicit_zero_disables_watchdog():
    from agent.chat_completion_helpers import resolve_codex_event_idle_timeout

    timeout, enabled = resolve_codex_event_idle_timeout(
        default_seconds=120.0,
        env_raw="0",
        model="grok-4.5",
    )
    assert enabled is False
    assert timeout == 0.0


def test_invalid_env_falls_back_to_default_plus_floor():
    from agent.chat_completion_helpers import resolve_codex_event_idle_timeout

    timeout, enabled = resolve_codex_event_idle_timeout(
        default_seconds=120.0,
        env_raw="not-a-number",
        model="grok-4.5",
    )
    assert enabled is True
    assert timeout == 600.0
