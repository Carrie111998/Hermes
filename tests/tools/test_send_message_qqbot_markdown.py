"""Regression test for #26697 — ``send_message`` honors ``markdown_support``
for the QQ platform.

The QQ gateway adapter (``gateway/platforms/qqbot/adapter.py``) reads
``markdown_support`` from ``pconfig.extra`` (default ``True``) and sends
C2C/group messages with ``msg_type: 2`` and a ``markdown.content`` body,
plus a ``msg_seq``. Before this fix the ``send_message`` tool's
``_send_qqbot`` helper hardcoded ``msg_type: 0`` and a plain ``content``
field, so markdown tables / formatted output were delivered as raw text.

This local edition also asserts the ``msg_seq`` field that the upstream
review required (C2C/group payloads must carry it), and that the guild
channel endpoint keeps the plain ``{"content": ...}`` shape.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import PlatformConfig
from tools.send_message_tool import _send_qqbot


def _make_resp(status_code, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_data or {})
    return resp


def _httpx_client_with_responses(responses):
    """Return a ``patch`` context for ``httpx.AsyncClient`` that replies
    with the supplied responses in order.
    """
    client = AsyncMock()
    client.post = AsyncMock(side_effect=responses)
    client_ctx = MagicMock()
    client_ctx.__aenter__ = AsyncMock(return_value=client)
    client_ctx.__aexit__ = AsyncMock(return_value=False)
    return client_ctx, client


def _make_pconfig(markdown_support=True):
    extra = {"app_id": "1234567"}
    if markdown_support is not None:
        extra["markdown_support"] = markdown_support
    return PlatformConfig(enabled=True, token="secret", extra=extra)


def _call(pconfig, content="| col1 | col2 |\n|---|---|\n| a | b |"):
    return asyncio.run(_send_qqbot(pconfig, "openid-1", content))


def test_c2c_uses_markdown_payload_when_markdown_support_true():
    token_resp = _make_resp(200, {"access_token": "***"})
    channel_resp = _make_resp(404, {"code": 11403, "message": "频道不存在"})
    c2c_resp = _make_resp(200, {"id": "msg-1"})

    client_ctx, client = _httpx_client_with_responses(
        [token_resp, channel_resp, c2c_resp]
    )
    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = _call(_make_pconfig(markdown_support=True))

    assert result == {
        "success": True, "platform": "qqbot", "chat_id": "openid-1",
        "message_id": "msg-1",
    }
    c2c_call = client.post.await_args_list[2]
    assert c2c_call.args[0] == "https://api.sgroup.qq.com/v2/users/openid-1/messages"
    sent = c2c_call.kwargs["json"]
    assert sent["msg_type"] == 2
    assert sent["markdown"]["content"].startswith("| col1 | col2 |")
    assert "msg_seq" in sent and 0 <= sent["msg_seq"] < 65536
    assert "content" not in sent  # the top-level `content` field is NOT used for markdown


def test_group_uses_markdown_payload_when_markdown_support_true():
    token_resp = _make_resp(200, {"access_token": "***"})
    channel_resp = _make_resp(404, {})
    c2c_resp = _make_resp(404, {})
    group_resp = _make_resp(200, {"id": "msg-2"})

    client_ctx, client = _httpx_client_with_responses(
        [token_resp, channel_resp, c2c_resp, group_resp]
    )
    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = _call(_make_pconfig(markdown_support=True))

    assert result["success"] is True
    group_call = client.post.await_args_list[3]
    assert group_call.args[0] == "https://api.sgroup.qq.com/v2/groups/openid-1/messages"
    sent = group_call.kwargs["json"]
    assert sent["msg_type"] == 2
    assert sent["markdown"]["content"].startswith("| col1 | col2 |")
    assert "msg_seq" in sent and 0 <= sent["msg_seq"] < 65536


def test_c2c_uses_plain_text_when_markdown_support_false():
    token_resp = _make_resp(200, {"access_token": "***"})
    channel_resp = _make_resp(404, {})
    c2c_resp = _make_resp(200, {"id": "msg-3"})

    client_ctx, client = _httpx_client_with_responses(
        [token_resp, channel_resp, c2c_resp]
    )
    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = _call(_make_pconfig(markdown_support=False), content="hello")

    assert result["success"] is True
    c2c_call = client.post.await_args_list[2]
    sent = c2c_call.kwargs["json"]
    assert sent["msg_type"] == 0
    assert sent["content"] == "hello"
    assert "markdown" not in sent
    assert "msg_seq" in sent and 0 <= sent["msg_seq"] < 65536


def test_markdown_support_defaults_to_true_when_unset():
    """Default behavior must match the gateway adapter (also defaults True)."""
    token_resp = _make_resp(200, {"access_token": "***"})
    channel_resp = _make_resp(404, {})
    c2c_resp = _make_resp(200, {"id": "msg-4"})

    client_ctx, client = _httpx_client_with_responses(
        [token_resp, channel_resp, c2c_resp]
    )
    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = _call(_make_pconfig(markdown_support=None), content="hi")

    assert result["success"] is True
    c2c_call = client.post.await_args_list[2]
    sent = c2c_call.kwargs["json"]
    assert sent["msg_type"] == 2
    assert sent["markdown"]["content"] == "hi"
    assert "msg_seq" in sent


def test_channel_endpoint_payload_unchanged():
    """The guild channel endpoint never used markdown; ensure the fix does
    not start sending an unsupported payload shape to it.
    """
    token_resp = _make_resp(200, {"access_token": "***"})
    channel_resp = _make_resp(200, {"id": "chan-1"})

    client_ctx, client = _httpx_client_with_responses([token_resp, channel_resp])
    with patch("httpx.AsyncClient", return_value=client_ctx):
        result = _call(_make_pconfig(markdown_support=True))

    assert result["success"] is True
    channel_call = client.post.await_args_list[1]
    assert channel_call.args[0] == "https://api.sgroup.qq.com/channels/openid-1/messages"
    sent = channel_call.kwargs["json"]
    assert "markdown" not in sent
    assert "msg_seq" not in sent
    assert sent["content"].startswith("| col1 | col2 |")