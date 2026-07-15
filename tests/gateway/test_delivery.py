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
# Local delivery routes through the central cron output-dir resolver
# ---------------------------------------------------------------------------

class TestDeliverLocalOutputDirNaming:
    """_deliver_local must write into the canonical {name-slug}-{job_id}
    directory via cron.jobs.resolve_job_output_dir — never recreate the plain
    legacy {job_id} directory and split output across two dirs."""

    def _router(self, tmp_path, monkeypatch):
        monkeypatch.setattr("gateway.delivery.get_hermes_home", lambda: tmp_path)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        return DeliveryRouter(GatewayConfig(), adapters={})

    def test_job_output_lands_in_slug_dir_not_legacy_dir(self, tmp_path, monkeypatch):
        router = self._router(tmp_path, monkeypatch)

        result = router._deliver_local("hello", "abc123", "Daily Report", None)

        output_root = tmp_path / "cron" / "output"
        slug_dir = output_root / "daily-report-abc123"
        assert slug_dir.is_dir()
        files = list(slug_dir.glob("*.md"))
        assert len(files) == 1
        assert "hello" in files[0].read_text(encoding="utf-8")
        assert result["path"] == str(files[0])
        # No plain legacy {job_id} dir gets (re)created.
        assert not (output_root / "abc123").exists()

    def test_job_output_migrates_existing_legacy_dir(self, tmp_path, monkeypatch):
        router = self._router(tmp_path, monkeypatch)
        legacy_dir = tmp_path / "cron" / "output" / "abc123"
        legacy_dir.mkdir(parents=True)
        (legacy_dir / "old.md").write_text("old run", encoding="utf-8")

        router._deliver_local("new run", "abc123", "Daily Report", None)

        output_root = tmp_path / "cron" / "output"
        slug_dir = output_root / "daily-report-abc123"
        assert not (output_root / "abc123").exists()  # migrated, not split
        assert (slug_dir / "old.md").read_text(encoding="utf-8") == "old run"
        assert len(list(slug_dir.glob("*.md"))) == 2

    def test_unsafe_job_id_falls_back_to_misc_without_raising(self, tmp_path, monkeypatch):
        router = self._router(tmp_path, monkeypatch)

        result = router._deliver_local("payload", "../escape", "Evil", None)

        misc_files = list((tmp_path / "cron" / "output" / "misc").glob("*.md"))
        assert len(misc_files) == 1
        assert result["path"] == str(misc_files[0])
        # Nothing escaped the output root.
        assert not (tmp_path / "cron" / "escape").exists()
        assert not (tmp_path / "escape").exists()
