"""Tests for the delivery routing module."""

import pytest
from typing import Any, cast

from gateway.config import GatewayConfig, HomeChannel, Platform, PlatformConfig
from gateway.delivery import DeliveryRouter, DeliveryTarget
from gateway.platforms.base import SendResult
from gateway.relay.adapter import RelayAdapter
from gateway.relay.descriptor import CONTRACT_VERSION, CapabilityDescriptor
from gateway.session import SessionSource


class TestParseTargetPlatformChat:
    def test_explicit_telegram_chat(self):
        target = DeliveryTarget.parse("telegram:12345")
        assert target.platform == Platform.TELEGRAM
        assert target.chat_id == "12345"
        assert target.is_explicit is True


    def test_origin_with_source(self):
        origin = SessionSource(platform=Platform.TELEGRAM, chat_id="789", thread_id="42")
        target = DeliveryTarget.parse("origin", origin=origin)
        assert target.platform == Platform.TELEGRAM
        assert target.chat_id == "789"
        assert target.thread_id == "42"
        assert target.is_origin is True


class TestTargetToStringRoundtrip:
    def test_origin_roundtrip(self):
        origin = SessionSource(platform=Platform.TELEGRAM, chat_id="111", thread_id="42")
        target = DeliveryTarget.parse("origin", origin=origin)
        assert target.to_string() == "origin"


class TestCaseSensitiveChatIdParsing:
    """Test that chat IDs preserve their original case (issue #11768)."""
    
    def test_slack_uppercase_chat_id_preserved(self):
        """Slack channel IDs like C123ABC should preserve case."""
        target = DeliveryTarget.parse("slack:C123ABC")
        assert target.platform == Platform.SLACK
        assert target.chat_id == "C123ABC"  # Should NOT be lowercased to c123abc
        assert target.is_explicit is True
    
    
    


class TestPlatformNameCaseInsensitivity:
    """Test that platform names are case-insensitive."""
    
    def test_uppercase_platform_name(self):
        """Platform names should be case-insensitive."""
        target = DeliveryTarget.parse("TELEGRAM:12345")
        assert target.platform == Platform.TELEGRAM
        assert target.chat_id == "12345"
    

class _RelayDeliveryTransport:
    """Relay transport that advertises Slack and records outbound wire frames."""

    def __init__(self):
        self._identities = [("slack", "bot-1")]
        self.sent = []

    async def send_outbound(self, action, *, platform=None):
        self.sent.append((action, platform))
        if not action.get("metadata", {}).get("user_id"):
            return {"success": False, "error": "target not routed to an onboarded tenant"}
        return {"success": True, "message_id": "relay-message-1"}


def _make_relay(transport):
    return RelayAdapter(
        PlatformConfig(enabled=True),
        CapabilityDescriptor(
            contract_version=CONTRACT_VERSION,
            platform="slack",
            label="Slack",
            max_message_length=4000,
            supports_draft_streaming=False,
            supports_edit=True,
            supports_threads=True,
            markdown_dialect="slack",
            len_unit="chars",
        ),
        transport=cast(Any, transport),
    )


@pytest.mark.asyncio
async def test_relay_fronted_target_delivers_without_prior_inbound_chat_state(tmp_path, monkeypatch):
    """A persisted Slack home must work immediately after a gateway restart."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    transport = _RelayDeliveryTransport()
    relay = _make_relay(transport)
    config = GatewayConfig(
        platforms={
            Platform.RELAY: PlatformConfig(enabled=True),
            Platform.SLACK: PlatformConfig(
                enabled=False,
                home_channel=HomeChannel(
                    platform=Platform.SLACK,
                    chat_id="D123",
                    name="Owner DM",
                    user_id="U123",
                ),
            ),
        },
    )
    router = DeliveryRouter(config, adapters={Platform.RELAY: relay})

    result = await router._deliver_to_platform(
        DeliveryTarget(platform=Platform.SLACK, chat_id="D123"),
        "scheduled result",
        metadata={"job_id": "cron-1", "user_id": "stale-user"},
    )

    assert getattr(result, "success", False) is True
    assert len(transport.sent) == 1
    action, wire_platform = transport.sent[0]
    assert wire_platform == "slack"
    assert action["chat_id"] == "D123"
    assert action["metadata"] == {"job_id": "cron-1", "user_id": "U123"}


class RecordingAdapter:
    def __init__(self):
        self.calls = []
        self.ensure_dm_topic_calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return {"success": True}

    async def ensure_dm_topic(self, chat_id, topic_name, force_create=False):
        self.ensure_dm_topic_calls.append(
            {"chat_id": chat_id, "topic_name": topic_name, "force_create": force_create}
        )
        return "38049"


@pytest.mark.asyncio
async def test_native_adapter_wins_when_relay_also_fronts_platform(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    native = RecordingAdapter()
    transport = _RelayDeliveryTransport()
    relay = _make_relay(transport)
    config = GatewayConfig(
        platforms={
            Platform.SLACK: PlatformConfig(enabled=True),
            Platform.RELAY: PlatformConfig(enabled=True),
        },
    )
    router = DeliveryRouter(
        config,
        adapters={Platform.SLACK: native, Platform.RELAY: relay},
    )

    await router._deliver_to_platform(
        DeliveryTarget(platform=Platform.SLACK, chat_id="D123"),
        "native result",
        metadata=None,
    )

    assert native.calls == [
        {"chat_id": "D123", "content": "native result", "metadata": None}
    ]
    assert transport.sent == []


@pytest.mark.asyncio
async def test_disabled_native_adapter_does_not_shadow_relay(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    native = RecordingAdapter()
    transport = _RelayDeliveryTransport()
    relay = _make_relay(transport)
    config = GatewayConfig(
        platforms={
            Platform.SLACK: PlatformConfig(
                enabled=False,
                home_channel=HomeChannel(
                    platform=Platform.SLACK,
                    chat_id="D123",
                    name="Owner DM",
                    user_id="U123",
                ),
            ),
            Platform.RELAY: PlatformConfig(enabled=True),
        },
    )
    router = DeliveryRouter(
        config,
        adapters={Platform.SLACK: native, Platform.RELAY: relay},
    )

    await router._deliver_to_platform(
        DeliveryTarget(platform=Platform.SLACK, chat_id="D123"),
        "relay result",
        metadata=None,
    )

    assert native.calls == []
    assert len(transport.sent) == 1
    assert transport.sent[0][1] == "slack"


class StaleTopicAdapter:
    def __init__(self):
        self.calls = []
        self.ensure_dm_topic_calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": dict(metadata or {})})
        if len(self.calls) == 1:
            return SendResult(success=False, error="Bad Request: message thread not found")
        return SendResult(success=True, message_id="fresh-message")

    async def ensure_dm_topic(self, chat_id, topic_name, force_create=False):
        self.ensure_dm_topic_calls.append(
            {"chat_id": chat_id, "topic_name": topic_name, "force_create": force_create}
        )
        return "38064" if force_create else "32343"


@pytest.mark.asyncio
async def test_named_telegram_private_topic_is_created_before_delivery(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram:722341991:Hermes API Test")

    await router._deliver_to_platform(target, "hello", metadata=None)

    assert adapter.ensure_dm_topic_calls == [
        {"chat_id": "722341991", "topic_name": "Hermes API Test", "force_create": False}
    ]
    assert adapter.calls == [
        {
            "chat_id": "722341991",
            "content": "hello",
            "metadata": {
                "thread_id": "38049",
                "telegram_dm_topic_created_for_send": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_explicit_telegram_private_thread_uses_reply_fallback_with_anchor(tmp_path, monkeypatch):
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram:722341991:32344")

    await router._deliver_to_platform(
        target,
        "hello",
        metadata={"telegram_reply_to_message_id": "9001"},
    )

    assert adapter.calls == [
        {
            "chat_id": "722341991",
            "content": "hello",
            "metadata": {
                "telegram_reply_to_message_id": "9001",
                "thread_id": "32344",
                "telegram_dm_topic_reply_fallback": True,
            },
        }
    ]


class FailingAdapter:
    async def send(self, chat_id, content, metadata=None):
        return SendResult(success=False, error="route failed", retryable=False)


# ---------------------------------------------------------------------------
# Cron output truncation / adapter-aware chunking (issue #50126)
# ---------------------------------------------------------------------------

class ChunkingAdapter:
    """Adapter that declares splits_long_messages=True (like Discord/Telegram)."""
    splits_long_messages = True

    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return {"success": True}


class NonChunkingAdapter:
    """Adapter without splits_long_messages (default False — legacy behavior)."""

    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return {"success": True}


@pytest.mark.asyncio
async def test_long_output_truncated_for_non_chunking_adapter(tmp_path, monkeypatch):
    """Non-chunking adapters receive truncated content with a footer + file save."""
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = NonChunkingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.DISCORD: adapter})
    target = DeliveryTarget.parse("discord:123")

    long_content = "x" * 5000
    await router._deliver_to_platform(target, long_content, metadata={"job_id": "job1"})

    delivered = adapter.calls[0]["content"]
    assert len(delivered) < 5000  # was truncated
    assert "truncated" in delivered.lower()
    assert "full output saved to" in delivered
    # Full output was saved to disk
    saved_files = list(tmp_path.glob("cron/output/job1_*.txt"))
    assert len(saved_files) == 1
    assert saved_files[0].read_text() == long_content


# ---------------------------------------------------------------------------
# Bare platform targets resolve configured home channels (issue #13704 / #75066)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bare_platform_target_resolves_configured_home_channel(tmp_path, monkeypatch):
    """Issue #13704 / #75066: ``DeliveryTarget.parse("telegram")`` must route to
    the configured home channel rather than raising "No chat ID".

    Regression: the parser correctly produces ``chat_id=None`` to mean
    "home channel", but ``_deliver_to_platform`` raised before resolving
    that intent. Verify the fix routes the send through the home channel.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="home123",
                    name="Home",
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram")

    result = await router.deliver("hello", [target])

    # After bare-platform resolution, target.chat_id is rewritten to the home
    # channel's chat_id, so target.to_string() (which keys results) reads
    # "telegram:home123".
    assert result == {"telegram:home123": {"success": True, "result": {"success": True}}}
    assert adapter.calls == [{"chat_id": "home123", "content": "hello", "metadata": None}]


@pytest.mark.asyncio
async def test_bare_platform_target_propagates_home_channel_thread_id(tmp_path, monkeypatch):
    """Bare-platform resolution must also propagate ``thread_id`` from the
    configured home channel so messages land in the right conversation.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.DISCORD,
                    chat_id="home-discord",
                    name="Home",
                    thread_id="thread-99",
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.DISCORD: adapter})
    target = DeliveryTarget.parse("discord")

    await router._deliver_to_platform(target, "hi", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["chat_id"] == "home-discord"
    assert adapter.calls[0]["metadata"] == {"thread_id": "thread-99"}


@pytest.mark.asyncio
async def test_bare_platform_target_without_home_channel_still_raises(tmp_path, monkeypatch):
    """When no home_channel is configured for the platform, preserve the
    legacy error message verbatim so existing callers don't see a silent
    behavior change.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.SLACK: adapter})
    target = DeliveryTarget.parse("slack")

    result = await router.deliver("hello", [target])

    assert result == {
        "slack": {"success": False, "error": "No chat ID for slack delivery"}
    }
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_explicit_platform_chat_id_overrides_home_channel(tmp_path, monkeypatch):
    """Explicit ``telegram:123`` targets must continue to use the explicit
    chat_id, never the configured home channel (sanity guard).
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="home123",
                    name="Home",
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram:987")

    await router._deliver_to_platform(target, "explicit", metadata=None)

    assert adapter.calls == [{"chat_id": "987", "content": "explicit", "metadata": None}]


@pytest.mark.asyncio
async def test_explicit_telegram_thread_preserves_behavior_with_anchor(tmp_path, monkeypatch):
    """Explicit ``telegram:722341991:32344`` with a reply anchor must still
    produce ``telegram_dm_topic_reply_fallback`` without direct_messages_topic_id
    (the existing behavior is unchanged).
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram:722341991:32344")

    await router._deliver_to_platform(
        target,
        "hello",
        metadata={"telegram_reply_to_message_id": "9001"},
    )

    assert adapter.calls == [
        {
            "chat_id": "722341991",
            "content": "hello",
            "metadata": {
                "telegram_reply_to_message_id": "9001",
                "thread_id": "32344",
                "telegram_dm_topic_reply_fallback": True,
            },
        }
    ]
    # When a reply anchor is present, direct_messages_topic_id must NOT be set.
    assert "direct_messages_topic_id" not in adapter.calls[0]["metadata"]


# ---------------------------------------------------------------------------
# Telegram DM-topic home channel: adapter-aware metadata contract
# ---------------------------------------------------------------------------


class DmTopicAdapter(RecordingAdapter):
    """Recording adapter with the real adapter instance-method contract."""

    def _get_dm_topic_info(self, chat_id, thread_id):
        # Return a dict for the specific chat/thread pair used in tests below.
        if chat_id == "722341991" and thread_id == "45036":
            return {"name": "Operator DM Topic"}
        return None


@pytest.mark.asyncio
async def test_telegram_dm_topic_home_routes_with_direct_topic_id(tmp_path, monkeypatch):
    """A bare Telegram target with a home channel that carries a numeric
    thread_id in a private chat must produce metadata with both
    ``telegram_dm_topic_reply_fallback`` and ``direct_messages_topic_id``
    so the Bot API places the message in the correct topic lane without
    a reply anchor.

    This is the core regression for issue #13704 / #75066: generic
    ``metadata['thread_id']`` was insufficient for DM-topic home delivery.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = DmTopicAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="722341991",  # positive int = private chat
                    name="Ops Home",
                    thread_id="45036",  # numeric DM-topic thread id
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram")

    await router._deliver_to_platform(target, "cron output", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["chat_id"] == "722341991"
    assert adapter.calls[0]["metadata"] == {
        "thread_id": "45036",
        "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "45036",
    }


@pytest.mark.asyncio
async def test_telegram_dm_topic_home_no_false_flags_for_group(tmp_path, monkeypatch):
    """When the home channel's chat_id is a group (negative int), the DM-topic
    flags must NOT be added — only plain thread_id metadata. Groups use supergroup/
    forum topics, not DM topics.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="-1001234567890",  # negative = group
                    name="Group Home",
                    thread_id="55",  # forum topic id
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram")

    await router._deliver_to_platform(target, "group message", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["chat_id"] == "-1001234567890"
    assert adapter.calls[0]["metadata"] == {"thread_id": "55"}
    # DM-topic flags must NOT leak into group delivery.
    assert "telegram_dm_topic_reply_fallback" not in adapter.calls[0]["metadata"]
    assert "direct_messages_topic_id" not in adapter.calls[0]["metadata"]


@pytest.mark.asyncio
async def test_telegram_dm_topic_home_no_false_flags_for_non_telegram(tmp_path, monkeypatch):
    """Discord threads with a home channel thread_id must receive plain
    thread_id metadata — never the Telegram-specific DM-topic flags.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.DISCORD: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.DISCORD,
                    chat_id="discord-chan",
                    name="Discord Home",
                    thread_id="thread-42",
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.DISCORD: adapter})
    target = DeliveryTarget.parse("discord")

    await router._deliver_to_platform(target, "discord msg", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["metadata"] == {"thread_id": "thread-42"}
    assert "telegram_dm_topic_reply_fallback" not in adapter.calls[0]["metadata"]
    assert "direct_messages_topic_id" not in adapter.calls[0]["metadata"]


@pytest.mark.asyncio
async def test_telegram_dm_topic_explicit_target_uses_direct_topic_id_without_anchor(tmp_path, monkeypatch):
    """An explicit ``telegram:chat:thread`` with a numeric thread_id in a
    private chat but no reply anchor must use ``direct_messages_topic_id``
    instead of raising. This covers synthetic/resumed sends where a reply
    anchor is unavailable.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = DmTopicAdapter()
    router = DeliveryRouter(GatewayConfig(), adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram:722341991:45036")

    await router._deliver_to_platform(target, "synthetic send", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["chat_id"] == "722341991"
    assert adapter.calls[0]["metadata"] == {
        "thread_id": "45036",
        "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "45036",
    }


@pytest.mark.asyncio
async def test_named_telegram_private_topic_home_is_created_before_delivery(tmp_path, monkeypatch):
    """A bare Telegram target with a home channel that has a named (non-numeric)
    thread_id must create the DM topic via ensure_dm_topic and then route
    with the created thread_id.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = RecordingAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="722341991",
                    name="Named Home",
                    thread_id="My Cron Topic",  # named, not numeric
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram")

    await router._deliver_to_platform(target, "cron named", metadata=None)

    assert adapter.ensure_dm_topic_calls == [
        {"chat_id": "722341991", "topic_name": "My Cron Topic", "force_create": False}
    ]
    assert adapter.calls == [
        {
            "chat_id": "722341991",
            "content": "cron named",
            "metadata": {
                "thread_id": "38049",
                "telegram_dm_topic_created_for_send": True,
            },
        }
    ]


# ---------------------------------------------------------------------------
# Adapter-aware DM-topic detection via _get_dm_topic_info
# ---------------------------------------------------------------------------


class NonDmTopicAdapter:
    """Adapter that returns None from _get_dm_topic_info (no operator-declared
    DM topic). The heuristic (positive chat_id) still applies, but the adapter
    itself does not confirm the topic."""

    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return {"success": True}

    def _get_dm_topic_info(self, chat_id, thread_id):
        return None  # adapter does not know about this topic


@pytest.mark.asyncio
async def test_adapter_aware_dm_topic_falls_back_to_heuristic(tmp_path, monkeypatch):
    """When the adapter's _get_dm_topic_info returns None, the heuristic
    (positive chat_id) still treats the thread as a DM-topic lane.
    User-created topics that aren't in operator config are covered.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = NonDmTopicAdapter()
    cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="722341991",
                    name="User Topic Home",
                    thread_id="99999",
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram")

    await router._deliver_to_platform(target, "user topic", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["metadata"] == {
        "thread_id": "99999",
        "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "99999",
    }


class MockAdapterNoDmTopicInfo:
    """Adapter without _get_dm_topic_info (like a MagicMock or non-Telegram
    adapter). The check must not crash on missing attribute."""

    def __init__(self):
        self.calls = []

    async def send(self, chat_id, content, metadata=None):
        self.calls.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return {"success": True}


@pytest.mark.asyncio
async def test_adapter_without_get_dm_topic_info_does_not_crash(tmp_path, monkeypatch):
    """An adapter without _get_dm_topic_info must not cause an AttributeError.
    The heuristic-based detection still applies.
    """
    monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
    adapter = MockAdapterNoDmTopicInfo()
    cfg = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(
                enabled=True,
                token="x",
                home_channel=HomeChannel(
                    platform=Platform.TELEGRAM,
                    chat_id="722341991",
                    name="NoDM Home",
                    thread_id="45036",
                ),
            )
        }
    )
    router = DeliveryRouter(cfg, adapters={Platform.TELEGRAM: adapter})
    target = DeliveryTarget.parse("telegram")

    await router._deliver_to_platform(target, "no crash", metadata=None)

    assert len(adapter.calls) == 1
    assert adapter.calls[0]["metadata"] == {
        "thread_id": "45036",
        "telegram_dm_topic_reply_fallback": True,
        "direct_messages_topic_id": "45036",
    }