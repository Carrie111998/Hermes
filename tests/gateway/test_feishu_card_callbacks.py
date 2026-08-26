"""Offline tests for the Feishu card callback envelope and dispatch boundary."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import PlatformConfig
from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
import plugins.platforms.feishu.adapter as feishu_module
from plugins.platforms.feishu.adapter import FeishuAdapter, FeishuCardHandlerResult


COMPLETED_CARD = {
    "schema": "2.0",
    "config": {"update_multi": True, "width_mode": "fill"},
    "header": {
        "template": "green",
        "title": {"tag": "plain_text", "content": "CARD MVP1"},
    },
    "body": {
        "direction": "vertical",
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "CARD MVP1: completed"},
            }
        ],
    },
}


def _make_adapter() -> FeishuAdapter:
    adapter = FeishuAdapter(PlatformConfig(enabled=True))
    adapter._client = MagicMock()
    adapter._allowed_group_users = {"ou_allowed"}
    return adapter


def _make_manager(callback=None, namespace="mvp1.test") -> PluginManager:
    manager = PluginManager()
    if callback is not None:
        context = PluginContext(
            manifest=PluginManifest(name="contract-test", version="1", description=""),
            manager=manager,
        )
        context.register_feishu_card_action_handler(namespace, callback)
    return manager


def _data(
    *,
    token="event-token-1",
    event_id="event-id-1",
    namespace="mvp1.test",
    open_id="ou_allowed",
    form_value=None,
    input_value="CARD-MVP1-OK",
):
    return SimpleNamespace(
        header=SimpleNamespace(event_id=event_id, token="header-token"),
        event=SimpleNamespace(
            token=token,
            context=SimpleNamespace(
                url="https://open.feishu.cn/callback",
                preview_token="preview-token",
                open_message_id="om_card",
                open_chat_id="oc_chat",
            ),
            operator=SimpleNamespace(
                tenant_key="tenant-key",
                user_id="u_allowed",
                open_id=open_id,
                union_id="on_allowed",
            ),
            # This models Feishu's callback payload after a JSON 2.0
            # ``behaviors`` callback has been resolved; it is not card JSON.
            action=SimpleNamespace(
                tag="button",
                name="submit",
                value={"namespace": namespace, "action": "submit", "safe": True},
                form_value=form_value or {"field": "CARD-MVP1-OK"},
                input_value=input_value,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_complete_callback_envelope_preserves_operator_context_and_inputs():
    received = []

    def handler(envelope):
        received.append(envelope)
        return {"status": "accepted", "message": "queued"}

    adapter = _make_adapter()
    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        result = await adapter._handle_card_action_event(_data())

    assert result == {"status": "accepted", "message": "queued"}
    assert len(received) == 1
    envelope = received[0]
    assert envelope.event_id == "event-id-1"
    assert envelope.event_token == "event-token-1"
    assert envelope.operator == {
        "tenant_key": "tenant-key",
        "user_id": "u_allowed",
        "open_id": "ou_allowed",
        "union_id": "on_allowed",
    }
    assert envelope.context["open_chat_id"] == "oc_chat"
    assert envelope.context["open_message_id"] == "om_card"
    assert envelope.action_tag == "button"
    assert envelope.action_name == "submit"
    assert envelope.action_value["namespace"] == "mvp1.test"
    assert envelope.form_value == {"field": "CARD-MVP1-OK"}
    assert envelope.input_value == "CARD-MVP1-OK"


@pytest.mark.asyncio
async def test_acl_rejection_does_not_invoke_handler():
    calls = []

    def handler(_envelope):
        calls.append(True)
        return {"status": "accepted", "message": "handled"}

    adapter = _make_adapter()
    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        result = await adapter._handle_card_action_event(_data(open_id="ou_intruder"))

    assert result["status"] == "error"
    assert "authorized" in result["message"]
    assert calls == []


@pytest.mark.asyncio
async def test_duplicate_event_invokes_handler_once():
    calls = []

    def handler(_envelope):
        calls.append(True)
        return {"status": "accepted", "message": "handled"}

    adapter = _make_adapter()
    manager = _make_manager(handler)
    data = _data()
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        first = await adapter._handle_card_action_event(data)
        second = await adapter._handle_card_action_event(data)

    assert first["status"] == "accepted"
    assert second == {"status": "ignored", "message": "Card action was already received."}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_unknown_namespace_has_zero_gateway_side_effect():
    adapter = _make_adapter()
    manager = _make_manager(
        namespace="mvp1.registered",
        callback=lambda _e: {"status": "accepted", "message": "handled"},
    )
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        adapter, "_handle_message_with_guards", new_callable=MagicMock
    ) as handle_message:
        result = await adapter._handle_card_action_event(_data(namespace="mvp1.unknown"))

    assert result == {"status": "error", "message": "Unknown card action namespace."}
    handle_message.assert_not_called()


def test_sync_callback_returns_handler_result_toast_inline():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    calls = []

    def handler(_envelope):
        calls.append(True)
        return FeishuCardHandlerResult(
            status="accepted",
            message="queued",
            card=COMPLETED_CARD,
        )

    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        response = adapter._on_card_action_trigger(_data())

    assert response.toast.type == "success"
    assert response.toast.content == "queued"
    assert COMPLETED_CARD["config"] == {"update_multi": True, "width_mode": "fill"}
    assert "wide_screen_mode" not in json.dumps(COMPLETED_CARD)
    assert response.card.type == "raw"
    assert response.card.data == COMPLETED_CARD
    assert calls == [True]


@pytest.mark.parametrize("card", [{"schema": "1.0"}, {"schema": "2.0", "body": object()}])
def test_invalid_handler_card_fails_closed_without_background_or_llm(card):
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    background_calls = []

    def background():
        background_calls.append(True)
        return asyncio.sleep(0)

    def handler(_envelope):
        return FeishuCardHandlerResult(
            status="accepted",
            message="should not be accepted",
            background=background,
            card=card,
        )

    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        adapter, "_submit_on_loop", new_callable=MagicMock
    ) as submit, patch.object(
        adapter, "_handle_message_with_guards", new_callable=MagicMock
    ) as llm:
        response = adapter._on_card_action_trigger(_data())

    assert response.toast.type == "error"
    assert "invalid result" in response.toast.content
    assert getattr(response, "card", None) is None
    submit.assert_not_called()
    llm.assert_not_called()
    assert background_calls == []


def test_sync_callback_invalid_result_returns_error_toast():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    manager = _make_manager(lambda _e: {"status": "accepted"})

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        response = adapter._on_card_action_trigger(_data())

    assert response.toast.type == "error"
    assert "invalid result" in response.toast.content


def test_sync_callback_exception_returns_error_toast_without_llm(caplog):
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False

    def handler(_envelope):
        raise RuntimeError("CARD-MVP1-OK")

    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        adapter, "_handle_message_with_guards", new_callable=MagicMock
    ) as handle_message:
        response = adapter._on_card_action_trigger(_data())

    assert response.toast.type == "error"
    assert response.toast.content == "Card action handler failed."
    assert "CARD-MVP1-OK" not in response.toast.content
    assert "CARD-MVP1-OK" not in caplog.text
    handle_message.assert_not_called()


def test_sync_callback_acl_duplicate_and_unknown_return_real_toasts():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    calls = []

    def handler(_envelope):
        calls.append(True)
        return {"status": "accepted", "message": "processed"}

    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        first = adapter._on_card_action_trigger(_data())
        duplicate = adapter._on_card_action_trigger(_data())
        unauthorized = adapter._on_card_action_trigger(_data(event_id="event-id-2", token="event-token-2", open_id="ou_intruder"))
        unknown = adapter._on_card_action_trigger(
            _data(event_id="event-id-3", token="event-token-3", namespace="mvp1.unknown")
        )

    assert first.toast.type == "success"
    assert first.toast.content == "processed"
    assert duplicate.toast.type == "info"
    assert "already" in duplicate.toast.content
    assert unauthorized.toast.type == "error"
    assert "authorized" in unauthorized.toast.content
    assert unknown.toast.type == "error"
    assert "Unknown" in unknown.toast.content
    assert calls == [True]


def test_sync_handler_wall_clock_timeout_returns_before_three_seconds():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    entered = threading.Event()
    release = threading.Event()
    late_returned = threading.Event()
    background_runs = threading.Event()
    late_backgrounds = []

    async def late_background():
        background_runs.set()

    def handler(_envelope):
        entered.set()
        release.wait(timeout=10)
        background = late_background()
        late_backgrounds.append(background)
        late_returned.set()
        return FeishuCardHandlerResult(
            status="accepted",
            message="late result",
            background=background,
        )

    manager = _make_manager(handler)
    try:
        with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
            adapter, "_handle_message_with_guards", new_callable=MagicMock
        ) as llm:
            started = time.monotonic()
            response = adapter._on_card_action_trigger(_data())
            elapsed = time.monotonic() - started

        assert entered.wait(timeout=1)
        assert elapsed < 3.0
        assert response.toast.type == "error"
        assert "timed out" in response.toast.content
        llm.assert_not_called()
        assert not background_runs.is_set()
    finally:
        release.set()

    assert late_returned.wait(timeout=2)
    deadline = time.monotonic() + 1
    while late_backgrounds and late_backgrounds[0].cr_frame is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert late_backgrounds[0].cr_frame is None
    assert not background_runs.is_set()


def test_concurrent_duplicate_callback_reserves_token_once():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    barrier = threading.Barrier(2)
    calls = []
    calls_lock = threading.Lock()
    responses = [None, None]
    errors = []

    def handler(_envelope):
        with calls_lock:
            calls.append(True)
        return {"status": "accepted", "message": "handled"}

    def find_handler(_namespace):
        barrier.wait(timeout=2)
        return handler, "contract-test"

    def invoke(index):
        try:
            responses[index] = adapter._on_card_action_trigger(_data())
        except BaseException as exc:  # pragma: no cover - assertion reports the failure
            errors.append(exc)

    threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
    with patch.object(adapter, "_find_feishu_card_handler", side_effect=find_handler), patch.object(
        adapter, "_handle_message_with_guards", new_callable=MagicMock
    ) as llm, patch.object(
        adapter,
        "_invoke_feishu_card_handler_sync",
        wraps=adapter._invoke_feishu_card_handler_sync,
    ) as invoke_handler:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

    assert errors == []
    assert all(response is not None for response in responses)
    assert sorted(response.toast.type for response in responses) == ["info", "success"]
    assert calls == [True]
    assert invoke_handler.call_count == 1
    llm.assert_not_called()


def test_concurrent_different_tokens_both_dispatch():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    barrier = threading.Barrier(2)
    calls = []
    calls_lock = threading.Lock()
    responses = [None, None]
    errors = []

    def handler(envelope):
        with calls_lock:
            calls.append(envelope.event_token)
        barrier.wait(timeout=2)
        return {"status": "accepted", "message": "handled"}

    def invoke(index):
        try:
            responses[index] = adapter._on_card_action_trigger(
                _data(event_id=f"different-{index}", token=f"different-token-{index}")
            )
        except BaseException as exc:  # pragma: no cover - assertion reports the failure
            errors.append(exc)

    threads = [threading.Thread(target=invoke, args=(index,)) for index in range(2)]
    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        adapter, "_handle_message_with_guards", new_callable=MagicMock
    ) as llm:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)

    assert errors == []
    assert all(response is not None for response in responses)
    assert all(response.toast.type == "success" for response in responses)
    assert sorted(calls) == ["different-token-0", "different-token-1"]
    llm.assert_not_called()


def test_sync_handler_pool_saturation_rejects_without_submit_and_recovers(monkeypatch):
    monkeypatch.setattr(feishu_module, "_FEISHU_CARD_HANDLER_TIMEOUT_SECONDS", 0.1)
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    release = threading.Event()
    lock = threading.Lock()
    started = 0
    returned = 0
    late_backgrounds = []
    background_runs = threading.Event()
    max_workers = feishu_module._FEISHU_CARD_HANDLER_MAX_WORKERS

    async def late_background():
        background_runs.set()

    def handler(_envelope):
        nonlocal started, returned
        with lock:
            started += 1
            invocation = started
        release.wait(timeout=5)
        if invocation > max_workers:
            return {"status": "accepted", "message": "recovered"}
        background = late_background()
        with lock:
            late_backgrounds.append(background)
            returned += 1
        return FeishuCardHandlerResult(
            status="accepted",
            message="late result",
            background=background,
        )

    manager = _make_manager(handler)
    responses = []
    try:
        with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
            feishu_module._FEISHU_CARD_HANDLER_EXECUTOR,
            "submit",
            wraps=feishu_module._FEISHU_CARD_HANDLER_EXECUTOR.submit,
        ) as submit:
            for index in range(max_workers):
                responses.append(
                    adapter._on_card_action_trigger(
                        _data(event_id=f"saturation-{index}", token=f"saturation-token-{index}")
                    )
                )
            started_extra = time.monotonic()
            extra = adapter._on_card_action_trigger(
                _data(event_id="saturation-extra", token="saturation-token-extra")
            )
            extra_elapsed = time.monotonic() - started_extra

            assert all(response.toast.type == "error" for response in responses)
            assert all("timed out" in response.toast.content for response in responses)
            assert extra.toast.type == "error"
            assert "busy" in extra.toast.content
            assert extra_elapsed < 0.2
            assert submit.call_count == max_workers
            with lock:
                assert started == max_workers
                assert returned == 0
    finally:
        release.set()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with lock:
            if returned == started:
                break
        time.sleep(0.01)
    with lock:
        assert returned == started == max_workers
        assert len(late_backgrounds) == returned
    deadline = time.monotonic() + 1
    while any(background.cr_frame is not None for background in late_backgrounds) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert all(background.cr_frame is None for background in late_backgrounds)
    assert not background_runs.is_set()

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        feishu_module._FEISHU_CARD_HANDLER_EXECUTOR,
        "submit",
        wraps=feishu_module._FEISHU_CARD_HANDLER_EXECUTOR.submit,
    ) as submit:
        recovered = adapter._on_card_action_trigger(
            _data(event_id="saturation-extra", token="saturation-token-extra")
        )
    assert recovered.toast.type == "success"
    assert recovered.toast.content == "recovered"
    assert submit.call_count == 1


def test_submit_failure_releases_permit_and_allows_retry():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    calls = []

    def handler(_envelope):
        calls.append(True)
        return {"status": "accepted", "message": "retried"}

    manager = _make_manager(handler)
    data = _data()
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        feishu_module._FEISHU_CARD_HANDLER_EXECUTOR,
        "submit",
        side_effect=RuntimeError("executor unavailable"),
    ):
        failed = adapter._on_card_action_trigger(data)

    assert failed.toast.type == "error"
    assert "scheduled" in failed.toast.content
    assert calls == []
    assert feishu_module._FEISHU_CARD_HANDLER_IN_FLIGHT.acquire(blocking=False)
    feishu_module._FEISHU_CARD_HANDLER_IN_FLIGHT.release()

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        retried = adapter._on_card_action_trigger(data)
    assert retried.toast.type == "success"
    assert retried.toast.content == "retried"
    assert calls == [True]


def test_async_handler_is_rejected_instead_of_awaited_for_callback():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False

    async def handler(_envelope):
        return {"status": "accepted", "message": "should not run"}

    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        response = adapter._on_card_action_trigger(_data())

    assert response.toast.type == "error"
    assert "immediate result" in response.toast.content


def test_controlled_background_is_separate_from_immediate_toast():
    adapter = _make_adapter()
    adapter._loop = MagicMock()
    adapter._loop.is_closed.return_value = False
    scheduled = []

    def close_scheduled(_loop, coro, **_kwargs):
        scheduled.append(coro)
        coro.close()
        return True

    def handler(_envelope):
        return FeishuCardHandlerResult(
            status="accepted",
            message="queued",
            background=lambda: asyncio.sleep(0),
        )

    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        adapter, "_submit_on_loop", side_effect=close_scheduled
    ):
        response = adapter._on_card_action_trigger(_data())

    assert response.toast.type == "success"
    assert response.toast.content == "queued"
    assert len(scheduled) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "handler, expected",
    [
        (lambda _e: "not a result", "invalid result"),
        (lambda _e: {"status": "accepted", "unexpected": True}, "invalid result"),
    ],
)
async def test_invalid_handler_result_is_rejected(handler, expected):
    adapter = _make_adapter()
    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        result = await adapter._handle_card_action_event(_data())

    assert result["status"] == "error"
    assert expected in result["message"]


@pytest.mark.asyncio
async def test_handler_exception_is_contained_and_never_enters_llm():
    def handler(_envelope):
        raise RuntimeError("handler failure")

    adapter = _make_adapter()
    manager = _make_manager(handler)
    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager), patch.object(
        adapter, "_handle_message_with_guards", new_callable=MagicMock
    ) as handle_message:
        result = await adapter._handle_card_action_event(_data())

    assert result == {"status": "error", "message": "Card action handler failed."}
    handle_message.assert_not_called()


@pytest.mark.asyncio
async def test_webhook_card_callback_serializes_real_toast_response():
    adapter = _make_adapter()
    adapter._loop = asyncio.get_running_loop()
    calls = []

    def handler(envelope):
        calls.append(envelope.event_id)
        return FeishuCardHandlerResult(
            status="accepted",
            message="callback accepted",
            card=COMPLETED_CARD,
        )

    payload = {
        "header": {
            "event_type": "card.action.trigger",
            "event_id": "webhook-event-1",
            "token": "header-token",
        },
        "event": {
            "token": "event-token-webhook",
            "context": {
                "open_message_id": "om_webhook",
                "open_chat_id": "oc_chat",
            },
            "operator": {"open_id": "ou_allowed"},
            "action": {
                "tag": "button",
                "name": "submit",
                "value": {"namespace": "mvp1.test"},
                "form_value": {"field": "CARD-MVP1-OK"},
                "input_value": "CARD-MVP1-OK",
            },
        },
    }
    body = json.dumps(payload).encode("utf-8")
    request = SimpleNamespace(
        remote="127.0.0.1",
        headers={"Content-Type": "application/json"},
        content_length=len(body),
        content=SimpleNamespace(readexactly=AsyncMock(return_value=body)),
    )
    manager = _make_manager(handler)

    with patch("hermes_cli.plugins.get_plugin_manager", return_value=manager):
        response = await adapter._handle_webhook_request(request)

    assert response.status == 200
    assert json.loads(response.text) == {
        "toast": {"type": "success", "content": "callback accepted"},
        "card": {"type": "raw", "data": COMPLETED_CARD},
    }
    assert calls == ["webhook-event-1"]


def test_plugin_namespace_registration_is_fixed_and_validated():
    manager = PluginManager()
    context = PluginContext(
        manifest=PluginManifest(name="contract-test", version="1", description=""),
        manager=manager,
    )
    with pytest.raises(ValueError, match="invalid"):
        context.register_feishu_card_action_handler("../../shell", lambda _e: {"status": "accepted"})
    with pytest.raises(ValueError, match="non-callable"):
        context.register_feishu_card_action_handler("mvp1.test", "not callable")  # type: ignore[arg-type]

    context.register_feishu_card_action_handler("mvp1.test", lambda _e: {"status": "accepted"})
    with pytest.raises(ValueError, match="already registered"):
        context.register_feishu_card_action_handler("mvp1.test", lambda _e: {"status": "accepted"})
