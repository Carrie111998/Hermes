"""Behavior-contract tests for #95066 — Desktop chats must fail over on Codex quota.

Two defects chained into the reported symptom:

1. A Desktop/TUI chat freezes its fallback chain at agent-create time, so a
   chat opened before ``hermes fallback add`` keeps an empty in-memory chain
   forever and a provider-quota 429 ends in a provider error even though a
   healthy fallback is configured. Contract: the live agent's chain is
   re-aligned with config.yaml at turn start (same per-message contract the
   messaging gateway applies to its cached agents).
2. A Codex OAuth credential pool holding several rows for the SAME ChatGPT
   account looks recoverable to the rate-limit hinge because quota is keyed
   by stored rows, but Codex usage quota is scoped by the JWT's
   ``chatgpt_account_id``: N rows of one account share one quota wall and
   rotating between them cannot clear ``usage_limit_reached``. Contract:
   rotation counts *accounts*, not rows.

The helper contracts are pinned against the shared module both surfaces now
use (``hermes_cli.fallback_config``), so the gateway delegate and the TUI
turn-start sync cannot drift apart again.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_jwt(account_id: str | None) -> str:
    """Minimal unsigned JWT whose auth claim carries a ChatGPT account id."""
    import base64
    import json

    def _b64(payload: dict) -> str:
        raw = json.dumps(payload).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    auth: dict = {}
    if account_id is not None:
        auth["chatgpt_account_id"] = account_id
    header = {"alg": "none", "typ": "JWT"}
    claims = {"https://api.openai.com/auth": auth}
    return f"{_b64(header)}.{_b64(claims)}."


def _pool_entry(access_token: str, label: str):
    from agent.credential_pool import PooledCredential

    return PooledCredential(
        provider="openai-codex",
        id=label,
        label=label,
        auth_type="oauth",
        priority=0,
        source="device_code",
        access_token=access_token,
    )


class _FakePool:
    """has_available/entries surface the recovery hinge actually consumes."""

    def __init__(self, entries, *, available=True):
        self._entries = entries
        self._available = available

    def has_available(self) -> bool:
        return self._available

    def entries(self):
        return self._entries


# ── 1. the rate-limit hinge counts ChatGPT accounts, not rows (#95066) ──────


def test_codex_duplicate_account_rows_defer_to_fallback():
    """Two Codex OAuth rows re-authenticating ONE ChatGPT account share a
    quota wall — the hinge must NOT claim rotation may recover."""
    from run_agent import _pool_may_recover_from_rate_limit

    pool = _FakePool(
        [
            _pool_entry(_make_jwt("acct-111"), "row-a"),
            _pool_entry(_make_jwt("acct-111"), "row-b"),
        ]
    )
    assert _pool_may_recover_from_rate_limit(pool) is False


def test_codex_distinct_accounts_keep_rotation():
    """Rows backed by DIFFERENT ChatGPT accounts have independent quotas —
    rotation remains a real recovery path."""
    from run_agent import _pool_may_recover_from_rate_limit

    pool = _FakePool(
        [
            _pool_entry(_make_jwt("acct-111"), "row-a"),
            _pool_entry(_make_jwt("acct-222"), "row-b"),
        ]
    )
    assert _pool_may_recover_from_rate_limit(pool) is True


def test_non_jwt_pool_entries_keep_rotation():
    """Plain API-key pools (Vertex service accounts, custom providers) never
    reach the Codex branch: rows without a decodable JWT keep legacy rotation."""
    from run_agent import _pool_may_recover_from_rate_limit

    plain_a = _pool_entry("", "key-a")
    plain_b = _pool_entry("", "key-b")
    plain_a.access_token = ""
    plain_b.access_token = ""
    assert _pool_may_recover_from_rate_limit(_FakePool([plain_a, plain_b])) is True


def test_single_row_and_unavailable_pools_still_defer():
    """Original #11314/#13636 contracts survive untouched."""
    from run_agent import _pool_may_recover_from_rate_limit

    solo = _FakePool([_pool_entry(_make_jwt("acct-111"), "row-a")])
    assert _pool_may_recover_from_rate_limit(solo) is False

    exhausted = _FakePool(
        [_pool_entry(_make_jwt("acct-111"), "row-a")], available=False
    )
    assert _pool_may_recover_from_rate_limit(exhausted) is False
    assert _pool_may_recover_from_rate_limit(None) is False


# ── 2. shared chain-application helper (gateway + desktop parity) ───────────


def test_apply_fallback_chain_updates_live_agent_and_clears_memo():
    """A chain added AFTER the chat opened reaches the cached agent: the new
    entries land, the index resets, and the unavailability memo clears so a
    previously-failing provider gets retried (#95066)."""
    from hermes_cli.fallback_config import apply_fallback_chain_to_agent

    stale = [{"provider": "grok", "model": "grok-4.6"}]
    memo = {("openrouter", "anthropic/claude-sonnet-4.6")}
    agent = SimpleNamespace(
        _fallback_chain=[],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
        _unavailable_fallback_keys=memo,
    )

    fresh = [{"provider": "xai-oauth", "model": "grok-4.6"}]
    apply_fallback_chain_to_agent(agent, fresh)

    assert agent._fallback_chain == fresh
    assert agent._fallback_model == fresh[0]
    assert agent._fallback_index == 0
    assert memo == set()


def test_apply_fallback_chain_noop_preserves_unavailability_memo():
    """Repeated no-op refreshes must not clear the memo (#60955 rate-limiting
    benefit), only real content changes do."""
    from hermes_cli.fallback_config import apply_fallback_chain_to_agent

    chain = [{"provider": "xai-oauth", "model": "grok-4.6"}]
    memo = {("openrouter", "some/model")}
    agent = SimpleNamespace(
        _fallback_chain=list(chain),
        _fallback_model=chain[0],
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
        _unavailable_fallback_keys=memo,
    )

    apply_fallback_chain_to_agent(agent, [dict(chain[0])])

    assert memo == {("openrouter", "some/model")}


def test_apply_fallback_chain_skips_cooldown_held_activation():
    """While a rate-limit cooldown holds the agent on an activated fallback,
    the refresh must not clobber the turn-scoped activation state."""
    from hermes_cli.fallback_config import apply_fallback_chain_to_agent

    live = [{"provider": "grok", "model": "grok-4.6"}]
    agent = SimpleNamespace(
        _fallback_chain=live,
        _fallback_model=live[0],
        _fallback_index=1,
        _fallback_activated=True,
        _rate_limited_until=time.monotonic() + 30,
    )

    apply_fallback_chain_to_agent(agent, [{"provider": "other", "model": "m"}])

    assert agent._fallback_chain == live
    assert agent._fallback_index == 1
    assert agent._fallback_activated is True


def test_gateway_delegate_shares_the_helper():
    """GatewayRunner._apply_fallback_chain_to_agent delegates to the shared
    helper so messaging and desktop cannot drift apart."""
    from gateway.run import GatewayRunner
    from hermes_cli.fallback_config import apply_fallback_chain_to_agent as shared

    agent = SimpleNamespace(
        _fallback_chain=[],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )
    fresh = [{"provider": "xai-oauth", "model": "grok-4.6"}]
    GatewayRunner._apply_fallback_chain_to_agent(agent, fresh)

    expected = SimpleNamespace(
        _fallback_chain=[],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )
    shared(expected, fresh)
    assert agent._fallback_chain == expected._fallback_chain
    assert agent._fallback_model == expected._fallback_model


# ── 3. Desktop/TUI turn-start sync adopts fallback edits (#95066) ───────────


def test_tui_turn_start_sync_adopts_fallback_added_after_chat_opened(monkeypatch):
    """The exact reported scenario: chat opened before `hermes fallback add`,
    chain configured afterwards — the NEXT turn must carry the fresh chain."""
    from tui_gateway import server

    fresh = [{"provider": "xai-oauth", "model": "grok-4.6"}]
    monkeypatch.setattr(server, "_load_cfg", lambda: {
        "fallback_providers": fresh,
    })

    agent = SimpleNamespace(
        _fallback_chain=[],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )
    session = {"agent": agent}

    server._sync_agent_fallback_chain_with_config("sid-1", session)

    assert agent._fallback_chain == fresh
    assert agent._fallback_model == fresh[0]


def test_tui_turn_start_sync_survives_broken_config(monkeypatch):
    """A transiently broken config read keeps the current chain and never
    blocks the turn (fail-open, mirroring the gateway's refresh)."""
    from tui_gateway import server

    def _boom():
        raise RuntimeError("torn mid-edit config.yaml")

    monkeypatch.setattr(server, "_load_cfg", _boom)

    existing = [{"provider": "xai-oauth", "model": "grok-4.6"}]
    agent = SimpleNamespace(
        _fallback_chain=existing,
        _fallback_model=existing[0],
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )
    session = {"agent": agent}

    server._sync_agent_fallback_chain_with_config("sid-1", session)

    assert agent._fallback_chain == existing


def test_tui_turn_start_sync_noop_without_agent():
    """Sessions without a built agent have nothing to sync."""
    from tui_gateway import server

    server._sync_agent_fallback_chain_with_config("sid-1", {"agent": None})
    server._sync_agent_fallback_chain_with_config("sid-1", {})


def test_tui_turn_start_sync_reads_real_config_disk(tmp_path, monkeypatch):
    """Integration over the REAL read path (no config mock): `hermes fallback
    add` writes config.yaml between two turns; the next turn's sync adopts
    it from disk through _load_cfg's managed-overlay pipeline."""
    from tui_gateway import server

    (tmp_path / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: xai-oauth\n"
        "    model: grok-4.6\n"
    )
    monkeypatch.setattr(server, "_hermes_home", str(tmp_path))
    monkeypatch.setattr(server, "get_hermes_home_override", lambda: None)
    monkeypatch.setattr(server, "_cfg_cache", None)
    monkeypatch.setattr(server, "_cfg_mtime", None)

    agent = SimpleNamespace(
        _fallback_chain=[],
        _fallback_model=None,
        _fallback_index=0,
        _fallback_activated=False,
        _rate_limited_until=0,
    )
    session = {"agent": agent}

    server._sync_agent_fallback_chain_with_config("sid-1", session)

    assert agent._fallback_chain == [{"provider": "xai-oauth", "model": "grok-4.6"}]
    assert agent._fallback_model == {"provider": "xai-oauth", "model": "grok-4.6"}


# ── 4. wiring invariants (call sites that resist unit testing) ──────────────


def _server_source() -> str:
    return (
        Path(__file__).resolve().parent.parent.parent
        / "tui_gateway"
        / "server.py"
    ).read_text(encoding="utf-8")


def test_turn_start_calls_both_syncs_in_order():
    """At turn start the pending-model switch runs first, then the config
    model sync, then the fallback-chain sync — all before the turn's first
    model call."""
    source = _server_source()
    model_call = source.find("_sync_agent_model_with_config(sid, session)")
    fb_call = source.find("_sync_agent_fallback_chain_with_config(sid, session)")
    assert model_call != -1, "config model sync call site vanished"
    assert fb_call != -1, "fallback chain sync must be wired at turn start"
    pending_call = source.find("_apply_pending_model_switch(sid, session)")
    assert pending_call < model_call < fb_call


def test_hinge_wiring_feeds_error_context_into_recovery():
    """The conversation loop consults the hinge before activating a fallback;
    the upstream-aggregator bypass stays intact (its contract predates this
    fix and must not regress)."""
    loop_source = (
        Path(__file__).resolve().parent.parent.parent
        / "agent"
        / "conversation_loop.py"
    ).read_text(encoding="utf-8")
    assert "_pool_may_recover_from_rate_limit(" in loop_source
    assert "FailoverReason.upstream_rate_limit" in loop_source
