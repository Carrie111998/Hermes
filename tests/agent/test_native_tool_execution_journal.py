from __future__ import annotations

import types

import pytest

from agent.session_contracts import SessionAuthorization, TurnCommand
from agent.tool_executor import _execute_tool_with_canonical_journal
from hermes_state import SessionDB


def _native_agent(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("session-1", "desktop")
    authorization = SessionAuthorization(
        principal="test",
        allowed_session_ids=frozenset({"session-1"}),
    )
    command = TurnCommand(
        session_id="session-1",
        turn_id="turn-1",
        idempotency_key="delivery-1",
        expected_revision=0,
        user_event={"role": "user", "content": "do it"},
    )
    db.append_turn(command, authorization=authorization)
    db.claim_turn_execution(
        "session-1",
        "turn-1",
        owner_id="gateway",
        lease_seconds=60,
        authorization=authorization,
    )
    return db, types.SimpleNamespace(
        _session_db=db,
        session_id="session-1",
        _current_turn_id="turn-1",
    )


def test_completed_native_tool_result_is_replayed_without_dispatch(tmp_path) -> None:
    db, agent = _native_agent(tmp_path)
    calls = []

    def execute(args):
        calls.append(dict(args))
        return "one result"

    first = _execute_tool_with_canonical_journal(
        agent,
        function_name="terminal",
        function_args={"command": "touch marker"},
        tool_call_id="call-1",
        execute=execute,
    )
    replay = _execute_tool_with_canonical_journal(
        agent,
        function_name="terminal",
        function_args={"command": "touch marker"},
        tool_call_id="call-1",
        execute=execute,
    )

    assert first == replay == "one result"
    assert calls == [{"command": "touch marker"}]
    db.close()


def test_effectful_exception_is_fenced_as_uncertain(tmp_path) -> None:
    db, agent = _native_agent(tmp_path)
    calls = 0

    def execute(_args):
        nonlocal calls
        calls += 1
        raise RuntimeError("connection disappeared after send")

    with pytest.raises(RuntimeError, match="connection disappeared"):
        _execute_tool_with_canonical_journal(
            agent,
            function_name="send_email",
            function_args={"to": "operator@example.invalid"},
            tool_call_id="call-send",
            execute=execute,
        )
    recovered = _execute_tool_with_canonical_journal(
        agent,
        function_name="send_email",
        function_args={"to": "operator@example.invalid"},
        tool_call_id="call-send",
        execute=execute,
    )

    assert calls == 1
    assert '"status":"uncertain"' in recovered
    db.close()
