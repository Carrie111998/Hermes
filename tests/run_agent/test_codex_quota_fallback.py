"""Codex account-quota failures must not rotate duplicate OAuth entries."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.credential_pool import PooledCredential
from agent.error_classifier import FailoverReason
from run_agent import AIAgent, _pool_may_recover_from_rate_limit


def _jwt_for_account(account_id: str) -> str:
    payload = {
        "https://api.openai.com/auth": {"chatgpt_account_id": account_id}
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{encoded}.signature"


def _entry(index: int, account_id: str) -> PooledCredential:
    return PooledCredential(
        provider="openai-codex",
        id=f"cred-{index}",
        label=f"Credential {index}",
        auth_type="device_code",
        priority=index,
        source="oauth",
        access_token=_jwt_for_account(account_id),
    )


def _pool(entries: list[PooledCredential]):
    pool = MagicMock()
    pool.provider = "openai-codex"
    pool.entries.return_value = entries
    pool.has_available.return_value = True
    pool.current.return_value = entries[0]
    pool.entry_id_for_api_key.return_value = entries[0].id
    return pool


def _agent(pool, entries, fallback_chain):
    return SimpleNamespace(
        _credential_pool=pool,
        provider="openai-codex",
        base_url="https://chatgpt.com/backend-api/codex",
        api_key=entries[0].access_token,
        _credential_pool_entry_id=entries[0].id,
        _swap_credential=MagicMock(),
        _fallback_chain=fallback_chain,
        _fallback_index=0,
    )


def test_same_codex_account_entries_cannot_recover_account_quota():
    pool = _pool([_entry(0, "acct-shared"), _entry(1, "acct-shared")])

    assert _pool_may_recover_from_rate_limit(
        pool,
        provider="openai-codex",
        error_context={"reason": "usage_limit_reached"},
    ) is False


def test_distinct_codex_accounts_can_recover_account_quota():
    pool = _pool([_entry(0, "acct-a"), _entry(1, "acct-b")])

    assert _pool_may_recover_from_rate_limit(
        pool,
        provider="openai-codex",
        error_context={"reason": "usage_limit_reached"},
    ) is True


def test_recovery_defers_duplicate_account_quota_to_fallback_chain():
    entries = [_entry(0, "acct-shared"), _entry(1, "acct-shared")]
    pool = _pool(entries)
    pool.mark_exhausted_and_rotate.return_value = entries[1]
    agent = _agent(
        pool,
        entries,
        [{"provider": "xai-oauth", "model": "grok-4.6"}],
    )

    recovered, retried = AIAgent._recover_with_credential_pool(
        agent,
        status_code=429,
        has_retried_429=False,
        classified_reason=FailoverReason.rate_limit,
        error_context={
            "reason": "usage_limit_reached",
            "message": "The usage limit has been reached.",
            "resets_in_seconds": 14400,
        },
    )

    assert recovered is False
    assert retried is True
    pool.mark_exhausted_and_rotate.assert_not_called()
    agent._swap_credential.assert_not_called()


def test_duplicate_account_quota_still_rotates_without_a_fallback():
    entries = [_entry(0, "acct-shared"), _entry(1, "acct-shared")]
    pool = _pool(entries)
    pool.mark_exhausted_and_rotate.return_value = entries[1]
    agent = _agent(pool, entries, [])

    recovered, retried = AIAgent._recover_with_credential_pool(
        agent,
        status_code=429,
        has_retried_429=False,
        classified_reason=FailoverReason.rate_limit,
        error_context={"reason": "usage_limit_reached"},
    )

    assert recovered is True
    assert retried is False
    pool.mark_exhausted_and_rotate.assert_called_once()
    agent._swap_credential.assert_called_once_with(entries[1])


def test_single_codex_entry_is_still_marked_exhausted():
    entries = [_entry(0, "acct-only")]
    pool = _pool(entries)
    pool.mark_exhausted_and_rotate.return_value = None
    agent = _agent(pool, entries, [])

    recovered, retried = AIAgent._recover_with_credential_pool(
        agent,
        status_code=429,
        has_retried_429=False,
        classified_reason=FailoverReason.rate_limit,
        error_context={"reason": "usage_limit_reached"},
    )

    assert recovered is False
    assert retried is True
    pool.mark_exhausted_and_rotate.assert_called_once()
