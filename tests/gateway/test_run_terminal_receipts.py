"""Durable gateway run receipts independent of observability telemetry."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _isolated_receipt_db(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.SLACK,
        chat_id="C1",
        chat_type="channel",
        user_id="U1",
    )


def _runner_with_inner(inner) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._run_agent_inner = inner
    return runner


async def _run_gateway_wrapper(runner: GatewayRunner):
    return await runner._run_agent(
        message="do the thing",
        context_prompt="",
        history=[],
        source=_source(),
        session_id="session-1",
        session_key="agent:main:slack:channel:C1",
        run_generation=7,
        event_message_id="message-1",
    )


@pytest.mark.asyncio
async def test_gateway_normal_result_preserves_exact_local_terminal_receipt():
    ended_at = 1234.5
    inner = AsyncMock(
        return_value={
            "final_response": "Done with proof.",
            "completed": True,
            "run_terminal_state": "done",
            "run_end_reason": "text_response(finish_reason=stop)",
            "run_ended_at": ended_at,
            "final_generated": True,
        }
    )
    result = await _run_gateway_wrapper(_runner_with_inner(inner))

    durable = dl.get_run_terminal_receipt(result["run_receipt_id"])
    assert durable is not None
    assert durable["run_terminal_state"] == "done"
    assert durable["run_end_reason"] == "text_response(finish_reason=stop)"
    assert durable["run_ended_at"] == ended_at
    assert durable["final_generated"] is True


@pytest.mark.asyncio
async def test_gateway_exception_closes_local_receipt_without_telemetry():
    runner = _runner_with_inner(
        AsyncMock(side_effect=RuntimeError("provider exploded"))
    )

    with pytest.raises(RuntimeError, match="provider exploded"):
        await _run_gateway_wrapper(runner)

    with dl._connect() as conn:
        row = conn.execute(
            """SELECT run_terminal_state, run_end_reason, final_generated
               FROM run_terminal_receipts"""
        ).fetchone()
    assert row == ("failed", "gateway_exception:RuntimeError", 0)


@pytest.mark.asyncio
async def test_gateway_cancellation_closes_local_receipt_without_telemetry():
    runner = _runner_with_inner(AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await _run_gateway_wrapper(runner)

    with dl._connect() as conn:
        row = conn.execute(
            """SELECT run_terminal_state, run_end_reason, final_generated
               FROM run_terminal_receipts"""
        ).fetchone()
    assert row == ("cancelled", "gateway_cancelled", 0)


def test_startup_sweep_closes_only_dead_owner_running_receipts(monkeypatch):
    dead_id = "dead-running"
    live_id = "live-running"
    for receipt_id in (dead_id, live_id):
        dl.begin_run_receipt(
            run_receipt_id=receipt_id,
            session_id=f"session-{receipt_id}",
            task_id=f"task-{receipt_id}",
        )
    with dl._transaction() as conn:
        conn.execute(
            """UPDATE run_terminal_receipts
               SET owner_pid=111, owner_started_at=10
               WHERE run_receipt_id=?""",
            (dead_id,),
        )
        conn.execute(
            """UPDATE run_terminal_receipts
               SET owner_pid=222, owner_started_at=20
               WHERE run_receipt_id=?""",
            (live_id,),
        )
    monkeypatch.setattr(
        dl,
        "_owner_alive",
        lambda pid, started_at: (pid, started_at) == (222, 20),
    )

    closed = dl.sweep_dead_run_receipts(now=9000.0)

    dead = dl.get_run_terminal_receipt(dead_id)
    live = dl.get_run_terminal_receipt(live_id)
    assert closed == [dead_id]
    assert dead is not None
    assert dead["run_terminal_state"] == "failed"
    assert dead["run_end_reason"] == "process_terminated_unknown_outcome"
    assert dead["run_ended_at"] == 9000.0
    assert live is not None
    assert live["run_terminal_state"] == "running"
    assert live["run_end_reason"] is None
    assert live["run_ended_at"] is None


@pytest.mark.asyncio
async def test_gateway_startup_invokes_dead_run_sweep_when_delivery_is_disabled(
    monkeypatch,
):
    receipt_id = "startup-dead-running"
    dl.begin_run_receipt(
        run_receipt_id=receipt_id,
        session_id="session-startup",
        task_id="task-startup",
    )
    monkeypatch.setattr(dl, "_owner_alive", lambda *_args: False)
    monkeypatch.setattr(dl, "ledger_enabled", lambda *_args, **_kwargs: False)
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}

    attempted = await runner._redeliver_pending_obligations()

    durable = dl.get_run_terminal_receipt(receipt_id)
    assert attempted == 0
    assert durable is not None
    assert durable["run_terminal_state"] == "failed"
    assert durable["run_end_reason"] == "process_terminated_unknown_outcome"
    assert isinstance(durable["run_ended_at"], float)


class _Adapter(BasePlatformAdapter):  # type: ignore[misc]
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True), Platform.SLACK)
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append(content)
        return SendResult(success=True, message_id="out-1")


def _event(run_receipt_id: str) -> MessageEvent:
    event = MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=_source(),
        message_id="message-1",
    )
    event._hermes_run_receipt_id = run_receipt_id
    return event


def _begin_adapter_receipt() -> str:
    receipt_id = dl.compute_run_receipt_id(
        "agent:main:slack:channel:C1",
        "message-1",
        7,
    )
    dl.begin_run_receipt(
        run_receipt_id=receipt_id,
        session_id="session-1",
        task_id="task-1",
        session_key="agent:main:slack:channel:C1",
        platform="slack",
        run_generation=7,
        message_ref="message-1",
    )
    dl.record_run_terminal_receipt(
        run_receipt_id=receipt_id,
        session_id="session-1",
        task_id="task-1",
        session_key="agent:main:slack:channel:C1",
        platform="slack",
        run_generation=7,
        message_ref="message-1",
        run_terminal_state="done",
        run_end_reason="completed",
        run_ended_at=100.0,
        final_generated=True,
    )
    return receipt_id


@pytest.mark.asyncio
async def test_real_adapter_delivery_links_obligation_to_generated_final():
    receipt_id = _begin_adapter_receipt()
    adapter = _Adapter()
    adapter._message_handler = AsyncMock(return_value="Done with proof.")
    session_key = "agent:main:slack:channel:C1"
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter._process_message_background(_event(receipt_id), session_key)

    durable = dl.get_run_terminal_receipt(receipt_id)
    assert adapter.sent == ["Done with proof."]
    assert durable is not None
    assert durable["run_terminal_state"] == "done"
    assert durable["final_generated"] is True
    assert durable["delivery_obligation_id"]
    assert durable["final_delivery_status"] == "delivered"


@pytest.mark.asyncio
async def test_failed_delivery_keeps_generated_truth_separate():
    receipt_id = _begin_adapter_receipt()
    adapter = _Adapter()
    adapter.send = AsyncMock(
        return_value=SendResult(
            success=False,
            error="channel rejected final",
        )
    )
    adapter._message_handler = AsyncMock(return_value="Done with proof.")
    session_key = "agent:main:slack:channel:C1"
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter._process_message_background(_event(receipt_id), session_key)

    durable = dl.get_run_terminal_receipt(receipt_id)
    assert durable is not None
    assert durable["run_terminal_state"] == "done"
    assert durable["final_generated"] is True
    assert durable["delivery_obligation_id"]
    assert durable["final_delivery_status"] == "failed"


@pytest.mark.asyncio
async def test_real_adapter_exception_amends_terminal_receipt():
    receipt_id = _begin_adapter_receipt()
    adapter = _Adapter()
    adapter._message_handler = AsyncMock(
        side_effect=RuntimeError("post-agent gateway failure")
    )
    session_key = "agent:main:slack:channel:C1"
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter._process_message_background(_event(receipt_id), session_key)

    durable = dl.get_run_terminal_receipt(receipt_id)
    assert durable is not None
    assert durable["run_terminal_state"] == "failed"
    assert durable["run_end_reason"] == "gateway_delivery_exception:RuntimeError"
    assert durable["final_generated"] is True


@pytest.mark.asyncio
async def test_real_adapter_cancellation_amends_terminal_receipt():
    receipt_id = _begin_adapter_receipt()
    adapter = _Adapter()
    adapter._message_handler = AsyncMock(side_effect=asyncio.CancelledError())
    session_key = "agent:main:slack:channel:C1"
    adapter._active_sessions[session_key] = asyncio.Event()

    with pytest.raises(asyncio.CancelledError):
        await adapter._process_message_background(
            _event(receipt_id),
            session_key,
        )

    durable = dl.get_run_terminal_receipt(receipt_id)
    assert durable is not None
    assert durable["run_terminal_state"] == "cancelled"
    assert durable["run_end_reason"] == "gateway_processing_cancelled"
    assert durable["final_generated"] is True
