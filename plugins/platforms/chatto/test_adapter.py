"""Unit tests for the Chatto platform adapter.

Covers:
  - Protobuf codec (varint, tag, fields, client/server frames, projections)
  - Emoji shortcode conversion
  - Adapter instantiation and properties
  - Registration and requirements
  - Send / reactions / edit / delete (mocked RPC)
  - Typing indicator lifecycle
  - Read state and notifications
  - DM initiation and room creation
  - User lookup (with caching)
  - Presence and custom status
  - Message dispatch (self-echo suppression, handler invocation)
  - Attachment upload (chunked)

All network calls are mocked — no real HTTP or WebSocket connections.
"""

import asyncio
import hashlib
import os
import sys
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch, call
from collections import OrderedDict

import pytest
import pytest_asyncio

# ── Path setup ────────────────────────────────────────────────────────────
sys.path.insert(0, "/opt/hermes")
sys.path.insert(0, "/root/.hermes/plugins/platforms/chatto")

import adapter as chatto_adapter
from adapter import (
    _encode_varint,
    _decode_varint,
    _encode_tag,
    _encode_field_varint,
    _encode_field_bytes,
    _encode_field_string,
    _encode_submessage,
    _decode_fields,
    _get_first,
    _get_all,
    _encode_client_hello,
    _encode_subscribe_events,
    _encode_ping,
    _encode_client_frame_hello,
    _encode_client_frame_subscribe,
    _encode_client_frame_ping,
    _decode_server_frame,
    _decode_projection_event,
    _decode_projection_operation,
    _decode_room_timeline_event,
    _decode_room_timeline_event_upsert,
    _decode_message_posted,
    _decode_message,
    _decode_thread,
    _decode_timestamp,
    _decode_event_envelope,
    _decode_mention_notification,
    _decode_dm_notification,
    _decode_server_hello,
    _decode_error,
    _EMOJI_TO_SHORTCODE,
    _REALTIME_PROTOCOL_VERSION,
    _MAX_MESSAGE_LENGTH,
    _SEEN_CAP,
    ChattoAdapter,
    check_requirements,
    validate_config,
    register,
)

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult, MessageEvent, MessageType


# ── Helpers ───────────────────────────────────────────────────────────────

class _MockPluginContext:
    """Minimal mock for the plugin registration context."""

    def __init__(self):
        self.registered_names = []
        self.registered_kwargs = None

    def register_platform(self, **kwargs):
        from gateway.platform_registry import platform_registry, PlatformEntry

        entry = PlatformEntry(
            name=kwargs["name"],
            label=kwargs.get("label", kwargs["name"]),
            adapter_factory=kwargs.get("adapter_factory"),
            check_fn=kwargs.get("check_fn"),
            validate_config=kwargs.get("validate_config"),
            is_connected=kwargs.get("is_connected"),
            required_env=kwargs.get("required_env", []),
            source="plugin",
        )
        platform_registry.register(entry)
        self.registered_names.append(kwargs["name"])
        self.registered_kwargs = kwargs


def _ensure_chatto_registered():
    """Register chatto in the platform registry so Platform('chatto') works."""
    from gateway.platform_registry import platform_registry

    if not platform_registry.is_registered("chatto"):
        ctx = _MockPluginContext()
        register(ctx)


_CHATTO_ENV_KEYS = [
    "CHATTO_URL",
    "CHATTO_LOGIN",
    "CHATTO_PASSWORD",
    "CHATTO_CHANNELS",
    "CHATTO_HOME_CHANNEL",
    "CHATTO_REQUIRE_MENTION",
    "CHATTO_ALLOWED_USERS",
    "CHATTO_ALLOW_ALL_USERS",
]


def _clear_chatto_env(monkeypatch=None):
    """Remove all CHATTO_* env vars so tests start from a clean slate."""
    for key in _CHATTO_ENV_KEYS:
        if monkeypatch is not None:
            monkeypatch.delenv(key, raising=False)
        else:
            os.environ.pop(key, None)


def _make_config(**extra_overrides):
    """Create a minimal PlatformConfig for testing."""
    _ensure_chatto_registered()
    extra = {"url": "https://chat.example.com", "channels": ["room1"]}
    extra.update(extra_overrides)
    return PlatformConfig(enabled=True, extra=extra)


def _make_adapter(**extra_overrides):
    """Create a ChattoAdapter with mocked config.  Token is pre-set to avoid
    _ensure_token triggering a real login.  All CHATTO_* env vars are cleared
    first so the config.extra values are not overridden by the environment."""
    _clear_chatto_env()
    cfg = _make_config(**extra_overrides)
    adapter = ChattoAdapter(cfg)
    adapter._token = "test-token"
    adapter._user_id = "bot-user-id"
    adapter._user_login = "hermes_bot"
    adapter._user_display = "Hermes Bot"
    return adapter


# ── Protobuf codec: varint ────────────────────────────────────────────────


class TestVarint:
    """Test _encode_varint / _decode_varint roundtrips."""

    @pytest.mark.parametrize(
        "value",
        [0, 1, 127, 128, 16384, 2**32, 2**63 - 1],
    )
    def test_varint_roundtrip(self, value):
        encoded = _encode_varint(value)
        decoded, offset = _decode_varint(encoded, 0)
        assert decoded == value
        assert offset == len(encoded)

    def test_varint_zero(self):
        assert _encode_varint(0) == b"\x00"

    def test_varint_one(self):
        assert _encode_varint(1) == b"\x01"

    def test_varint_127(self):
        assert _encode_varint(127) == b"\x7f"

    def test_varint_128(self):
        assert _encode_varint(128) == b"\x80\x01"

    def test_varint_300(self):
        # 300 = 0b100101100 → 0xAC 0x02
        assert _encode_varint(300) == b"\xac\x02"

    def test_varint_16384(self):
        # 16384 = 0x4000 → 0x80 0x80 0x01
        assert _encode_varint(16384) == b"\x80\x80\x01"

    def test_decode_varint_truncated(self):
        with pytest.raises(ValueError, match="Truncated"):
            _decode_varint(b"\x80", 0)

    def test_decode_varint_too_long(self):
        # 10 continuation bytes — exceeds 64-bit
        with pytest.raises(ValueError, match="too long"):
            _decode_varint(b"\x80" * 10, 0)


# ── Protobuf codec: tag and field encoders ────────────────────────────────


class TestTagAndFields:
    """Test tag encoding and field-level helpers."""

    def test_encode_tag_field1_varint(self):
        # field 1, wire type 0 → (1<<3)|0 = 8 → 0x08
        assert _encode_tag(1, 0) == b"\x08"

    def test_encode_tag_field2_length_delimited(self):
        # field 2, wire type 2 → (2<<3)|2 = 18 → 0x12
        assert _encode_tag(2, 2) == b"\x12"

    def test_encode_tag_field15_varint(self):
        # field 15, wire type 0 → (15<<3)|0 = 120 → 0x78
        assert _encode_tag(15, 0) == b"\x78"

    def test_encode_field_varint(self):
        result = _encode_field_varint(1, 150)
        # tag(1,0)=0x08 + varint(150)=0x96 0x01
        assert result == b"\x08\x96\x01"

    def test_encode_field_bytes(self):
        result = _encode_field_bytes(2, b"hello")
        # tag(2,2)=0x12 + len(5)=0x05 + "hello"
        assert result == b"\x12\x05hello"

    def test_encode_field_string(self):
        result = _encode_field_string(3, "hi")
        # tag(3,2)=0x1a + len(2)=0x02 + "hi"
        assert result == b"\x1a\x02hi"

    def test_encode_submessage(self):
        inner = _encode_field_varint(1, 42)
        result = _encode_submessage(5, inner)
        # The submessage is length-delimited
        fields = _decode_fields(result)
        assert 5 in fields
        assert isinstance(fields[5][0], bytes)

    def test_decode_fields_varint(self):
        data = _encode_field_varint(1, 42)
        fields = _decode_fields(data)
        assert fields[1] == [42]

    def test_decode_fields_bytes(self):
        data = _encode_field_bytes(2, b"test")
        fields = _decode_fields(data)
        assert fields[2] == [b"test"]

    def test_decode_fields_string(self):
        data = _encode_field_string(3, "hello")
        fields = _decode_fields(data)
        assert fields[3] == [b"hello"]

    def test_decode_fields_multiple(self):
        data = _encode_field_varint(1, 10) + _encode_field_string(2, "abc")
        fields = _decode_fields(data)
        assert fields[1] == [10]
        assert fields[2] == [b"abc"]

    def test_decode_fields_repeated(self):
        data = _encode_field_string(2, "a") + _encode_field_string(2, "b")
        fields = _decode_fields(data)
        assert fields[2] == [b"a", b"b"]

    def test_decode_fields_empty(self):
        fields = _decode_fields(b"")
        assert fields == {}

    def test_get_first(self):
        fields = {1: [10, 20], 2: [b"x"]}
        assert _get_first(fields, 1) == 10
        assert _get_first(fields, 2) == b"x"
        assert _get_first(fields, 99, "default") == "default"

    def test_get_all(self):
        fields = {1: [10, 20]}
        assert _get_all(fields, 1) == [10, 20]
        assert _get_all(fields, 99) == []


# ── Protobuf codec: client hello / subscribe / ping ──────────────────────


class TestClientFrames:
    """Test client-side protobuf message encoders."""

    def test_encode_client_hello_with_token(self):
        msg = _encode_client_hello("my-bearer-token")
        fields = _decode_fields(msg)
        # field 1 = protocol_version (varint)
        assert _get_first(fields, 1) == _REALTIME_PROTOCOL_VERSION
        # field 2 = bearer_token (bytes)
        token_val = _get_first(fields, 2)
        assert isinstance(token_val, bytes)
        assert token_val.decode("utf-8") == "my-bearer-token"

    def test_encode_client_hello_without_token(self):
        msg = _encode_client_hello("")
        fields = _decode_fields(msg)
        assert _get_first(fields, 1) == _REALTIME_PROTOCOL_VERSION
        assert 2 not in fields  # no bearer_token field

    def test_encode_client_hello_protocol_version_is_1(self):
        msg = _encode_client_hello("x")
        fields = _decode_fields(msg)
        assert _get_first(fields, 1) == 1

    def test_encode_subscribe_events_with_rooms(self):
        msg = _encode_subscribe_events(retained_room_ids=["room1", "room2"])
        fields = _decode_fields(msg)
        # field 2 = repeated string
        room_vals = _get_all(fields, 2)
        assert len(room_vals) == 2
        assert room_vals[0].decode("utf-8") == "room1"
        assert room_vals[1].decode("utf-8") == "room2"

    def test_encode_subscribe_events_with_cursor(self):
        msg = _encode_subscribe_events(resume_cursor="cursor123")
        fields = _decode_fields(msg)
        cursor = _get_first(fields, 1)
        assert isinstance(cursor, bytes)
        assert cursor.decode("utf-8") == "cursor123"

    def test_encode_subscribe_events_empty(self):
        msg = _encode_subscribe_events()
        assert msg == b""

    def test_encode_ping_is_empty(self):
        assert _encode_ping() == b""

    def test_encode_client_frame_hello(self):
        hello = _encode_client_hello("token")
        frame = _encode_client_frame_hello(hello)
        fields = _decode_fields(frame)
        # field 1 = hello submessage
        assert 1 in fields
        inner = _get_first(fields, 1)
        assert isinstance(inner, bytes)
        # Decode inner to verify
        inner_fields = _decode_fields(inner)
        assert _get_first(inner_fields, 1) == _REALTIME_PROTOCOL_VERSION

    def test_encode_client_frame_subscribe(self):
        sub = _encode_subscribe_events(retained_room_ids=["r1"])
        frame = _encode_client_frame_subscribe(sub)
        fields = _decode_fields(frame)
        # field 2 = subscribe_events submessage
        assert 2 in fields

    def test_encode_client_frame_ping(self):
        ping = _encode_ping()
        frame = _encode_client_frame_ping(ping)
        fields = _decode_fields(frame)
        # field 3 = ping submessage (empty)
        assert 3 in fields

    def test_client_hello_roundtrip(self):
        """Encode a client hello, wrap in a frame, decode the frame, decode
        the inner hello, and verify values match."""
        original = _encode_client_hello("roundtrip-token")
        frame = _encode_client_frame_hello(original)
        decoded_frame = _decode_server_frame  # not for client frames, but
        # We decode the frame manually
        frame_fields = _decode_fields(frame)
        inner_bytes = _get_first(frame_fields, 1)
        inner_fields = _decode_fields(inner_bytes)
        assert _get_first(inner_fields, 1) == _REALTIME_PROTOCOL_VERSION
        token_raw = _get_first(inner_fields, 2)
        assert token_raw.decode("utf-8") == "roundtrip-token"


# ── Protobuf codec: server frame decoding ────────────────────────────────


class TestServerFrameDecoding:
    """Test _decode_server_frame with synthetic frames."""

    def test_decode_server_hello_frame(self):
        # Build a RealtimeServerHello { protocol_version = 1 }
        hello_inner = _encode_field_varint(1, 1)
        # Wrap in RealtimeServerFrame { hello = 1 }
        frame = _encode_submessage(1, hello_inner)
        result = _decode_server_frame(frame)
        assert result["type"] == "hello"
        assert isinstance(result["data"], bytes)

        # Decode the hello data
        hello = _decode_server_hello(result["data"])
        assert hello["protocolVersion"] == 1

    def test_decode_subscribed_frame(self):
        # Build an empty submessage for field 2 (subscribed)
        frame = _encode_submessage(2, b"")
        result = _decode_server_frame(frame)
        assert result["type"] == "subscribed"

    def test_decode_error_frame(self):
        # Build RealtimeError { message = "bad", code = 500 }
        error_inner = _encode_field_string(1, "bad") + _encode_field_varint(2, 500)
        # Wrap in RealtimeServerFrame { error = 5 }
        frame = _encode_submessage(5, error_inner)
        result = _decode_server_frame(frame)
        assert result["type"] == "error"
        decoded = _decode_error(result["data"])
        assert decoded["message"] == "bad"
        assert decoded["code"] == 500

    def test_decode_pong_frame(self):
        frame = _encode_submessage(7, b"")
        result = _decode_server_frame(frame)
        assert result["type"] == "pong"

    def test_decode_heartbeat_frame(self):
        frame = _encode_submessage(4, b"")
        result = _decode_server_frame(frame)
        assert result["type"] == "heartbeat"

    def test_decode_close_frame(self):
        close_inner = _encode_field_string(1, "bye")
        frame = _encode_submessage(6, close_inner)
        result = _decode_server_frame(frame)
        assert result["type"] == "close"

    def test_decode_caught_up_frame(self):
        frame = _encode_submessage(8, b"")
        result = _decode_server_frame(frame)
        assert result["type"] == "caught_up"

    def test_decode_unknown_frame(self):
        # Empty frame → no fields → unknown
        result = _decode_server_frame(b"")
        assert result["type"] == "unknown"
        assert result["data"] is None


# ── Protobuf codec: projection event decoding ────────────────────────────


class TestProjectionEventDecoding:
    """Test _decode_projection_event and related decoders."""

    def test_decode_projection_event_basic(self):
        # Build RealtimeProjectionEvent {
        #   id = "evt1",
        #   actor_id = "user1",
        #   resume_cursor = "cursor1"
        # }
        proj = (
            _encode_field_string(1, "evt1")
            + _encode_field_string(3, "user1")
            + _encode_field_string(4, "cursor1")
        )
        result = _decode_projection_event(proj)
        assert result["id"] == "evt1"
        assert result["actor_id"] == "user1"
        assert result["resume_cursor"] == "cursor1"
        assert result["operations"] == []

    def test_decode_projection_event_with_timestamp(self):
        ts_inner = _encode_field_varint(1, 1700000000)  # seconds
        proj = (
            _encode_field_string(1, "evt2")
            + _encode_submessage(2, ts_inner)
        )
        result = _decode_projection_event(proj)
        assert result["id"] == "evt2"
        assert "1700000000" not in result["created_at"]  # should be ISO format
        assert "T" in result["created_at"]  # ISO format has T separator

    def test_decode_projection_event_with_operation(self):
        # Build a room_timeline_event_upsert operation
        room_id = _encode_field_string(1, "room123")
        # Build a minimal RoomTimelineEvent
        event_inner = _encode_field_string(1, "evt456")
        event_upsert = room_id + _encode_submessage(2, event_inner)
        # Wrap in RealtimeProjectionOperation { room_timeline_event_upsert = 10 }
        op = _encode_submessage(10, event_upsert)
        # Wrap in RealtimeProjectionEvent { operations = 5 }
        proj = _encode_field_string(1, "proj1") + _encode_submessage(5, op)

        result = _decode_projection_event(proj)
        assert len(result["operations"]) == 1
        op_result = result["operations"][0]
        assert op_result["type"] == "room_timeline_event_upsert"
        assert op_result["room_id"] == "room123"
        assert op_result["event"]["id"] == "evt456"

    def test_decode_projection_operation_unknown(self):
        # An operation with field 1 (room_upsert) — not handled specifically
        op = _encode_submessage(1, _encode_field_string(1, "room1"))
        result = _decode_projection_operation(op)
        assert result["type"] == "room_upsert"

    def test_decode_projection_operation_empty(self):
        result = _decode_projection_operation(b"")
        assert result["type"] == "empty"


# ── Protobuf codec: room timeline event decoding ─────────────────────────


class TestRoomTimelineEventDecoding:
    """Test _decode_room_timeline_event and _decode_message."""

    def test_decode_room_timeline_event_basic(self):
        # Build RoomTimelineEvent {
        #   id = "evt1", room_id = "room1", kind = 1 (message_posted)
        # }
        ts_inner = _encode_field_varint(1, 1700000000)
        event = (
            _encode_field_string(1, "evt1")
            + _encode_submessage(2, ts_inner)
            + _encode_field_string(3, "room1")
            + _encode_field_varint(4, 1)
        )
        result = _decode_room_timeline_event(event)
        assert result["id"] == "evt1"
        assert result["roomId"] == "room1"
        assert result["kind"] == 1
        assert "T" in result["createdAt"]

    def test_decode_room_timeline_event_with_message(self):
        # Build a Message { id="m1", room_id="r1", actor_id="u1", body="hello" }
        ts_inner = _encode_field_varint(1, 1700000000)
        msg = (
            _encode_field_string(1, "m1")
            + _encode_field_string(2, "r1")
            + _encode_field_string(3, "u1")
            + _encode_field_string(4, "hello")
            + _encode_submessage(5, ts_inner)
        )
        # Wrap in MessagePosted { message = 1 }
        posted = _encode_submessage(1, msg)
        # Wrap in RoomTimelineEvent { message_posted = 5 }
        event = (
            _encode_field_string(1, "evt1")
            + _encode_field_string(3, "r1")
            + _encode_submessage(5, posted)
        )
        result = _decode_room_timeline_event(event)
        assert result["id"] == "evt1"
        assert result["messagePosted"] is not None
        assert result["messagePosted"]["message"]["id"] == "m1"
        assert result["messagePosted"]["message"]["body"] == "hello"
        assert result["messagePosted"]["message"]["actorId"] == "u1"

    def test_decode_message_with_thread(self):
        # Build Message with a thread
        thread_inner = _encode_field_string(1, "thread-root-123")
        msg = (
            _encode_field_string(1, "m1")
            + _encode_field_string(2, "r1")
            + _encode_field_string(3, "u1")
            + _encode_field_string(4, "threaded reply")
            + _encode_submessage(8, thread_inner)
        )
        result = _decode_message(msg)
        assert result["id"] == "m1"
        assert result["body"] == "threaded reply"
        assert result["thread"]["threadRootEventId"] == "thread-root-123"

    def test_decode_message_with_login_and_display(self):
        msg = (
            _encode_field_string(1, "m1")
            + _encode_field_string(2, "r1")
            + _encode_field_string(3, "u1")
            + _encode_field_string(4, "hi")
            + _encode_field_string(6, "alice")
            + _encode_field_string(7, "Alice Smith")
        )
        result = _decode_message(msg)
        assert result["actorLogin"] == "alice"
        assert result["actorDisplayName"] == "Alice Smith"

    def test_decode_thread(self):
        thread = _encode_field_string(1, "thread-abc")
        result = _decode_thread(thread)
        assert result["threadRootEventId"] == "thread-abc"

    def test_decode_thread_empty(self):
        result = _decode_thread(b"")
        assert result["threadRootEventId"] == ""

    def test_decode_message_posted_empty(self):
        result = _decode_message_posted(b"")
        assert result == {"message": {}}


# ── Protobuf codec: timestamp decoding ────────────────────────────────────


class TestTimestampDecoding:
    """Test _decode_timestamp."""

    def test_decode_timestamp_basic(self):
        ts = _encode_field_varint(1, 1700000000)
        result = _decode_timestamp(ts)
        assert "2023" in result  # Nov 14, 2023
        assert result.endswith("Z")

    def test_decode_timestamp_with_nanos(self):
        ts = _encode_field_varint(1, 1700000000) + _encode_field_varint(2, 500000)
        result = _decode_timestamp(ts)
        assert "2023" in result
        # 500000 nanos = 0.5 seconds → microsecond=500000 → ".000500" in ISO
        assert ".000500" in result

    def test_decode_timestamp_zero(self):
        ts = b""
        result = _decode_timestamp(ts)
        assert result == ""

    def test_decode_timestamp_only_nanos(self):
        # No seconds, just nanos — should return "" since seconds == 0
        ts = _encode_field_varint(2, 1000000)
        result = _decode_timestamp(ts)
        assert result == ""


# ── Protobuf codec: event envelope (transient events) ────────────────────


class TestEventEnvelopeDecoding:
    """Test _decode_event_envelope for mention and DM notifications."""

    def test_decode_mention_notification(self):
        # Build MentionNotification { room_id = "r1", event_id = "e1" }
        mention_inner = _encode_field_string(1, "r1") + _encode_field_string(2, "e1")
        # Wrap in RealtimeEventEnvelope { mention_notification = 88 }
        envelope = (
            _encode_field_string(1, "envelope1")
            + _encode_submessage(88, mention_inner)
        )
        result = _decode_event_envelope(envelope)
        assert result["id"] == "envelope1"
        assert result["type"] == "mention_notification"
        assert result["data"]["roomId"] == "r1"
        assert result["data"]["eventId"] == "e1"

    def test_decode_dm_notification(self):
        # Build NewDirectMessageNotification { room_id = "r2", event_id = "e2" }
        dm_inner = _encode_field_string(1, "r2") + _encode_field_string(2, "e2")
        # Wrap in RealtimeEventEnvelope { new_direct_message_notification = 89 }
        envelope = (
            _encode_field_string(1, "envelope2")
            + _encode_submessage(89, dm_inner)
        )
        result = _decode_event_envelope(envelope)
        assert result["id"] == "envelope2"
        assert result["type"] == "new_direct_message_notification"
        assert result["data"]["roomId"] == "r2"
        assert result["data"]["eventId"] == "e2"

    def test_decode_event_envelope_unknown(self):
        envelope = _encode_field_string(1, "env3")
        result = _decode_event_envelope(envelope)
        assert result["id"] == "env3"
        assert result["type"] == "unknown"
        assert result["data"] == {}

    def test_decode_mention_notification_directly(self):
        mention_inner = _encode_field_string(1, "roomX") + _encode_field_string(2, "evtX")
        result = _decode_mention_notification(mention_inner)
        assert result["roomId"] == "roomX"
        assert result["eventId"] == "evtX"

    def test_decode_dm_notification_directly(self):
        dm_inner = _encode_field_string(1, "roomY") + _encode_field_string(2, "evtY")
        result = _decode_dm_notification(dm_inner)
        assert result["roomId"] == "roomY"
        assert result["eventId"] == "evtY"


# ── Emoji shortcode conversion ───────────────────────────────────────────


class TestEmojiShortcode:
    """Test _emoji_to_shortcode static method and the emoji mapping."""

    @pytest.mark.parametrize(
        "emoji,shortcode",
        [
            ("👍", "thumbsup"),
            ("👎", "thumbsdown"),
            ("❤️", "heart"),
            ("❤", "heart"),
            ("✅", "white_check_mark"),
            ("❌", "x"),
            ("👀", "eyes"),
            ("🎉", "tada"),
            ("😂", "joy"),
            ("🚀", "rocket"),
            ("🔥", "fire"),
            ("💯", "100"),
            ("🤔", "thinking"),
            ("👏", "clap"),
            ("🙏", "pray"),
            ("😅", "sweat_smile"),
            ("😴", "sleeping"),
            ("⏳", "hourglass"),
        ],
    )
    def test_known_emoji_to_shortcode(self, emoji, shortcode):
        assert ChattoAdapter._emoji_to_shortcode(emoji) == shortcode

    def test_unknown_emoji_passes_through(self):
        # 🦀 (crab) is not in the mapping
        assert ChattoAdapter._emoji_to_shortcode("🦀") == "🦀"

    def test_shortcode_passes_through(self):
        assert ChattoAdapter._emoji_to_shortcode("thumbsup") == "thumbsup"

    def test_empty_string_passes_through(self):
        assert ChattoAdapter._emoji_to_shortcode("") == ""

    def test_emoji_mapping_completeness(self):
        """Verify the mapping dict has the expected keys."""
        assert "👍" in _EMOJI_TO_SHORTCODE
        assert _EMOJI_TO_SHORTCODE["👍"] == "thumbsup"
        assert _EMOJI_TO_SHORTCODE["❤️"] == "heart"


# ── Adapter instantiation and properties ──────────────────────────────────


class TestAdapterInstantiation:
    """Test ChattoAdapter creation and property values."""

    def test_platform_name(self):
        adapter = _make_adapter()
        assert adapter.platform_name == "chatto"

    def test_supports_markdown(self):
        adapter = _make_adapter()
        assert adapter.supports_markdown is True

    def test_supports_reactions(self):
        adapter = _make_adapter()
        assert adapter.supports_reactions is True

    def test_supports_threads(self):
        adapter = _make_adapter()
        assert adapter.supports_threads is True

    def test_max_message_length(self):
        adapter = _make_adapter()
        assert adapter.MAX_MESSAGE_LENGTH == 10000

    def test_splits_long_messages(self):
        adapter = _make_adapter()
        assert adapter.splits_long_messages is True

    def test_typing_tasks_empty(self):
        adapter = _make_adapter()
        assert adapter._typing_tasks == {}

    def test_user_cache_empty(self):
        adapter = _make_adapter()
        assert adapter._user_cache == {}

    def test_base_url_from_extra(self):
        adapter = _make_adapter()
        assert adapter._base_url == "https://chat.example.com"

    def test_channels_from_extra(self):
        adapter = _make_adapter()
        assert adapter._channel_ids == ["room1"]

    def test_require_mention_default(self):
        adapter = _make_adapter(require_mention=False)
        assert adapter._require_mention is False

    def test_is_base_platform_adapter(self):
        from gateway.platforms.base import BasePlatformAdapter

        adapter = _make_adapter()
        assert isinstance(adapter, BasePlatformAdapter)


# ── Registration and requirements ─────────────────────────────────────────


class TestRegistration:
    """Test plugin registration and requirements checking."""

    def test_register_calls_register_platform(self):
        ctx = _MockPluginContext()
        register(ctx)
        assert "chatto" in ctx.registered_names
        kwargs = ctx.registered_kwargs
        assert kwargs["name"] == "chatto"
        assert kwargs["label"] == "Chatto"
        assert callable(kwargs["adapter_factory"])
        assert callable(kwargs["check_fn"])

    def test_register_adapter_factory_creates_adapter(self):
        ctx = _MockPluginContext()
        register(ctx)
        kwargs = ctx.registered_kwargs
        cfg = _make_config()
        adapter = kwargs["adapter_factory"](cfg)
        assert adapter is not None
        assert isinstance(adapter, ChattoAdapter)

    def test_check_requirements_true_when_env_set(self, monkeypatch):
        monkeypatch.setenv("CHATTO_URL", "https://chat.example.com")
        monkeypatch.setenv("CHATTO_LOGIN", "user")
        monkeypatch.setenv("CHATTO_PASSWORD", "pass")
        assert check_requirements() is True

    def test_check_requirements_false_when_url_missing(self, monkeypatch):
        monkeypatch.delenv("CHATTO_URL", raising=False)
        monkeypatch.setenv("CHATTO_LOGIN", "user")
        monkeypatch.setenv("CHATTO_PASSWORD", "pass")
        assert check_requirements() is False

    def test_check_requirements_false_when_login_missing(self, monkeypatch):
        monkeypatch.setenv("CHATTO_URL", "https://chat.example.com")
        monkeypatch.delenv("CHATTO_LOGIN", raising=False)
        monkeypatch.setenv("CHATTO_PASSWORD", "pass")
        assert check_requirements() is False

    def test_check_requirements_false_when_password_missing(self, monkeypatch):
        monkeypatch.setenv("CHATTO_URL", "https://chat.example.com")
        monkeypatch.setenv("CHATTO_LOGIN", "user")
        monkeypatch.delenv("CHATTO_PASSWORD", raising=False)
        assert check_requirements() is False

    def test_check_requirements_false_all_missing(self, monkeypatch):
        monkeypatch.delenv("CHATTO_URL", raising=False)
        monkeypatch.delenv("CHATTO_LOGIN", raising=False)
        monkeypatch.delenv("CHATTO_PASSWORD", raising=False)
        assert check_requirements() is False

    def test_validate_config_with_extra_url(self):
        cfg = MagicMock()
        cfg.extra = {"url": "https://chat.example.com"}
        # Also need login/password from env
        with patch.dict(os.environ, {"CHATTO_LOGIN": "u", "CHATTO_PASSWORD": "p"}):
            assert validate_config(cfg) is True

    def test_validate_config_missing_url(self):
        cfg = MagicMock()
        cfg.extra = {}
        with patch.dict(os.environ, {"CHATTO_LOGIN": "u", "CHATTO_PASSWORD": "p"}, clear=False):
            # Remove CHATTO_URL if set
            os.environ.pop("CHATTO_URL", None)
            assert validate_config(cfg) is False


# ── Send method (mocked RPC) ──────────────────────────────────────────────


class TestSend:
    """Test the send() method with mocked _rpc."""

    @pytest.mark.asyncio
    async def test_send_basic(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"message": {"id": "evt123"}}))
        result = await adapter.send("room1", "Hello world")
        assert result.success is True
        assert result.message_id == "evt123"

    @pytest.mark.asyncio
    async def test_send_with_reply_to(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"message": {"id": "evt456"}}))
        adapter._follow_thread = AsyncMock()
        await adapter.send("room1", "Reply", reply_to="thread-root-1")
        # Verify RPC body contains threadRootEventId
        call_args = adapter._rpc.call_args
        body = call_args.kwargs.get("body") or call_args.args[1]
        assert body["threadRootEventId"] == "thread-root-1"

    @pytest.mark.asyncio
    async def test_send_with_metadata_thread_id(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"message": {"id": "evt789"}}))
        adapter._follow_thread = AsyncMock()
        await adapter.send("room1", "Reply", metadata={"thread_id": "thread456"})
        call_args = adapter._rpc.call_args
        body = call_args.kwargs.get("body") or call_args.args[1]
        assert body["threadRootEventId"] == "thread456"

    @pytest.mark.asyncio
    async def test_send_empty_content(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock()
        result = await adapter.send("room1", "")
        assert result.success is False
        assert "Empty" in (result.error or "")
        adapter._rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_send_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {"error": "server error"}))
        result = await adapter.send("room1", "Hello")
        assert result.success is False
        assert "server error" in (result.error or "")

    @pytest.mark.asyncio
    async def test_send_long_message_split(self):
        adapter = _make_adapter()
        # Return different message IDs for each call
        call_count = [0]

        async def mock_rpc(path, body, **kwargs):
            call_count[0] += 1
            return 200, {"message": {"id": f"evt-{call_count[0]}"}}

        adapter._rpc = AsyncMock(side_effect=mock_rpc)
        adapter._follow_thread = AsyncMock()
        # Create content longer than MAX_MESSAGE_LENGTH (10000)
        long_content = "A" * 12000
        result = await adapter.send("room1", long_content)
        assert result.success is True
        assert result.message_id == "evt-1"
        # Should have been called multiple times
        assert adapter._rpc.call_count > 1

    @pytest.mark.asyncio
    async def test_send_marks_seen(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"message": {"id": "new_evt"}}))
        await adapter.send("room1", "Hello")
        assert "new_evt" in adapter._seen.get("room1", {})


# ── Reactions (mocked RPC) ────────────────────────────────────────────────


class TestReactions:
    """Test send_reaction and remove_reaction."""

    @pytest.mark.asyncio
    async def test_send_reaction_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"added": True}))
        result = await adapter.send_reaction("room1", "evt1", "👍")
        assert result is True
        # Verify emoji was converted to shortcode
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["emoji"] == "thumbsup"

    @pytest.mark.asyncio
    async def test_send_reaction_heart(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        await adapter.send_reaction("room1", "evt1", "❤️")
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["emoji"] == "heart"

    @pytest.mark.asyncio
    async def test_remove_reaction_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.remove_reaction("room1", "evt1", "❤️")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["emoji"] == "heart"

    @pytest.mark.asyncio
    async def test_send_reaction_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.send_reaction("room1", "evt1", "👍")
        assert result is False

    @pytest.mark.asyncio
    async def test_remove_reaction_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(404, {}))
        result = await adapter.remove_reaction("room1", "evt1", "❤️")
        assert result is False

    @pytest.mark.asyncio
    async def test_send_reaction_exception(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(side_effect=Exception("network error"))
        result = await adapter.send_reaction("room1", "evt1", "👍")
        assert result is False


# ── Message edit/delete (mocked RPC) ──────────────────────────────────────


class TestEditDelete:
    """Test edit_message and delete_message."""

    @pytest.mark.asyncio
    async def test_edit_message_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.edit_message("room1", "evt1", "new text")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["body"] == "new text"
        assert body["eventId"] == "evt1"

    @pytest.mark.asyncio
    async def test_edit_message_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(404, {}))
        result = await adapter.edit_message("room1", "evt1", "new text")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_message_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.delete_message("room1", "evt1")
        assert result is True

    @pytest.mark.asyncio
    async def test_delete_message_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(404, {}))
        result = await adapter.delete_message("room1", "evt1")
        assert result is False

    @pytest.mark.asyncio
    async def test_edit_message_exception(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(side_effect=Exception("boom"))
        result = await adapter.edit_message("room1", "evt1", "text")
        assert result is False

    @pytest.mark.asyncio
    async def test_delete_message_exception(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(side_effect=Exception("boom"))
        result = await adapter.delete_message("room1", "evt1")
        assert result is False


# ── Typing indicator lifecycle ─────────────────────────────────────────────


class TestTypingIndicator:
    """Test send_typing and stop_typing lifecycle."""

    @pytest.mark.asyncio
    async def test_send_typing_creates_task(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        await adapter.send_typing("room1")
        assert "room1" in adapter._typing_tasks
        assert isinstance(adapter._typing_tasks["room1"], asyncio.Task)
        # Clean up
        await adapter.stop_typing("room1")

    @pytest.mark.asyncio
    async def test_send_typing_no_duplicate(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        await adapter.send_typing("room1")
        first_task = adapter._typing_tasks["room1"]
        await adapter.send_typing("room1")
        assert adapter._typing_tasks["room1"] is first_task
        await adapter.stop_typing("room1")

    @pytest.mark.asyncio
    async def test_stop_typing_cancels_task(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        await adapter.send_typing("room1")
        assert "room1" in adapter._typing_tasks
        await adapter.stop_typing("room1")
        assert "room1" not in adapter._typing_tasks

    @pytest.mark.asyncio
    async def test_stop_typing_when_not_running(self):
        adapter = _make_adapter()
        # Should not raise even if no task exists
        await adapter.stop_typing("room1")

    @pytest.mark.asyncio
    async def test_typing_loop_calls_rpc(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        await adapter.send_typing("room1")
        # Allow the loop to run one iteration
        await asyncio.sleep(0.05)
        # The typing loop should have called _rpc at least once
        assert adapter._rpc.call_count >= 1
        # Verify it was called with the typing path
        first_call = adapter._rpc.call_args_list[0]
        path = first_call.args[0] if len(first_call.args) > 0 else first_call.kwargs.get("path")
        assert "UpdateTypingIndicator" in path
        body = first_call.args[1] if len(first_call.args) > 1 else first_call.kwargs.get("body")
        assert body["roomId"] == "room1"
        assert body["typing"] is True
        await adapter.stop_typing("room1")


# ── Read state and notifications (mocked RPC) ─────────────────────────────


class TestReadStateAndNotifications:
    """Test mark_room_as_read, mark_thread_as_read, dismiss notifications."""

    @pytest.mark.asyncio
    async def test_mark_room_as_read_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.mark_room_as_read("room1")
        assert result is True

    @pytest.mark.asyncio
    async def test_mark_room_as_read_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.mark_room_as_read("room1")
        assert result is False

    @pytest.mark.asyncio
    async def test_mark_thread_as_read_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.mark_thread_as_read("room1", "thread-root-1")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["roomId"] == "room1"
        assert body["threadRootEventId"] == "thread-root-1"

    @pytest.mark.asyncio
    async def test_mark_thread_as_read_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(404, {}))
        result = await adapter.mark_thread_as_read("room1", "thread-root-1")
        assert result is False

    @pytest.mark.asyncio
    async def test_dismiss_all_notifications_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.dismiss_all_notifications()
        assert result is True

    @pytest.mark.asyncio
    async def test_dismiss_all_notifications_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.dismiss_all_notifications()
        assert result is False

    @pytest.mark.asyncio
    async def test_dismiss_notification_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.dismiss_notification("notif123")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["notificationId"] == "notif123"

    @pytest.mark.asyncio
    async def test_dismiss_notification_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(404, {}))
        result = await adapter.dismiss_notification("notif123")
        assert result is False


# ── DM initiation and room creation (mocked RPC) ──────────────────────────


class TestDMAndRoomCreation:
    """Test start_dm and create_room."""

    @pytest.mark.asyncio
    async def test_start_dm_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"room": {"id": "room789"}}))
        result = await adapter.start_dm("user123")
        assert result == "room789"
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["participantIds"] == ["user123"]

    @pytest.mark.asyncio
    async def test_start_dm_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.start_dm("user123")
        assert result is None

    @pytest.mark.asyncio
    async def test_start_dm_no_room_id(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"room": {}}))
        result = await adapter.start_dm("user123")
        assert result is None

    @pytest.mark.asyncio
    async def test_start_dm_sets_room_kind(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"room": {"id": "dm-room-1"}}))
        await adapter.start_dm("user123")
        assert adapter._room_kinds["dm-room-1"] == "ROOM_KIND_DM"

    @pytest.mark.asyncio
    async def test_create_room_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"room": {"id": "room789"}}))
        result = await adapter.create_room("test-room", "description")
        assert result == "room789"
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["name"] == "test-room"
        assert body["description"] == "description"

    @pytest.mark.asyncio
    async def test_create_room_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.create_room("test-room", "description")
        assert result is None

    @pytest.mark.asyncio
    async def test_create_room_sets_room_kind(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {"room": {"id": "group-room-1"}}))
        await adapter.create_room("test-room")
        assert adapter._room_kinds["group-room-1"] == "ROOM_KIND_GROUP"
        assert adapter._room_names["group-room-1"] == "test-room"


# ── User lookup (mocked RPC) ──────────────────────────────────────────────


class TestUserLookup:
    """Test get_user, list_users, batch_get_users with caching."""

    @pytest.mark.asyncio
    async def test_get_user_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(
            return_value=(200, {"user": {"id": "u1", "login": "alice", "displayName": "Alice"}})
        )
        result = await adapter.get_user("u1")
        assert result is not None
        assert result["id"] == "u1"
        assert result["login"] == "alice"

    @pytest.mark.asyncio
    async def test_get_user_caches(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(
            return_value=(200, {"user": {"id": "u1", "login": "alice"}})
        )
        await adapter.get_user("u1")
        # Second call should use cache — no second RPC
        await adapter.get_user("u1")
        assert adapter._rpc.call_count == 1
        assert "u1" in adapter._user_cache

    @pytest.mark.asyncio
    async def test_get_user_empty_id(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock()
        result = await adapter.get_user("")
        assert result is None
        adapter._rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_user_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(404, {}))
        result = await adapter.get_user("u1")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_user_exception(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(side_effect=Exception("boom"))
        result = await adapter.get_user("u1")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_users_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(
            return_value=(200, {"users": [{"id": "u1"}, {"id": "u2"}]})
        )
        result = await adapter.list_users()
        assert len(result) == 2
        assert result[0]["id"] == "u1"
        # Should cache all returned users
        assert "u1" in adapter._user_cache
        assert "u2" in adapter._user_cache

    @pytest.mark.asyncio
    async def test_list_users_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.list_users()
        assert result == []

    @pytest.mark.asyncio
    async def test_batch_get_users_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(
            return_value=(200, {"users": [{"id": "u1"}, {"id": "u2"}]})
        )
        result = await adapter.batch_get_users(["u1", "u2"])
        assert len(result) == 2
        # Should cache results
        assert "u1" in adapter._user_cache
        assert "u2" in adapter._user_cache

    @pytest.mark.asyncio
    async def test_batch_get_users_empty_list(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock()
        result = await adapter.batch_get_users([])
        assert result == []
        adapter._rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_get_users_uses_cache(self):
        adapter = _make_adapter()
        # Pre-populate cache
        adapter._user_cache["u1"] = {"id": "u1", "login": "alice"}
        adapter._rpc = AsyncMock(
            return_value=(200, {"users": [{"id": "u2"}]})
        )
        result = await adapter.batch_get_users(["u1", "u2"])
        assert len(result) == 2
        # Should have only fetched u2 from server
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["userIds"] == ["u2"]

    @pytest.mark.asyncio
    async def test_batch_get_users_all_cached(self):
        adapter = _make_adapter()
        adapter._user_cache["u1"] = {"id": "u1"}
        adapter._rpc = AsyncMock()
        result = await adapter.batch_get_users(["u1"])
        assert len(result) == 1
        adapter._rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_batch_get_users_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.batch_get_users(["u1"])
        assert result == []


# ── Presence and custom status (mocked RPC) ──────────────────────────────


class TestPresenceAndStatus:
    """Test set_presence, set_custom_status, clear_custom_status."""

    @pytest.mark.asyncio
    async def test_set_presence_online(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.set_presence("online")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["status"] == 1

    @pytest.mark.asyncio
    async def test_set_presence_dnd(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.set_presence("dnd")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["status"] == 3

    @pytest.mark.asyncio
    async def test_set_presence_away(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.set_presence("away")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["status"] == 2

    @pytest.mark.asyncio
    async def test_set_presence_do_not_disturb(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.set_presence("do_not_disturb")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["status"] == 3

    @pytest.mark.asyncio
    async def test_set_presence_unknown(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.set_presence("invisible")
        assert result is False
        adapter._rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_presence_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.set_presence("online")
        assert result is False

    @pytest.mark.asyncio
    async def test_set_custom_status_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.set_custom_status("Processing...")
        assert result is True
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert body["status"] == "Processing..."

    @pytest.mark.asyncio
    async def test_set_custom_status_empty(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock()
        result = await adapter.set_custom_status("")
        assert result is False
        adapter._rpc.assert_not_called()

    @pytest.mark.asyncio
    async def test_set_custom_status_truncates(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        long_status = "A" * 200
        await adapter.set_custom_status(long_status)
        call_args = adapter._rpc.call_args
        body = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("body")
        assert len(body["status"]) == 100

    @pytest.mark.asyncio
    async def test_set_custom_status_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.set_custom_status("test")
        assert result is False

    @pytest.mark.asyncio
    async def test_clear_custom_status_success(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        result = await adapter.clear_custom_status()
        assert result is True

    @pytest.mark.asyncio
    async def test_clear_custom_status_failure(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(500, {}))
        result = await adapter.clear_custom_status()
        assert result is False


# ── Message dispatch (mocked) ─────────────────────────────────────────────


class TestMessageDispatch:
    """Test _dispatch_message with mocked handler.

    The base class ``handle_message`` does complex session management and
    spawns background tasks, so we mock it to verify _dispatch_message calls
    it with the right MessageEvent.
    """

    @pytest.mark.asyncio
    async def test_dispatch_message_calls_handler(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_names["room1"] = "General"
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        adapter._require_mention = False  # don't require mention for this test

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "Hello bot",
            "createdAt": "",
            "actorLogin": "alice",
            "actorDisplayName": "Alice",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        assert adapter.handle_message.called
        event = adapter.handle_message.call_args.args[0]
        assert event.text == "Hello bot"
        assert event.message_id == "evt1"

    @pytest.mark.asyncio
    async def test_dispatch_message_self_echo_suppressed(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "bot-user-id",  # matches adapter._user_id
            "body": "My own message",
            "createdAt": "",
            "actorLogin": "hermes_bot",
            "actorDisplayName": "Hermes Bot",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_message_empty_body_skipped(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "",
            "createdAt": "",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_message_no_handler(self):
        adapter = _make_adapter()
        adapter._message_handler = None
        adapter._rpc = AsyncMock(return_value=(200, {}))
        # Should not raise
        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "Hello",
            "createdAt": "",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")

    @pytest.mark.asyncio
    async def test_dispatch_message_mark_room_read_called(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        adapter._require_mention = False  # don't require mention for this test

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "Hello",
            "createdAt": "",
            "actorLogin": "alice",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        # mark_room_as_read and dismiss_all_notifications should have been called
        # via _rpc — check for MarkRoomAsRead and DismissAllNotifications paths
        rpc_paths = [c.args[0] for c in adapter._rpc.call_args_list if len(c.args) > 0]
        assert any("MarkRoomAsRead" in p for p in rpc_paths)
        assert any("DismissAllNotifications" in p for p in rpc_paths)

    @pytest.mark.asyncio
    async def test_dispatch_message_dm_always_responds(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_kinds["room1"] = "ROOM_KIND_DM"
        adapter._require_mention = True  # even with require_mention, DMs respond

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "Hello without mention",
            "createdAt": "",
            "actorLogin": "alice",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        assert adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_dispatch_message_require_mention_no_mention_skipped(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        adapter._require_mention = True

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "Hello without mention",
            "createdAt": "",
            "actorLogin": "alice",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatch_message_with_mention(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        adapter._require_mention = True

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "@hermes_bot do something",
            "createdAt": "",
            "actorLogin": "alice",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        assert adapter.handle_message.called

    @pytest.mark.asyncio
    async def test_dispatch_message_with_thread(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        adapter._require_mention = False  # don't require mention for this test

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "Hello",
            "createdAt": "",
            "actorLogin": "alice",
            "thread": {"threadRootEventId": "thread-root-123"},
        }
        await adapter._dispatch_message(msg, "room1")
        assert adapter.handle_message.called
        # Verify the event was created with the thread_id
        event = adapter.handle_message.call_args.args[0]
        assert event.source.thread_id == "thread-root-123"

    @pytest.mark.asyncio
    async def test_dispatch_message_strips_mention_prefix(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        adapter._require_mention = True

        msg = {
            "id": "evt1",
            "roomId": "room1",
            "actorId": "user1",
            "body": "@hermes_bot please help",
            "createdAt": "",
            "actorLogin": "alice",
            "thread": {},
        }
        await adapter._dispatch_message(msg, "room1")
        event = adapter.handle_message.call_args.args[0]
        # The mention prefix should be stripped
        assert not event.text.startswith("@hermes_bot")
        assert "please help" in event.text


# ── Processing lifecycle hooks ─────────────────────────────────────────────


class TestProcessingLifecycle:
    """Test on_processing_start and on_processing_complete reaction hooks."""

    @pytest.mark.asyncio
    async def test_on_processing_start_adds_eyes(self):
        adapter = _make_adapter()
        adapter.send_reaction = AsyncMock(return_value=True)
        event = MagicMock()
        event.message_id = "evt1"
        event.source = MagicMock()
        event.source.chat_id = "room1"
        event.raw_message = {}
        await adapter.on_processing_start(event)
        adapter.send_reaction.assert_called_once_with("room1", "evt1", "👀")

    @pytest.mark.asyncio
    async def test_on_processing_complete_success(self):
        from gateway.platforms.base import ProcessingOutcome

        adapter = _make_adapter()
        adapter.send_reaction = AsyncMock(return_value=True)
        adapter.remove_reaction = AsyncMock(return_value=True)
        event = MagicMock()
        event.message_id = "evt1"
        event.source = MagicMock()
        event.source.chat_id = "room1"
        event.raw_message = {}
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)
        adapter.remove_reaction.assert_called_once_with("room1", "evt1", "👀")
        adapter.send_reaction.assert_called_once_with("room1", "evt1", "✅")

    @pytest.mark.asyncio
    async def test_on_processing_complete_failure(self):
        from gateway.platforms.base import ProcessingOutcome

        adapter = _make_adapter()
        adapter.send_reaction = AsyncMock(return_value=True)
        adapter.remove_reaction = AsyncMock(return_value=True)
        event = MagicMock()
        event.message_id = "evt1"
        event.source = MagicMock()
        event.source.chat_id = "room1"
        event.raw_message = {}
        await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)
        adapter.send_reaction.assert_called_once_with("room1", "evt1", "❌")

    @pytest.mark.asyncio
    async def test_reactions_disabled(self, monkeypatch):
        adapter = _make_adapter()
        monkeypatch.setenv("CHATTO_REACTIONS", "false")
        adapter.send_reaction = AsyncMock()
        event = MagicMock()
        event.message_id = "evt1"
        event.source = MagicMock()
        event.source.chat_id = "room1"
        event.raw_message = {}
        await adapter.on_processing_start(event)
        adapter.send_reaction.assert_not_called()


# ── Attachment upload (mocked RPC) ────────────────────────────────────────


class TestAttachmentUpload:
    """Test the chunked asset upload flow."""

    @pytest.mark.asyncio
    async def test_upload_asset_success(self):
        adapter = _make_adapter()

        # Create a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            tmp_path = f.name

        try:
            rpc_responses = [
                (200, {"upload": {"id": "upload-1"}}),  # CreateUpload
                (200, {}),  # UploadChunk
                (200, {"asset": {"id": "asset-1"}}),  # CompleteUpload
            ]
            adapter._rpc = AsyncMock(side_effect=rpc_responses)

            asset_id = await adapter._upload_asset("room1", tmp_path)
            assert asset_id == "asset-1"
            assert adapter._rpc.call_count == 3

            # Verify the first call was CreateUpload
            first_call = adapter._rpc.call_args_list[0]
            path = first_call.args[0]
            assert "CreateUpload" in path
            body = first_call.args[1]
            assert body["roomId"] == "room1"
            assert "sha256" in body

            # Verify last call was CompleteUpload
            last_call = adapter._rpc.call_args_list[-1]
            path = last_call.args[0]
            assert "CompleteUpload" in path
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_upload_asset_create_fails(self):
        adapter = _make_adapter()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"test data")
            tmp_path = f.name

        try:
            adapter._rpc = AsyncMock(return_value=(500, {"error": "fail"}))
            result = await adapter._upload_asset("room1", tmp_path)
            assert result is None
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_upload_asset_empty_file(self):
        adapter = _make_adapter()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(b"")
            tmp_path = f.name

        try:
            adapter._rpc = AsyncMock()
            result = await adapter._upload_asset("room1", tmp_path)
            assert result is None
            adapter._rpc.assert_not_called()
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_send_image_file_success(self):
        adapter = _make_adapter()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
            tmp_path = f.name

        try:
            rpc_responses = [
                (200, {"upload": {"id": "upload-1"}}),  # CreateUpload
                (200, {}),  # UploadChunk
                (200, {"asset": {"id": "asset-1"}}),  # CompleteUpload
                (200, {"message": {"id": "msg-1"}}),  # CreateMessage
            ]
            adapter._rpc = AsyncMock(side_effect=rpc_responses)

            result = await adapter.send_image_file("room1", tmp_path, caption="Test image")
            assert result.success is True
            assert result.message_id == "msg-1"

            # Verify the CreateMessage call had attachmentAssetIds
            create_msg_call = adapter._rpc.call_args_list[-1]
            body = create_msg_call.args[1]
            assert body["attachmentAssetIds"] == ["asset-1"]
            assert body["body"] == "Test image"
        finally:
            os.unlink(tmp_path)

    @pytest.mark.asyncio
    async def test_send_image_file_upload_fails_fallback(self):
        adapter = _make_adapter()

        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as f:
            f.write(b"\x89PNG" + b"\x00" * 100)
            tmp_path = f.name

        try:
            # Upload fails, then fallback send succeeds
            rpc_responses = [
                (500, {"error": "fail"}),  # CreateUpload fails
                (200, {"message": {"id": "fallback-msg"}}),  # Fallback send
            ]
            adapter._rpc = AsyncMock(side_effect=rpc_responses)

            result = await adapter.send_image_file("room1", tmp_path)
            assert result.success is True
            assert result.message_id == "fallback-msg"
        finally:
            os.unlink(tmp_path)


# ── Seen tracking ─────────────────────────────────────────────────────────


class TestSeenTracking:
    """Test _mark_seen, _is_seen, and the _SEEN_CAP."""

    def test_mark_and_check_seen(self):
        adapter = _make_adapter()
        adapter._mark_seen("room1", "evt1")
        assert adapter._is_seen("room1", "evt1") is True
        assert adapter._is_seen("room1", "evt2") is False
        assert adapter._is_seen("room2", "evt1") is False

    def test_mark_seen_evicts_old_beyond_cap(self):
        adapter = _make_adapter()
        # Add more than _SEEN_CAP events
        for i in range(_SEEN_CAP + 10):
            adapter._mark_seen("room1", f"evt{i}")
        # The first events should have been evicted
        assert adapter._is_seen("room1", "evt0") is False
        # The most recent should still be there
        assert adapter._is_seen("room1", f"evt{_SEEN_CAP + 9}") is True
        # Total should not exceed cap
        assert len(adapter._seen["room1"]) <= _SEEN_CAP


# ── WebSocket URL building ────────────────────────────────────────────────


class TestWebSocketURL:
    """Test _websocket_url conversion."""

    def test_https_to_wss(self):
        adapter = _make_adapter()
        adapter._base_url = "https://chat.example.com"
        url = adapter._websocket_url()
        assert url.startswith("wss://")
        assert "/api/realtime" in url

    def test_http_to_ws(self):
        adapter = _make_adapter()
        adapter._base_url = "http://localhost:8080"
        url = adapter._websocket_url()
        assert url.startswith("ws://")
        assert "/api/realtime" in url

    def test_websocket_url_with_path(self):
        adapter = _make_adapter()
        adapter._base_url = "https://chat.example.com/subpath"
        url = adapter._websocket_url()
        assert "/subpath/api/realtime" in url

    def test_websocket_url_invalid_scheme(self):
        adapter = _make_adapter()
        adapter._base_url = "ftp://chat.example.com"
        with pytest.raises(ValueError, match="must use http"):
            adapter._websocket_url()


# ── Get chat info ─────────────────────────────────────────────────────────


class TestGetChatInfo:
    """Test get_chat_info."""

    @pytest.mark.asyncio
    async def test_get_chat_info_group(self):
        adapter = _make_adapter()
        adapter._room_names["room1"] = "General"
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        info = await adapter.get_chat_info("room1")
        assert info["name"] == "General"
        assert info["type"] == "group"

    @pytest.mark.asyncio
    async def test_get_chat_info_dm(self):
        adapter = _make_adapter()
        adapter._room_names["room2"] = "Alice"
        adapter._room_kinds["room2"] = "ROOM_KIND_DM"
        info = await adapter.get_chat_info("room2")
        assert info["name"] == "Alice"
        assert info["type"] == "dm"

    @pytest.mark.asyncio
    async def test_get_chat_info_unknown_room(self):
        adapter = _make_adapter()
        info = await adapter.get_chat_info("unknown-room")
        assert info["name"] == "unknown-room"
        assert info["type"] == "group"  # default


# ── Handle projection event ──────────────────────────────────────────────


class TestHandleProjectionEvent:
    """Test _handle_projection_event and _handle_timeline_event_upsert."""

    @pytest.mark.asyncio
    async def test_handle_projection_event_updates_cursor(self):
        adapter = _make_adapter()
        # Build a projection event with a resume cursor
        proj = _encode_field_string(4, "new-cursor-123")
        await adapter._handle_projection_event(proj)
        assert adapter._resume_cursor == "new-cursor-123"

    @pytest.mark.asyncio
    async def test_handle_projection_event_empty_data(self):
        adapter = _make_adapter()
        await adapter._handle_projection_event(b"")
        # Should not raise, cursor unchanged

    @pytest.mark.asyncio
    async def test_handle_timeline_event_dedup(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()
        adapter.handle_message = AsyncMock()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._room_kinds["room1"] = "ROOM_KIND_GROUP"
        adapter._require_mention = False  # don't require mention for this test

        op = {
            "room_id": "room1",
            "event": {
                "id": "evt-dedup",
                "messagePosted": {
                    "message": {
                        "id": "m1",
                        "roomId": "room1",
                        "actorId": "user1",
                        "body": "Hello",
                        "createdAt": "",
                        "thread": {},
                    }
                },
            },
        }
        # First call should dispatch
        await adapter._handle_timeline_event_upsert(op)
        assert adapter.handle_message.called

        # Reset mock
        adapter.handle_message.reset_mock()

        # Second call should be deduped
        await adapter._handle_timeline_event_upsert(op)
        adapter.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_timeline_event_no_message_posted(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()

        op = {
            "room_id": "room1",
            "event": {
                "id": "evt1",
                "messagePosted": None,
            },
        }
        await adapter._handle_timeline_event_upsert(op)
        adapter._message_handler.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_timeline_event_empty_event(self):
        adapter = _make_adapter()
        adapter._message_handler = AsyncMock()

        op = {"room_id": "room1", "event": {}}
        await adapter._handle_timeline_event_upsert(op)
        adapter._message_handler.assert_not_called()


# ── Handle transient event ────────────────────────────────────────────────


class TestHandleTransientEvent:
    """Test _handle_transient_event."""

    @pytest.mark.asyncio
    async def test_handle_transient_mention(self):
        adapter = _make_adapter()
        # Build a transient event envelope with a mention notification
        mention_inner = _encode_field_string(1, "room1") + _encode_field_string(2, "evt1")
        envelope = _encode_field_string(1, "env1") + _encode_submessage(88, mention_inner)
        # Should not raise
        await adapter._handle_transient_event(envelope)

    @pytest.mark.asyncio
    async def test_handle_transient_dm(self):
        adapter = _make_adapter()
        dm_inner = _encode_field_string(1, "room2") + _encode_field_string(2, "evt2")
        envelope = _encode_field_string(1, "env2") + _encode_submessage(89, dm_inner)
        await adapter._handle_transient_event(envelope)

    @pytest.mark.asyncio
    async def test_handle_transient_empty(self):
        adapter = _make_adapter()
        await adapter._handle_transient_event(b"")


# ── Disconnect ────────────────────────────────────────────────────────────


class TestDisconnect:
    """Test disconnect cleanup."""

    @pytest.mark.asyncio
    async def test_disconnect_cancels_typing_tasks(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._ws_active = False  # avoid websocket teardown
        adapter._liveness_task = None
        adapter._ws_task = None

        await adapter.send_typing("room1")
        assert "room1" in adapter._typing_tasks
        await adapter.disconnect()
        assert "room1" not in adapter._typing_tasks

    @pytest.mark.asyncio
    async def test_disconnect_clears_token(self):
        adapter = _make_adapter()
        adapter._rpc = AsyncMock(return_value=(200, {}))
        adapter._ws_active = False
        adapter._liveness_task = None
        adapter._ws_task = None
        assert adapter._token is not None
        await adapter.disconnect()
        assert adapter._token is None