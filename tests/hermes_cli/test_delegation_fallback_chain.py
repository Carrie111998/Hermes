"""Tests for delegation fallback chain config helper.

These tests cover hermes_cli.fallback_config.get_fallback_chain when applied
to the delegation block (delegation.fallback_providers / fallback_model /
fallback_chain). They mirror the auxiliary fallback parity but exercise the
delegation surface specifically.
"""

from hermes_cli.fallback_config import get_fallback_chain
from tools.delegate_tool import _get_delegation_fallback_chain


class TestDelegationFallbackChainConfig:
    def test_empty_delegation_returns_empty_chain(self):
        assert get_fallback_chain({}) == []
        assert _get_delegation_fallback_chain({}) is None
        assert _get_delegation_fallback_chain(None) is None

    def test_fallback_providers_list_preserved(self):
        chain = [{"provider": "openrouter", "model": "gpt-4o-mini"}, {"provider": "nous", "model": "hermes-4-405b"}]
        assert get_fallback_chain({"fallback_providers": chain}) == chain
        assert _get_delegation_fallback_chain({"fallback_providers": chain}) == chain

    def test_fallback_model_single_dict_normalized(self):
        entry = {"provider": "nous", "model": "hermes-4-405b"}
        assert get_fallback_chain({"fallback_model": entry}) == [entry]
        assert _get_delegation_fallback_chain({"fallback_model": entry}) == [entry]

    def test_fallback_chain_alias_honoured(self):
        chain = [{"provider": "nous", "model": "hermes-4-405b"}]
        assert _get_delegation_fallback_chain({"fallback_chain": chain}) == chain

    def test_fallback_providers_takes_precedence_over_chain_alias(self):
        providers_chain = [{"provider": "openrouter", "model": "gpt-4o-mini"}]
        alias_chain = [{"provider": "nous", "model": "hermes-4-405b"}]
        cfg = {"fallback_providers": providers_chain, "fallback_chain": alias_chain}
        # get_fallback_chain merges in priority order: providers > chain > model
        merged = providers_chain + alias_chain
        assert _get_delegation_fallback_chain(cfg) == merged
        assert get_fallback_chain(cfg) == merged

    def test_invalid_entries_filtered(self):
        cfg = {"fallback_providers": [{"provider": "", "model": "x"}, {"provider": "nous", "model": ""}, {"provider": "nous", "model": "hermes-4-405b"}]}
        chain = _get_delegation_fallback_chain(cfg)
        assert chain == [{"provider": "nous", "model": "hermes-4-405b"}]

    def test_pinned_delegation_fallback_chain_roundtrips(self):
        chain = [{"provider": "openrouter", "model": "gpt-4o-mini"}]
        cfg = {"provider": "minimax", "model": "minimax/m2", "fallback_providers": chain}
        assert _get_delegation_fallback_chain(cfg) == chain
        # provider/model keys coexist with fallback chain
        assert cfg["provider"] == "minimax"

    def test_both_legacy_and_new_keys_merged(self):
        # fallback_providers + fallback_model merged, deduped by identity
        cfg = {
            "fallback_providers": [{"provider": "openrouter", "model": "gpt-4o-mini"}],
            "fallback_model": {"provider": "nous", "model": "hermes-4-405b"},
        }
        chain = get_fallback_chain(cfg)
        assert len(chain) == 2
        assert chain[0]["provider"] == "openrouter"
        assert chain[1]["provider"] == "nous"
