"""Conversation-cache and structured-clarification contracts for the API server."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import re
import time
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


AUTH_HEADERS = {"Authorization": "Bearer sk-api-test"}
APPROVAL_PASSKEY = "approval-passkey-for-tests-only-0123456789"


def _test_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/chat/completions", adapter._handle_chat_completions)
    app.router.add_post("/v1/responses", adapter._handle_responses)
    app.router.add_get("/v1/approvals", adapter._handle_list_approvals)
    app.router.add_post(
        "/v1/approvals/{approval_id}/response",
        adapter._handle_resolve_approval,
    )
    app.router.add_get("/v1/clarifications", adapter._handle_list_clarifications)
    app.router.add_post(
        "/v1/clarifications/{clarify_id}/response",
        adapter._handle_resolve_clarification,
    )
    app.router.add_delete(
        "/api/sessions/{session_id}", adapter._handle_delete_session
    )
    app.router.add_patch(
        "/api/sessions/{session_id}", adapter._handle_patch_session
    )
    return app


@pytest.fixture
def api_runtime(monkeypatch, tmp_path):
    """Real adapter/SessionDB with only remote model execution replaced."""

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    session_db = SessionDB(hermes_home / "state.db")
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": "sk-api-test",
                "approval_passkey": APPROVAL_PASSKEY,
            },
        )
    )
    adapter._session_db = session_db
    from tools import clarify_gateway as clarify_module

    with clarify_module._lock:
        clarify_module._entries.clear()
        clarify_module._session_index.clear()
        clarify_module._current_generations.clear()

    runtime = {
        "provider": "openai-codex",
        "api_key": "provider-key-one",
        "base_url": "https://provider.invalid/v1",
        "api_mode": "codex_responses",
    }
    model = {"name": "gpt-5.6-sol"}
    config = {
        "compression": {"enabled": True, "threshold": 0.8},
        "memory": {"provider": ""},
    }

    class FakeAgent:
        instances = []
        schema_source = [{"type": "function", "name": "schema-v1"}]
        fail_next = False

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.session_id = kwargs["session_id"]
            self.session_db = kwargs["session_db"]
            self.clarify_callback = kwargs["clarify_callback"]
            self.stream_delta_callback = kwargs.get("stream_delta_callback")
            self.tool_progress_callback = kwargs.get("tool_progress_callback")
            self.tool_start_callback = kwargs.get("tool_start_callback")
            self.tool_complete_callback = kwargs.get("tool_complete_callback")
            self.reasoning_config = kwargs.get("reasoning_config")
            self.max_iterations = kwargs.get("max_iterations")
            self.tool_schemas = copy.deepcopy(type(self).schema_source)
            self.session_prompt_tokens = 1
            self.session_completion_tokens = 1
            self.session_total_tokens = 2
            self._interrupt_requested = False
            self._last_activity_ts = time.time()
            self._last_activity_desc = "created"
            self._last_flushed_db_idx = 0
            self._api_call_count = 0
            self.release_count = 0
            type(self).instances.append(self)

        def run_conversation(self, user_message, conversation_history, task_id):
            if self.session_db.get_session(self.session_id) is None:
                self.session_db.create_session(self.session_id, "api_server")
            self.session_db.append_message(self.session_id, "user", user_message)

            if type(self).fail_next:
                type(self).fail_next = False
                return {
                    "final_response": "",
                    "completed": False,
                    "failed": True,
                    "incomplete": True,
                }

            if user_message == "ask-clarify":
                answer = self.clarify_callback(
                    "Which exact option should I use?", ["alpha", "beta"]
                )
                final_response = f"selected:{answer}"
            elif user_message == "danger":
                from tools.approval import check_all_command_guards

                decision = check_all_command_guards(
                    "rm -rf /tmp/api-approval "
                    "sk-proj-super-secret-value-that-must-never-leak",
                    "local",
                )
                final_response = (
                    "approved"
                    if decision.get("approved")
                    else f"denied:{decision.get('outcome', 'blocked')}"
                )
            else:
                final_response = f"answer:{user_message}"

            self.session_db.append_message(
                self.session_id, "assistant", final_response
            )
            return {
                "final_response": final_response,
                "messages": [
                    *conversation_history,
                    {"role": "user", "content": user_message},
                    {"role": "assistant", "content": final_response},
                ],
                "completed": True,
            }

        def interrupt(self, _reason):
            self._interrupt_requested = True

        def release_clients(self):
            self.release_count += 1

    monkeypatch.setattr("run_agent.AIAgent", FakeAgent)
    monkeypatch.setattr(
        "gateway.run._resolve_runtime_agent_kwargs", lambda: dict(runtime)
    )
    monkeypatch.setattr(
        "gateway.run._resolve_gateway_model", lambda: model["name"]
    )
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config)
    monkeypatch.setattr(
        "gateway.run._isolated_gateway_runtime_active", lambda: False
    )
    monkeypatch.setattr("gateway.run._current_max_iterations", lambda: 90)
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_reasoning_config",
        staticmethod(lambda: {"effort": "high"}),
    )
    monkeypatch.setattr(
        "gateway.run.GatewayRunner._load_fallback_model",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args: {"clarify", "todo"},
    )
    monkeypatch.setattr(adapter, "_session_model_override_for", lambda _key: None)

    # Exercise the real binding and cleanup state machine while replacing the
    # unavailable privileged writer with its explicit local-runtime receipt.
    monkeypatch.setattr(
        adapter,
        "_revoke_api_server_run_capabilities",
        lambda _session_key, _epoch: {
            "writer_required": False,
            "success": True,
            "scope_revoked": False,
            "authority_active": False,
        },
    )
    monkeypatch.setattr(
        adapter,
        "_clear_api_server_run_local_authority",
        lambda _session_key, _epoch: None,
    )

    harness = SimpleNamespace(
        adapter=adapter,
        db=session_db,
        FakeAgent=FakeAgent,
        runtime=runtime,
        model=model,
        config=config,
    )
    try:
        yield harness
    finally:
        cached = [
            entry.get("agent") for entry in adapter._api_agent_cache.values()
        ]
        adapter._api_agent_cache.clear()
        for agent in cached:
            adapter._release_api_cached_agent(agent)
        from tools import approval as approval_module

        approval_module._gateway_queues.clear()
        approval_module._gateway_notify_cbs.clear()
        adapter._api_pending_approvals.clear()
        with clarify_module._lock:
            clarify_module._entries.clear()
            clarify_module._session_index.clear()
            clarify_module._current_generations.clear()
        adapter._response_store.close()
        close = getattr(session_db, "close", None)
        if callable(close):
            close()


async def _run_turn(adapter: APIServerAdapter, session_id: str, text: str):
    return await adapter._run_agent(
        user_message=text,
        conversation_history=[],
        session_id=session_id,
        cleanup_ref=[None],
    )


async def _wait_for_approval(
    client: TestClient,
    session_id: str,
) -> dict:
    for _ in range(300):
        response = await client.get(
            f"/v1/approvals?session_id={session_id}",
            headers=AUTH_HEADERS,
        )
        assert response.status == 200
        pending = (await response.json())["data"]
        if pending:
            return pending[0]
        await asyncio.sleep(0.01)
    raise AssertionError("approval did not become pending")


async def _resolve_approval(
    client: TestClient,
    approval: dict,
    choice: str,
) -> dict:
    body = {"choice": choice}
    if choice != "deny":
        body["owner_authority"] = _owner_authority(approval, choice)
    response = await client.post(
        approval["response_endpoint"],
        headers={
            **AUTH_HEADERS,
            "X-Hermes-Session-Id": approval["session_id"],
        },
        json=body,
    )
    assert response.status == 200
    return await response.json()


def _owner_authority(
    approval: dict,
    choice: str,
    *,
    issued_at: int | None = None,
    expires_at: int | None = None,
    nonce: str | None = None,
    passkey: str = APPROVAL_PASSKEY,
    run_id: str = "",
) -> dict:
    issued_at = int(time.time()) if issued_at is None else issued_at
    expires_at = issued_at + 60 if expires_at is None else expires_at
    nonce = nonce or hashlib.sha256(
        f"{approval['id']}:{choice}:{issued_at}:{run_id}".encode()
    ).hexdigest()[:32]
    authority = {
        "schema": "hermes.api.approval-owner-authority.v1",
        "nonce": nonce,
        "issued_at_unix": issued_at,
        "expires_at_unix": expires_at,
        "capability_epoch_sha256": approval["capability_epoch_sha256"],
    }
    payload = APIServerAdapter._api_approval_authority_payload(
        session_id=approval["session_id"],
        run_id=run_id,
        approval_id=approval["id"],
        choice=choice,
        nonce=nonce,
        issued_at_unix=issued_at,
        expires_at_unix=expires_at,
        capability_epoch_sha256=approval["capability_epoch_sha256"],
    )
    authority["signature"] = hmac.new(
        passkey.encode(),
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode(),
        hashlib.sha256,
    ).hexdigest()
    return authority


@pytest.mark.asyncio
async def test_responses_chain_reuses_one_agent_and_freezes_tool_schemas(api_runtime):
    adapter = api_runtime.adapter
    FakeAgent = api_runtime.FakeAgent
    app = _test_app(adapter)

    async with TestClient(TestServer(app)) as client:
        first_response = await client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={"model": "hermes-agent", "input": "first turn"},
        )
        assert first_response.status == 200
        first = await first_response.json()
        first_agent = FakeAgent.instances[0]
        assert first_agent.tool_schemas == [
            {"type": "function", "name": "schema-v1"}
        ]

        FakeAgent.schema_source = [{"type": "function", "name": "schema-v2"}]
        continued_response = await client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={
                "model": "hermes-agent",
                "input": "second turn",
                "previous_response_id": first["id"],
            },
        )
        assert continued_response.status == 200
        assert len(FakeAgent.instances) == 1
        assert FakeAgent.instances[0] is first_agent
        assert first_agent.tool_schemas == [
            {"type": "function", "name": "schema-v1"}
        ]

        independent_response = await client.post(
            "/v1/responses",
            headers=AUTH_HEADERS,
            json={"model": "hermes-agent", "input": "independent turn"},
        )
        assert independent_response.status == 200

    assert len(FakeAgent.instances) == 2
    assert FakeAgent.instances[1].tool_schemas == [
        {"type": "function", "name": "schema-v2"}
    ]


@pytest.mark.asyncio
async def test_model_config_auth_failure_and_session_end_evict_exact_agent(api_runtime):
    adapter = api_runtime.adapter
    db = api_runtime.db
    FakeAgent = api_runtime.FakeAgent
    session_id = db.create_session("cache-boundary", "api_server")

    await _run_turn(adapter, session_id, "initial")
    first = FakeAgent.instances[-1]

    api_runtime.model["name"] = "gpt-5.6-sol-canary"
    await _run_turn(adapter, session_id, "model changed")
    second = FakeAgent.instances[-1]
    assert second is not first
    assert first.release_count == 1

    api_runtime.config["compression"]["threshold"] = 0.7
    await _run_turn(adapter, session_id, "config changed")
    third = FakeAgent.instances[-1]
    assert third is not second
    assert second.release_count == 1

    api_runtime.runtime["api_key"] = "provider-key-two"
    await _run_turn(adapter, session_id, "auth changed")
    fourth = FakeAgent.instances[-1]
    assert fourth is not third
    assert third.release_count == 1

    FakeAgent.fail_next = True
    failed, _usage = await _run_turn(adapter, session_id, "terminal failure")
    assert failed["failed"] is True
    assert session_id not in adapter._api_agent_cache
    assert fourth.release_count == 1

    await _run_turn(adapter, session_id, "after failure")
    fifth = FakeAgent.instances[-1]
    assert fifth is not fourth

    async with TestClient(TestServer(_test_app(adapter))) as client:
        deleted = await client.delete(
            f"/api/sessions/{session_id}", headers=AUTH_HEADERS
        )
        assert deleted.status == 200

    assert session_id not in adapter._api_agent_cache
    assert fifth.release_count == 1


@pytest.mark.asyncio
async def test_chat_clarification_round_trip_uses_exact_structured_id(api_runtime):
    adapter = api_runtime.adapter
    api_runtime.db.create_session("clarify-session", "api_server")
    app = _test_app(adapter)
    headers = {
        **AUTH_HEADERS,
        "X-Hermes-Session-Id": "clarify-session",
    }

    async with TestClient(TestServer(app)) as client:
        chat_task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "ask-clarify"}],
                },
            )
        )

        pending = []
        for _ in range(200):
            poll = await client.get(
                "/v1/clarifications?session_id=clarify-session",
                headers=AUTH_HEADERS,
            )
            assert poll.status == 200
            pending = (await poll.json())["data"]
            if pending:
                break
            await asyncio.sleep(0.01)

        assert len(pending) == 1
        clarification = pending[0]
        assert clarification["question"] == "Which exact option should I use?"
        assert clarification["choices"] == ["alpha", "beta"]
        assert clarification["id"].isalnum()

        unscoped = await client.get(
            "/v1/clarifications",
            headers=AUTH_HEADERS,
        )
        assert unscoped.status == 400
        other_session = await client.get(
            "/v1/clarifications?session_id=another-session",
            headers=AUTH_HEADERS,
        )
        assert (await other_session.json())["data"] == []
        cross_session = await client.post(
            clarification["response_endpoint"],
            headers={
                **AUTH_HEADERS,
                "X-Hermes-Session-Id": "another-session",
            },
            json={"choice_index": 1},
        )
        assert cross_session.status == 404

        invalid = await client.post(
            clarification["response_endpoint"],
            headers={
                **AUTH_HEADERS,
                "X-Hermes-Session-Id": "clarify-session",
            },
            json={"response": "beta", "choice_index": 1},
        )
        assert invalid.status == 400

        resolved = await client.post(
            clarification["response_endpoint"],
            headers={
                **AUTH_HEADERS,
                "X-Hermes-Session-Id": "clarify-session",
            },
            json={"choice_index": 1},
        )
        assert resolved.status == 200
        assert (await resolved.json())["clarify_id"] == clarification["id"]

        chat_response = await chat_task
        assert chat_response.status == 200
        chat = await chat_response.json()
        assert chat["choices"][0]["message"]["content"] == "selected:beta"

        final_poll = await client.get(
            "/v1/clarifications?session_id=clarify-session",
            headers=AUTH_HEADERS,
        )
        assert (await final_poll.json())["data"] == []


def test_api_clarify_authority_retirement_is_bounded_and_aba_safe(
    api_runtime,
    monkeypatch,
):
    adapter = api_runtime.adapter
    from tools import clarify_gateway

    def _finish_immediately(
        _clarify_id,
        timeout,
        *,
        session_key=None,
        generation=None,
    ):
        del timeout
        clarify_gateway.clear_session(
            session_key,
            generation=generation,
        )
        return None

    monkeypatch.setattr(
        clarify_gateway,
        "wait_for_response",
        _finish_immediately,
    )

    first_callback = adapter._make_api_clarify_callback("retired-session")
    first_callback("First?", ["A"])
    first_authority = first_callback._api_clarify_authority
    first_generation = first_authority.generation
    assert first_generation is not None
    assert adapter._retire_api_clarify_authority(first_authority) is True

    # A stale closure is permanently fenced and cannot republish its retired
    # token even after a new turn for the same public session starts.
    second_callback = adapter._make_api_clarify_callback("retired-session")
    second_callback("Second?", ["B"])
    second_authority = second_callback._api_clarify_authority
    assert second_authority.generation > first_generation
    assert first_callback("Late stale?", ["C"]).startswith(
        "[clarification unavailable"
    )
    assert adapter._retire_api_clarify_authority(second_authority) is True

    # Distinct session churn retains no per-scope authority map in either the
    # API adapter or clarify core.
    for index in range(500):
        callback = adapter._make_api_clarify_callback(f"scope-{index}")
        callback("Q?", ["A"])
        assert adapter._retire_api_clarify_authority(
            callback._api_clarify_authority
        ) is True
    with clarify_gateway._lock:
        assert clarify_gateway._current_generations == {}
    assert not hasattr(adapter, "_api_clarify_generations")


def test_api_active_release_fences_but_retires_only_at_worker_boundary(
    api_runtime,
    monkeypatch,
):
    adapter = api_runtime.adapter
    from tools import clarify_gateway

    def _finish_immediately(
        _clarify_id,
        timeout,
        *,
        session_key=None,
        generation=None,
    ):
        del timeout
        clarify_gateway.clear_session(
            session_key,
            generation=generation,
        )
        return None

    monkeypatch.setattr(
        clarify_gateway,
        "wait_for_response",
        _finish_immediately,
    )
    callback = adapter._make_api_clarify_callback("active-cleanup")
    callback("Q?", ["A"])
    authority = callback._api_clarify_authority

    class Agent:
        release_count = 0
        _session_messages = ["large"]

        def release_clients(self):
            self.release_count += 1

    agent = Agent()
    agent._api_clarify_authority = authority
    agent._api_clarify_scope = authority.scope

    adapter._release_api_cached_agent(agent)
    assert authority.active is True
    assert authority.accepting is False
    assert authority.scope in clarify_gateway._current_generations
    assert agent.release_count == 0
    assert callback("Late?", ["B"]).startswith("[clarification unavailable")

    # Only the execution owner may cross this boundary.
    assert adapter._retire_api_agent_clarifications(agent) is True
    assert authority.scope not in clarify_gateway._current_generations
    adapter._release_api_cached_agent(agent)
    assert agent.release_count == 1
    assert agent._session_messages == []
    assert id(agent) not in adapter._api_deferred_agent_releases


def test_api_clarify_retirement_retries_transient_core_clear_failure(
    api_runtime,
    monkeypatch,
):
    adapter = api_runtime.adapter
    from tools import clarify_gateway

    callback = adapter._make_api_clarify_callback("retry-retirement")
    authority = callback._api_clarify_authority
    generation = clarify_gateway.claim_session_generation(authority.scope)
    authority.generation = generation
    clarify_gateway.register(
        "retry-core-entry",
        authority.scope,
        "Q?",
        ["A"],
        generation=generation,
        identity_v1=True,
    )

    original_clear = clarify_gateway.clear_session
    attempts = 0

    def _flaky_clear(session_key, *, generation=None):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("injected transient core clear failure")
        return original_clear(session_key, generation=generation)

    monkeypatch.setattr(clarify_gateway, "clear_session", _flaky_clear)

    assert adapter._retire_api_clarify_authority(authority) is False
    assert authority.active is False
    assert authority.accepting is False
    assert authority.retired is False
    assert clarify_gateway.session_generation_retained(
        authority.scope,
        generation,
    ) is True

    assert adapter._retire_api_clarify_authority(authority) is True
    assert authority.retired is True
    assert clarify_gateway.session_generation_retained(
        authority.scope,
        generation,
    ) is False
    assert attempts == 2


def test_api_clarify_retirement_accepts_exact_supersession_without_rollback(
    api_runtime,
):
    adapter = api_runtime.adapter
    from tools import clarify_gateway

    callback = adapter._make_api_clarify_callback("superseded-retirement")
    authority = callback._api_clarify_authority
    old_generation = clarify_gateway.claim_session_generation(authority.scope)
    authority.generation = old_generation
    clarify_gateway.register(
        "superseded-core-entry",
        authority.scope,
        "Old?",
        ["A"],
        generation=old_generation,
        identity_v1=True,
    )
    new_generation = clarify_gateway.claim_session_generation(authority.scope)

    assert adapter._retire_api_clarify_authority(authority) is True
    assert authority.retired is True
    assert clarify_gateway.session_generation_retained(
        authority.scope,
        old_generation,
    ) is False
    assert clarify_gateway.session_generation_retained(
        authority.scope,
        new_generation,
    ) is True
    assert clarify_gateway.retire_session_generation(
        authority.scope,
        new_generation,
    ) is True


@pytest.mark.asyncio
async def test_chat_nonstream_approval_is_authenticated_exact_and_session_scoped(
    api_runtime,
):
    adapter = api_runtime.adapter
    api_runtime.db.create_session("approval-chat", "api_server")
    app = _test_app(adapter)
    headers = {
        **AUTH_HEADERS,
        "X-Hermes-Session-Id": "approval-chat",
    }

    async with TestClient(TestServer(app)) as client:
        unauthenticated = await client.get(
            "/v1/approvals?session_id=approval-chat"
        )
        assert unauthenticated.status == 401

        request_task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "danger"}],
                },
            )
        )
        approval = await _wait_for_approval(client, "approval-chat")

        assert re.fullmatch(r"[0-9a-f]{32}", approval["id"])
        assert approval["choices"] == ["once", "deny"]
        assert approval["session_id"] == "approval-chat"
        assert "super-secret-value" in approval["command"]

        other_session = await client.get(
            "/v1/approvals?session_id=approval-other",
            headers=AUTH_HEADERS,
        )
        assert other_session.status == 200
        assert (await other_session.json())["data"] == []

        cross_session = await client.post(
            approval["response_endpoint"],
            headers={
                **AUTH_HEADERS,
                "X-Hermes-Session-Id": "approval-other",
            },
            json={"choice": "once"},
        )
        assert cross_session.status == 404

        generic_bearer_grant = await client.post(
            approval["response_endpoint"],
            headers=headers,
            json={"choice": "once"},
        )
        assert generic_bearer_grant.status == 403

        now = int(time.time())
        expired_grant = await client.post(
            approval["response_endpoint"],
            headers=headers,
            json={
                "choice": "once",
                "owner_authority": _owner_authority(
                    approval,
                    "once",
                    issued_at=now - 120,
                    expires_at=now - 60,
                ),
            },
        )
        assert expired_grant.status == 409

        invalid_shape = await client.post(
            approval["response_endpoint"],
            headers=headers,
            json={"choice": "deny", "reason": "text is not authority"},
        )
        assert invalid_shape.status == 400

        resolved = await _resolve_approval(client, approval, "deny")
        assert resolved["id"] == approval["id"]
        assert resolved["choice"] == "deny"

        stale = await client.post(
            approval["response_endpoint"],
            headers=headers,
            json={"choice": "once"},
        )
        assert stale.status == 404

        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status == 200
        body = await response.json()
        assert body["choices"][0]["message"]["content"] == "denied:denied"

        final_poll = await client.get(
            "/v1/approvals?session_id=approval-chat",
            headers=AUTH_HEADERS,
        )
        assert (await final_poll.json())["data"] == []


@pytest.mark.asyncio
async def test_responses_nonstream_approval_uses_explicit_session_and_polling(
    api_runtime,
):
    adapter = api_runtime.adapter
    app = _test_app(adapter)
    headers = {
        **AUTH_HEADERS,
        "X-Hermes-Session-Id": "approval-responses",
    }

    async with TestClient(TestServer(app)) as client:
        request_task = asyncio.create_task(
            client.post(
                "/v1/responses",
                headers=headers,
                json={"model": "hermes-agent", "input": "danger"},
            )
        )
        approval = await _wait_for_approval(client, "approval-responses")
        await _resolve_approval(client, approval, "once")

        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status == 200
        assert response.headers["X-Hermes-Session-Id"] == "approval-responses"
        body = await response.json()
        assert body["status"] == "completed"
        assert "approved" in json.dumps(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "request_body", "request_event", "response_event"),
    [
        (
            "/v1/chat/completions",
            {
                "model": "hermes-agent",
                "stream": True,
                "messages": [{"role": "user", "content": "danger"}],
            },
            "event: hermes.approval.request",
            "event: hermes.approval.responded",
        ),
        (
            "/v1/responses",
            {"model": "hermes-agent", "stream": True, "input": "danger"},
            "response.hermes.approval.request",
            "response.hermes.approval.responded",
        ),
    ],
)
async def test_streaming_approval_emits_request_and_response_events(
    api_runtime,
    path,
    request_body,
    request_event,
    response_event,
):
    adapter = api_runtime.adapter
    session_id = "stream-" + ("chat" if "chat" in path else "responses")
    if "chat" in path:
        api_runtime.db.create_session(session_id, "api_server")
    headers = {
        **AUTH_HEADERS,
        "X-Hermes-Session-Id": session_id,
    }

    async with TestClient(TestServer(_test_app(adapter))) as client:
        response = await client.post(path, headers=headers, json=request_body)
        assert response.status == 200
        approval = await _wait_for_approval(client, session_id)
        await _resolve_approval(client, approval, "once")

        stream_text = await asyncio.wait_for(response.text(), timeout=5)
        assert request_event in stream_text
        assert response_event in stream_text
        assert approval["id"] in stream_text


@pytest.mark.asyncio
async def test_approval_timeout_cleans_public_and_core_state(
    api_runtime,
    monkeypatch,
):
    from tools import approval as approval_module

    monkeypatch.setattr(
        approval_module,
        "_get_approval_config",
        lambda: {"mode": "manual", "gateway_timeout": 0, "timeout": 0},
    )
    session_id = "approval-timeout"
    api_runtime.db.create_session(session_id, "api_server")
    headers = {
        **AUTH_HEADERS,
        "X-Hermes-Session-Id": session_id,
    }

    async with TestClient(TestServer(_test_app(api_runtime.adapter))) as client:
        response = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": "danger"}],
            },
        )
        assert response.status == 200
        body = await response.json()
        assert body["choices"][0]["message"]["content"] == "denied:timeout"

        pending = await client.get(
            f"/v1/approvals?session_id={session_id}",
            headers=AUTH_HEADERS,
        )
        assert (await pending.json())["data"] == []
        assert approval_module.get_pending_gateway_approvals(session_id) == []


@pytest.mark.asyncio
async def test_session_end_cancels_pending_approval_and_fails_closed(api_runtime):
    session_id = "approval-session-end"
    api_runtime.db.create_session(session_id, "api_server")
    headers = {
        **AUTH_HEADERS,
        "X-Hermes-Session-Id": session_id,
    }

    async with TestClient(TestServer(_test_app(api_runtime.adapter))) as client:
        request_task = asyncio.create_task(
            client.post(
                "/v1/chat/completions",
                headers=headers,
                json={
                    "model": "hermes-agent",
                    "messages": [{"role": "user", "content": "danger"}],
                },
            )
        )
        approval = await _wait_for_approval(client, session_id)

        ended = await client.patch(
            f"/api/sessions/{session_id}",
            headers=AUTH_HEADERS,
            json={"end_reason": "rotated"},
        )
        assert ended.status == 200

        stale = await client.post(
            approval["response_endpoint"],
            headers=headers,
            json={"choice": "once"},
        )
        assert stale.status == 404

        response = await asyncio.wait_for(request_task, timeout=5)
        assert response.status == 200
        body = await response.json()
        assert body["choices"][0]["message"]["content"].startswith("denied:")


def test_exact_core_approval_ids_resolve_out_of_order_without_fifo_fallback():
    from tools import approval as approval_module

    session_id = "exact-id-session"
    first = approval_module._ApprovalEntry(
        {"command": "first"},
        authority_generation=7,
        capability_epoch_sha256="a" * 64,
    )
    second = approval_module._ApprovalEntry({"command": "second"})
    with approval_module._lock:
        approval_module._gateway_queues[session_id] = [first, second]
    try:
        public = approval_module.get_pending_gateway_approvals(session_id)
        assert public[0]["approval_id"] == first.approval_id
        assert all(
            not key.startswith("_")
            for snapshot in public
            for key in snapshot
        )
        private = approval_module.get_pending_gateway_approvals(
            session_id,
            include_authority_binding=True,
        )
        assert private[0]["_authority_generation"] == 7
        assert private[0]["_capability_epoch_sha256"] == "a" * 64

        assert approval_module.resolve_gateway_approval_by_id(
            "another-session", second.approval_id, "deny"
        ) == 0
        assert approval_module.resolve_gateway_approval_by_id(
            session_id, second.approval_id, "not-an-enum"
        ) == 0
        assert not first.event.is_set()
        assert not second.event.is_set()

        assert approval_module.resolve_gateway_approval_by_id(
            session_id, second.approval_id, "deny"
        ) == 1
        assert second.event.is_set()
        assert second.result == "deny"
        assert not first.event.is_set()
        assert approval_module._gateway_queues[session_id] == [first]

        assert approval_module.resolve_gateway_approval_by_id(
            session_id, second.approval_id, "once"
        ) == 0
        assert approval_module._gateway_queues[session_id] == [first]
        assert approval_module.resolve_gateway_approval_by_id(
            session_id, first.approval_id, "once"
        ) == 1
        assert first.event.is_set()
        assert first.result == "once"
    finally:
        with approval_module._lock:
            approval_module._gateway_queues.pop(session_id, None)


def test_owner_authority_nonce_is_exact_and_replay_proof():
    adapter = APIServerAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "key": "sk-api-test",
                "approval_passkey": APPROVAL_PASSKEY,
            },
        )
    )
    approval = {
        "id": "1" * 32,
        "session_id": "authority-session",
        "capability_epoch_sha256": "2" * 64,
    }
    authority = _owner_authority(
        approval,
        "once",
        nonce="3" * 32,
    )
    try:
        first = adapter._verify_and_consume_api_approval_authority(
            authority,
            session_id=approval["session_id"],
            approval_id=approval["id"],
            choice="once",
            capability_epoch_sha256=approval[
                "capability_epoch_sha256"
            ],
        )
        assert first is None

        replay = adapter._verify_and_consume_api_approval_authority(
            authority,
            session_id=approval["session_id"],
            approval_id=approval["id"],
            choice="once",
            capability_epoch_sha256=approval[
                "capability_epoch_sha256"
            ],
        )
        assert replay is not None
        assert replay.status == 409

        wrong_choice = adapter._verify_and_consume_api_approval_authority(
            authority,
            session_id=approval["session_id"],
            approval_id=approval["id"],
            choice="session",
            capability_epoch_sha256=approval[
                "capability_epoch_sha256"
            ],
        )
        assert wrong_choice is not None
        assert wrong_choice.status == 403
    finally:
        adapter._response_store.close()
