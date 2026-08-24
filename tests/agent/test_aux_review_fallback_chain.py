"""Regression tests: auxiliary review forks must honor configured fallback chains.

``_run_review_in_thread()`` (background_review) and ``_run_llm_review()``
(curator) spawn forked ``AIAgent`` objects whose failover runs through
``run_conversation()`` → ``_try_activate_fallback()``, which consults only
``agent._fallback_chain`` — built from the constructor's ``fallback_model``
argument. Neither fork passed one, so ``auxiliary.<task>.fallback_chain``
was silently ignored and a primary-provider failure killed the whole review
with zero fallback attempts (#93592 background_review, #78371 curator).

What these tests pin down:

A. The chain resolver returns per-task entries first, then the top-level
   chain as safety net, deduplicated by backend identity.
B. The background-review fork is CONSTRUCTED with that chain (wiring).
C. The curator fork is CONSTRUCTED with its own chain (wiring).

Construction-time wiring is asserted through a fake agent class because the
fork's post-construction flow (tool whitelist, usage accounting) is already
covered by dedicated suites; here every fake method is inert by contract.
"""

from __future__ import annotations

import copy
import os
import sys
import types

import pytest

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from hermes_cli.fallback_config import resolve_aux_task_fallback_chain


def _entry(provider: str, model: str, **extra) -> dict:
    out = {"provider": provider, "model": model}
    out.update(extra)
    return out


def _patch_config(monkeypatch, cfg: dict) -> None:
    """Route every ``load_config_readonly()`` call to an in-memory config."""
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config_readonly",
        lambda: copy.deepcopy(cfg),
    )


# ---------------------------------------------------------------------------
# A. resolve_aux_task_fallback_chain
# ---------------------------------------------------------------------------


class TestResolveAuxTaskFallbackChain:
    def test_task_entries_first_then_top_level(self):
        cfg = {
            "auxiliary": {
                "background_review": {
                    "fallback_chain": [_entry("custom:a", "model-a")]
                }
            },
            "fallback_providers": [_entry("nous", "m1")],
        }
        chain = resolve_aux_task_fallback_chain("background_review", config=cfg)
        assert [e["provider"] for e in chain] == ["custom:a", "nous"]

    def test_deduplicates_by_backend_identity(self):
        cfg = {
            "auxiliary": {
                "curator": {
                    "fallback_chain": [_entry("nous", "m1")]
                }
            },
            "fallback_providers": [
                _entry("NOUS", "M1"),  # same identity, different case
                _entry("cohere", "c1"),
            ],
        }
        chain = resolve_aux_task_fallback_chain("curator", config=cfg)
        assert [(e["provider"], e["model"]) for e in chain] == [
            ("nous", "m1"),
            ("cohere", "c1"),
        ]

    def test_invalid_entries_dropped_not_fatal(self):
        cfg = {
            "auxiliary": {
                "background_review": {
                    "fallback_chain": [
                        "junk",
                        {"provider": "x"},  # missing model
                        _entry("ok", "fine"),
                    ]
                }
            },
        }
        chain = resolve_aux_task_fallback_chain("background_review", config=cfg)
        assert chain == [_entry("ok", "fine")]

    def test_no_config_anywhere_yields_empty_list(self):
        assert resolve_aux_task_fallback_chain("background_review", config={}) == []

    def test_returns_fresh_dicts(self):
        entry = _entry("nous", "m1")
        cfg = {
            "auxiliary": {"background_review": {"fallback_chain": [entry]}},
        }
        chain = resolve_aux_task_fallback_chain("background_review", config=cfg)
        chain[0]["provider"] = "mutated"
        assert entry["provider"] == "nous"

    def test_entry_fields_survive_resolution(self):
        cfg = {
            "auxiliary": {
                "background_review": {
                    "fallback_chain": [
                        _entry(
                            "custom:x",
                            "mx",
                            base_url="https://api.x.com/v1/",
                            key_env="X_API_KEY",
                            transport="anthropic_messages",
                        )
                    ]
                }
            },
        }
        chain = resolve_aux_task_fallback_chain("background_review", config=cfg)
        assert len(chain) == 1
        got = chain[0]
        # base_url normalized (trailing slash stripped), credentials and
        # wire format preserved for the conversation-loop failover consumer.
        assert got["base_url"] == "https://api.x.com/v1"
        assert got["key_env"] == "X_API_KEY"
        # ``transport`` is aliased to api_mode — the only spelling
        # _try_activate_fallback reads.
        assert got["api_mode"] == "anthropic_messages"


# ---------------------------------------------------------------------------
# B/C. Construction wiring for both forks
# ---------------------------------------------------------------------------


class _FakeReviewFork:
    """Inert stand-in capturing constructor kwargs."""

    def __init__(self, **kwargs):
        self.constructor_kwargs = kwargs
        self._memory_enabled = False
        self._user_profile_enabled = False
        self._session_messages = []

    def run_conversation(self, *args, **kwargs):
        return {"final_response": ""}

    def shutdown_memory_provider(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def fake_fork(monkeypatch):
    captured = {}

    class _CapturingFork(_FakeReviewFork):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            captured.update(kwargs)

    import run_agent

    monkeypatch.setattr(run_agent, "AIAgent", _CapturingFork)
    return captured


def _fake_parent(**extra) -> types.SimpleNamespace:
    base = dict(
        model="parent-model",
        provider="parent-prov",
        platform="cli",
        session_id="s-parent",
        session_start="2026-08-24T00:00:00",
        enabled_toolsets=None,
        disabled_toolsets=None,
        reasoning_config=None,
        ephemeral_system_prompt=None,
        prefill_messages=None,
        memory_notifications="on",
        cached_system_prompt=None,
        _current_main_runtime=lambda: {},
        _credential_pool=None,
        request_overrides={},
        _memory_store=None,
        _cached_system_prompt=None,
        # Memory/profile flags copied onto the fork after construction.
        _memory_enabled=False,
        _user_profile_enabled=False,
        # User-facing surfaces the worker touches on completion paths.
        background_review_callback=None,
        _safe_print=lambda *args, **kwargs: None,
        _emit_auxiliary_failure=lambda *args, **kwargs: None,
    )
    base.update(extra)
    return types.SimpleNamespace(**base)


class TestBackgroundReviewWiring:
    def test_fork_constructed_with_configured_chain(self, tmp_path, monkeypatch, fake_fork):
        # Same provider/model as the parent -> _resolve_review_runtime takes
        # the non-routed shortcut and no real provider resolution happens.
        task_block = {
            "provider": "parent-prov",
            "model": "parent-model",
            "fallback_chain": [
                _entry("working", "m1"),
                _entry("backup", "m2"),
            ],
        }
        _patch_config(monkeypatch, {"auxiliary": {"background_review": task_block}})

        from agent.background_review import _run_review_in_thread

        _run_review_in_thread(
            _fake_parent(),
            [{"role": "user", "content": "hi"}],
            "review prompt",
            task_cfg=task_block,
        )
        assert fake_fork["fallback_model"] == [
            _entry("working", "m1"),
            _entry("backup", "m2"),
        ]

    def test_fork_without_any_chain_gets_none(self, monkeypatch, fake_fork):
        _patch_config(monkeypatch, {})
        from agent.background_review import _run_review_in_thread

        _run_review_in_thread(
            _fake_parent(),
            [{"role": "user", "content": "hi"}],
            "p",
            task_cfg={},
        )
        assert fake_fork["fallback_model"] is None


class TestCuratorWiring:
    def test_curator_fork_constructed_with_configured_chain(self, monkeypatch, fake_fork):
        cfg = {
            "model": {"provider": "nous", "default": "main-m"},
            "auxiliary": {
                "curator": {
                    "fallback_chain": [_entry("working", "c1")],
                }
            },
        }
        _patch_config(monkeypatch, cfg)

        from agent.curator import _run_llm_review

        result = _run_llm_review("curator prompt")
        assert fake_fork["fallback_model"] == [_entry("working", "c1")]
        # With an inert fake fork the pass completes cleanly.
        assert result["error"] is None


class TestChainReadsLiveConfig:
    def test_resolver_reads_live_config_when_none_passed(self, monkeypatch):
        cfg = {
            "auxiliary": {
                "background_review": {
                    "fallback_chain": [_entry("live", "l1")]
                }
            },
        }
        _patch_config(monkeypatch, cfg)
        # No explicit config -> resolver loads through load_config_readonly().
        chain = resolve_aux_task_fallback_chain("background_review")
        assert chain == [_entry("live", "l1")]
