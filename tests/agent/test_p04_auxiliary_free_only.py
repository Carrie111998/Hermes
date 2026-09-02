"""P0.4 regression guard — auxiliary free-only governance.

Goal (per task):
  * Every auxiliary route (title gen, vision, approval, housekeeping,
    compression, session search, web extract) may ONLY use FREE models.
  * When ``auxiliary.free_only: true``, auxiliary must NEVER route to
    OpenRouter via a paid SKU.
  * The conversation path (main agent) must NOT be affected — its fallback
    is governed independently by P0.3.

Audit finding (auxiliary_client.py):
  Before P0.4 the only free_only guard lived in ``_try_openrouter``
  (Step-3 discovery).  Two PAID escape hatches remained:
    1. Step 1 of ``_resolve_auto_route`` reused the user's MAIN chat model
       directly — a paid main model (Claude / paid OpenRouter SKU) ran
       auxiliary side-tasks.
    2. Step 2 consulted the user's ``auxiliary.<task>.fallback_chain`` and
       the main-agent ``fallback_providers``/``fallback_model`` — which can
       name paid Claude/OpenRouter entries.
  P0.4 adds ``_aux_free_only()`` and gates BOTH Step 1 (skip non-:free main
  model) and Step 2 (skip all user fallback chains) when free_only is on.
  ``_try_openrouter`` already refuses non-:free SKUs under free_only.

The tests drive the real functions:
  * ``_aux_free_only`` (config read)
  * ``_try_openrouter`` (OpenRouter paid-lane block)
  * ``_is_free_model`` (the free detector used by Step-1 skip)
  * ``get_fallback_chain`` (proves conversation/main-agent chain is
    untouched by the auxiliary gate — only the auxiliary path skips it)
"""

from __future__ import annotations

import os
import types

from hermes_cli.fallback_config import get_fallback_chain


def _monkeypatch_config(monkeypatch, aux_cfg):
    """Point load_config_readonly at a fixed dict for the auxiliary gates."""
    import agent.auxiliary_client as ac

    class _Cfg(dict):
        pass

    cfg = {"auxiliary": aux_cfg}
    monkeypatch.setattr(ac, "load_config_readonly", lambda: cfg)
    # _aux_openrouter_settings also imports hermes_cli.config.load_config_readonly
    import hermes_cli.config as hc

    monkeypatch.setattr(hc, "load_config_readonly", lambda: cfg)
    monkeypatch.setattr(hc, "cfg_get",
                        lambda c, *a, **k: c.get(a[0], {}).get(a[1], k.get("default")))


class TestP04_FreeOnlyConfigRead:
    def test_aux_free_only_true(self, monkeypatch):
        _monkeypatch_config(monkeypatch, {"free_only": True})
        from agent.auxiliary_client import _aux_free_only

        assert _aux_free_only() is True

    def test_aux_free_only_false_default(self, monkeypatch):
        _monkeypatch_config(monkeypatch, {})
        from agent.auxiliary_client import _aux_free_only

        assert _aux_free_only() is False


class TestP04_OpenRouterNeverPaidUnderFreeOnly:
    def test_paid_openrouter_model_blocked(self, monkeypatch):
        """free_only=true + non-:free model → OpenRouter NOT selected."""
        _monkeypatch_config(monkeypatch, {"free_only": True})
        from agent.auxiliary_client import _try_openrouter

        client, model = _try_openrouter(model="google/gemini-3.6-flash")
        assert client is None, "OpenRouter paid SKU must be rejected under free_only"
        assert model is None

    def test_free_openrouter_model_allowed(self, monkeypatch):
        """free_only=true + :free model → OpenRouter may be used (free)."""
        _monkeypatch_config(monkeypatch, {"free_only": True})
        from agent.auxiliary_client import _try_openrouter

        sentinel = object()
        monkeypatch.setattr(
            "agent.auxiliary_client._create_openai_client", lambda **k: sentinel)
        monkeypatch.setattr(
            "agent.auxiliary_client._scoped_key_env", lambda *_a, **_k: "or-key")
        client, model = _try_openrouter(model="nvidia/nemotron-3-ultra-550b-a55b:free")
        assert client is sentinel, "OpenRouter :free SKU must be allowed under free_only"
        assert model == "nvidia/nemotron-3-ultra-550b-a55b:free"

    def test_paid_openrouter_blocked_without_explicit_model(self, monkeypatch):
        """free_only=true + default (paid) openrouter_model → blocked."""
        _monkeypatch_config(monkeypatch, {"free_only": True})
        from agent.auxiliary_client import _try_openrouter

        client, model = _try_openrouter()
        assert client is None


class TestP04_MainModelPaidEscapeClosed:
    def test_paid_main_model_is_not_free(self):
        """Step-1 skip hinges on _is_free_model rejecting a paid main model."""
        from agent.auxiliary_client import _is_free_model

        assert _is_free_model("anthropic/claude-sonnet-4") is False
        assert _is_free_model("openai/gpt-4o") is False
        assert _is_free_model("google/gemini-3.6-flash") is False
        # Free models (current Nous default) are still allowed for aux.
        assert _is_free_model("tencent/hy3:free") is True
        assert _is_free_model("nvidia/nemotron-3-ultra-550b-a55b:free") is True


class TestP04_ConversationPathUnaffected:
    def test_main_agent_fallback_chain_still_readable(self, monkeypatch):
        """The auxiliary free_only gate must NOT mutate the main-agent
        fallback chain config — conversation/aux-main-fallback still reads
        the configured chain verbatim.  Only the auxiliary resolution path
        chooses to skip it under free_only (P0.3 governs conversation)."""
        cfg = {
            "fallback_providers": [
                {"provider": "openrouter", "model": "anthropic/claude-3.5-sonnet"}
            ]
        }
        chain = get_fallback_chain(cfg)
        assert chain == [
            {"provider": "openrouter", "model": "anthropic/claude-3.5-sonnet"}
        ]
        # Sanity: that entry is paid, confirming the gate must actively skip
        # it for auxiliary (tested via _try_main_fallback_chain under free_only
        # being bypassed upstream in _resolve_auto_route).
        from agent.auxiliary_client import _is_free_model

        assert _is_free_model(chain[0]["model"]) is False
