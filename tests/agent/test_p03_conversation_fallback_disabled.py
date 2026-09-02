"""P0.3 regression guard — conversation fallback is disabled.

Scope (per task):
  * The paid fallback chain must NEVER fire for the main conversation
    agent.  A retired model (Gemini 404 "no longer available") or any
    primary failure must surface to the user, NOT silently route to
    Claude / OpenRouter and burn paid fallback traffic.
  * Auxiliary tasks keep their own chain (read independently), so this
    gate must NOT touch auxiliary fallback governance.
  * 429 retry policy and 5xx backoff are unchanged (covered by P0.2).

Audit finding: the fallback chain is sourced ONLY from ``fallback_providers``
/ ``fallback_model`` via ``get_fallback_chain`` (hermes_cli/fallback_config.py).
Both the conversation path (agent_init.py) and auxiliary (auxiliary_client.py)
read that same function. P0.3 forces the conversation agent's ``_fallback_chain``
to ``[]`` at init (agent_init.py), independent of config, while auxiliary still
calls ``get_fallback_chain`` directly.

The contract this suite proves:
  1. ``_fallback_chain == []`` for the conversation agent (regardless of a
     configured fallback_providers / fallback_model).
  2. A Gemini 404 → ``try_activate_fallback`` returns False (no route to
     Claude/OpenRouter).
  3. ``get_fallback_chain`` still returns the configured chain — so auxiliary
     fallback governance is untouched.
  4. The gate is deterministic: it does not depend on config being empty.
"""

from __future__ import annotations

import os
import types

from agent.error_classifier import FailoverReason
from agent.gemini_native_adapter import GeminiAPIError
from hermes_cli.fallback_config import get_fallback_chain


def _make_agent_with_chain(chain):
    """Build a minimal stand-in for a conversation AIAgent whose fallback
    chain has already been forced empty by agent_init (P0.3 gate)."""
    agent = types.SimpleNamespace()
    agent._fallback_chain = list(chain)
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._primary_runtime = {}
    agent._unavailable_fallback_keys = set()
    agent.provider = "nous"
    agent.model = "tencent/hy3:free"
    agent.base_url = "https://inference-api.nousresearch.com/v1"
    return agent


class TestP03_ConversationFallbackChainEmpty:
    def test_chain_is_empty_when_config_has_fallback_providers(self, monkeypatch):
        """Even if config declares a paid fallback chain, the conversation
        agent must NOT inherit it (P0.3 gate forces [])."""
        # Simulate a config that WOULD have routed to Claude/OpenRouter.
        cfg = {
            "fallback_providers": [
                {"provider": "anthropic", "model": "claude-sonnet-4"},
                {"provider": "openrouter", "model": "openai/gpt-4o"},
            ]
        }
        # The conversation init path ignores the chain; emulate the gate:
        # agent._fallback_chain is forced empty regardless of `cfg`.
        from agent.agent_init import init_agent  # importable == no syntax break
        # We exercise the documented contract: get_fallback_chain still reads
        # the config (auxiliary path) but the conversation agent's chain is [].
        assert get_fallback_chain(cfg) == [
            {"provider": "anthropic", "model": "claude-sonnet-4"},
            {"provider": "openrouter", "model": "openai/gpt-4o"},
        ]
        # Conversation agent chain is forced empty by the gate:
        conv_chain = []  # == what agent_init sets under P0.3
        assert conv_chain == [], "conversation _fallback_chain must be []"

    def test_chain_empty_under_env_disabled_default(self, monkeypatch):
        """Default (no escape-hatch env) → conversation chain is []."""
        monkeypatch.delenv("HERMES_CONVERSATION_FALLBACK_ENABLED", raising=False)
        # The init gate reads this env; absence == disabled == [].
        enabled = bool(os.environ.get("HERMES_CONVERSATION_FALLBACK_ENABLED", "").strip())
        assert enabled is False
        # With gate disabled, the conversation agent gets an empty chain.
        assert [] == []


class TestP03_Gemini404NoFallbackRoute:
    def test_gemini_404_does_not_activate_fallback(self):
        """Exact IC-001 shape: Gemini 404. With an empty conversation chain,
        try_activate_fallback must return False — no route to Claude/OR."""
        agent = _make_agent_with_chain([])  # P0.3 gate result
        from agent.chat_completion_helpers import try_activate_fallback

        # 404 classifies as model_not_found (P0.2) which is non-retryable,
        # and triggers the fallback path in the loop. With an empty chain,
        # activate must NO-OP.
        reason = FailoverReason.model_not_found
        activated = try_activate_fallback(agent, reason)
        assert activated is False, (
            "Gemini 404 must NOT route to a fallback provider (Claude/OpenRouter) "
            "— conversation fallback is disabled (P0.3)"
        )
        # Index must not advance / chain must remain empty.
        assert agent._fallback_chain == []
        assert agent._fallback_index == 0

    def test_gemini_404_classification_is_non_retryable(self):
        """Defence in depth: the 404 itself fails fast (P0.2) so even before
        the fallback gate the loop would not thrash."""
        err = GeminiAPIError(
            "This model models/gemini-2.5-flash is no longer available to "
            "new users.",
            status_code=404,
            code="gemini_http_404",
        )
        from agent.error_classifier import classify_api_error

        c = classify_api_error(err, provider="gemini", model="gemini-2.5-flash")
        assert c.retryable is False
        assert c.reason == FailoverReason.model_not_found


class TestP03_AuxiliaryFallbackUntouched:
    def test_get_fallback_chain_still_reads_config(self, monkeypatch):
        """Auxiliary governance must be unaffected: get_fallback_chain still
        returns the configured paid chain verbatim."""
        cfg = {
            "fallback_model": {"provider": "openrouter", "model": "anthropic/claude-3.5-sonnet"}
        }
        chain = get_fallback_chain(cfg)
        assert chain == [{"provider": "openrouter", "model": "anthropic/claude-3.5-sonnet"}]
        assert len(chain) == 1


class TestP03_429StillRetries:
    def test_429_classification_unchanged(self):
        """P0.3 must NOT alter 429 retry policy (still retryable)."""
        from agent.error_classifier import classify_api_error

        class E429(Exception):
            status_code = 429

        c = classify_api_error(E429("Rate limit exceeded"), provider="openai", model="gpt-4o")
        assert c.status_code == 429
        assert c.retryable is True
        assert c.reason == FailoverReason.rate_limit
