from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.codex_runtime import _record_codex_app_server_usage
from hermes_state import SessionDB


def _jwt(account_id: str) -> str:
    payload = json.dumps(
        {"https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{encoded}.signature"


def test_codex_app_server_usage_does_not_infer_account_from_agent_token():
    token = _jwt("acct-real")
    db = MagicMock()
    agent = SimpleNamespace(
        api_key=token,
        provider="openai-codex",
        model="gpt-test",
        base_url="https://chatgpt.com/backend-api/codex",
        session_id="session-1",
        _session_db=db,
        _session_db_created=True,
        context_compressor=None,
        session_api_calls=0,
        session_prompt_tokens=0,
        session_completion_tokens=0,
        session_total_tokens=0,
        session_input_tokens=0,
        session_output_tokens=0,
        session_cache_read_tokens=0,
        session_cache_write_tokens=0,
        session_reasoning_tokens=0,
        session_estimated_cost_usd=0.0,
        session_cost_status=None,
        session_cost_source=None,
    )
    turn = SimpleNamespace(
        token_usage_last={
            "inputTokens": 30,
            "cachedInputTokens": 20,
            "outputTokens": 5,
            "reasoningOutputTokens": 2,
            "totalTokens": 35,
        },
        model_context_window=200_000,
    )
    cost = SimpleNamespace(amount_usd=None, status="included", source="subscription")

    with patch("agent.usage_pricing.estimate_usage_cost", return_value=cost):
        _record_codex_app_server_usage(agent, turn)

    kwargs = db.queue_token_counts.call_args.kwargs
    assert "account_key" not in kwargs
    assert token not in repr(kwargs)
    assert "acct-real" not in repr(kwargs)


def test_codex_app_server_usage_remains_unattributed_without_authoritative_identity(
    tmp_path,
):
    token = _jwt("acct-real")
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("session-1", "feishu", model="gpt-test")
        agent = SimpleNamespace(
            api_key=token,
            provider="openai-codex",
            model="gpt-test",
            base_url="https://chatgpt.com/backend-api/codex",
            session_id="session-1",
            _session_db=db,
            _session_db_created=True,
            context_compressor=None,
            session_api_calls=0,
            session_prompt_tokens=0,
            session_completion_tokens=0,
            session_total_tokens=0,
            session_input_tokens=0,
            session_output_tokens=0,
            session_cache_read_tokens=0,
            session_cache_write_tokens=0,
            session_reasoning_tokens=0,
            session_estimated_cost_usd=0.0,
            session_cost_status=None,
            session_cost_source=None,
        )
        turn = SimpleNamespace(
            token_usage_last={
                "inputTokens": 30,
                "cachedInputTokens": 20,
                "outputTokens": 5,
                "reasoningOutputTokens": 2,
                "totalTokens": 35,
            },
            model_context_window=200_000,
        )
        cost = SimpleNamespace(
            amount_usd=None, status="included", source="subscription"
        )

        with patch("agent.usage_pricing.estimate_usage_cost", return_value=cost):
            _record_codex_app_server_usage(agent, turn)

        assert db.account_usage_totals(provider="openai-codex") == []
    finally:
        db.close()
