from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway import delivery_ledger as dl
from gateway.delivery_metadata import (
    TERMINAL_DELIVERY_METADATA_KEY,
    mark_terminal_delivery,
    project_terminal_delivery,
)
from gateway.platform_registry import PlatformEntry, platform_registry
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.platforms.webhook import WebhookAdapter
from gateway.run import GatewayRunner
from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig


def _webhook_adapter(
    target_platform: Platform = Platform.TELEGRAM,
) -> tuple[WebhookAdapter, AsyncMock]:
    adapter = WebhookAdapter(
        PlatformConfig(
            enabled=True,
            typing_indicator=False,
            extra={"routes": {}, "host": "127.0.0.1", "port": 0},
        )
    )
    target = AsyncMock()
    target.send = AsyncMock(return_value=SendResult(success=True))
    runner = MagicMock()
    runner.adapters = {target_platform: target}
    runner.config = GatewayConfig(
        platforms={
            target_platform: PlatformConfig(enabled=True, token="test"),
        }
    )
    adapter.gateway_runner = runner
    return adapter, target


def _seed_delivery(
    adapter: WebhookAdapter,
    *,
    chat_id: str = "webhook:synthetic:external-1",
    internal_id: str = "server-turn-1",
    correlation_id: object = "correlation-1",
    deliver: str = "telegram",
) -> str:
    adapter._delivery_info[chat_id] = {
        "deliver": deliver,
        "deliver_extra": {
            "chat_id": correlation_id,
        },
        "_hermes_delivery_id": internal_id,
    }
    return chat_id


def _terminal_metadata(
    *,
    outcome: str = "success",
    correlation_id: str = "external-1",
    delivery_id: str = "external-1",
) -> dict:
    return mark_terminal_delivery(
        None,
        outcome=outcome,
        correlation_id=correlation_id,
        delivery_id=delivery_id,
    )


@pytest.fixture
def ella_callback_platform():
    name = "ella_callback"
    previous = platform_registry._entries.get(name)
    platform_registry.register(
        PlatformEntry(
            name=name,
            label="Synthetic Ella callback",
            adapter_factory=lambda config: None,
            check_fn=lambda: True,
        )
    )
    platform = Platform(name)
    try:
        yield platform
    finally:
        platform_registry.unregister(name)
        if previous is not None:
            platform_registry.register(previous)


def _request(
    *,
    route_name: str,
    delivery_id: str,
    payload: dict,
    secret: str | None = None,
):
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"X-Request-ID": delivery_id}
    if secret is not None:
        headers["X-Webhook-Signature"] = hmac.new(
            secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    request = MagicMock()
    request.headers = headers
    request.content_length = len(body)
    request.match_info = {"route_name": route_name}
    request.method = "POST"
    request.read = AsyncMock(return_value=body)
    return request


def _fake_web(monkeypatch) -> None:
    monkeypatch.setattr(
        "gateway.platforms.webhook.web",
        SimpleNamespace(
            json_response=lambda payload, status=200: SimpleNamespace(
                status=status,
                payload=payload,
            )
        ),
    )


def test_terminal_metadata_projection_is_exact_and_bounded() -> None:
    metadata = mark_terminal_delivery(
        {"notify": True},
        outcome="success",
        correlation_id="c" * 300,
        delivery_id="d" * 300,
    )

    marker = project_terminal_delivery(metadata)

    assert marker is not None
    assert marker["outcome"] == "success"
    assert marker["correlation_id"].startswith("sha256:")
    assert marker["delivery_id"].startswith("sha256:")
    assert "notify" not in marker


def test_delivery_ledger_additive_schema_migration_preserves_existing_row() -> None:
    path = dl._db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """CREATE TABLE delivery_obligations (
                obligation_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT,
                content TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                owner_pid INTEGER,
                owner_started_at INTEGER,
                last_error TEXT
            )"""
        )
        conn.execute(
            """INSERT INTO delivery_obligations
               (obligation_id, session_key, platform, chat_id, content, state,
                created_at, updated_at)
               VALUES ('legacy-1', 'session-1', 'webhook', 'chat-1',
                       'existing final', 'pending', 1, 1)"""
        )

    with dl._connect() as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(delivery_obligations)")
        }
        preserved = conn.execute(
            """SELECT obligation_id, content, state
               FROM delivery_obligations WHERE obligation_id='legacy-1'"""
        ).fetchone()

    assert {"delivery_context_json", "delivery_metadata_json"} <= columns
    assert preserved == ("legacy-1", "existing final", "pending")


@pytest.mark.parametrize(
    "marker",
    [
        None,
        "final",
        {},
        {
            "version": 1,
            "outcome": "success",
            "correlation_id": "c",
            "delivery_id": "d",
            "extra": "not allowed",
        },
        {
            "version": 1,
            "outcome": "interim",
            "correlation_id": "c",
            "delivery_id": "d",
        },
    ],
)
def test_malformed_terminal_metadata_fails_closed(marker) -> None:
    assert project_terminal_delivery({TERMINAL_DELIVERY_METADATA_KEY: marker}) is None


def test_stream_metadata_marks_only_final_webhook_text() -> None:
    adapter = SimpleNamespace(platform=Platform.WEBHOOK)
    consumer = GatewayStreamConsumer(
        adapter,
        "webhook:synthetic:external-1",
        metadata={"thread_id": "thread-1"},
        initial_reply_to_id="reply-1",
    )

    preview = consumer._metadata_for_send(expect_edits=True)
    progress = consumer._metadata_for_send()
    segment_final = consumer._metadata_for_send(
        final=True,
        is_turn_final=False,
    )
    final = consumer._metadata_for_send(final=True)

    assert preview == {
        "thread_id": "thread-1",
        "reply_to_message_id": "reply-1",
        "expect_edits": True,
    }
    assert progress == {
        "thread_id": "thread-1",
        "reply_to_message_id": "reply-1",
    }
    assert TERMINAL_DELIVERY_METADATA_KEY not in preview
    assert TERMINAL_DELIVERY_METADATA_KEY not in progress
    assert segment_final is not None
    assert segment_final["notify"] is True
    assert TERMINAL_DELIVERY_METADATA_KEY not in segment_final
    assert final is not None
    marker = final[TERMINAL_DELIVERY_METADATA_KEY]
    assert marker == {
        "version": 1,
        "outcome": "success",
        "correlation_id": "reply-1",
        "delivery_id": "reply-1",
    }
    assert final["notify"] is True


def test_non_webhook_final_metadata_remains_backward_compatible() -> None:
    adapter = SimpleNamespace(platform=Platform.TELEGRAM)
    consumer = GatewayStreamConsumer(
        adapter,
        "chat-1",
        metadata={"thread_id": "thread-1"},
        initial_reply_to_id="reply-1",
    )

    final = consumer._metadata_for_send(final=True)

    assert final == {
        "thread_id": "thread-1",
        "reply_to_message_id": "reply-1",
        "notify": True,
    }
    assert TERMINAL_DELIVERY_METADATA_KEY not in final


@pytest.mark.asyncio
async def test_multiple_interim_sends_then_final_project_one_terminal_marker() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)

    await adapter.send(chat_id, "interim one")
    await adapter.send(chat_id, "interim two", metadata={"notify": True})
    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(),
    )

    assert target.send.await_count == 3
    first, second, final = target.send.await_args_list
    assert first.kwargs["metadata"] is None
    assert second.kwargs["metadata"] is None
    marker = final.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker == {
        "version": 1,
        "outcome": "success",
        "correlation_id": "correlation-1",
        "delivery_id": "server-turn-1",
    }


@pytest.mark.asyncio
async def test_segment_break_interim_then_final_marks_only_true_turn_final(
    ella_callback_platform,
) -> None:
    adapter, target = _webhook_adapter(ella_callback_platform)
    target.send.side_effect = [
        SendResult(success=True, message_id="interim-callback"),
        SendResult(success=True, message_id="final-callback"),
    ]
    chat_id = _seed_delivery(
        adapter,
        correlation_id="opaque-correlation-42",
        deliver="ella_callback",
    )
    consumer = GatewayStreamConsumer(
        adapter,
        chat_id,
        StreamConsumerConfig(
            edit_interval=0.01,
            buffer_threshold=1,
            cursor="",
        ),
        initial_reply_to_id="provider-delivery-1",
    )
    consumer.on_delta("interim pre-tool segment")
    consumer.on_segment_break()
    consumer.on_delta("completed final answer")
    consumer.finish()

    await consumer.run()

    assert target.send.await_count == 2
    interim, final = target.send.await_args_list
    assert interim.args[:2] == (
        "opaque-correlation-42",
        "interim pre-tool segment",
    )
    assert interim.kwargs["metadata"] is None
    assert final.args[:2] == (
        "opaque-correlation-42",
        "completed final answer",
    )
    assert final.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY] == {
        "version": 1,
        "outcome": "success",
        "correlation_id": "opaque-correlation-42",
        "delivery_id": "server-turn-1",
    }


@pytest.mark.asyncio
async def test_retry_reprojects_same_accepted_turn_identity() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)

    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(delivery_id="external-first"),
    )
    await adapter.send(
        chat_id,
        "complete response retry",
        metadata=_terminal_metadata(delivery_id="external-retry"),
    )

    first, retry = target.send.await_args_list
    first_marker = first.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    retry_marker = retry.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert first_marker == retry_marker == {
        "version": 1,
        "outcome": "success",
        "correlation_id": "correlation-1",
        "delivery_id": "server-turn-1",
    }


@pytest.mark.asyncio
async def test_missing_server_delivery_identity_drops_terminal_marker() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)
    adapter._delivery_info[chat_id].pop("_hermes_delivery_id")

    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(),
    )

    assert target.send.await_args.kwargs["metadata"] is None


@pytest.mark.asyncio
async def test_blank_external_correlation_falls_back_to_terminal_source() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter, correlation_id="")

    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(correlation_id="source-correlation"),
    )

    marker = target.send.await_args.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker["correlation_id"] == "source-correlation"
    assert marker["delivery_id"] == "server-turn-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rendered_correlation", "expected_correlation"),
    [
        ("  opaque-correlation-42  ", "opaque-correlation-42"),
        (4242, "4242"),
    ],
)
async def test_exact_managed_route_uses_rendered_chat_id_as_correlation(
    monkeypatch,
    ella_callback_platform,
    rendered_correlation,
    expected_correlation,
) -> None:
    _fake_web(monkeypatch)
    adapter, target = _webhook_adapter(ella_callback_platform)
    adapter._routes["managed"] = {
        "secret": "INSECURE_NO_AUTH",
        "prompt": "{text}",
        "deliver": "ella_callback",
        "deliver_extra": {"chat_id": "{correlation_id}"},
    }
    adapter.handle_message = AsyncMock()

    response = await adapter._handle_webhook(
        _request(
            route_name="managed",
            delivery_id="provider-delivery-1",
            payload={
                "text": "synthetic request",
                "correlation_id": rendered_correlation,
            },
        )
    )
    chat_id = "webhook:managed:provider-delivery-1"
    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(correlation_id="provider-delivery-1"),
    )

    assert response.status == 202
    accepted_turn_id = adapter._delivery_info[chat_id]["_hermes_delivery_id"]
    target.send.assert_awaited_once()
    call = target.send.await_args
    assert call.args[:2] == (expected_correlation, "complete response")
    assert call.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY] == {
        "version": 1,
        "outcome": "success",
        "correlation_id": expected_correlation,
        "delivery_id": accepted_turn_id,
    }
    assert accepted_turn_id != "provider-delivery-1"
    assert len(accepted_turn_id) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [None, "", "   ", {}, [], True, 1.5, "{correlation_id}"],
)
async def test_malformed_rendered_chat_id_falls_back_deterministically(
    monkeypatch,
    malformed,
    ella_callback_platform,
) -> None:
    _fake_web(monkeypatch)
    adapter, target = _webhook_adapter(ella_callback_platform)
    adapter._routes["managed"] = {
        "secret": "INSECURE_NO_AUTH",
        "prompt": "{text}",
        "deliver": "ella_callback",
        "deliver_extra": {"chat_id": "{correlation_id}"},
    }
    adapter.handle_message = AsyncMock()

    response = await adapter._handle_webhook(
        _request(
            route_name="managed",
            delivery_id="provider-delivery-1",
            payload={
                "text": "synthetic request",
                "correlation_id": malformed,
            },
        )
    )
    chat_id = "webhook:managed:provider-delivery-1"

    await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(correlation_id="source-correlation"),
    )

    assert response.status == 202
    call = target.send.await_args
    assert call.args[0] == "source-correlation"
    marker = call.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker["correlation_id"] == "source-correlation"
    assert marker["delivery_id"] == adapter._delivery_info[chat_id][
        "_hermes_delivery_id"
    ]


@pytest.mark.asyncio
async def test_terminal_callback_fails_closed_when_ledger_write_fails(
    monkeypatch,
    ella_callback_platform,
) -> None:
    adapter, target = _webhook_adapter(ella_callback_platform)
    chat_id = _seed_delivery(
        adapter,
        correlation_id="opaque-correlation-42",
        deliver="ella_callback",
    )

    def _fail_record(**_kwargs) -> None:
        raise OSError("synthetic ledger failure")

    monkeypatch.setattr(
        "gateway.delivery_ledger.record_obligation",
        _fail_record,
    )

    result = await adapter.send(
        chat_id,
        "complete response",
        metadata=_terminal_metadata(correlation_id="source-correlation"),
    )

    assert result.success is False
    assert result.error == "Terminal webhook delivery could not be journaled"
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_cross_platform_delivery_strips_untrusted_source_metadata() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)

    source_metadata = _terminal_metadata()
    source_metadata["api_key"] = "sk-should-not-cross-platform"
    source_metadata["tool_calls"] = [{"name": "read_secret"}]

    await adapter.send(
        chat_id,
        "complete response",
        metadata=source_metadata,
    )

    delivered_metadata = target.send.await_args.kwargs["metadata"]
    assert set(delivered_metadata) == {TERMINAL_DELIVERY_METADATA_KEY}
    marker = delivered_metadata[TERMINAL_DELIVERY_METADATA_KEY]
    assert marker["correlation_id"] == "correlation-1"
    assert marker["delivery_id"] == "server-turn-1"


@pytest.mark.asyncio
async def test_untrusted_terminal_marker_with_extra_fields_fails_closed() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)

    await adapter.send(
        chat_id,
        "complete response",
        metadata={
            TERMINAL_DELIVERY_METADATA_KEY: {
                "version": 1,
                "outcome": "success",
                "correlation_id": "source-correlation",
                "delivery_id": "source-delivery",
                "tool_calls": [{"name": "read_secret"}],
            }
        },
    )

    assert target.send.await_args.kwargs["metadata"] is None


@pytest.mark.asyncio
async def test_successful_handler_marks_only_final_text() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)
    source = adapter.build_source(
        chat_id=chat_id,
        chat_name="webhook/synthetic",
        chat_type="webhook",
        user_id="webhook:synthetic",
        user_name="synthetic",
    )
    event = MessageEvent(
        text="synthetic request",
        message_type=MessageType.TEXT,
        source=source,
        raw_message={"synthetic": True},
        message_id="external-1",
    )
    adapter.set_message_handler(AsyncMock(return_value="complete response"))
    session_key = "webhook:synthetic:external-1"
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter._process_message_background(event, session_key)

    target.send.assert_awaited_once()
    call = target.send.await_args
    assert call.args[:2] == ("correlation-1", "complete response")
    marker = call.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker["outcome"] == "success"
    assert marker["delivery_id"] == "server-turn-1"


@pytest.mark.asyncio
async def test_terminal_handler_error_is_marked_once() -> None:
    adapter, target = _webhook_adapter()
    chat_id = _seed_delivery(adapter)
    source = adapter.build_source(
        chat_id=chat_id,
        chat_name="webhook/synthetic",
        chat_type="webhook",
        user_id="webhook:synthetic",
        user_name="synthetic",
    )
    event = MessageEvent(
        text="synthetic request",
        message_type=MessageType.TEXT,
        source=source,
        raw_message={"synthetic": True},
        message_id="external-1",
    )
    adapter.set_message_handler(
        AsyncMock(side_effect=RuntimeError("synthetic failure"))
    )
    session_key = "webhook:synthetic:external-1"
    adapter._active_sessions[session_key] = asyncio.Event()

    await adapter._process_message_background(event, session_key)

    target.send.assert_awaited_once()
    call = target.send.await_args
    marker = call.kwargs["metadata"][TERMINAL_DELIVERY_METADATA_KEY]
    assert marker["outcome"] == "error"
    assert marker["correlation_id"] == "correlation-1"
    assert marker["delivery_id"] == "server-turn-1"
    assert "synthetic failure" in call.args[1]


@pytest.mark.asyncio
async def test_crash_before_callback_acceptance_replays_on_fresh_adapter(
    monkeypatch,
    ella_callback_platform,
) -> None:
    class SimulatedProcessCrash(BaseException):
        pass

    adapter, target = _webhook_adapter(ella_callback_platform)
    chat_id = _seed_delivery(
        adapter,
        internal_id="server-turn-durable",
        correlation_id="opaque-correlation-42",
        deliver="ella_callback",
    )
    adapter._delivery_info[chat_id]["prompt"] = "must-not-persist"
    adapter._delivery_info[chat_id]["secret"] = "must-not-persist"
    adapter._delivery_info[chat_id]["deliver_extra"].update(
        {
            "api_key": "must-not-persist",
            "tool_calls": [{"name": "must-not-persist"}],
        }
    )
    target.send.side_effect = SimulatedProcessCrash()

    with pytest.raises(SimulatedProcessCrash):
        await adapter.send(
            chat_id,
            "complete response",
            metadata=_terminal_metadata(correlation_id="provider-delivery-1"),
        )

    with dl._connect() as conn:
        row = conn.execute(
            """SELECT obligation_id, state, delivery_context_json,
                      delivery_metadata_json
               FROM delivery_obligations"""
        ).fetchone()
    assert row is not None
    obligation_id, state, context_json, metadata_json = row
    assert state == "attempting"
    context = json.loads(context_json)
    terminal_metadata = json.loads(metadata_json)
    assert context == {
        "version": 1,
        "deliver": "ella_callback",
        "deliver_extra": {"chat_id": "opaque-correlation-42"},
        "delivery_id": "server-turn-durable",
    }
    assert terminal_metadata == {
        TERMINAL_DELIVERY_METADATA_KEY: {
            "version": 1,
            "outcome": "success",
            "correlation_id": "opaque-correlation-42",
            "delivery_id": "server-turn-durable",
        }
    }
    assert all(
        forbidden not in context_json + metadata_json
        for forbidden in ("must-not-persist", "secret", "tool_calls", "prompt")
    )

    fresh_adapter, fresh_target = _webhook_adapter(ella_callback_platform)
    assert fresh_adapter._delivery_info == {}
    runner = object.__new__(GatewayRunner)
    runner.adapters = {
        Platform.WEBHOOK: fresh_adapter,
        ella_callback_platform: fresh_target,
    }
    runner.config = GatewayConfig(
        platforms={
            Platform.WEBHOOK: PlatformConfig(enabled=True),
            ella_callback_platform: PlatformConfig(enabled=True),
        }
    )
    fresh_adapter.gateway_runner = runner
    runner.session_store = None
    store = MagicMock()
    store._store = None
    store.clear_resume_pending = AsyncMock()
    runner._async_session_store = store
    monkeypatch.setattr(dl, "_owner_alive", lambda *_args: False)

    redelivered = await runner._redeliver_pending_obligations()

    assert redelivered == 1
    fresh_target.send.assert_awaited_once()
    replay_call = fresh_target.send.await_args
    assert replay_call.args[0] == "opaque-correlation-42"
    assert replay_call.args[1].endswith("complete response")
    assert replay_call.kwargs["metadata"] == terminal_metadata
    with dl._connect() as conn:
        replay_state = conn.execute(
            "SELECT state FROM delivery_obligations WHERE obligation_id=?",
            (obligation_id,),
        ).fetchone()[0]
    assert replay_state == "delivered"


@pytest.mark.asyncio
async def test_fresh_adapter_without_durable_route_fails_instead_of_logging() -> None:
    adapter, target = _webhook_adapter()

    result = await adapter.send(
        "webhook:managed:provider-delivery-1",
        "complete response",
        metadata=_terminal_metadata(),
    )

    assert result.success is False
    assert "route" in (result.error or "").lower()
    target.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_distinct_accepted_turns_get_distinct_internal_delivery_ids(
    monkeypatch,
) -> None:
    _fake_web(monkeypatch)
    adapter, _target = _webhook_adapter()
    adapter._routes["synthetic"] = {
        "secret": "INSECURE_NO_AUTH",
        "prompt": "{text}",
        "deliver": "telegram",
        "deliver_extra": {
            "chat_id": "{correlation_id}",
        },
    }
    adapter.handle_message = AsyncMock()

    payload = {"text": "same", "correlation_id": "same-correlation"}
    first = await adapter._handle_webhook(
        _request(
            route_name="synthetic",
            delivery_id="external-1",
            payload=payload,
        )
    )
    second = await adapter._handle_webhook(
        _request(
            route_name="synthetic",
            delivery_id="external-2",
            payload=payload,
        )
    )

    assert first.status == 202
    assert second.status == 202
    assert (
        adapter._delivery_info["webhook:synthetic:external-1"]["_hermes_delivery_id"]
        != "external-1"
    )
    assert (
        adapter._delivery_info["webhook:synthetic:external-2"]["_hermes_delivery_id"]
        != "external-2"
    )
    assert (
        adapter._delivery_info["webhook:synthetic:external-1"][
            "_hermes_delivery_id"
        ]
        != adapter._delivery_info["webhook:synthetic:external-2"][
            "_hermes_delivery_id"
        ]
    )


@pytest.mark.asyncio
async def test_same_signed_provider_delivery_keeps_identity_across_restart(
    monkeypatch,
) -> None:
    _fake_web(monkeypatch)
    secret = "synthetic-signing-secret"
    route = {
        "secret": secret,
        "prompt": "{text}",
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "{correlation_id}"},
    }
    payload = {
        "text": "same synthetic request",
        "correlation_id": "opaque-correlation-42",
    }

    first_adapter, _ = _webhook_adapter()
    first_adapter._routes["managed"] = route
    first_adapter.handle_message = AsyncMock()
    first = await first_adapter._handle_webhook(
        _request(
            route_name="managed",
            delivery_id="provider-delivery-1",
            payload=payload,
            secret=secret,
        )
    )
    first_identity = first_adapter._delivery_info[
        "webhook:managed:provider-delivery-1"
    ]["_hermes_delivery_id"]

    restarted_adapter, _ = _webhook_adapter()
    restarted_adapter._routes["managed"] = route
    restarted_adapter.handle_message = AsyncMock()
    replay = await restarted_adapter._handle_webhook(
        _request(
            route_name="managed",
            delivery_id="provider-delivery-1",
            payload=payload,
            secret=secret,
        )
    )
    replay_identity = restarted_adapter._delivery_info[
        "webhook:managed:provider-delivery-1"
    ]["_hermes_delivery_id"]

    assert first.status == replay.status == 202
    assert first_identity == replay_identity
    assert first_identity != "provider-delivery-1"
    assert len(first_identity) == 64
