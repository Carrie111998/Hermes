"""Unit tests for the mid-session cache-rebuild notice.

Pins the gate and message-builder behaviour of
``hermes_cli.cache_switch_notice`` so a future refactor cannot silently
regress the cost signal. The integration paths (CLI apply + gateway
finish/picker) just call the same helpers — if these unit tests hold,
the surfaces emit the same shape of text.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli.cache_switch_notice import (
    MIN_CONTEXT_TOKENS,
    build_cache_switch_notice,
    cache_switch_notice_for_agent,
    estimate_context_tokens,
)


class _FakeCompressor:
    def __init__(self, last_prompt_tokens: int):
        self.last_prompt_tokens = last_prompt_tokens


class _FakeAgent:
    def __init__(
        self,
        *,
        last_prompt_tokens: int = 0,
        messages=None,
        system_prompt: str = "",
        tools=None,
    ):
        self.context_compressor = (
            _FakeCompressor(last_prompt_tokens) if last_prompt_tokens is not None else None
        )
        self.conversation_history = list(messages or [])
        self.system_prompt = system_prompt
        self.tools = tools


# ---------------------------------------------------------------------------
# estimate_context_tokens
# ---------------------------------------------------------------------------


def test_estimate_prefers_provider_reported_tokens():
    agent = _FakeAgent(last_prompt_tokens=84_000)
    assert estimate_context_tokens(agent) == 84_000


def test_estimate_clamps_compression_sentinel():
    # last_prompt_tokens parks at -1 right after a compression until the next
    # real API call reports usage — treat that as "unknown" (0).
    agent = _FakeAgent(last_prompt_tokens=-1)
    assert estimate_context_tokens(agent) == 0


def test_estimate_returns_zero_for_none_agent():
    assert estimate_context_tokens(None) == 0


def test_estimate_falls_back_to_rough_count_when_no_provider_report(monkeypatch):
    # No provider-reported tokens; force the rough estimator to a known value.
    agent = _FakeAgent(last_prompt_tokens=0, messages=[{"role": "user", "content": "hi"}])
    monkeypatch.setattr(
        "agent.model_metadata.estimate_request_tokens_rough",
        lambda *a, **kw: 42_000,
    )
    assert estimate_context_tokens(agent) == 42_000


# ---------------------------------------------------------------------------
# build_cache_switch_notice
# ---------------------------------------------------------------------------


def test_build_silent_below_threshold():
    assert (
        build_cache_switch_notice(
            old_model_display="Grok 4.6",
            new_model_display="Grok 4.5",
            est_context_tokens=MIN_CONTEXT_TOKENS - 1,
        )
        is None
    )


def test_build_silent_on_same_model():
    # Re-selecting the same model keeps the cache warm — nothing to warn about.
    assert (
        build_cache_switch_notice(
            old_model_display="Grok 4.6",
            new_model_display="Grok 4.6",
            est_context_tokens=100_000,
        )
        is None
    )


def test_build_silent_on_empty_names():
    assert (
        build_cache_switch_notice(
            old_model_display="",
            new_model_display="Grok 4.5",
            est_context_tokens=100_000,
        )
        is None
    )


def test_build_emits_notice_and_revert_hint_above_threshold():
    notice = build_cache_switch_notice(
        old_model_display="Ox Alpha",
        new_model_display="Grok 4.5",
        est_context_tokens=84_000,
    )
    assert notice is not None
    lines = notice.splitlines()
    assert len(lines) == 2
    # Notice names the new model and rounds the estimate to ~Nk.
    assert "Grok 4.5" in lines[0]
    assert "~84k" in lines[0]
    assert "uncached" in lines[0].lower() or "uncached" in lines[0]
    # Revert hint names the OLD model and gives the /model command.
    assert "Ox Alpha" in lines[1]
    assert "/model Ox Alpha" in lines[1]


def test_build_rounds_tokens_to_nearest_k():
    notice = build_cache_switch_notice(
        old_model_display="A",
        new_model_display="B",
        est_context_tokens=30_499,  # rounds to 30k
    )
    assert notice is not None
    assert "~30k" in notice

    notice = build_cache_switch_notice(
        old_model_display="A",
        new_model_display="B",
        est_context_tokens=30_500,  # rounds to 31k
    )
    assert notice is not None
    assert "~31k" in notice


# ---------------------------------------------------------------------------
# cache_switch_notice_for_agent (config + estimate composition)
# ---------------------------------------------------------------------------


def test_for_agent_respects_config_toggle_off(monkeypatch):
    agent = _FakeAgent(last_prompt_tokens=100_000)
    monkeypatch.setattr(
        "hermes_cli.cache_switch_notice.cache_switch_notice_enabled",
        lambda: False,
    )
    assert (
        cache_switch_notice_for_agent(
            agent=agent,
            old_model_display="A",
            new_model_display="B",
        )
        is None
    )


def test_for_agent_emits_when_enabled_and_above_threshold(monkeypatch):
    agent = _FakeAgent(last_prompt_tokens=100_000)
    monkeypatch.setattr(
        "hermes_cli.cache_switch_notice.cache_switch_notice_enabled",
        lambda: True,
    )
    notice = cache_switch_notice_for_agent(
        agent=agent,
        old_model_display="A",
        new_model_display="B",
    )
    assert notice is not None
    assert "B" in notice
    assert "/model A" in notice


def test_for_agent_silent_on_empty_session(monkeypatch):
    agent = _FakeAgent(last_prompt_tokens=0)
    monkeypatch.setattr(
        "hermes_cli.cache_switch_notice.cache_switch_notice_enabled",
        lambda: True,
    )
    # Force the rough estimate too, so we don't accidentally trip on a
    # non-zero structural estimate of system prompt + tools.
    monkeypatch.setattr(
        "agent.model_metadata.estimate_request_tokens_rough",
        lambda *a, **kw: 1_000,
    )
    assert (
        cache_switch_notice_for_agent(
            agent=agent,
            old_model_display="A",
            new_model_display="B",
        )
        is None
    )


def test_cache_switch_notice_enabled_defaults_true_on_config_error(monkeypatch):
    # A broken config.yaml must not silently suppress the cost signal.
    def _boom():
        raise RuntimeError("config unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", _boom)
    from hermes_cli.cache_switch_notice import cache_switch_notice_enabled

    assert cache_switch_notice_enabled() is True


def test_cache_switch_notice_enabled_reads_false(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"display": {"cache_switch_notice": False}},
    )
    from hermes_cli.cache_switch_notice import cache_switch_notice_enabled

    assert cache_switch_notice_enabled() is False
