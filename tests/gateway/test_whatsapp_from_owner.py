"""Tests for WhatsApp owner-message metadata and source-level text tagging.

The Node bridge sets ``fromOwner: true`` on inbound `fromMe` messages that
look owner-typed (linked-device send, not echoed from /send) when the
operator opts into ``WHATSAPP_FORWARD_OWNER_MESSAGES``.  These tests pin
the adapter's responsibility: lift that flag onto
``MessageEvent.metadata["whatsapp_from_owner"]``, prefix ``MessageEvent.text``
with ``[owner reply] ``, and otherwise leave metadata absent and text
unchanged.  The env-var gate itself lives in the bridge — the adapter just
trusts the payload.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig, load_gateway_config
from plugins.platforms.whatsapp.adapter import WhatsAppAdapter, _apply_yaml_config


@pytest.fixture(autouse=True)
def _whatsapp_open_optin(monkeypatch):
    """Opt into WhatsApp allow-all so ``dm_policy: open`` dispatch tests run.

    The adapter fails closed on ``open`` without an allow-all opt-in
    (SECURITY.md 2.6); these owner-DM tests set ``_dm_policy = "open"``.
    """
    monkeypatch.setenv("WHATSAPP_ALLOW_ALL_USERS", "true")


def _make_adapter():
    adapter = WhatsAppAdapter.__new__(WhatsAppAdapter)
    adapter.platform = Platform.WHATSAPP
    adapter.config = PlatformConfig(enabled=True)
    adapter._message_handler = AsyncMock()
    adapter._dm_policy = "open"
    adapter._allow_from = set()
    adapter._group_policy = "open"
    adapter._group_allow_from = set()
    adapter._mention_patterns = []
    adapter._free_response_chats = set()
    adapter._whatsapp_free_response_chats = lambda: set()
    return adapter


def _dm_payload(**overrides):
    payload = {
        "messageId": "M1",
        "chatId": "6281234567890@s.whatsapp.net",
        "senderId": "6281234567890@s.whatsapp.net",
        "senderName": "Customer",
        "chatName": "Customer",
        "isGroup": False,
        "body": "hi from the linked phone",
        "hasMedia": False,
        "mediaType": "",
        "mediaUrls": [],
        "mentionedIds": [],
        "quotedParticipant": "",
        "botIds": [],
        "timestamp": 0,
    }
    payload.update(overrides)
    return payload


def test_metadata_flag_set_when_payload_has_from_owner():
    adapter = _make_adapter()
    payload = _dm_payload(fromOwner=True)

    event = asyncio.run(adapter._build_message_event(payload))

    assert event is not None
    assert event.metadata.get("whatsapp_from_owner") is True
    assert event.text.startswith("[owner reply] ")
    assert event.text == "[owner reply] hi from the linked phone"


def test_from_owner_does_not_double_prefix_when_already_tagged():
    adapter = _make_adapter()
    payload = _dm_payload(
        fromOwner=True,
        body="[owner reply] already tagged",
    )

    event = asyncio.run(adapter._build_message_event(payload))

    assert event is not None
    assert event.metadata.get("whatsapp_from_owner") is True
    assert event.text == "[owner reply] already tagged"


def _oversight_adapter(*, home="15550001111", respond_as_owner=False):
    adapter = _make_adapter()
    adapter._oversight_mode = True
    adapter._oversight_home_channel = home
    adapter._respond_as_owner = respond_as_owner
    adapter._running = True
    adapter._http_session = MagicMock()
    return adapter


def test_oversight_blocks_owner_forwarded_contact_reply_before_transport():
    adapter = _oversight_adapter()

    result = asyncio.run(
        adapter.send("15550002222@s.whatsapp.net", "busy/system/agent reply")
    )

    assert result.success is True
    assert result.raw_response == {
        "suppressed": True,
        "reason": "oversight_outbound_policy",
    }
    adapter._http_session.post.assert_not_called()


def test_oversight_allows_home_chat_with_equivalent_jid_shape():
    adapter = _oversight_adapter(home="+1 (555) 000-1111")

    assert adapter._oversight_allows_outbound("15550001111@s.whatsapp.net") is True


def test_oversight_without_home_channel_fails_closed():
    adapter = _oversight_adapter(home="")

    assert adapter._oversight_allows_outbound("15550002222@s.whatsapp.net") is False


def test_oversight_explicit_respond_as_owner_allows_contact_outbound():
    adapter = _oversight_adapter(respond_as_owner=True)

    assert adapter._oversight_allows_outbound("15550002222@s.whatsapp.net") is True


def test_oversight_yaml_options_seed_adapter_extra(monkeypatch):
    for name in (
        "WHATSAPP_REQUIRE_MENTION",
        "WHATSAPP_MENTION_PATTERNS",
        "WHATSAPP_FREE_RESPONSE_CHATS",
        "WHATSAPP_DM_POLICY",
        "WHATSAPP_ALLOWED_USERS",
        "WHATSAPP_GROUP_POLICY",
        "WHATSAPP_GROUP_ALLOWED_USERS",
    ):
        monkeypatch.delenv(name, raising=False)

    seeded = _apply_yaml_config(
        {},
        {
            "oversight_mode": True,
            "respond_as_owner": False,
            "forward_owner_messages": True,
        },
    )

    assert seeded == {
        "oversight_mode": True,
        "respond_as_owner": False,
        "forward_owner_messages": True,
    }


def test_documented_oversight_yaml_shape_loads_home_and_policy(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in tuple(os.environ):
        if name.startswith("WHATSAPP_"):
            monkeypatch.delenv(name, raising=False)
    (tmp_path / "config.yaml").write_text(
        """whatsapp:
  enabled: true
  oversight_mode: true
  respond_as_owner: false
  forward_owner_messages: true
  home_channel:
    chat_id: "15550001111@s.whatsapp.net"
    name: "Owner"
""",
        encoding="utf-8",
    )

    config = load_gateway_config().platforms[Platform.WHATSAPP]

    assert config.home_channel is not None
    assert config.home_channel.chat_id == "15550001111@s.whatsapp.net"
    assert config.extra["oversight_mode"] is True
    assert config.extra["respond_as_owner"] is False
    assert config.extra["forward_owner_messages"] is True


def test_home_channel_only_does_not_enable_whatsapp(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("WHATSAPP_ENABLED", raising=False)
    (tmp_path / "config.yaml").write_text(
        """whatsapp:
  home_channel:
    chat_id: "15550001111@s.whatsapp.net"
""",
        encoding="utf-8",
    )

    config = load_gateway_config().platforms[Platform.WHATSAPP]

    assert config.enabled is False
    assert config.home_channel is not None
    assert config.home_channel.chat_id == "15550001111@s.whatsapp.net"


