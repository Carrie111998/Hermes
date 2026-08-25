"""Routed background reviews must honor auxiliary.background_review.reasoning_effort (#94825).

The review fork is a full AIAgent, not an auxiliary_client call, and the
routed branch deliberately skips the parent's reasoning_config (its effort
vocabulary may not be valid for the routed model). But an explicitly
configured ``auxiliary.background_review.reasoning_effort`` — declared config
since #64597 — must win over provider defaults, mirroring how every other
auxiliary task folds the same key into ``extra_body.reasoning``.
"""
from __future__ import annotations

import types

import pytest

import agent.background_review as br


class _StubAgent:
    """Minimal parent satisfying the fork path's attribute reads."""

    model = "gpt-5.6-terra"
    provider = "openai-codex"
    platform = "cli"
    session_id = "sess-review-effort"
    enabled_toolsets = None
    disabled_toolsets = None
    reasoning_config = {"enabled": True, "effort": "high"}
    ephemeral_system_prompt = None
    prefill_messages = None

    def __init__(self):
        for attr in (
            "providers_allowed", "providers_ignored", "providers_order",
            "provider_sort", "provider_require_parameters",
            "provider_data_collection",
        ):
            setattr(self, attr, None)

    def _safe_print(self, *a, **k):
        pass

    _memory_store = None
    _memory_enabled = False
    _session_db = None

    def _emit_auxiliary_failure(self, *a, **k):
        pass


def _run_with_captured_fork(monkeypatch, routed_runtime, task_cfg):
    captured = {}

    class _FakeFork:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.session_id = "fork"
            self._session_db = None
            self._memory_write_origin = None
            self._memory_write_context = None

    monkeypatch.setattr("run_agent.AIAgent", _FakeFork)
    monkeypatch.setattr(
        br, "_resolve_review_runtime", lambda agent, cfg: routed_runtime
    )

    # Silence the review run itself: capture run_conversation so the thread
    # returns before touching any provider.
    monkeypatch.setattr(
        br, "_REVIEW_MAX_ITERATIONS", 1, raising=False
    )
    ran = {}

    def _fake_run(self, *a, **k):
        ran["called"] = True
        return None

    _FakeFork.run_conversation = _fake_run
    monkeypatch.setattr(
        br, "_snapshot_review_usage", lambda ra: {}, raising=False
    )
    monkeypatch.setattr(
        br, "_record_review_usage_to_parent",
        lambda *a, **k: None, raising=False,
    )
    monkeypatch.setattr(
        br, "_classify_review_result", lambda actions: "ok", raising=False
    )
    monkeypatch.setattr(
        br, "_log_review_completion", lambda *a, **k: None, raising=False
    )

    agent = _StubAgent()
    br._run_review_in_thread(
        agent,
        [{"role": "user", "content": "review me"}],
        "review prompt",
        task_cfg=task_cfg,
    )
    return captured, ran


ROUTED_RUNTIME = {
    "routed": True,
    "model": "gpt-5.6-luna-900k",
    "provider": "openai-codex",
    "api_mode": None,
    "base_url": None,
    "api_key": None,
    "credential_pool": None,
    "request_overrides": {},
}


def test_routed_review_applies_configured_effort(monkeypatch):
    captured, _ = _run_with_captured_fork(
        monkeypatch,
        ROUTED_RUNTIME,
        {"reasoning_effort": "xhigh"},
    )
    assert captured["reasoning_config"] == {"enabled": True, "effort": "xhigh"}


def test_routed_review_without_effort_uses_provider_default(monkeypatch):
    captured, _ = _run_with_captured_fork(
        monkeypatch,
        ROUTED_RUNTIME,
        {"reasoning_effort": ""},
    )
    assert "reasoning_config" not in captured, (
        "no explicit effort: the routed fork keeps provider defaults"
    )


def test_routed_review_invalid_effort_ignored_with_warning(monkeypatch, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        captured, _ = _run_with_captured_fork(
            monkeypatch,
            ROUTED_RUNTIME,
            {"reasoning_effort": "ludicrous"},
        )
    assert "reasoning_config" not in captured
    assert "not a valid level" in caplog.text


def test_same_model_review_still_inherits_parent_config(monkeypatch):
    captured, _ = _run_with_captured_fork(
        monkeypatch,
        dict(ROUTED_RUNTIME, routed=False),
        {"reasoning_effort": "xhigh"},
    )
    # Same-model path is unchanged: parent parity wins for cache reuse.
    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
