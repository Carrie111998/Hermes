"""Durable delivery proof for finals sent by GatewayStreamConsumer."""

import asyncio

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import (
    _apply_stream_final_delivery_status,
    _delivery_message_ref,
    _send_final_with_delivery_proof,
)
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


@pytest.fixture(autouse=True)
def _isolated_delivery_ledger(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(dl, "_db_path", lambda: home / "state.db")
    monkeypatch.setattr(dl, "ledger_enabled", lambda *_args, **_kwargs: True)


def _row():
    with dl._connect() as conn:
        row = conn.execute(
            """
            SELECT obligation_id, session_key, platform, chat_id, thread_id,
                   content, state, last_error
            FROM delivery_obligations
            """
        ).fetchone()
    if row is None:
        return None
    return dict(
        zip(
            (
                "obligation_id",
                "session_key",
                "platform",
                "chat_id",
                "thread_id",
                "content",
                "state",
                "last_error",
            ),
            row,
        )
    )


def _row_for_content(content):
    with dl._connect() as conn:
        row = conn.execute(
            """
            SELECT obligation_id, session_key, platform, chat_id, thread_id,
                   content, state, last_error
            FROM delivery_obligations
            WHERE content = ?
            """,
            (content,),
        ).fetchone()
    if row is None:
        return None
    return dict(
        zip(
            (
                "obligation_id",
                "session_key",
                "platform",
                "chat_id",
                "thread_id",
                "content",
                "state",
                "last_error",
            ),
            row,
        )
    )


class _Adapter:
    platform = Platform.TELEGRAM
    REQUIRES_EDIT_FINALIZE = False

    def __init__(self, outcome):
        self.outcome = outcome
        self.states_during_await = []
        self.send_calls = []
        self.retry_calls = []
        self.edit_calls = []

    async def _send_with_retry(self, **kwargs):
        self.retry_calls.append(kwargs)
        raise AssertionError("turn-final proof must use one platform attempt")

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        self.states_during_await.append(_row()["state"])
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def edit_message(self, **kwargs):
        self.edit_calls.append(kwargs)
        self.states_during_await.append(_row()["state"])
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class _HangingAdapter(_Adapter):
    def __init__(self):
        super().__init__(SendResult(success=True, message_id="out-1"))
        self.started = asyncio.Event()

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        self.states_during_await.append(_row()["state"])
        self.started.set()
        await asyncio.Event().wait()


def _consumer(adapter):
    return GatewayStreamConsumer(
        adapter,
        "owner-chat",
        StreamConsumerConfig(buffer_only=True),
        metadata={"thread_id": "office-topic"},
        initial_reply_to_id="42197",
        delivery_session_key="agent:main:telegram:office-topic",
        delivery_message_ref="42197",
        delivery_platform="telegram",
        delivery_thread_id="office-topic",
    )


def test_internal_run_gets_stable_unique_delivery_reference():
    assert _delivery_message_ref(
        None,
        None,
        session_id="goal-session",
        run_generation=7,
    ) == "internal:goal-session:7"
    assert _delivery_message_ref(
        "42197",
        "reply-anchor",
        session_id="goal-session",
        run_generation=7,
    ) == "42197"


@pytest.mark.asyncio
async def test_streamed_final_is_pending_before_send_and_delivered_after_ack():
    adapter = _Adapter(SendResult(success=True, message_id="out-1"))
    consumer = _consumer(adapter)
    consumer.on_delta("Done with exact proof.")
    consumer.finish()

    await consumer.run()

    row = _row()
    assert adapter.states_during_await == ["attempting"]
    assert row["session_key"] == "agent:main:telegram:office-topic"
    assert row["platform"] == "telegram"
    assert row["chat_id"] == "owner-chat"
    assert row["thread_id"] == "office-topic"
    assert row["content"] == "Done with exact proof."
    assert row["state"] == "delivered"
    assert consumer.final_response_sent is True
    assert consumer.final_content_delivered is True


@pytest.mark.asyncio
async def test_turn_final_edit_uses_the_same_delivery_contract():
    adapter = _Adapter(SendResult(success=True, message_id="preview-1"))
    consumer = _consumer(adapter)
    consumer._message_id = "preview-1"
    consumer._last_sent_text = "Draft"

    delivered = await consumer._send_or_edit(
        "Final answer",
        finalize=True,
        is_turn_final=True,
    )

    assert delivered is True
    assert adapter.states_during_await == ["attempting"]
    assert len(adapter.edit_calls) == 1
    assert not adapter.send_calls
    assert _row()["state"] == "delivered"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint", "expected_state"),
    [
        ("record_obligation", None),
        ("mark_attempting", "pending"),
    ],
)
async def test_transformed_final_checkpoint_failure_blocks_edit(
    monkeypatch,
    checkpoint,
    expected_state,
):
    adapter = _Adapter(SendResult(success=True, message_id="preview-1"))
    consumer = _consumer(adapter)
    consumer._message_id = "preview-1"
    consumer._delivery_obligation_content = "Original answer"
    consumer._delivery_obligation_id = "original-obligation"
    consumer._delivery_ledger_state = "delivered"

    def fail_checkpoint(*_args, **_kwargs):
        raise OSError("state.db unavailable")

    monkeypatch.setattr(dl, checkpoint, fail_checkpoint)

    with pytest.raises(RuntimeError, match="blocked before send"):
        await consumer.replace_final_with_delivery_proof(
            message_id="preview-1",
            content="Transformed answer",
        )

    assert adapter.edit_calls == []
    row = _row_for_content("Transformed answer")
    assert (row["state"] if row else None) == expected_state
    assert consumer.final_response_sent is False
    assert consumer.final_content_delivered is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected_state", "expected_unknown"),
    [
        (
            SendResult(
                success=False,
                error="bot lacks permission",
                error_kind="forbidden",
            ),
            "failed",
            False,
        ),
        (
            SendResult(
                success=False,
                error="platform request timed out",
                retryable=True,
            ),
            "attempting",
            True,
        ),
    ],
)
async def test_transformed_final_rejection_preserves_exact_delivery_truth(
    outcome,
    expected_state,
    expected_unknown,
):
    adapter = _Adapter(outcome)
    consumer = _consumer(adapter)
    consumer._message_id = "preview-1"
    consumer._delivery_obligation_content = "Original answer"
    consumer._delivery_obligation_id = "original-obligation"
    consumer._delivery_ledger_state = "delivered"

    result = await consumer.replace_final_with_delivery_proof(
        message_id="preview-1",
        content="Transformed answer",
    )

    assert result is outcome
    assert len(adapter.edit_calls) == 1
    assert adapter.states_during_await == ["attempting"]
    assert _row_for_content("Transformed answer")["state"] == expected_state
    assert consumer.final_delivery_outcome_unknown is expected_unknown
    assert consumer.final_response_sent is False
    assert consumer.final_content_delivered is False


@pytest.mark.asyncio
async def test_transformed_final_success_receipts_the_exact_new_content():
    adapter = _Adapter(SendResult(success=True, message_id="preview-1"))
    consumer = _consumer(adapter)
    consumer._message_id = "preview-1"
    consumer._delivery_obligation_content = "Original answer"
    consumer._delivery_obligation_id = "original-obligation"
    consumer._delivery_ledger_state = "delivered"

    result = await consumer.replace_final_with_delivery_proof(
        message_id="preview-1",
        content="Transformed answer",
    )

    assert result.success is True
    assert adapter.states_during_await == ["attempting"]
    assert _row_for_content("Transformed answer")["state"] == "delivered"
    assert consumer.final_delivery_outcome_unknown is False
    assert consumer.final_response_sent is True
    assert consumer.final_content_delivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint", "expected_state"),
    [
        ("record_obligation", None),
        ("mark_attempting", "pending"),
    ],
)
async def test_stream_final_fails_closed_before_send_when_ledger_checkpoint_fails(
    monkeypatch,
    checkpoint,
    expected_state,
):
    adapter = _Adapter(SendResult(success=True, message_id="out-1"))
    consumer = _consumer(adapter)

    def fail_checkpoint(*_args, **_kwargs):
        raise OSError("state.db unavailable")

    monkeypatch.setattr(dl, checkpoint, fail_checkpoint)

    delivered = await consumer._send_or_edit(
        "Final answer",
        finalize=True,
        is_turn_final=True,
    )

    assert delivered is False
    assert adapter.send_calls == []
    assert adapter.edit_calls == []
    row = _row()
    assert (row["state"] if row else None) == expected_state
    assert "blocked before send" in consumer.final_delivery_ledger_error


@pytest.mark.asyncio
async def test_definitive_stream_rejection_is_recorded_failed():
    adapter = _Adapter(
        SendResult(
            success=False,
            error="bot lacks permission",
            error_kind="forbidden",
        )
    )
    consumer = _consumer(adapter)

    delivered = await consumer._send_or_edit(
        "Final answer",
        finalize=True,
        is_turn_final=True,
    )

    row = _row()
    assert delivered is False
    assert adapter.states_during_await == ["attempting"]
    assert row["state"] == "failed"
    assert row["last_error"] == "bot lacks permission"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        SendResult(
            success=False,
            error="platform request timed out",
            retryable=True,
        ),
        SendResult(
            success=False,
            error="provider outcome unavailable",
            error_kind="unknown",
            retryable=True,
        ),
        asyncio.TimeoutError("platform request timed out"),
    ],
)
async def test_timeout_outcome_remains_attempting_for_honest_recovery(outcome):
    adapter = _Adapter(outcome)
    consumer = _consumer(adapter)

    delivered = await consumer._send_or_edit(
        "Final answer",
        finalize=True,
        is_turn_final=True,
    )

    row = _row()
    assert delivered is False
    assert adapter.states_during_await == ["attempting"]
    assert row["state"] == "attempting"
    assert row["last_error"] is None
    assert consumer.final_content_delivered is False
    assert consumer.final_response_sent is False
    assert consumer.final_delivery_outcome_unknown is True


@pytest.mark.asyncio
async def test_unknown_stream_outcome_is_not_silently_retried():
    adapter = _Adapter(
        SendResult(
            success=False,
            error="platform request timed out",
            retryable=True,
        )
    )
    consumer = _consumer(adapter)
    consumer.on_delta("Final answer")
    consumer.finish()

    await consumer.run()

    assert len(adapter.send_calls) == 1
    assert _row()["state"] == "attempting"
    assert consumer.final_content_delivered is False
    assert consumer.final_response_sent is False
    assert consumer.final_delivery_outcome_unknown is True


@pytest.mark.asyncio
async def test_cancelled_inflight_final_is_not_retried_outside_the_ledger():
    adapter = _HangingAdapter()
    consumer = _consumer(adapter)
    consumer.on_delta("Final answer")
    consumer.finish()

    task = asyncio.create_task(consumer.run())
    await asyncio.wait_for(adapter.started.wait(), timeout=1)
    task.cancel()
    await asyncio.wait_for(task, timeout=1)

    assert len(adapter.send_calls) == 1
    assert adapter.states_during_await == ["attempting"]
    assert _row()["state"] == "attempting"
    assert consumer.final_delivery_outcome_unknown is True
    assert consumer.final_response_sent is False
    assert consumer.final_content_delivered is False


@pytest.mark.parametrize("transformed", [False, True])
def test_unknown_stream_outcome_suppresses_resend_without_claiming_delivery(
    transformed,
):
    response = {
        "final_response": "Final answer",
        "response_transformed": transformed,
    }

    status = _apply_stream_final_delivery_status(
        response,
        has_final=True,
        transformed=transformed,
        outcome_unknown=True,
        delivery_confirmed=False,
    )

    assert status == "unknown"
    assert response["already_sent"] is True
    assert response["final_delivery_status"] == "unknown"


def test_confirmed_stream_outcome_has_distinct_delivery_status():
    response = {"final_response": "Final answer"}

    status = _apply_stream_final_delivery_status(
        response,
        has_final=True,
        transformed=False,
        outcome_unknown=False,
        delivery_confirmed=True,
    )

    assert status == "delivered"
    assert response["already_sent"] is True
    assert response["final_delivery_status"] == "delivered"


def test_stream_ledger_failure_has_explicit_terminal_delivery_state():
    response = {"final_response": "Final answer"}

    status = _apply_stream_final_delivery_status(
        response,
        has_final=True,
        transformed=False,
        outcome_unknown=False,
        delivery_confirmed=False,
        ledger_error="Final delivery was blocked before send.",
    )

    assert status == "failed"
    assert response["final_delivery_status"] == "failed"
    assert response["final_delivery_error"] == (
        "Final delivery was blocked before send."
    )
    assert response.get("already_sent") is not True


async def _queued_final(outcome):
    adapter = _Adapter(outcome)
    result = await _send_final_with_delivery_proof(
        adapter=adapter,
        chat_id="owner-chat",
        content="First final before follow-up",
        metadata={"thread_id": "office-topic"},
        session_key="agent:main:telegram:office-topic",
        message_ref="42197",
        platform=Platform.TELEGRAM,
        thread_id="office-topic",
    )
    return adapter, result


@pytest.mark.asyncio
async def test_queued_first_final_rejection_is_durable_before_followup():
    adapter, result = await _queued_final(
        SendResult(
            success=False,
            error="bot lacks permission",
            error_kind="forbidden",
        )
    )

    assert result.success is False
    assert adapter.states_during_await == ["attempting"]
    assert adapter.retry_calls == []
    assert _row()["state"] == "failed"
    assert _row()["last_error"] == "bot lacks permission"


@pytest.mark.asyncio
async def test_queued_first_final_timeout_stays_attempting_before_followup():
    adapter, result = await _queued_final(
        SendResult(
            success=False,
            error="platform request timed out",
            retryable=True,
        )
    )

    assert result.success is False
    assert adapter.states_during_await == ["attempting"]
    assert adapter.retry_calls == []
    assert _row()["state"] == "attempting"
    assert _row()["last_error"] is None


@pytest.mark.asyncio
async def test_queued_first_final_exception_stays_attempting_before_followup():
    with pytest.raises(asyncio.TimeoutError):
        await _queued_final(asyncio.TimeoutError("platform request timed out"))

    assert _row()["state"] == "attempting"
    assert _row()["last_error"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("checkpoint", "expected_state"),
    [
        ("record_obligation", None),
        ("mark_attempting", "pending"),
    ],
)
async def test_queued_final_fails_closed_before_send_when_ledger_checkpoint_fails(
    monkeypatch,
    checkpoint,
    expected_state,
):
    def fail_checkpoint(*_args, **_kwargs):
        raise OSError("state.db unavailable")

    monkeypatch.setattr(dl, checkpoint, fail_checkpoint)

    adapter, result = await _queued_final(
        SendResult(success=True, message_id="out-1")
    )

    assert result.success is False
    assert result.error_kind == "transient"
    assert "blocked before send" in result.error
    assert adapter.send_calls == []
    row = _row()
    assert (row["state"] if row else None) == expected_state
