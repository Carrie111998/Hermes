from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from grover_runtime.telegram_callbacks import (
    _default_dependencies,
    handle_typed_action_callback,
)


CALLBACK = "od:" + "A" * 24
CARD_REF = "TGC-0123456789abcdef"
RECEIPT_ID = "ACT-0123456789ab"


def test_default_bridge_dependencies_are_carried_by_the_pinned_runtime():
    factory, renderer = _default_dependencies()

    assert factory.__module__ == "grover_runtime.action_service_client"
    assert renderer.__module__ == "grover_runtime.action_service_client"


def _query(data=CALLBACK):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=123456, first_name="Kevin"),
        message=SimpleNamespace(
            chat_id=-1004474237403,
            message_id=321,
            message_thread_id=91,
            text="Decision",
            chat=SimpleNamespace(type="supergroup"),
        ),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_opaque_callback_resolves_server_action_and_mirrors_exact_receipt():
    query = _query()
    client = Mock()
    client.resolve_callback.return_value = {
        "card_ref": CARD_REF,
        "receipt_id": RECEIPT_ID,
        "action": "approve",
        "mode": "shadow",
    }
    pending = {
        "card_ref": CARD_REF,
        "binding": {
            "chat_id": "-1004474237403",
            "thread_id": "91",
            "message_id": "321",
            "card_html": "<b>Decision</b>",
        },
        "resolution": {
            "receipt_id": RECEIPT_ID,
            "action": "approve",
            "actor_label": "Kevin",
            "mode": "shadow",
        },
    }
    client.pending_card.return_value = pending
    render = Mock(return_value="<b>Decision</b>\n\n<b>SHADOW RECEIPT</b>")

    handled = await handle_typed_action_callback(
        query,
        is_authorized=lambda *_args, **_kwargs: True,
        client_factory=lambda: client,
        render_receipt=render,
    )

    assert handled is True
    client.resolve_callback.assert_called_once_with(
        CALLBACK, "-1004474237403", "321", "123456", "Kevin"
    )
    client.pending_card.assert_called_once_with(
        CARD_REF,
        chat_id="-1004474237403",
        thread_id="91",
        message_id="321",
        receipt_id=RECEIPT_ID,
        action="approve",
    )
    client.mirrored.assert_called_once_with(CARD_REF, RECEIPT_ID)
    query.edit_message_text.assert_awaited_once_with(
        text="<b>Decision</b>\n\n<b>SHADOW RECEIPT</b>",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=None,
    )
    query.answer.assert_awaited_once_with(
        text="Recorded in SHADOW. Nothing was executed.", show_alert=True
    )


@pytest.mark.asyncio
async def test_arbitrary_act_text_is_not_a_typed_callback():
    query = _query("act:approve this arbitrary request")

    handled = await handle_typed_action_callback(
        query,
        is_authorized=lambda *_args, **_kwargs: True,
        client_factory=Mock(),
        render_receipt=Mock(),
    )

    assert handled is False
    query.answer.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_unauthorized_opaque_callback_fails_closed_before_server_resolution():
    query = _query()
    client_factory = Mock()

    handled = await handle_typed_action_callback(
        query,
        is_authorized=lambda *_args, **_kwargs: False,
        client_factory=client_factory,
        render_receipt=Mock(),
    )

    assert handled is True
    client_factory.assert_not_called()
    query.answer.assert_awaited_once_with(
        text="You are not authorized to resolve this action.", show_alert=True
    )
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_authorization_failure_is_consumed_and_fails_closed():
    query = _query()
    client_factory = Mock()

    def broken_authorization(*_args, **_kwargs):
        raise RuntimeError("authorization backend unavailable")

    handled = await handle_typed_action_callback(
        query,
        is_authorized=broken_authorization,
        client_factory=client_factory,
        render_receipt=Mock(),
    )

    assert handled is True
    client_factory.assert_not_called()
    query.edit_message_text.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        text="Could not verify authorization. Nothing was executed.", show_alert=True
    )


@pytest.mark.asyncio
async def test_malformed_or_unknown_opaque_callback_fails_closed_without_receipt():
    for data in ("od:short", CALLBACK):
        query = _query(data)
        client = Mock()
        client.resolve_callback.side_effect = RuntimeError("secret server detail")

        handled = await handle_typed_action_callback(
            query,
            is_authorized=lambda *_args, **_kwargs: True,
            client_factory=lambda: client,
            render_receipt=Mock(),
        )

        assert handled is True
        query.edit_message_text.assert_not_awaited()
        query.answer.assert_awaited_once_with(
            text="Could not record this decision. Nothing was executed.",
            show_alert=True,
        )
        assert "secret server detail" not in query.answer.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_expired_callback_fails_closed_without_edit_or_mirror():
    query = _query()
    client = Mock()
    client.resolve_callback.side_effect = RuntimeError("HTTP 404 expired")

    handled = await handle_typed_action_callback(
        query,
        is_authorized=lambda *_args, **_kwargs: True,
        client_factory=lambda: client,
        render_receipt=Mock(),
    )

    assert handled is True
    client.pending_card.assert_not_called()
    client.mirrored.assert_not_called()
    query.edit_message_text.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        text="Could not record this decision. Nothing was executed.", show_alert=True
    )


@pytest.mark.asyncio
async def test_wrong_thread_or_tampered_receipt_stays_pending_and_removes_buttons():
    query = _query()
    client = Mock()
    client.resolve_callback.return_value = {
        "card_ref": CARD_REF,
        "receipt_id": RECEIPT_ID,
        "action": "approve",
        "mode": "shadow",
    }
    client.pending_card.side_effect = RuntimeError(
        "pending receipt does not match callback"
    )

    handled = await handle_typed_action_callback(
        query,
        is_authorized=lambda *_args, **_kwargs: True,
        client_factory=lambda: client,
        render_receipt=Mock(),
    )

    assert handled is True
    client.pending_card.assert_called_once_with(
        CARD_REF,
        chat_id="-1004474237403",
        thread_id="91",
        message_id="321",
        receipt_id=RECEIPT_ID,
        action="approve",
    )
    client.mirrored.assert_not_called()
    query.edit_message_text.assert_not_awaited()
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    query.answer.assert_awaited_once_with(
        text="Recorded in SHADOW; receipt sync pending. Nothing was executed.",
        show_alert=True,
    )


@pytest.mark.asyncio
async def test_telegram_adapter_routes_opaque_callback_before_builtin_dispatch(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from plugins.platforms.telegram import adapter as telegram_adapter

    bridge = AsyncMock(return_value=True)
    monkeypatch.setattr(telegram_adapter, "handle_typed_action_callback", bridge)
    adapter = object.__new__(telegram_adapter.TelegramAdapter)
    adapter._is_callback_user_authorized = Mock(return_value=True)
    query = _query()
    update = SimpleNamespace(callback_query=query)

    await adapter._handle_callback_query(update, SimpleNamespace())

    bridge.assert_awaited_once_with(
        query,
        is_authorized=adapter._is_callback_user_authorized,
    )
