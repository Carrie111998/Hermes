"""prompt_caching.subagent_cache_ttl — per-subagent prompt-cache TTL override.

Inspired by Claude Code v2.1.243's ``promptCacheTtl`` / ``subagentPromptCacheTtl``
split: the 1h Anthropic cache tier costs 2x on writes and only amortizes across
a long-lived conversation. Delegation subagents live minutes, so operators can
keep a 1h main-session cache while pinning subagents to 5m (or disabling
subagent caching entirely) without touching the main conversation's tier.

Contract pinned here (agent/agent_init.py):
- default ``"inherit"`` (or any unknown value) → subagents keep ``cache_ttl``
- ``"5m"`` / ``"1h"`` → subagents get that tier regardless of ``cache_ttl``
- disable synonym (``"off"``, ``false``, …) → caching disabled for subagents
  only; the main agent's tier is untouched
- non-subagent platforms never read the key
"""

import contextlib
import io

import pytest

from hermes_state import SessionDB
from run_agent import AIAgent


def _config(cache_ttl="1h", subagent_cache_ttl=None):
    pc = {"cache_ttl": cache_ttl}
    if subagent_cache_ttl is not None:
        pc["subagent_cache_ttl"] = subagent_cache_ttl
    return {
        "prompt_caching": pc,
        "sessions": {},
        "bedrock": {},
    }


def _make_agent(monkeypatch, tmp_path, *, platform, cache_ttl="1h",
                subagent_cache_ttl=None):
    from hermes_cli import config as config_mod

    cfg = _config(cache_ttl=cache_ttl, subagent_cache_ttl=subagent_cache_ttl)
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setattr(config_mod, "load_config_readonly", lambda: cfg)

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            agent = AIAgent(
                base_url="https://openrouter.ai/api/v1",
                api_key="test-key",
                provider="openrouter",
                model="anthropic/claude-opus-4.8",
                enabled_toolsets=[],
                disabled_toolsets=[],
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                session_db=db,
                platform=platform,
            )
        return agent
    finally:
        db.close()


class TestSubagentCacheTtlOverride:
    def test_default_inherit_keeps_main_ttl(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, platform="subagent",
                            cache_ttl="1h")
        assert agent._cache_ttl == "1h"
        assert agent._cache_disabled is False

    def test_explicit_inherit_keeps_main_ttl(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, platform="subagent",
                            cache_ttl="1h", subagent_cache_ttl="inherit")
        assert agent._cache_ttl == "1h"

    def test_5m_override_downgrades_subagent_only(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, platform="subagent",
                            cache_ttl="1h", subagent_cache_ttl="5m")
        assert agent._cache_ttl == "5m"
        assert agent._cache_disabled is False

    def test_1h_override_upgrades_subagent(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, platform="subagent",
                            cache_ttl="5m", subagent_cache_ttl="1h")
        assert agent._cache_ttl == "1h"

    def test_disable_synonym_disables_subagent_caching(self, monkeypatch, tmp_path):
        agent = _make_agent(monkeypatch, tmp_path, platform="subagent",
                            cache_ttl="1h", subagent_cache_ttl="off")
        assert agent._cache_ttl is None
        assert agent._cache_disabled is True
        assert agent._use_prompt_caching is False
        assert agent._use_native_cache_layout is False

    def test_unknown_value_falls_back_to_main_ttl(self, monkeypatch, tmp_path):
        # Unknown tiers are neither valid nor a disable synonym — keep the
        # main cache_ttl, matching cache_ttl's own unknown-value behavior.
        agent = _make_agent(monkeypatch, tmp_path, platform="subagent",
                            cache_ttl="1h", subagent_cache_ttl="2h")
        assert agent._cache_ttl == "1h"
        assert agent._cache_disabled is False


class TestNonSubagentPlatformsIgnoreOverride:
    @pytest.mark.parametrize("platform", ["cli", "telegram", None])
    def test_main_agent_ignores_subagent_key(self, monkeypatch, tmp_path, platform):
        agent = _make_agent(monkeypatch, tmp_path, platform=platform,
                            cache_ttl="1h", subagent_cache_ttl="off")
        assert agent._cache_ttl == "1h"
        assert agent._cache_disabled is False

    def test_main_agent_disable_still_works(self, monkeypatch, tmp_path):
        # Sibling guard: the main cache_ttl disable path is untouched.
        agent = _make_agent(monkeypatch, tmp_path, platform="cli",
                            cache_ttl="off")
        assert agent._cache_ttl is None
        assert agent._cache_disabled is True
