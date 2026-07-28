"""API-owner contracts for durable non-push wake execution."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from gateway.wake import (
    INTERNAL_WAKE_TOKEN_HEADER,
    _internal_wake_idempotency_key,
    mint_internal_wake_token,
)
from tools.async_delegation import DurableWakeClaim


def _app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post(
        "/v1/chat/completions",
        adapter._handle_chat_completions,
    )
    app.router.add_get(
        "/api/delegations/{delegation_id}",
        adapter._handle_get_delegation,
    )
    return app


def _adapter(tmp_path, monkeypatch) -> APIServerAdapter:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_constants.get_hermes_home",
        lambda: home,
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.get_active_profile_name",
        lambda: "default",
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex: [("default", home)],
    )
    adapter = APIServerAdapter(
        PlatformConfig(enabled=True, extra={"key": "sk-test"})
    )
    db = MagicMock()
    db.get_messages_as_conversation.return_value = []
    adapter._session_db = db
    return adapter


def _durable_request(
    adapter: APIServerAdapter,
    *,
    delegation_id: str = "deleg-durable-api-owner",
    session_id: str = "durable-api-session",
    text: str = "durable completion",
) -> tuple[dict, dict]:
    identity = adapter._response_store_default_identity
    authority = {
        "profile": identity.profile,
        "delivery_home": identity.source_home,
        "profile_generation": identity.profile_generation,
    }
    idempotency_key = _internal_wake_idempotency_key(
        producer_id=delegation_id,
        session_id=session_id,
        text=text,
        durable_wake_required=True,
        durable_delegation_id=delegation_id,
        durable_execution_owner="api",
        **authority,
    )
    token = mint_internal_wake_token(
        session_id=session_id,
        text=text,
        producer_id=delegation_id,
        durable_wake_required=True,
        durable_delegation_id=delegation_id,
        durable_execution_owner="api",
        **authority,
    )
    return (
        {
            "Authorization": "Bearer sk-test",
            "X-Hermes-Session-Id": session_id,
            "Idempotency-Key": idempotency_key,
            INTERNAL_WAKE_TOKEN_HEADER: token,
        },
        {
            "model": "hermes-agent",
            "messages": [{"role": "user", "content": text}],
            "stream": False,
        },
    )


def _patch_cas(
    monkeypatch,
    *,
    claim,
    complete=True,
    abandon=True,
    release=True,
):
    import tools.async_delegation as delegation

    claim_mock = MagicMock(side_effect=claim) if callable(claim) else MagicMock(
        return_value=claim
    )
    complete_mock = MagicMock(return_value=complete)
    abandon_mock = MagicMock(return_value=abandon)
    release_mock = MagicMock(return_value=release)
    monkeypatch.setattr(
        delegation,
        "claim_durable_wake_execution",
        claim_mock,
    )
    monkeypatch.setattr(
        delegation,
        "complete_durable_wake_execution",
        complete_mock,
    )
    monkeypatch.setattr(
        delegation,
        "abandon_durable_wake_execution",
        abandon_mock,
    )
    monkeypatch.setattr(
        delegation,
        "release_durable_wake_execution",
        release_mock,
    )
    return SimpleNamespace(
        claim=claim_mock,
        complete=complete_mock,
        abandon=abandon_mock,
        release=release_mock,
    )


@pytest.mark.asyncio
async def test_claimed_success_commits_and_completed_retry_replays_exactly(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    saved: dict = {}

    def claim(**_kwargs):
        if not saved:
            return DurableWakeClaim(state="claimed", claim_id="claim-one")
        return DurableWakeClaim(
            state="completed",
            response=saved["response"],
        )

    cas = _patch_cas(monkeypatch, claim=claim)

    def complete(**kwargs):
        saved["response"] = kwargs["response"]
        return True

    cas.complete.side_effect = complete
    adapter._run_agent = AsyncMock(
        return_value=(
            {"final_response": "exact durable answer", "completed": True},
            {
                "input_tokens": 3,
                "output_tokens": 5,
                "total_tokens": 8,
            },
        )
    )
    reserve = MagicMock(wraps=adapter._reserve_agent_run)
    monkeypatch.setattr(adapter, "_reserve_agent_run", reserve)
    first_headers, payload = _durable_request(adapter)
    second_headers, _ = _durable_request(adapter)

    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/v1/chat/completions",
                headers=first_headers,
                json=payload,
            )
            first_text = await first.text()
            second = await client.post(
                "/v1/chat/completions",
                headers=second_headers,
                json=payload,
            )
            second_text = await second.text()
    finally:
        await adapter.disconnect()

    assert (first.status, second.status) == (200, 200)
    assert first_text == second_text
    body = __import__("json").loads(first_text)
    assert body["choices"][0]["message"]["content"] == "exact durable answer"
    assert body["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 5,
        "total_tokens": 8,
    }
    assert saved["response"]["terminal_status"] == 200
    assert adapter._run_agent.await_count == 1
    assert reserve.call_count == 1
    assert cas.complete.call_count == 1
    cas.abandon.assert_not_called()
    cas.release.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("claim", "expected_status"),
    (
        (
            DurableWakeClaim(
                state="in_progress",
                reason="another owner is live",
            ),
            429,
        ),
        (
            DurableWakeClaim(
                state="uncertain",
                reason="previous owner disappeared",
            ),
            200,
        ),
    ),
)
async def test_non_owner_claim_states_never_reserve_or_run_agent(
    tmp_path,
    monkeypatch,
    claim,
    expected_status,
):
    adapter = _adapter(tmp_path, monkeypatch)
    cas = _patch_cas(monkeypatch, claim=claim)
    adapter._run_agent = AsyncMock()
    reserve = MagicMock(wraps=adapter._reserve_agent_run)
    monkeypatch.setattr(adapter, "_reserve_agent_run", reserve)
    headers, payload = _durable_request(adapter)
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == expected_status
    if claim.state == "uncertain":
        assert body["status"] == "partial"
        assert body["partial"] is True
        assert body["incomplete"] is True
        assert body["turn_exit_reason"] == "durable_wake_uncertain"
        assert body["usage"]["total_tokens"] == 0
    reserve.assert_not_called()
    adapter._run_agent.assert_not_awaited()
    cas.complete.assert_not_called()
    cas.abandon.assert_not_called()
    cas.release.assert_not_called()


@pytest.mark.asyncio
async def test_claim_storage_failure_is_retryable_and_never_acks_or_runs(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    cas = _patch_cas(
        monkeypatch,
        claim=lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("temporary sqlite outage")
        ),
    )
    adapter._run_agent = AsyncMock()
    reserve = MagicMock(wraps=adapter._reserve_agent_run)
    monkeypatch.setattr(adapter, "_reserve_agent_run", reserve)
    headers, payload = _durable_request(adapter)
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 429
    assert body["error"]["code"] == "durable_wake_claim_unavailable"
    reserve.assert_not_called()
    adapter._run_agent.assert_not_awaited()
    cas.complete.assert_not_called()
    cas.abandon.assert_not_called()
    cas.release.assert_not_called()


@pytest.mark.asyncio
async def test_capacity_denial_releases_claim_and_retry_runs_once(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    claims = iter(("claim-capacity-one", "claim-capacity-two"))
    cas = _patch_cas(
        monkeypatch,
        claim=lambda **_kwargs: DurableWakeClaim(
            state="claimed",
            claim_id=next(claims),
        ),
    )
    adapter._run_agent = AsyncMock(
        return_value=(
            {"final_response": "ran after capacity returned"},
            {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )
    )
    admitted = adapter._reserve_agent_run()
    reserve = MagicMock(side_effect=[None, admitted])
    monkeypatch.setattr(adapter, "_reserve_agent_run", reserve)
    first_headers, payload = _durable_request(adapter)
    second_headers, _ = _durable_request(adapter)
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/v1/chat/completions",
                headers=first_headers,
                json=payload,
            )
            await first.read()
            second = await client.post(
                "/v1/chat/completions",
                headers=second_headers,
                json=payload,
            )
            second_body = await second.json()
    finally:
        await adapter.disconnect()

    assert first.status == 429
    assert second.status == 200
    assert second_body["choices"][0]["message"]["content"] == (
        "ran after capacity returned"
    )
    assert cas.claim.call_count == 2
    assert cas.release.call_count == 1
    assert cas.release.call_args.kwargs["claim_id"] == "claim-capacity-one"
    assert adapter._run_agent.await_count == 1
    assert cas.complete.call_count == 1
    cas.abandon.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "expected"),
    (
        (
            {
                "final_response": "",
                "completed": True,
                "partial": True,
                "interrupted": True,
                "failed": True,
                "error": "known failure",
            },
            "failed",
        ),
        (
            {
                "final_response": "",
                "completed": True,
                "partial": True,
                "interrupted": True,
            },
            "interrupted",
        ),
        (
            {
                "final_response": "",
                "completed": True,
                "partial": True,
            },
            "partial",
        ),
    ),
)
async def test_known_terminal_precedence_commits_exact_replay(
    tmp_path,
    monkeypatch,
    result,
    expected,
):
    adapter = _adapter(tmp_path, monkeypatch)
    saved: dict = {}

    def claim(**_kwargs):
        if not saved:
            return DurableWakeClaim(state="claimed", claim_id="terminal-claim")
        return DurableWakeClaim(
            state="completed",
            response=saved["response"],
        )

    cas = _patch_cas(monkeypatch, claim=claim)

    def complete(**kwargs):
        saved["response"] = kwargs["response"]
        return True

    cas.complete.side_effect = complete
    adapter._run_agent = AsyncMock(
        return_value=(
            result,
            {
                "input_tokens": 11,
                "output_tokens": 13,
                "total_tokens": 24,
            },
        )
    )
    first_headers, payload = _durable_request(adapter)
    second_headers, _ = _durable_request(adapter)
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            first = await client.post(
                "/v1/chat/completions",
                headers=first_headers,
                json=payload,
            )
            first_text = await first.text()
            second = await client.post(
                "/v1/chat/completions",
                headers=second_headers,
                json=payload,
            )
            second_text = await second.text()
    finally:
        await adapter.disconnect()

    assert (first.status, second.status) == (200, 200)
    assert first_text == second_text
    body = __import__("json").loads(first_text)
    hermes = body["error"]["hermes"]
    assert hermes["status"] == expected
    assert hermes["terminal_outcome_contradictory"] is True
    assert sum(
        bool(hermes[key])
        for key in ("completed", "partial", "interrupted", "failed")
    ) == 1
    assert body["usage"]["total_tokens"] == 24
    assert saved["response"]["terminal_status"] == 502
    assert adapter._run_agent.await_count == 1
    cas.abandon.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "complete_result"),
    (
        ("agent", True),
        ("serialization", True),
        ("complete_cas", False),
    ),
)
async def test_claim_owner_abandons_every_unknown_terminal_path(
    tmp_path,
    monkeypatch,
    failure,
    complete_result,
):
    adapter = _adapter(tmp_path, monkeypatch)
    cas = _patch_cas(
        monkeypatch,
        claim=DurableWakeClaim(state="claimed", claim_id="fault-claim"),
        complete=complete_result,
    )
    if failure == "agent":
        adapter._run_agent = AsyncMock(side_effect=RuntimeError("agent boom"))
    else:
        usage = {
            "input_tokens": float("nan") if failure == "serialization" else 1,
            "output_tokens": 2,
            "total_tokens": 3,
        }
        adapter._run_agent = AsyncMock(
            return_value=({"final_response": "must not leak"}, usage)
        )
    headers, payload = _durable_request(adapter)
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 200
    assert body["object"] == "hermes.durable_wake"
    assert body["turn_exit_reason"] == "durable_wake_uncertain"
    assert "must not leak" not in str(body)
    assert cas.abandon.call_count == 1
    if failure == "agent":
        cas.complete.assert_not_called()
    elif failure == "serialization":
        cas.complete.assert_not_called()
    else:
        cas.complete.assert_called_once()


@pytest.mark.asyncio
async def test_abandon_failure_never_returns_terminal_ack(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    cas = _patch_cas(
        monkeypatch,
        claim=DurableWakeClaim(
            state="claimed",
            claim_id="unsettled-claim",
        ),
        abandon=False,
    )
    adapter._run_agent = AsyncMock(side_effect=RuntimeError("agent boom"))
    headers, payload = _durable_request(adapter)
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 429
    assert body["error"]["code"] == (
        "durable_wake_settlement_unavailable"
    )
    cas.abandon.assert_called_once()
    cas.complete.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_uncertainty_is_retrievable_without_chat_history(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    import tools.async_delegation as delegation

    delegation_id = "deleg_" + ("a" * 32)
    status_read = MagicMock(
        return_value={
            "delegation_id": delegation_id,
            "state": "completed",
            "origin_session_id": "durable-api-session",
            "dispatched_at": 10.0,
            "completed_at": 20.0,
            "delivery_state": "pending",
            "delivery_attempts": 1,
            "delivery_disposition_reason": "",
            "wake_state": "uncertain",
            "wake_disposition_reason": (
                "owner disappeared after effects may have started"
            ),
            # These internal payloads must never be reflected by the API.
            "event": {"secret": "must-not-leak"},
            "event_json": '{"secret":"must-not-leak"}',
            "result": {"summary": "must-not-leak"},
        }
    )
    monkeypatch.setattr(
        delegation,
        "get_durable_delegation",
        status_read,
    )
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.get(
                f"/api/delegations/{delegation_id}",
                headers={"Authorization": "Bearer sk-test"},
            )
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 200
    assert set(body) == {
        "object",
        "delegation_id",
        "state",
        "dispatched_at",
        "completed_at",
        "delivery_state",
        "delivery_attempts",
        "delivery_disposition_reason",
        "wake_state",
        "wake_disposition_reason",
    }
    assert body["wake_state"] == "uncertain"
    assert body["wake_disposition_reason"] == (
        "owner disappeared after effects may have started"
    )
    assert "must-not-leak" not in str(body)
    store = status_read.call_args.kwargs["store"]
    identity = adapter._response_store_default_identity
    assert (
        store.profile,
        store.source_home,
        store.hermes_home,
        store.profile_generation,
    ) == (
        identity.profile,
        identity.source_home,
        identity.canonical_home,
        identity.profile_generation,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "corrupt_record",
    (
        {"delegation_id": "wrong-row"},
        {
            "delegation_id": "deleg_" + ("b" * 32),
            "state": "completed",
            "dispatched_at": float("nan"),
            "completed_at": 20.0,
            "delivery_state": "pending",
            "delivery_attempts": 1,
            "delivery_disposition_reason": "",
            "wake_state": "uncertain",
            "wake_disposition_reason": "unknown",
        },
    ),
)
async def test_delegation_status_rejects_corrupt_rows(
    tmp_path,
    monkeypatch,
    corrupt_record,
):
    adapter = _adapter(tmp_path, monkeypatch)
    import tools.async_delegation as delegation

    delegation_id = "deleg_" + ("b" * 32)
    monkeypatch.setattr(
        delegation,
        "get_durable_delegation",
        MagicMock(return_value=corrupt_record),
    )
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.get(
                f"/api/delegations/{delegation_id}",
                headers={"Authorization": "Bearer sk-test"},
            )
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 500
    assert body["error"]["code"] == "delegation_status_corrupt"
    assert "wrong-row" not in str(body)


@pytest.mark.asyncio
async def test_delegation_status_reader_failure_is_controlled(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    import tools.async_delegation as delegation

    delegation_id = "deleg_" + ("c" * 32)
    monkeypatch.setattr(
        delegation,
        "get_durable_delegation",
        MagicMock(side_effect=ValueError("corrupt result_json secret")),
    )
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.get(
                f"/api/delegations/{delegation_id}",
                headers={"Authorization": "Bearer sk-test"},
            )
            body = await response.json()
    finally:
        await adapter.disconnect()

    assert response.status == 500
    assert body["error"]["code"] == "delegation_status_corrupt"
    assert "secret" not in str(body)


@pytest.mark.asyncio
async def test_cancelled_claim_owner_abandons_then_propagates(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    cas = _patch_cas(
        monkeypatch,
        claim=DurableWakeClaim(state="claimed", claim_id="cancel-claim"),
    )
    adapter._run_agent = AsyncMock(side_effect=asyncio.CancelledError())
    headers, payload = _durable_request(adapter)
    request = MagicMock()
    request.headers = headers
    request.json = AsyncMock(return_value=payload)

    try:
        with pytest.raises(asyncio.CancelledError):
            await adapter._handle_chat_completions(request)
    finally:
        await adapter.disconnect()

    cas.abandon.assert_called_once()
    cas.complete.assert_not_called()


@pytest.mark.asyncio
async def test_non_durable_internal_wake_never_touches_durable_cas(
    tmp_path,
    monkeypatch,
):
    adapter = _adapter(tmp_path, monkeypatch)
    cas = _patch_cas(
        monkeypatch,
        claim=AssertionError("durable claim must not be called"),
    )
    identity = adapter._response_store_default_identity
    producer = "ordinary-internal-wake"
    session_id = "ordinary-session"
    text = "ordinary completion"
    authority = {
        "profile": identity.profile,
        "delivery_home": identity.source_home,
        "profile_generation": identity.profile_generation,
    }
    key = _internal_wake_idempotency_key(
        producer_id=producer,
        session_id=session_id,
        text=text,
        **authority,
    )
    token = mint_internal_wake_token(
        session_id=session_id,
        text=text,
        producer_id=producer,
        **authority,
    )
    adapter._run_agent = AsyncMock(
        return_value=(
            {"final_response": "ordinary ok"},
            {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        )
    )
    try:
        async with TestClient(TestServer(_app(adapter))) as client:
            response = await client.post(
                "/v1/chat/completions",
                headers={
                    "Authorization": "Bearer sk-test",
                    "X-Hermes-Session-Id": session_id,
                    "Idempotency-Key": key,
                    INTERNAL_WAKE_TOKEN_HEADER: token,
                },
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": text}],
                },
            )
            await response.read()
    finally:
        await adapter.disconnect()

    assert response.status == 200
    adapter._run_agent.assert_awaited_once()
    cas.claim.assert_not_called()
    cas.complete.assert_not_called()
    cas.abandon.assert_not_called()
    cas.release.assert_not_called()
