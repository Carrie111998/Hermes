"""Offline tests for Feishu JSON 2.0 card transport."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.feishu.adapter import (
    FeishuAdapter,
    FeishuCardValidationError,
    parse_feishu_card_json,
)


CARD = {
    "schema": "2.0",
    "config": {"update_multi": True, "width_mode": "fill"},
    "header": {
        "template": "blue",
        "title": {"tag": "plain_text", "content": "CARD MVP1"},
    },
    "body": {
        "direction": "vertical",
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": "CARD MVP1"},
            }
        ],
    },
}


def test_card_fixture_uses_official_json_2_0_fields():
    assert CARD["schema"] == "2.0"
    assert CARD["config"] == {"update_multi": True, "width_mode": "fill"}
    assert "wide_screen_mode" not in json.dumps(CARD)
    assert CARD["header"]["title"]["tag"] == "plain_text"
    assert CARD["body"]["direction"] == "vertical"


def _response(message_id: str | None = None, *, success: bool = True):
    return SimpleNamespace(
        success=lambda: success,
        data=SimpleNamespace(message_id=message_id) if message_id else None,
        code=0 if success else 123,
        msg="ok" if success else "rejected",
    )


def _make_adapter():
    adapter = FeishuAdapter(PlatformConfig(enabled=True))
    client = MagicMock()
    adapter._client = client
    return adapter, client


@pytest.mark.asyncio
async def test_send_card_uses_interactive_create_and_returns_message_id():
    adapter, client = _make_adapter()
    client.im.v1.message.create.return_value = _response("om_created")

    result = await adapter.send_card("oc_chat", CARD)

    assert result.success is True
    assert result.message_id == "om_created"
    client.im.v1.message.create.assert_called_once()
    request = client.im.v1.message.create.call_args.args[0]
    assert request.request_body.receive_id == "oc_chat"
    assert request.request_body.msg_type == "interactive"
    assert json.loads(request.request_body.content) == CARD


@pytest.mark.asyncio
async def test_send_card_preserves_thread_metadata():
    adapter, client = _make_adapter()
    client.im.v1.message.create.return_value = _response("om_threaded")

    result = await adapter.send_card("oc_chat", CARD, metadata={"thread_id": "omt_thread"})

    assert result.success is True
    request = client.im.v1.message.create.call_args.args[0]
    assert request.receive_id_type == "thread_id"
    assert request.request_body.receive_id == "omt_thread"


@pytest.mark.asyncio
async def test_patch_card_uses_message_id_and_content_only():
    adapter, client = _make_adapter()
    client.im.v1.message.patch.return_value = _response()

    result = await adapter.patch_card("om_existing", CARD)

    assert result.success is True
    assert result.message_id == "om_existing"
    client.im.v1.message.patch.assert_called_once()
    request = client.im.v1.message.patch.call_args.args[0]
    assert request.message_id == "om_existing"
    assert request.request_body.content == json.dumps(CARD, ensure_ascii=False)
    assert not hasattr(request.request_body, "msg_type")
    client.im.v1.message.create.assert_not_called()
    client.im.v1.message.update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "card",
    [
        {"schema": "1.0"},
        [],
        "not an object",
        {"schema": "2.0", "body": object()},
    ],
)
async def test_invalid_card_is_rejected_before_any_feishu_request(card):
    adapter, client = _make_adapter()

    result = await adapter.send_card("oc_chat", card)  # type: ignore[arg-type]
    patch_result = await adapter.patch_card("om_existing", card)  # type: ignore[arg-type]

    assert result.success is False
    assert patch_result.success is False
    client.im.v1.message.create.assert_not_called()
    client.im.v1.message.patch.assert_not_called()
    client.im.v1.message.update.assert_not_called()


def test_damaged_card_json_fails_closed():
    with pytest.raises(FeishuCardValidationError, match="not valid JSON"):
        parse_feishu_card_json("{broken")


def test_non_object_and_non_2_0_roots_fail_closed():
    with pytest.raises(FeishuCardValidationError, match="JSON object"):
        parse_feishu_card_json("[]")
    with pytest.raises(FeishuCardValidationError, match="schema='2.0'"):
        parse_feishu_card_json('{"msg_type":"text"}')


@pytest.mark.asyncio
async def test_sdk_error_does_not_fall_back_to_text():
    adapter, client = _make_adapter()
    client.im.v1.message.create.side_effect = RuntimeError("SDK failure")

    result = await adapter.send_card("oc_chat", CARD)

    assert result.success is False
    assert client.im.v1.message.create.call_count == 3
    client.im.v1.message.update.assert_not_called()
    client.im.v1.message.reply.assert_not_called()
