"""Integration regressions for the existing turn and auxiliary seams."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import uuid

import pytest

from run_agent import AIAgent


SENSITIVE_VALUE = uuid.uuid4().hex


class CapturingDB:
    def __init__(
        self,
        *,
        failure_message: str = "",
        failure: BaseException | None = None,
    ):
        self.rows = []
        self.failure_message = failure_message
        self.failure = failure

    def record_turn_telemetry(self, **row):
        if self.failure is not None:
            raise self.failure
        if self.failure_message:
            raise RuntimeError(self.failure_message)
        self.rows.append(row)

    def get_conversation_root(self, session_id):
        return session_id

    def record_auxiliary_usage(self, *args, **kwargs):
        return None


@contextmanager
def _null_scope(*args, **kwargs):
    del args, kwargs
    yield


def _bare_agent(db):
    agent = object.__new__(AIAgent)
    agent.session_id = "integration-session"
    agent.platform = "cli"
    agent.model = "gpt-5.5"
    agent.provider = "openai-codex"
    agent.requested_provider = "openai-codex"
    agent._primary_runtime = {
        "provider": "openai-codex",
        "model": "gpt-5.5",
    }
    agent._parent_session_id = None
    setattr(agent, "_parent_turn_id", "")
    agent.session_estimated_cost_usd = 0.0
    agent.session_cost_status = "unknown"
    agent._session_db = db
    agent._relay_pending_turn_id = None
    agent._reset_activity_labels_after_turn = MagicMock()
    return agent


def _patch_run_scaffolding(monkeypatch, fake_run):
    from agent import conversation_loop, relay_runtime, subagent_lifecycle
    from agent import auxiliary_client
    from hermes_cli.observability import relay_shared_metrics, turn_telemetry

    coordinator = MagicMock()
    coordinator.acquire_conversation.return_value = object()
    coordinator.begin_turn.return_value = object()
    monkeypatch.setattr(relay_runtime, "SESSION_COORDINATOR", coordinator)
    monkeypatch.setattr(relay_runtime, "current_profile_key", lambda: "/qa-profile")
    monkeypatch.setattr(conversation_loop, "run_conversation", fake_run)
    monkeypatch.setattr(subagent_lifecycle, "bind_subagent_parent", _null_scope)
    monkeypatch.setattr(auxiliary_client, "scoped_runtime_main", _null_scope)
    monkeypatch.setattr(relay_shared_metrics, "start_task_run", lambda **kwargs: None)
    monkeypatch.setattr(relay_shared_metrics, "finish_task_run", lambda **kwargs: None)
    monkeypatch.setattr(turn_telemetry, "_active_profile_name", lambda: "qa")
    return coordinator


@pytest.fixture(autouse=True)
def _isolate_observation_contextvars():
    """Keep intentional RED failures from contaminating the next regression."""
    from agent import aux_accounting, auxiliary_client, relay_runtime
    from agent import portal_tags
    from hermes_cli.observability import turn_telemetry

    owned = (
        (aux_accounting._accounting, aux_accounting._accounting.set(None)),
        (
            auxiliary_client._RELAY_AUX_CALL_CONTEXT,
            auxiliary_client._RELAY_AUX_CALL_CONTEXT.set(None),
        ),
        (portal_tags._conversation_id, portal_tags._conversation_id.set(None)),
        (relay_runtime._CURRENT_TURN, relay_runtime._CURRENT_TURN.set(None)),
        (turn_telemetry._CURRENT_TURN, turn_telemetry._CURRENT_TURN.set(None)),
    )
    try:
        yield
    finally:
        for variable, token in reversed(owned):
            variable.reset(token)


def _raise(error: BaseException):
    raise error


def _assert_turn_contexts_cleared(agent, *, activity_calls: int = 1):
    from agent import aux_accounting, auxiliary_client, relay_runtime
    from agent.portal_tags import get_conversation_context
    from hermes_cli.observability import turn_telemetry

    assert agent._relay_pending_turn_id is None
    assert aux_accounting.get_accounting_context() is None
    assert get_conversation_context() is None
    assert auxiliary_client._RELAY_AUX_CALL_CONTEXT.get() is None
    assert relay_runtime.current_turn() is None
    assert turn_telemetry._CURRENT_TURN.get() is None
    assert agent._reset_activity_labels_after_turn.call_count == activity_calls


def test_run_conversation_records_main_seam_without_changing_result(monkeypatch):
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli import plugins

    db = CapturingDB()
    agent = _bare_agent(db)
    expected = {"completed": True, "final_response": "ok"}

    def fake_run(current_agent, *args, **kwargs):
        del args, kwargs
        invoke_hook(
            "pre_api_request",
            session_id=current_agent.session_id,
            turn_id=current_agent._relay_pending_turn_id,
            api_request_id="main-1",
            provider="openai-codex",
            model="gpt-5.5",
            request={"messages": [SENSITIVE_VALUE]},
        )
        invoke_hook(
            "post_api_request",
            session_id=current_agent.session_id,
            turn_id=current_agent._relay_pending_turn_id,
            api_request_id="main-1",
            provider="openai-codex",
            model="gpt-5.5",
            usage={"input_tokens": 8, "output_tokens": 5, "total_tokens": 13},
            response={"content": SENSITIVE_VALUE},
        )
        return expected

    monkeypatch.setattr(plugins, "invoke_hook", lambda *args, **kwargs: [])
    coordinator = _patch_run_scaffolding(monkeypatch, fake_run)

    result = agent.run_conversation(SENSITIVE_VALUE, task_id="task-1")

    assert result is expected
    assert len(db.rows) == 1
    row = db.rows[0]
    assert row["attempt_count"] == 1
    assert row["input_tokens"] == 8
    assert row["output_tokens"] == 5
    assert row["total_tokens"] == 13
    assert row["outcome"] == "success"
    assert SENSITIVE_VALUE not in json.dumps(row, sort_keys=True)
    coordinator.end_turn.assert_called_once()
    assert agent._relay_pending_turn_id is None


def test_run_conversation_success_survives_telemetry_storage_failure(
    monkeypatch, caplog
):
    from hermes_cli.observability import turn_telemetry

    db = CapturingDB(failure_message=SENSITIVE_VALUE)
    agent = _bare_agent(db)
    expected = {"completed": True, "final_response": "unchanged"}

    def fake_run(*args, **kwargs):
        del args, kwargs
        return expected

    _patch_run_scaffolding(monkeypatch, fake_run)
    caplog.set_level("DEBUG", logger=turn_telemetry.__name__)

    result = agent.run_conversation(SENSITIVE_VALUE, task_id="task-store-failure")

    assert result is expected
    assert db.rows == []
    assert "RuntimeError" in caplog.text
    assert SENSITIVE_VALUE not in caplog.text
    assert agent._relay_pending_turn_id is None


def test_run_conversation_preserves_exception_and_records_terminal(monkeypatch):
    db = CapturingDB()
    agent = _bare_agent(db)
    expected = TimeoutError(SENSITIVE_VALUE)

    def fake_run(*args, **kwargs):
        del args, kwargs
        raise expected

    _patch_run_scaffolding(monkeypatch, fake_run)

    with pytest.raises(TimeoutError) as caught:
        agent.run_conversation(SENSITIVE_VALUE, task_id="task-timeout")

    assert caught.value is expected
    assert len(db.rows) == 1
    assert db.rows[0]["outcome"] == "failed"
    assert db.rows[0]["failure_class"] == "timeout"
    assert SENSITIVE_VALUE not in json.dumps(db.rows[0], sort_keys=True)
    assert agent._relay_pending_turn_id is None


def test_auxiliary_relay_and_usage_seams_feed_active_turn(monkeypatch):
    from agent import aux_accounting, auxiliary_client
    from hermes_cli.observability import turn_telemetry

    db = CapturingDB()
    agent = _bare_agent(db)
    monkeypatch.setattr(turn_telemetry, "_active_profile_name", lambda: "qa")
    binding = turn_telemetry.begin_turn(agent, "turn-aux", started_at=10.0)
    accounting_token = aux_accounting.set_accounting_context(db, agent.session_id)

    @auxiliary_client._relay_auxiliary_call
    def physical_call(task):
        auxiliary_client._set_relay_auxiliary_route(
            "openai-codex", "gpt-5.5-mini", "chat_completions"
        )
        auxiliary_client._relay_auxiliary_metadata()
        auxiliary_client._set_relay_auxiliary_route(
            "openrouter", "qwen/qwen3-8b", "chat_completions"
        )
        auxiliary_client._relay_auxiliary_metadata()
        response = SimpleNamespace(
            model="qwen/qwen3-8b",
            usage=SimpleNamespace(
                prompt_tokens=4,
                completion_tokens=3,
                total_tokens=7,
            ),
        )
        aux_accounting.record_aux_usage(response, task, provider="openrouter")
        return response

    try:
        physical_call("compression")
    finally:
        aux_accounting.reset_accounting_context(accounting_token)
    turn_telemetry.finish_turn(
        binding,
        result={"completed": True},
        ended_at=10.2,
    )

    assert len(db.rows) == 1
    row = db.rows[0]
    assert row["auxiliary_attempt_count"] == 2
    assert row["retry_count"] == 1
    assert row["fallback_count"] == 1
    assert row["input_tokens"] == 4
    assert row["output_tokens"] == 3
    assert row["total_tokens"] == 7


def test_gateway_bridge_writes_through_async_store_wrapper():
    from gateway.run import _record_gateway_terminal_telemetry

    db = CapturingDB()
    wrapper = SimpleNamespace(_db=db)
    _record_gateway_terminal_telemetry(
        wrapper,
        opaque_session_id="gateway-session",
        source="telegram",
        failure_class="gateway_refused",
    )

    assert len(db.rows) == 1
    assert db.rows[0]["session_id"] == "gateway-session"
    assert db.rows[0]["source"] == "telegram"
    assert db.rows[0]["failure_class"] == "gateway_refused"
    assert db.rows[0]["attempt_count"] == 0
    assert db.rows[0]["outcome"] == "refused"
    assert db.rows[0]["route_type"] == "local_triage"
    assert db.rows[0]["disposition"] == "triaged"


def test_repeated_max_length_task_ids_use_distinct_opaque_full_uuid_turn_ids(
    tmp_path, monkeypatch, caplog
):
    """Caller task text must never become a durable correlation key."""
    from hermes_state import SessionDB

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    agent = _bare_agent(db)
    agent.session_id = "s" * 256
    sensitive_task = (f"task-{uuid.uuid4().hex}-" + ("t" * 256))[:256]
    expected = {"completed": True, "final_response": "unchanged"}
    observed_task_ids = []

    def fake_run(_agent, *args, **kwargs):
        del kwargs
        observed_task_ids.append(args[3])
        return expected

    coordinator = _patch_run_scaffolding(monkeypatch, fake_run)
    caplog.set_level("DEBUG")
    try:
        assert agent.run_conversation("first", task_id=sensitive_task) is expected
        assert agent.run_conversation("second", task_id=sensitive_task) is expected
        rows = db.list_turn_telemetry(session_id=agent.session_id, limit=10)
    finally:
        db.close()

    assert observed_task_ids == [sensitive_task, sensitive_task]
    assert len(rows) == 2
    turn_ids = {row["turn_id"] for row in rows}
    assert len(turn_ids) == 2
    for row in rows:
        assert re.fullmatch(r"turn-[0-9a-f]{32}", row["turn_id"])
        suffix = row["turn_id"].removeprefix("turn-")
        assert len(suffix) == 32
        assert uuid.UUID(hex=suffix).version == 4
        assert row["correlation_id"] == row["turn_id"]
    relay_turn_ids = {
        call.kwargs["turn_id"] for call in coordinator.begin_turn.call_args_list
    }
    assert relay_turn_ids == turn_ids
    assert sensitive_task not in json.dumps(rows, sort_keys=True)
    assert sensitive_task not in caplog.text
    for artifact in tmp_path.rglob("*"):
        if artifact.is_file():
            assert sensitive_task.encode() not in artifact.read_bytes()
    _assert_turn_contexts_cleared(agent, activity_calls=2)


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_turn_initialization_isolates_only_ordinary_exceptions(
    monkeypatch, fault_type
):
    from hermes_cli.observability import turn_telemetry

    fault = fault_type("initialization fault")
    monkeypatch.setattr(
        turn_telemetry,
        "_active_profile_name",
        lambda: _raise(fault),
    )

    if fault_type is RuntimeError:
        binding = turn_telemetry.begin_turn(_bare_agent(CapturingDB()), "caller-text")
        assert binding.collector is None
    else:
        with pytest.raises(fault_type) as caught:
            turn_telemetry.begin_turn(_bare_agent(CapturingDB()), "caller-text")
        assert caught.value is fault
    assert turn_telemetry._CURRENT_TURN.get() is None


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_run_boundary_initialization_fault_preserves_semantics_and_cleanup(
    monkeypatch, fault_type
):
    from hermes_cli.observability import turn_telemetry

    fault = fault_type("initialization boundary fault")
    expected = {"completed": True, "final_response": "same object"}
    agent = _bare_agent(CapturingDB())
    coordinator = _patch_run_scaffolding(
        monkeypatch,
        lambda *_args, **_kwargs: expected,
    )
    monkeypatch.setattr(
        turn_telemetry,
        "begin_turn",
        lambda *_args, **_kwargs: _raise(fault),
    )

    if fault_type is RuntimeError:
        assert agent.run_conversation("message", task_id="task") is expected
        coordinator.end_turn.assert_called_once()
    else:
        with pytest.raises(fault_type) as caught:
            agent.run_conversation("message", task_id="task")
        assert caught.value is fault
        coordinator.begin_turn.assert_not_called()
    _assert_turn_contexts_cleared(agent)


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_lifecycle_observer_fault_preserves_signal_identity_and_turn_cleanup(
    monkeypatch, fault_type
):
    from hermes_cli import plugins
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.observability import turn_telemetry

    fault = fault_type("lifecycle fault")
    expected = {"completed": True, "final_response": "same object"}
    db = CapturingDB()
    agent = _bare_agent(db)

    def fake_run(current_agent, *_args, **_kwargs):
        invoke_hook(
            "pre_api_request",
            session_id=current_agent.session_id,
            turn_id=current_agent._relay_pending_turn_id,
            api_request_id="request-1",
            provider="openai-codex",
            model="gpt-5.5",
        )
        return expected

    _patch_run_scaffolding(monkeypatch, fake_run)
    monkeypatch.setattr(plugins, "invoke_hook", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        turn_telemetry,
        "_observe_lifecycle",
        lambda *_args, **_kwargs: _raise(fault),
    )

    if fault_type is RuntimeError:
        assert agent.run_conversation("message", task_id="task") is expected
        assert db.rows[0]["outcome"] == "success"
    else:
        with pytest.raises(fault_type) as caught:
            agent.run_conversation("message", task_id="task")
        assert caught.value is fault
        assert len(db.rows) == 1
    _assert_turn_contexts_cleared(agent)


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_storage_finalization_isolates_only_ordinary_exceptions(
    monkeypatch, fault_type
):
    from hermes_cli.observability import turn_telemetry

    monkeypatch.setattr(turn_telemetry, "_active_profile_name", lambda: "qa")
    fault = fault_type("storage finalization fault")
    binding = turn_telemetry.begin_turn(
        _bare_agent(CapturingDB(failure=fault)),
        "turn-storage",
        started_at=1.0,
    )

    if fault_type is RuntimeError:
        assert turn_telemetry.finish_turn(
            binding, result={"completed": True}, ended_at=1.1
        ) is None
    else:
        with pytest.raises(fault_type) as caught:
            turn_telemetry.finish_turn(
                binding, result={"completed": True}, ended_at=1.1
            )
        assert caught.value is fault
    assert turn_telemetry._CURRENT_TURN.get() is None


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_caller_finalizer_fault_preserves_semantics_and_nested_cleanup(
    monkeypatch, fault_type
):
    from hermes_cli.observability import turn_telemetry

    fault = fault_type("caller finalizer fault")
    expected = {"completed": True, "final_response": "same object"}
    agent = _bare_agent(CapturingDB())
    _patch_run_scaffolding(monkeypatch, lambda *_args, **_kwargs: expected)
    monkeypatch.setattr(
        turn_telemetry,
        "finish_turn",
        lambda *_args, **_kwargs: _raise(fault),
    )

    if fault_type is RuntimeError:
        assert agent.run_conversation("message", task_id="task") is expected
    else:
        with pytest.raises(fault_type) as caught:
            agent.run_conversation("message", task_id="task")
        assert caught.value is fault
    _assert_turn_contexts_cleared(agent)


def test_primary_turn_exception_identity_survives_ordinary_finalizer_fault(monkeypatch):
    from hermes_cli.observability import turn_telemetry

    primary = TimeoutError("primary")
    agent = _bare_agent(CapturingDB())

    def fake_run(*_args, **_kwargs):
        raise primary

    _patch_run_scaffolding(monkeypatch, fake_run)
    monkeypatch.setattr(
        turn_telemetry,
        "finish_turn",
        lambda *_args, **_kwargs: _raise(RuntimeError("observer")),
    )

    with pytest.raises(TimeoutError) as caught:
        agent.run_conversation("message", task_id="task")
    assert caught.value is primary
    _assert_turn_contexts_cleared(agent)


@pytest.mark.parametrize("bridge", ["attempt", "terminal"])
@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_auxiliary_bridges_isolate_only_ordinary_exceptions(
    monkeypatch, bridge, fault_type
):
    from agent import auxiliary_client
    from hermes_cli.observability import turn_telemetry

    fault = fault_type(f"auxiliary {bridge} fault")
    target = (
        "record_auxiliary_attempt"
        if bridge == "attempt"
        else "record_auxiliary_terminal"
    )
    monkeypatch.setattr(
        turn_telemetry,
        target,
        lambda *_args, **_kwargs: _raise(fault),
    )
    expected = object()

    @auxiliary_client._relay_auxiliary_call
    def physical_call(_task):
        if bridge == "attempt":
            auxiliary_client._set_relay_auxiliary_route(
                "openai-codex", "gpt-5.5", "chat_completions"
            )
            auxiliary_client._relay_auxiliary_metadata()
        return expected

    if fault_type is RuntimeError:
        assert physical_call("compression") is expected
    else:
        with pytest.raises(fault_type) as caught:
            physical_call("compression")
        assert caught.value is fault
    assert auxiliary_client._RELAY_AUX_CALL_CONTEXT.get() is None


def test_auxiliary_primary_exception_identity_survives_observer_fault(monkeypatch):
    from agent import auxiliary_client
    from hermes_cli.observability import turn_telemetry

    primary = TimeoutError("primary auxiliary fault")
    monkeypatch.setattr(
        turn_telemetry,
        "record_auxiliary_terminal",
        lambda *_args, **_kwargs: _raise(RuntimeError("observer fault")),
    )

    @auxiliary_client._relay_auxiliary_call
    def physical_call(_task):
        raise primary

    with pytest.raises(TimeoutError) as caught:
        physical_call("compression")
    assert caught.value is primary
    assert auxiliary_client._RELAY_AUX_CALL_CONTEXT.get() is None


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_auxiliary_usage_bridge_isolates_only_ordinary_exceptions(
    monkeypatch, fault_type
):
    from agent import aux_accounting, usage_pricing
    from hermes_cli.observability import turn_telemetry

    fault = fault_type("auxiliary usage fault")
    monkeypatch.setattr(
        turn_telemetry,
        "record_auxiliary_usage",
        lambda **_kwargs: _raise(fault),
    )
    normalized = SimpleNamespace(
        input_tokens=1,
        output_tokens=1,
        cache_read_tokens=0,
        cache_write_tokens=0,
        reasoning_tokens=0,
    )
    monkeypatch.setattr(usage_pricing, "normalize_usage", lambda *_a, **_k: normalized)
    monkeypatch.setattr(
        usage_pricing,
        "estimate_usage_cost",
        lambda *_a, **_k: SimpleNamespace(status="estimated", amount_usd=0.01),
    )
    token = aux_accounting.set_accounting_context(CapturingDB(), "session")
    response = SimpleNamespace(model="gpt-5.5", usage=object())
    try:
        if fault_type is RuntimeError:
            assert aux_accounting.record_aux_usage(response, "compression") is None
        else:
            with pytest.raises(fault_type) as caught:
                aux_accounting.record_aux_usage(response, "compression")
            assert caught.value is fault
    finally:
        aux_accounting.reset_accounting_context(token)
    assert aux_accounting.get_accounting_context() is None


@pytest.mark.parametrize(
    "fault_type",
    [RuntimeError, asyncio.CancelledError, KeyboardInterrupt],
)
def test_gateway_bridge_isolates_only_ordinary_exceptions(monkeypatch, fault_type):
    from gateway.run import _record_gateway_terminal_telemetry
    from hermes_cli.observability import turn_telemetry

    fault = fault_type("gateway observer fault")
    monkeypatch.setattr(
        turn_telemetry,
        "record_gateway_terminal",
        lambda *_args, **_kwargs: _raise(fault),
    )

    if fault_type is RuntimeError:
        assert _record_gateway_terminal_telemetry(
            CapturingDB(),
            opaque_session_id="session",
            source="telegram",
            failure_class="gateway_refused",
        ) is None
    else:
        with pytest.raises(fault_type) as caught:
            _record_gateway_terminal_telemetry(
                CapturingDB(),
                opaque_session_id="session",
                source="telegram",
                failure_class="gateway_refused",
            )
        assert caught.value is fault


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal", ["general_drain", "external_drain", "capacity"])
@pytest.mark.parametrize(
    "existing_session_id",
    ["20260806_191345_a1b2c3d4", None],
    ids=["existing-opaque-session", "no-existing-session"],
)
async def test_gateway_refusal_paths_record_exactly_one_zero_attempt_terminal(
    monkeypatch, refusal, existing_session_id
):
    from gateway.platforms.base import MessageEvent, MessageType
    from tests.gateway.restart_test_helpers import (
        make_restart_runner,
        make_restart_source,
    )
    from hermes_cli import plugins

    db = CapturingDB()
    runner, _adapter = make_restart_runner()
    runner._session_db = db
    runner._draining = refusal == "general_drain"
    runner._external_drain_active = refusal == "external_drain"
    agent_start = AsyncMock(return_value={"completed": True})
    runner._handle_message_with_agent = agent_start
    if refusal == "capacity":
        runner._claim_active_session_slot = lambda *_args, **_kwargs: (
            None,
            "Capacity refusal",
        )
    monkeypatch.setattr(plugins, "invoke_hook", lambda *_args, **_kwargs: [])

    chat_id = f"chat-{SENSITIVE_VALUE}"
    user_id = f"user-{SENSITIVE_VALUE}"
    thread_id = f"thread-{SENSITIVE_VALUE}"
    account_id = f"account-{SENSITIVE_VALUE}@s.whatsapp.net"
    raw_key = (
        f"agent:main:telegram:{chat_id}:{user_id}:{thread_id}:{account_id}"
    )
    source = make_restart_source(chat_id=chat_id, thread_id=thread_id)
    source.user_id = user_id
    generate_key = MagicMock(return_value=raw_key)
    peek_session_id = MagicMock(return_value=existing_session_id)
    monkeypatch.setattr(runner.session_store, "_generate_session_key", generate_key)
    monkeypatch.setattr(runner.session_store, "peek_session_id", peek_session_id)
    event = MessageEvent(
        text=f"prompt-{SENSITIVE_VALUE}",
        message_type=MessageType.TEXT,
        source=source,
        message_id=f"message-{SENSITIVE_VALUE}",
    )

    result = await runner._handle_message(event)

    assert result
    agent_start.assert_not_awaited()
    peek_session_id.assert_called_once_with(raw_key)
    assert len(db.rows) == 1
    row = db.rows[0]
    serialized = json.dumps(row, sort_keys=True)
    assert row["event_type"] == "gateway_terminal"
    assert re.fullmatch(r"gateway-[0-9a-f]{32}", row["turn_id"])
    assert row["correlation_id"] == row["turn_id"]
    if existing_session_id:
        assert row["session_id"] == existing_session_id
    else:
        assert row["session_id"] == row["turn_id"]
    assert row["attempt_count"] == 0
    assert row["auxiliary_attempt_count"] == 0
    assert row["outcome"] == "refused"
    assert row["failure_class"] == "gateway_refused"
    for prohibited in (
        raw_key,
        chat_id,
        user_id,
        thread_id,
        account_id,
        str(event.text or ""),
        str(event.message_id or ""),
        SENSITIVE_VALUE,
        "/Users/private/sensitive-path",
        "Authorization: Bearer secret-token",
    ):
        assert prohibited not in serialized


@pytest.mark.parametrize(
    "existing_session_id",
    ["20260806_191345_e5f6a7b8", None],
    ids=["existing-opaque-session", "no-existing-session"],
)
def test_gateway_provider_preflight_uses_only_opaque_identity(
    monkeypatch, existing_session_id
):
    from gateway.run import TurnRunner
    from gateway.turn_context import TurnContext
    from tests.gateway.restart_test_helpers import make_restart_source

    db = CapturingDB()
    chat_id = f"chat-{SENSITIVE_VALUE}"
    user_id = f"user-{SENSITIVE_VALUE}"
    thread_id = f"thread-{SENSITIVE_VALUE}"
    account_id = f"account-{SENSITIVE_VALUE}@s.whatsapp.net"
    raw_key = (
        f"agent:main:telegram:{chat_id}:{user_id}:{thread_id}:{account_id}"
    )
    source = make_restart_source(chat_id=chat_id, thread_id=thread_id)
    source.user_id = user_id
    failure_text = f"provider-error-{SENSITIVE_VALUE}"
    preflight_runner = MagicMock()
    preflight_runner._session_db = db
    preflight_runner._get_system_prompt_for_channel.return_value = ""
    preflight_runner._resolve_session_agent_runtime.side_effect = RuntimeError(
        failure_text
    )

    ctx = TurnContext(
        source=source,
        session_id=existing_session_id,
        session_key=raw_key,
        context_prompt=f"prompt-{SENSITIVE_VALUE}",
        channel_prompt=f"channel-content-{SENSITIVE_VALUE}",
    )

    result = TurnRunner(preflight_runner, ctx).run_sync()

    assert result["api_calls"] == 0
    assert len(db.rows) == 1
    row = db.rows[0]
    serialized = json.dumps(row, sort_keys=True)
    assert row["event_type"] == "gateway_terminal"
    assert re.fullmatch(r"gateway-[0-9a-f]{32}", row["turn_id"])
    assert row["correlation_id"] == row["turn_id"]
    if existing_session_id:
        assert row["session_id"] == existing_session_id
    else:
        assert row["session_id"] == row["turn_id"]
    assert row["attempt_count"] == 0
    assert row["outcome"] == "refused"
    assert row["failure_class"] == "gateway_preflight"
    for prohibited in (
        raw_key,
        chat_id,
        user_id,
        thread_id,
        account_id,
        failure_text,
        str(ctx.context_prompt or ""),
        str(ctx.channel_prompt or ""),
        SENSITIVE_VALUE,
        "/Users/private/sensitive-path",
        "Authorization: Bearer secret-token",
    ):
        assert prohibited not in serialized
