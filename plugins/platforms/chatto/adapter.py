"""
Chatto Platform Adapter for Hermes Agent.

A plugin-based gateway adapter that connects to a Chatto server
(self-hosted team chat) and relays messages to/from the Hermes agent.

The adapter uses the Chatto REST/ConnectRPC API (JSON over HTTP) for
outbound (CreateMessage) and the Chatto WebSocket realtime protocol
(binary protobuf) for inbound message delivery.

Configuration in config.yaml::

    gateway:
      platforms:
        chatto:
          enabled: true
          extra:
            url: https://chat.lacy.casa
            channels:                  # room IDs to watch (empty = all joined)
              - REljMv5Pgolo6Y9
            home_channel: REljMv5Pgolo6Y9
            require_mention: true      # only respond to @mentions in rooms
            allowed_users: []          # empty = allow all
            allow_all_users: true

Or via environment variables (overrides config.yaml):
    CHATTO_URL, CHATTO_LOGIN, CHATTO_PASSWORD (secrets in ~/.hermes/.env),
    CHATTO_CHANNELS, CHATTO_HOME_CHANNEL,
    CHATTO_REQUIRE_MENTION, CHATTO_ALLOWED_USERS, CHATTO_ALLOW_ALL_USERS
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import ssl
import urllib.error
import urllib.request
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
)
from gateway.config import Platform

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

_HTTP_TIMEOUT = 30.0
_CONNECT_RPC_VERSION = "1"
_MAX_MESSAGE_LENGTH = 10000
_SEEN_CAP = 500

# WebSocket / realtime protocol
_REALTIME_PROTOCOL_VERSION = 1  # v0.4.19 supports v1; v2 reserved for future
_WS_PATH = "/api/realtime"
_WS_AUTH_TIMEOUT = 20.0
_WS_MAX_MESSAGE_BYTES = 4_000_000
_WS_PING_INTERVAL = 30.0  # seconds between client ping frames
_WS_RECONNECT_INITIAL_BACKOFF = 1.0
_WS_RECONNECT_MAX_BACKOFF = 30.0

# ConnectRPC service paths (relative to base URL) — used for REST calls
_RPC_BASE = "/api/connect"
_PATH_LIST_ROOMS = f"{_RPC_BASE}/chatto.api.v1.RoomDirectoryService/ListRooms"
_PATH_JOIN_ROOM = f"{_RPC_BASE}/chatto.api.v1.RoomService/JoinRoom"
_PATH_GET_ROOM_EVENTS = f"{_RPC_BASE}/chatto.api.v1.RoomService/GetRoomEvents"
_PATH_GET_THREAD_EVENTS = f"{_RPC_BASE}/chatto.api.v1.ThreadService/GetThreadEvents"
_PATH_CREATE_MESSAGE = f"{_RPC_BASE}/chatto.api.v1.MessageService/CreateMessage"
_PATH_GET_MESSAGE = f"{_RPC_BASE}/chatto.api.v1.MessageService/GetMessage"
_PATH_GET_VIEWER = f"{_RPC_BASE}/chatto.api.v1.ViewerService/GetViewer"
_PATH_UPDATE_TYPING = f"{_RPC_BASE}/chatto.api.v1.RoomService/UpdateTypingIndicator"
_PATH_START_DM = f"{_RPC_BASE}/chatto.api.v1.RoomService/StartDM"
_PATH_LIST_MEMBERS = f"{_RPC_BASE}/chatto.api.v1.RoomService/ListMembers"

# Message lifecycle: reactions, editing, deletion
_PATH_ADD_REACTION = f"{_RPC_BASE}/chatto.api.v1.MessageService/AddReaction"
_PATH_REMOVE_REACTION = f"{_RPC_BASE}/chatto.api.v1.MessageService/RemoveReaction"
_PATH_UPDATE_MESSAGE = f"{_RPC_BASE}/chatto.api.v1.MessageService/UpdateMessage"
_PATH_DELETE_MESSAGE = f"{_RPC_BASE}/chatto.api.v1.MessageService/DeleteMessage"

# Chunked asset upload
_PATH_CREATE_UPLOAD = f"{_RPC_BASE}/chatto.api.v1.AssetUploadService/CreateUpload"
_PATH_UPLOAD_CHUNK = f"{_RPC_BASE}/chatto.api.v1.AssetUploadService/UploadChunk"
_PATH_COMPLETE_UPLOAD = f"{_RPC_BASE}/chatto.api.v1.AssetUploadService/CompleteUpload"

# Read state management
_PATH_MARK_ROOM_READ = f"{_RPC_BASE}/chatto.api.v1.RoomService/MarkRoomAsRead"
_PATH_MARK_THREAD_READ = f"{_RPC_BASE}/chatto.api.v1.ThreadService/MarkThreadAsRead"

# Thread following
_PATH_FOLLOW_THREAD = f"{_RPC_BASE}/chatto.api.v1.ThreadService/FollowThread"

# Room creation
_PATH_CREATE_ROOM = f"{_RPC_BASE}/chatto.api.v1.RoomService/CreateRoom"

# Notification dismissal
_PATH_DISMISS_ALL_NOTIFICATIONS = f"{_RPC_BASE}/chatto.api.v1.NotificationService/DismissAllNotifications"
_PATH_DISMISS_NOTIFICATION = f"{_RPC_BASE}/chatto.api.v1.NotificationService/DismissNotification"

# Member directory — user lookup and mention resolution
_PATH_LIST_USERS = f"{_RPC_BASE}/chatto.api.v1.UserService/ListUsers"
_PATH_GET_USER = f"{_RPC_BASE}/chatto.api.v1.UserService/GetUser"
_PATH_BATCH_GET_USERS = f"{_RPC_BASE}/chatto.api.v1.UserService/BatchGetUsers"

# Presence broadcasting — online/away/DND status
_PATH_UPDATE_PRESENCE = f"{_RPC_BASE}/chatto.api.v1.MyAccountService/UpdatePresence"

# Custom status messages
_PATH_UPDATE_CUSTOM_STATUS = f"{_RPC_BASE}/chatto.api.v1.MyAccountService/UpdateCustomStatus"
_PATH_DELETE_CUSTOM_STATUS = f"{_RPC_BASE}/chatto.api.v1.MyAccountService/DeleteCustomStatus"

# Presence status int mapping (Chatto API)
_PRESENCE_STATUS_MAP: Dict[str, int] = {
    "online": 1,
    "away": 2,
    "dnd": 3,
    "do_not_disturb": 3,
}

# Emoji shortcode mapping (Chatto uses shortcode names, not unicode emoji)
_EMOJI_TO_SHORTCODE: Dict[str, str] = {
    "👍": "thumbsup",
    "👎": "thumbsdown",
    "❤️": "heart",
    "❤": "heart",
    "✅": "white_check_mark",
    "❌": "x",
    "👀": "eyes",
    "🎉": "tada",
    "😂": "joy",
    "🚀": "rocket",
    "🔥": "fire",
    "💯": "100",
    "🤔": "thinking",
    "👏": "clap",
    "🙏": "pray",
    "😅": "sweat_smile",
    "😴": "sleeping",
    "⏳": "hourglass",
}

# Chunk size for asset uploads (256 KB)
_UPLOAD_CHUNK_SIZE = 256 * 1024


# --------------------------------------------------------------------------- #
# Minimal Protobuf Encoder/Decoder (stdlib only)
# --------------------------------------------------------------------------- #
#
# Implements just enough of the protobuf binary format to encode/decode
# the Chatto realtime protocol frames.  No external protobuf library needed.
#
# Wire types:
#   0 = varint
#   2 = length-delimited (bytes/string/submessage)
#
# Field tag = (field_number << 3) | wire_type

def _encode_varint(value: int) -> bytes:
    """Encode an unsigned integer as a protobuf varint."""
    if value < 0:
        # Treat as unsigned 64-bit
        value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _decode_varint(data: bytes, offset: int) -> Tuple[int, int]:
    """Decode a varint from data at offset. Returns (value, new_offset)."""
    result = 0
    shift = 0
    while True:
        if offset >= len(data):
            raise ValueError("Truncated varint")
        byte = data[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            break
        shift += 7
        if shift >= 64:
            raise ValueError("Varint too long")
    return result, offset


def _encode_tag(field_number: int, wire_type: int) -> bytes:
    """Encode a protobuf field tag."""
    return _encode_varint((field_number << 3) | wire_type)


def _encode_field_varint(field_number: int, value: int) -> bytes:
    """Encode a varint field."""
    return _encode_tag(field_number, 0) + _encode_varint(value)


def _encode_field_bytes(field_number: int, value: bytes) -> bytes:
    """Encode a length-delimited field (bytes/string/submessage)."""
    return _encode_tag(field_number, 2) + _encode_varint(len(value)) + value


def _encode_field_string(field_number: int, value: str) -> bytes:
    """Encode a string field."""
    return _encode_field_bytes(field_number, value.encode("utf-8"))


def _encode_submessage(field_number: int, submessage: bytes) -> bytes:
    """Encode a submessage field (length-delimited)."""
    return _encode_field_bytes(field_number, submessage)


def _decode_fields(data: bytes) -> Dict[int, List[Any]]:
    """Decode all fields from a protobuf message.

    Returns a dict mapping field_number -> list of values.
    For varint fields, value is int.
    For length-delimited fields, value is bytes (raw).
    """
    fields: Dict[int, List[Any]] = {}
    offset = 0
    while offset < len(data):
        tag, offset = _decode_varint(data, offset)
        field_number = tag >> 3
        wire_type = tag & 0x07
        if wire_type == 0:  # varint
            value, offset = _decode_varint(data, offset)
            fields.setdefault(field_number, []).append(value)
        elif wire_type == 2:  # length-delimited
            length, offset = _decode_varint(data, offset)
            if offset + length > len(data):
                raise ValueError("Truncated length-delimited field")
            value = data[offset:offset + length]
            offset += length
            fields.setdefault(field_number, []).append(value)
        elif wire_type == 1:  # 64-bit
            if offset + 8 > len(data):
                raise ValueError("Truncated 64-bit field")
            value = data[offset:offset + 8]
            offset += 8
            fields.setdefault(field_number, []).append(value)
        elif wire_type == 5:  # 32-bit
            if offset + 4 > len(data):
                raise ValueError("Truncated 32-bit field")
            value = data[offset:offset + 4]
            offset += 4
            fields.setdefault(field_number, []).append(value)
        else:
            raise ValueError(f"Unknown wire type {wire_type} for field {field_number}")
    return fields


def _get_first(fields: Dict[int, List[Any]], field_number: int, default: Any = None) -> Any:
    """Get the first value for a field number, or default."""
    values = fields.get(field_number)
    if values:
        return values[0]
    return default


def _get_all(fields: Dict[int, List[Any]], field_number: int) -> List[Any]:
    """Get all values for a field number."""
    return fields.get(field_number, [])


# --------------------------------------------------------------------------- #
# Protobuf message helpers for Chatto realtime protocol
# --------------------------------------------------------------------------- #

def _encode_client_hello(bearer_token: str) -> bytes:
    """Encode RealtimeClientHello {
        uint32 protocol_version = 1;  // v0.4.19 uses v1
        optional string bearer_token = 2;
    }"""
    msg = _encode_field_varint(1, _REALTIME_PROTOCOL_VERSION)
    if bearer_token:
        msg += _encode_field_string(2, bearer_token)
    return msg


def _encode_subscribe_events(
    resume_cursor: Optional[str] = None,
    retained_room_ids: Optional[List[str]] = None,
) -> bytes:
    """Encode RealtimeSubscribeEvents {
        optional string resume_cursor = 1;
        repeated string retained_room_ids = 2;
    }"""
    msg = b""
    if resume_cursor:
        msg += _encode_field_string(1, resume_cursor)
    if retained_room_ids:
        for rid in retained_room_ids:
            msg += _encode_field_string(2, rid)
    return msg


def _encode_ping() -> bytes:
    """Encode RealtimePing (empty message)."""
    return b""


def _encode_client_frame_hello(hello_bytes: bytes) -> bytes:
    """Encode RealtimeClientFrame { oneof frame { RealtimeClientHello hello = 1; } }"""
    return _encode_submessage(1, hello_bytes)


def _encode_client_frame_subscribe(subscribe_bytes: bytes) -> bytes:
    """Encode RealtimeClientFrame { oneof frame { RealtimeSubscribeEvents subscribe_events = 2; } }"""
    return _encode_submessage(2, subscribe_bytes)


def _encode_client_frame_ping(ping_bytes: bytes) -> bytes:
    """Encode RealtimeClientFrame { oneof frame { RealtimePing ping = 3; } }"""
    return _encode_submessage(3, ping_bytes)


def _decode_server_frame(data: bytes) -> Dict[str, Any]:
    """Decode RealtimeServerFrame and identify which oneof variant is set.

    Returns a dict like:
        {"type": "hello", "data": <decoded RealtimeServerHello bytes>}
        {"type": "subscribed", "data": <decoded RealtimeSubscribed bytes>}
        {"type": "event", "data": <decoded RealtimeEventEnvelope bytes>}
        {"type": "heartbeat", "data": <raw bytes>}
        {"type": "error", "data": <decoded RealtimeError bytes>}
        {"type": "close", "data": <decoded RealtimeClose bytes>}
        {"type": "pong", "data": <raw bytes>}
        {"type": "caught_up", "data": <raw bytes>}
        {"type": "projection_event", "data": <decoded RealtimeProjectionEvent bytes>}
        {"type": "unknown", "data": None}
    """
    fields = _decode_fields(data)
    # oneof frame: only one of these fields is set
    # field 1 = hello, 2 = subscribed, 3 = event, 4 = heartbeat,
    # 5 = error, 6 = close, 7 = pong, 8 = caught_up, 9 = projection_event
    type_map = {
        1: "hello",
        2: "subscribed",
        3: "event",
        4: "heartbeat",
        5: "error",
        6: "close",
        7: "pong",
        8: "caught_up",
        9: "projection_event",
    }
    for field_num, type_name in type_map.items():
        if field_num in fields:
            return {"type": type_name, "data": fields[field_num][0]}
    return {"type": "unknown", "data": None}


def _decode_projection_event(data: bytes) -> Dict[str, Any]:
    """Decode RealtimeProjectionEvent {
        string id = 1;
        google.protobuf.Timestamp created_at = 2;
        optional string actor_id = 3;
        optional string resume_cursor = 4;
        repeated RealtimeProjectionOperation operations = 5;
    }

    Returns dict with keys: id, created_at (ISO str), actor_id, resume_cursor,
    operations (list of decoded operation dicts).
    """
    fields = _decode_fields(data)
    event_id = _get_first(fields, 1, b"")
    if isinstance(event_id, bytes):
        event_id = event_id.decode("utf-8", errors="replace")

    created_at = ""
    ts_bytes = _get_first(fields, 2)
    if isinstance(ts_bytes, bytes):
        created_at = _decode_timestamp(ts_bytes)

    actor_id_raw = _get_first(fields, 3)
    actor_id = actor_id_raw.decode("utf-8", errors="replace") if isinstance(actor_id_raw, bytes) else ""

    resume_cursor_raw = _get_first(fields, 4)
    resume_cursor = resume_cursor_raw.decode("utf-8", errors="replace") if isinstance(resume_cursor_raw, bytes) else ""

    operations = []
    for op_bytes in _get_all(fields, 5):
        if isinstance(op_bytes, bytes):
            operations.append(_decode_projection_operation(op_bytes))

    return {
        "id": event_id,
        "created_at": created_at,
        "actor_id": actor_id,
        "resume_cursor": resume_cursor,
        "operations": operations,
    }


def _decode_projection_operation(data: bytes) -> Dict[str, Any]:
    """Decode RealtimeProjectionOperation (oneof).

    We only care about room_timeline_event_upsert (field 10).

    Returns dict like:
        {"type": "room_timeline_event_upsert", "room_id": ..., "event": {...}, "includes": {...}}
        {"type": "unknown", "field": N}
    """
    fields = _decode_fields(data)

    # field 10 = room_timeline_event_upsert
    if 10 in fields:
        upsert_bytes = fields[10][0]
        if isinstance(upsert_bytes, bytes):
            return _decode_room_timeline_event_upsert(upsert_bytes)

    # Find which field is set for debugging
    for field_num in fields:
        if field_num != 10:
            type_names = {
                1: "room_upsert",
                2: "room_delete",
                3: "room_member_upsert",
                4: "room_member_delete",
                5: "room_typing_upsert",
                6: "room_typing_delete",
                7: "room_read_state_upsert",
                8: "room_read_state_delete",
                9: "room_subscription_upsert",
                10: "room_timeline_event_upsert",
                11: "room_timeline_event_delete",
            }
            return {"type": type_names.get(field_num, f"field_{field_num}"), "field": field_num}

    return {"type": "empty"}


def _decode_room_timeline_event_upsert(data: bytes) -> Dict[str, Any]:
    """Decode RealtimeProjectionRoomTimelineEventUpsert {
        string room_id = 1;
        chatto.api.v1.RoomTimelineEvent event = 2;
        chatto.api.v1.RoomTimelineIncludes includes = 3;
    }"""
    fields = _decode_fields(data)

    room_id_raw = _get_first(fields, 1)
    room_id = room_id_raw.decode("utf-8", errors="replace") if isinstance(room_id_raw, bytes) else ""

    event_dict = {}
    event_bytes = _get_first(fields, 2)
    if isinstance(event_bytes, bytes):
        event_dict = _decode_room_timeline_event(event_bytes)

    return {
        "type": "room_timeline_event_upsert",
        "room_id": room_id,
        "event": event_dict,
    }


def _decode_room_timeline_event(data: bytes) -> Dict[str, Any]:
    """Decode RoomTimelineEvent from the API proto.

    RoomTimelineEvent {
        string id = 1;
        google.protobuf.Timestamp created_at = 2;
        string room_id = 3;
        chatto.api.v1.RoomTimelineEventKind kind = 4;  // enum as varint
        chatto.api.v1.RoomTimelineEventMessagePosted message_posted = 5;
        // ... other event kinds (member_joined, etc.) at higher field numbers
    }

    RoomTimelineEventMessagePosted {
        chatto.api.v1.Message message = 1;
    }

    Message {
        string id = 1;
        string room_id = 2;
        string actor_id = 3;
        string body = 4;
        google.protobuf.Timestamp created_at = 5;
        optional string actor_login = 6;
        optional string actor_display_name = 7;
        optional chatto.api.v1.MessageThread thread = 8;
        // ... other fields
    }

    MessageThread {
        string thread_root_event_id = 1;
    }
    """
    fields = _decode_fields(data)

    event_id_raw = _get_first(fields, 1)
    event_id = event_id_raw.decode("utf-8", errors="replace") if isinstance(event_id_raw, bytes) else ""

    created_at = ""
    ts_bytes = _get_first(fields, 2)
    if isinstance(ts_bytes, bytes):
        created_at = _decode_timestamp(ts_bytes)

    room_id_raw = _get_first(fields, 3)
    room_id = room_id_raw.decode("utf-8", errors="replace") if isinstance(room_id_raw, bytes) else ""

    kind = _get_first(fields, 4, 0)  # enum as varint int

    # field 5 = message_posted (submessage)
    message = {}
    posted_bytes = _get_first(fields, 5)
    if isinstance(posted_bytes, bytes):
        message = _decode_message_posted(posted_bytes)

    return {
        "id": event_id,
        "createdAt": created_at,
        "roomId": room_id,
        "kind": kind,
        "messagePosted": message if message else None,
    }


def _decode_message_posted(data: bytes) -> Dict[str, Any]:
    """Decode RoomTimelineEventMessagePosted {
        chatto.api.v1.Message message = 1;
    }"""
    fields = _decode_fields(data)
    msg_bytes = _get_first(fields, 1)
    if isinstance(msg_bytes, bytes):
        return {"message": _decode_message(msg_bytes)}
    return {"message": {}}


def _decode_message(data: bytes) -> Dict[str, Any]:
    """Decode Message proto.

    Message {
        string id = 1;
        string room_id = 2;
        string actor_id = 3;
        string body = 4;
        google.protobuf.Timestamp created_at = 5;
        optional string actor_login = 6;
        optional string actor_display_name = 7;
        optional MessageThread thread = 8;
    }
    """
    fields = _decode_fields(data)

    def _str_field(fnum: int) -> str:
        val = _get_first(fields, fnum)
        if isinstance(val, bytes):
            return val.decode("utf-8", errors="replace")
        return ""

    msg_id = _str_field(1)
    room_id = _str_field(2)
    actor_id = _str_field(3)
    body = _str_field(4)

    created_at = ""
    ts_bytes = _get_first(fields, 5)
    if isinstance(ts_bytes, bytes):
        created_at = _decode_timestamp(ts_bytes)

    actor_login = _str_field(6)
    actor_display_name = _str_field(7)

    thread = {}
    thread_bytes = _get_first(fields, 8)
    if isinstance(thread_bytes, bytes):
        thread = _decode_thread(thread_bytes)

    return {
        "id": msg_id,
        "roomId": room_id,
        "actorId": actor_id,
        "body": body,
        "createdAt": created_at,
        "actorLogin": actor_login,
        "actorDisplayName": actor_display_name,
        "thread": thread,
    }


def _decode_thread(data: bytes) -> Dict[str, Any]:
    """Decode MessageThread {
        string thread_root_event_id = 1;
    }"""
    fields = _decode_fields(data)
    thread_root = _get_first(fields, 1)
    if isinstance(thread_root, bytes):
        thread_root = thread_root.decode("utf-8", errors="replace")
    else:
        thread_root = ""
    return {"threadRootEventId": thread_root}


def _decode_timestamp(data: bytes) -> str:
    """Decode google.protobuf.Timestamp {
        int64 seconds = 1;
        int32 nanos = 2;
    }
    Returns ISO 8601 string.
    """
    fields = _decode_fields(data)
    seconds = _get_first(fields, 1, 0)
    nanos = _get_first(fields, 2, 0)

    # Handle signed int64 (protobuf varints are unsigned, but int64 values
    # may be negative — reinterpret)
    if isinstance(seconds, int) and seconds >= (1 << 63):
        seconds -= (1 << 64)

    if not seconds:
        return ""

    # Convert to ISO format
    try:
        dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
        # Add nanosecond precision if present
        if nanos:
            # microsecond precision is the best Python supports
            micros = nanos // 1000
            dt = dt.replace(microsecond=micros % 1000000)
        return dt.isoformat().replace("+00:00", "Z")
    except (OSError, ValueError, OverflowError):
        return ""


def _decode_event_envelope(data: bytes) -> Dict[str, Any]:
    """Decode RealtimeEventEnvelope for transient events.

    RealtimeEventEnvelope {
        string id = 1;
        google.protobuf.Timestamp created_at = 2;
        optional string actor_id = 3;
        oneof event {
            RealtimeMessagePostedEvent message_posted = 10;
            RealtimeMessageEditedEvent message_edited = 11;
            ...
            RealtimeMentionNotificationEvent mention_notification = 88;
            RealtimeNewDirectMessageNotificationEvent new_direct_message_notification = 89;
        }
    }
    """
    fields = _decode_fields(data)

    event_id_raw = _get_first(fields, 1)
    event_id = event_id_raw.decode("utf-8", errors="replace") if isinstance(event_id_raw, bytes) else ""

    created_at = ""
    ts_bytes = _get_first(fields, 2)
    if isinstance(ts_bytes, bytes):
        created_at = _decode_timestamp(ts_bytes)

    actor_id = ""
    actor_raw = _get_first(fields, 3)
    if isinstance(actor_raw, bytes):
        actor_id = actor_raw.decode("utf-8", errors="replace")

    # Check for message_posted (field 10), mention_notification (field 88),
    # or new_direct_message_notification (field 89)
    event_type = "unknown"
    event_data = {}
    logger.info("Chatto WS: event envelope fields: %s", list(fields.keys()))
    if 10 in fields:
        event_type = "message_posted"
        raw = fields[10][0]
        if isinstance(raw, bytes):
            event_data = _decode_message_posted_event(raw)
    elif 11 in fields:
        event_type = "message_edited"
        raw = fields[11][0]
        if isinstance(raw, bytes):
            event_data = _decode_message_edited_event(raw)
    elif 12 in fields:
        event_type = "message_retracted"
        raw = fields[12][0]
        if isinstance(raw, bytes):
            event_data = _decode_message_retracted_event(raw)
    elif 46 in fields:
        event_type = "user_left_room"
        raw = fields[46][0]
        if isinstance(raw, bytes):
            event_data = _decode_room_event(raw)
    elif 90 in fields:
        event_type = "session_terminated"
        raw = fields[90][0]
        if isinstance(raw, bytes):
            event_data = _decode_session_terminated_event(raw)
    elif 40 in fields:
        event_type = "room_created"
        raw = fields[40][0]
        if isinstance(raw, bytes):
            event_data = _decode_room_event(raw)
    elif 45 in fields:
        event_type = "user_joined_room"
        raw = fields[45][0]
        if isinstance(raw, bytes):
            event_data = _decode_room_event(raw)
    elif 88 in fields:
        event_type = "mention_notification"
        raw = fields[88][0]
        if isinstance(raw, bytes):
            event_data = _decode_mention_notification(raw)
    elif 89 in fields:
        event_type = "new_direct_message_notification"
        raw = fields[89][0]
        if isinstance(raw, bytes):
            event_data = _decode_dm_notification(raw)

    return {
        "id": event_id,
        "createdAt": created_at,
        "actorId": actor_id,
        "type": event_type,
        "data": event_data,
    }

def _decode_message_posted_event(data: bytes) -> Dict[str, Any]:
    """Decode a RealtimeMessagePostedEvent.

    RealtimeMessagePostedEvent {
        string room_id = 1;
        string message_event_id = 2;
        optional string thread_root_event_id = 3;
    }
    """
    fields = _decode_fields(data)
    room_id_raw = _get_first(fields, 1)
    room_id = room_id_raw.decode("utf-8", errors="replace") if isinstance(room_id_raw, bytes) else ""
    event_id_raw = _get_first(fields, 2)
    message_event_id = event_id_raw.decode("utf-8", errors="replace") if isinstance(event_id_raw, bytes) else ""
    thread_root_raw = _get_first(fields, 3)
    thread_root_event_id = thread_root_raw.decode("utf-8", errors="replace") if isinstance(thread_root_raw, bytes) else ""
    return {
        "roomId": room_id,
        "messageEventId": message_event_id,
        "threadRootEventId": thread_root_event_id,
    }


def _decode_mention_notification(data: bytes) -> Dict[str, Any]:
    """Decode a mention notification.

    MentionNotification {
        string room_id = 1;
        string event_id = 2;
        // ... other fields
    }
    """
    fields = _decode_fields(data)
    room_id_raw = _get_first(fields, 1)
    room_id = room_id_raw.decode("utf-8", errors="replace") if isinstance(room_id_raw, bytes) else ""
    event_id_raw = _get_first(fields, 2)
    event_id = event_id_raw.decode("utf-8", errors="replace") if isinstance(event_id_raw, bytes) else ""
    return {"roomId": room_id, "eventId": event_id}


def _decode_dm_notification(data: bytes) -> Dict[str, Any]:
    """Decode a new direct message notification.

    NewDirectMessageNotification {
        string room_id = 1;
        string event_id = 2;
        // ... other fields
    }
    """
    fields = _decode_fields(data)
    room_id_raw = _get_first(fields, 1)
    room_id = room_id_raw.decode("utf-8", errors="replace") if isinstance(room_id_raw, bytes) else ""
    event_id_raw = _get_first(fields, 2)
    event_id = event_id_raw.decode("utf-8", errors="replace") if isinstance(event_id_raw, bytes) else ""
    return {"roomId": room_id, "eventId": event_id}


def _decode_room_event(data: bytes) -> Dict[str, Any]:
    """Decode a RealtimeRoomEvent.

    RealtimeRoomEvent {
        string room_id = 1;
    }
    """
    fields = _decode_fields(data)
    room_id_raw = _get_first(fields, 1)
    room_id = room_id_raw.decode("utf-8", errors="replace") if isinstance(room_id_raw, bytes) else ""
    return {"roomId": room_id}


def _decode_message_edited_event(data: bytes) -> Dict[str, Any]:
    """Decode a RealtimeMessageEditedEvent.

    RealtimeMessageEditedEvent {
        string room_id = 1;
        string message_event_id = 2;
    }
    """
    fields = _decode_fields(data)
    room_id = _get_first(fields, 1)
    room_id = room_id.decode("utf-8", errors="replace") if isinstance(room_id, bytes) else ""
    event_id = _get_first(fields, 2)
    message_event_id = event_id.decode("utf-8", errors="replace") if isinstance(event_id, bytes) else ""
    return {"roomId": room_id, "messageEventId": message_event_id}


def _decode_message_retracted_event(data: bytes) -> Dict[str, Any]:
    """Decode a RealtimeMessageRetractedEvent.

    RealtimeMessageRetractedEvent {
        string room_id = 1;
        string message_event_id = 2;
        optional string reason = 3;
    }
    """
    fields = _decode_fields(data)
    room_id = _get_first(fields, 1)
    room_id = room_id.decode("utf-8", errors="replace") if isinstance(room_id, bytes) else ""
    event_id = _get_first(fields, 2)
    message_event_id = event_id.decode("utf-8", errors="replace") if isinstance(event_id, bytes) else ""
    reason_raw = _get_first(fields, 3)
    reason = reason_raw.decode("utf-8", errors="replace") if isinstance(reason_raw, bytes) else ""
    return {"roomId": room_id, "messageEventId": message_event_id, "reason": reason}


def _decode_session_terminated_event(data: bytes) -> Dict[str, Any]:
    """Decode a RealtimeSessionTerminatedEvent.

    RealtimeSessionTerminatedEvent {
        string reason = 1;
    }
    """
    fields = _decode_fields(data)
    reason_raw = _get_first(fields, 1)
    reason = reason_raw.decode("utf-8", errors="replace") if isinstance(reason_raw, bytes) else ""
    return {"reason": reason}


def _decode_server_hello(data: bytes) -> Dict[str, Any]:
    """Decode RealtimeServerHello.

    RealtimeServerHello {
        uint32 protocol_version = 1;
        // ... other fields
    }
    """
    fields = _decode_fields(data)
    protocol_version = _get_first(fields, 1, 0)
    return {"protocolVersion": protocol_version}


def _decode_error(data: bytes) -> Dict[str, Any]:
    """Decode RealtimeError {
        string message = 1;
        uint32 code = 2;
    }"""
    fields = _decode_fields(data)
    msg_raw = _get_first(fields, 1)
    message = msg_raw.decode("utf-8", errors="replace") if isinstance(msg_raw, bytes) else ""
    code = _get_first(fields, 2, 0)
    return {"message": message, "code": code}


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #

def _ssl_context() -> ssl.SSLContext:
    """Create a default SSL context that verifies certificates."""
    return ssl.create_default_context()


def _rpc_request(
    base_url: str,
    path: str,
    token: Optional[str],
    body: dict,
) -> Tuple[int, dict]:
    """Make a ConnectRPC JSON POST request. Returns (status_code, response_dict)."""
    url = base_url.rstrip("/") + path
    headers = {
        "Content-Type": "application/json",
        "Connect-Protocol-Version": _CONNECT_RPC_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            err_body = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            err_body = {"error": raw}
        return e.code, err_body
    except Exception as e:
        return 0, {"error": str(e)}


def _auth_login(base_url: str, login: str, password: str) -> Optional[str]:
    """Login to Chatto and return the bearer token, or None on failure."""
    url = base_url.rstrip("/") + "/auth/login"
    headers = {"Content-Type": "application/json"}
    data = json.dumps({"login": login, "password": password}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    ctx = _ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=ctx) as resp:
            raw = resp.read().decode("utf-8")
            result = json.loads(raw)
            if result.get("success") and result.get("token"):
                return str(result["token"])
            logger.error("Chatto: login response did not include token: %s", raw)
            return None
    except Exception as e:
        logger.error("Chatto: login failed for %s — %s", base_url, e)
        return None


# --------------------------------------------------------------------------- #
# Adapter
# --------------------------------------------------------------------------- #


class ChattoAdapter(BasePlatformAdapter):
    """Chatto platform adapter — receives messages via WebSocket realtime,
    sends via ConnectRPC REST."""

    MAX_MESSAGE_LENGTH = 10000
    _SPLIT_THRESHOLD = 9900
    splits_long_messages = True

    def __init__(self, config, **kwargs):
        platform = Platform("chatto")
        super().__init__(config=config, platform=platform)

        extra = getattr(config, "extra", {}) or {}

        # --- Configuration (env > config.yaml extra) ---
        self._base_url = (
            os.getenv("CHATTO_URL", "").strip()
            or str(extra.get("url", "")).strip()
        )
        self._login = os.getenv("CHATTO_LOGIN", "").strip()
        self._password = os.getenv("CHATTO_PASSWORD", "").strip()

        raw_channels = os.getenv("CHATTO_CHANNELS", "").strip()
        if raw_channels:
            self._channel_ids = [c.strip() for c in raw_channels.split(",") if c.strip()]
        elif isinstance(extra.get("channels"), list):
            self._channel_ids = [str(c) for c in extra["channels"]]
        else:
            self._channel_ids = []

        self._home_channel = (
            os.getenv("CHATTO_HOME_CHANNEL", "").strip()
            or str(extra.get("home_channel", "")).strip()
        )

        self._require_mention = os.getenv("CHATTO_REQUIRE_MENTION", "").strip().lower()
        if self._require_mention:
            self._require_mention = self._require_mention in ("true", "1", "yes")
        else:
            self._require_mention = bool(extra.get("require_mention", True))
        # free_response_channels: room IDs where the bot responds without being tagged
        fr_env = os.getenv("CHATTO_FREE_RESPONSE_CHANNELS", "").strip()
        if fr_env:
            self._free_response_channels = set(c.strip() for c in fr_env.split(",") if c.strip())
        else:
            self._free_response_channels = set(
                str(c) for c in extra.get("free_response_channels", []) if str(c).strip()
            )

        # --- Runtime state ---
        self._token: Optional[str] = None
        self._user_id: str = ""
        self._user_login: str = ""
        self._user_display: str = ""
        self._room_names: Dict[str, str] = {}
        self._room_kinds: Dict[str, str] = {}
        self._our_thread_roots: set = set()  # thread root event IDs we created
        self._our_message_ids: set = set()  # message IDs we sent (for thread root detection)
        self._seen: Dict[str, OrderedDict] = {}  # room_id -> OrderedDict(event_id -> None)
        self._resume_cursor: Optional[str] = None
        self._watch_room_ids: List[str] = []
        self._ws_task: Optional[asyncio.Task] = None
        self._ws_ready: Optional[asyncio.Event] = None
        self._ws_active = False
        self._ws_ref = None  # reference to open websocket for dynamic resubscribe

        # Persistent typing indicator loops per room
        self._typing_tasks: Dict[str, asyncio.Task] = {}

        # Liveness probe (REST health check)
        self._liveness_interval_seconds = 60.0
        self._liveness_failure_threshold = 3
        self._liveness_task: Optional[asyncio.Task] = None

        # Member directory cache: user_id -> user info dict
        self._user_cache: Dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    async def _ensure_token(self) -> bool:
        """Login if we don't have a token, or re-login on 401."""
        if self._token:
            return True
        if not self._base_url or not self._login or not self._password:
            logger.error("Chatto: missing configuration (URL, login, or password)")
            self._set_fatal_error("config_missing", "CHATTO_URL/LOGIN/PASSWORD required", retryable=False)
            return False
        loop = asyncio.get_event_loop()
        token = await loop.run_in_executor(None, _auth_login, self._base_url, self._login, self._password)
        if not token:
            self._set_fatal_error("auth_failed", "Chatto login failed", retryable=True)
            return False
        self._token = token
        logger.info("Chatto: logged in as %s", self._login)
        return True

    async def _relogin(self) -> bool:
        """Force re-login (token expired)."""
        self._token = None
        return await self._ensure_token()

    async def _rpc(self, path: str, body: dict, *, retry: bool = True) -> Tuple[int, dict]:
        """Make an RPC call with automatic re-login on 401."""
        if not await self._ensure_token():
            return 0, {"error": "no token"}
        loop = asyncio.get_event_loop()
        status, resp = await loop.run_in_executor(None, _rpc_request, self._base_url, path, self._token, body)
        if status == 401 and retry:
            logger.debug("Chatto: got 401, re-logging in")
            if await self._relogin():
                status, resp = await loop.run_in_executor(None, _rpc_request, self._base_url, path, self._token, body)
        return status, resp

    # ------------------------------------------------------------------ #
    # Connection
    # ------------------------------------------------------------------ #

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Login, discover rooms, start WebSocket realtime connection."""
        if not await self._ensure_token():
            return False

        # Get our own user info
        status, resp = await self._rpc(_PATH_GET_VIEWER, {})
        if status != 200:
            msg = resp.get("message") or resp.get("error") or f"HTTP {status}"
            logger.error("Chatto: GetViewer failed — %s", msg)
            self._set_fatal_error("connect_failed", msg, retryable=True)
            return False
        user = resp.get("user", {}).get("profile", {})
        self._user_id = str(user.get("id", ""))
        self._user_login = str(user.get("login", ""))
        self._user_display = str(user.get("displayName", ""))

        # Discover rooms
        status, resp = await self._rpc(_PATH_LIST_ROOMS, {})
        if status != 200:
            msg = resp.get("message") or resp.get("error") or f"HTTP {status}"
            logger.error("Chatto: ListRooms failed — %s", msg)
            self._set_fatal_error("connect_failed", msg, retryable=True)
            return False

        rooms = resp.get("rooms", [])
        all_room_ids = []
        for entry in rooms:
            room = entry.get("room", {})
            rid = str(room.get("id", ""))
            if not rid:
                continue
            name = str(room.get("name", rid))
            kind = str(room.get("kind", ""))
            self._room_names[rid] = name
            self._room_kinds[rid] = kind
            viewer = entry.get("viewerState", {})
            is_member = viewer.get("isMember", False)
            # If user-specified channels, only watch those; otherwise watch all joined rooms
            if self._channel_ids:
                if rid in self._channel_ids and not is_member:
                    await self._join_room(rid)
                all_room_ids.append(rid)
            elif is_member:
                all_room_ids.append(rid)

        if self._channel_ids:
            watch = list(self._channel_ids)
        else:
            watch = all_room_ids

        if not watch:
            logger.error("Chatto: no rooms to watch (join a room or set CHATTO_CHANNELS)")
            self._set_fatal_error("config_missing", "no Chatto rooms to watch", retryable=False)
            return False

        # Ensure we're a member of each watched room
        for rid in watch:
            if self._room_kinds.get(rid) != "ROOM_KIND_DM":
                await self._join_room(rid)

        # Pick home channel
        if not self._home_channel:
            self._home_channel = watch[0]

        self._watch_room_ids = watch

        # Initialize seen for each room — seed from REST to avoid replaying history
        for rid in watch:
            self._seen[rid] = OrderedDict()
            await self._seed_room(rid)

        # Start WebSocket realtime connection
        if not await self._start_websocket():
            self._set_fatal_error(
                "ws_connect_failed",
                "Chatto WebSocket realtime connection failed",
                retryable=True,
            )
            return False

        self._mark_connected()
        self._start_liveness_probe()
        logger.info(
            "Chatto: connected to %s as %s, watching %d room(s) via WebSocket",
            self._base_url,
            self._user_display or self._user_login,
            len(watch),
        )

        # Broadcast online presence so the bot appears online in the member list
        try:
            await self.set_presence("online")
        except Exception:
            logger.debug("Chatto: set_presence(online) failed on connect", exc_info=True)

        return True

    async def disconnect(self) -> None:
        """Stop WebSocket, liveness probe, typing tasks, and clear state."""
        # Broadcast away presence before tearing down
        try:
            await self.set_presence("away")
        except Exception:
            logger.debug("Chatto: set_presence(away) failed on disconnect", exc_info=True)

        self._mark_disconnected()
        self._ws_active = False

        # Cancel liveness probe
        await self._cancel_liveness_task()

        # Cancel all typing tasks
        for chat_id in list(self._typing_tasks.keys()):
            await self.stop_typing(chat_id)

        if self._ws_task and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
            self._ws_task = None
        self._token = None

    # ------------------------------------------------------------------ #
    # Liveness probe
    # ------------------------------------------------------------------ #

    def _start_liveness_probe(self) -> None:
        """Start the periodic REST health probe."""
        if (
            self._liveness_interval_seconds <= 0
            or self._liveness_failure_threshold <= 0
        ):
            return
        if self._liveness_task and not self._liveness_task.done():
            return
        self._liveness_task = asyncio.create_task(self._liveness_loop())

    async def _cancel_liveness_task(self) -> None:
        """Cancel the liveness probe task."""
        task = self._liveness_task
        self._liveness_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _liveness_loop(self) -> None:
        """Periodically check if the REST API is alive via ViewerService/GetViewer.

        Also refreshes presence status on each successful probe so the bot
        stays showing as online — Chatto's presence expires if not refreshed.

        On ``threshold`` consecutive failures, set a fatal error with
        ``retryable=True`` so the gateway runner rebuilds the adapter.
        """
        interval = self._liveness_interval_seconds
        threshold = self._liveness_failure_threshold
        failures = 0
        while self._running:
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                return
            if not self._running:
                return
            try:
                status, resp = await self._rpc(_PATH_GET_VIEWER, {}, retry=False)
                if status == 200:
                    failures = 0
                    # Refresh presence to keep showing as online
                    try:
                        await self.set_presence("online")
                    except Exception:
                        logger.debug("Chatto: presence refresh failed", exc_info=True)
                    continue
                # Non-200 — count as failure
                reason = f"HTTP {status}"
            except asyncio.CancelledError:
                return
            except Exception as e:
                reason = str(e)

            failures += 1
            logger.warning(
                "Chatto: liveness probe failed (%s, %d/%d)",
                reason,
                failures,
                threshold,
            )
            if failures < threshold:
                continue

            # Threshold exceeded — force reconnect
            logger.error(
                "Chatto: liveness probe failed %d times consecutively; forcing reconnect",
                failures,
            )
            self._set_fatal_error(
                "chatto_liveness_failed",
                f"Chatto REST API liveness check failed: {reason}",
                retryable=True,
            )
            # Cancel the WebSocket to trigger reconnect
            if self._ws_task and not self._ws_task.done():
                self._ws_task.cancel()
            return

    async def _join_room(self, room_id: str) -> None:
        """Join a room if not already a member."""
        status, resp = await self._rpc(_PATH_JOIN_ROOM, {"roomId": room_id})
        if status == 200:
            logger.debug("Chatto: joined room %s (%s)", room_id, self._room_names.get(room_id, room_id))
        elif status == 403 or (resp.get("code") == "permission_denied"):
            logger.debug("Chatto: already a member of %s or cannot join", room_id)
        else:
            logger.debug("Chatto: join room %s returned %d — %s", room_id, status, resp.get("message", ""))

    async def _seed_room(self, room_id: str) -> None:
        """Seed high-water mark from the newest events so a restart doesn't replay history."""
        status, resp = await self._rpc(_PATH_GET_ROOM_EVENTS, {"roomId": room_id})
        if status != 200:
            logger.debug("Chatto: seed GetRoomEvents for %s returned %d", room_id, status)
            return
        events = resp.get("page", {}).get("events", [])
        for ev in events:
            ev_id = str(ev.get("id", ""))
            if ev_id:
                self._mark_seen(room_id, ev_id)

    def _mark_seen(self, room_id: str, event_id: str) -> None:
        seen = self._seen.setdefault(room_id, OrderedDict())
        seen[event_id] = None
        while len(seen) > _SEEN_CAP:
            seen.popitem(last=False)

    def _is_seen(self, room_id: str, event_id: str) -> bool:
        return event_id in self._seen.get(room_id, {})

    # ------------------------------------------------------------------ #
    # WebSocket Realtime Transport
    # ------------------------------------------------------------------ #

    def _websocket_url(self) -> str:
        """Build the WebSocket URL from the base HTTP URL."""
        parsed = urlsplit(self._base_url.strip())
        scheme = {"http": "ws", "https": "wss"}.get(parsed.scheme, parsed.scheme)
        if scheme not in ("ws", "wss") or not parsed.netloc:
            raise ValueError(f"Chatto URL must use http(s) or ws(s), got {parsed.scheme}")
        path = parsed.path.rstrip("/") + _WS_PATH
        return urlunsplit((scheme, parsed.netloc, path, parsed.query, ""))

    async def _start_websocket(self) -> bool:
        """Start the WebSocket realtime loop. Returns True if handshake succeeds."""
        try:
            import websockets  # noqa: F401 (availability probe)
            self._websocket_url()
        except Exception as e:
            logger.error("Chatto: WebSocket transport unavailable (%s)", e)
            return False

        self._ws_ready = asyncio.Event()
        self._ws_task = asyncio.create_task(self._websocket_loop())
        try:
            await asyncio.wait_for(self._ws_ready.wait(), timeout=_WS_AUTH_TIMEOUT + 10)
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning("Chatto: WebSocket did not authenticate in time")
            self._ws_active = False
            if self._ws_task and not self._ws_task.done():
                self._ws_task.cancel()
                try:
                    await self._ws_task
                except asyncio.CancelledError:
                    pass
            self._ws_task = None
            return False
        return True

    async def _websocket_loop(self) -> None:
        """Persistent WebSocket connection with bounded reconnect backoff.

        Protocol flow:
        1. Connect to /api/realtime
        2. Send RealtimeClientFrame with hello (protocol_version=1, bearer_token)
        3. Receive RealtimeServerFrame with hello (RealtimeServerHello)
        4. Send RealtimeClientFrame with subscribe_events (retained_room_ids)
        5. Receive RealtimeServerFrame with subscribed
        6. Receive projection_event frames with room_timeline_event_upsert operations
        7. Also handle transient event frames (mention/DM notifications)
        8. Send periodic ping frames for keepalive
        """
        import websockets

        backoff = _WS_RECONNECT_INITIAL_BACKOFF
        try:
            while True:
                try:
                    ws_url = self._websocket_url()
                    extra_headers = {}
                    # Some WebSocket servers accept auth via header
                    if self._token:
                        extra_headers["Authorization"] = f"Bearer {self._token}"

                    async with websockets.connect(
                        ws_url,
                        additional_headers=extra_headers if extra_headers else None,
                        open_timeout=_WS_AUTH_TIMEOUT,
                        close_timeout=5,
                        ping_interval=None,  # we send our own protocol-level pings
                        ping_timeout=None,
                        max_size=_WS_MAX_MESSAGE_BYTES,
                    ) as websocket:
                        # Step 1: Send hello
                        hello_body = _encode_client_hello(self._token or "")
                        hello_frame = _encode_client_frame_hello(hello_body)
                        await websocket.send(hello_frame)
                        logger.debug("Chatto WS: sent hello (protocol_version=%d)", _REALTIME_PROTOCOL_VERSION)

                        # Step 2: Receive server hello
                        raw = await asyncio.wait_for(websocket.recv(), timeout=_WS_AUTH_TIMEOUT)
                        server_frame = _decode_server_frame(raw)
                        if server_frame["type"] != "hello":
                            if server_frame["type"] == "error":
                                err = _decode_error(server_frame["data"]) if server_frame["data"] else {}
                                raise ConnectionError(f"Server error during hello: {err.get('message', 'unknown')}")
                            raise ConnectionError(f"Expected server hello, got {server_frame['type']}")
                        server_hello = _decode_server_hello(server_frame["data"]) if server_frame["data"] else {}
                        proto_ver = server_hello.get("protocolVersion", 0)
                        logger.info("Chatto WS: server hello received (protocol_version=%s)", proto_ver)

                        # Step 3: Send subscribe_events
                        subscribe_body = _encode_subscribe_events(
                            resume_cursor=self._resume_cursor,
                            retained_room_ids=self._watch_room_ids,
                        )
                        subscribe_frame = _encode_client_frame_subscribe(subscribe_body)
                        await websocket.send(subscribe_frame)
                        logger.debug(
                            "Chatto WS: sent subscribe_events for %d room(s), cursor=%s",
                            len(self._watch_room_ids),
                            self._resume_cursor or "(none)",
                        )

                        # Step 4: Receive subscribed confirmation
                        raw = await asyncio.wait_for(websocket.recv(), timeout=_WS_AUTH_TIMEOUT)
                        server_frame = _decode_server_frame(raw)
                        if server_frame["type"] == "error":
                            err = _decode_error(server_frame["data"]) if server_frame["data"] else {}
                            raise ConnectionError(f"Server error during subscribe: {err.get('message', 'unknown')}")
                        if server_frame["type"] not in ("subscribed", "caught_up", "projection_event"):
                            # Be lenient — some servers may send events immediately
                            logger.debug("Chatto WS: received %s after subscribe (expected subscribed)", server_frame["type"])
                            # Process it as an event if it is one
                            if server_frame["type"] == "projection_event":
                                await self._handle_projection_event(server_frame["data"])
                            elif server_frame["type"] == "event":
                                await self._handle_transient_event(server_frame["data"])

                        self._ws_active = True
                        if self._ws_ready is not None and not self._ws_ready.is_set():
                            self._ws_ready.set()
                        backoff = _WS_RECONNECT_INITIAL_BACKOFF
                        logger.info("Chatto WS: subscribed, listening for events")

                        # Store websocket reference for dynamic resubscribe
                        self._ws_ref = websocket

                        # Step 5: Main event loop with ping keepalive
                        await self._websocket_event_loop(websocket)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._ws_active = False
                    if self._ws_ready is not None and not self._ws_ready.is_set():
                        # Signal failure to connect() waiter
                        self._ws_ready.set()
                    logger.warning("Chatto WS: disconnected; retrying in %.1fs: %s", backoff, e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _WS_RECONNECT_MAX_BACKOFF)
        finally:
            self._ws_active = False
            self._ws_ref = None

    async def _websocket_event_loop(self, websocket) -> None:
        """Main event loop: receive frames and send periodic pings."""
        logger.info("Chatto WS: event loop started, waiting for frames")
        ping_task = asyncio.create_task(self._ping_loop(websocket))
        try:
            async for raw in websocket:
                logger.info("Chatto WS: received frame (%d bytes)", len(raw) if raw else 0)
                if isinstance(raw, str):
                    # Shouldn't happen with binary protobuf, but handle gracefully
                    logger.debug("Chatto WS: received text frame (unexpected)")
                    continue

                try:
                    server_frame = _decode_server_frame(raw)
                except (ValueError, IndexError) as e:
                    logger.warning("Chatto WS: failed to decode server frame: %s", e)
                    continue

                frame_type = server_frame["type"]
                logger.info("Chatto WS: frame type=%s size=%d", frame_type, len(raw))
                frame_data = server_frame["data"]

                if frame_type == "projection_event":
                    await self._handle_projection_event(frame_data)
                elif frame_type == "event":
                    await self._handle_transient_event(frame_data)
                elif frame_type == "heartbeat":
                    logger.debug("Chatto WS: heartbeat received")
                elif frame_type == "pong":
                    logger.debug("Chatto WS: pong received")
                elif frame_type == "caught_up":
                    logger.debug("Chatto WS: caught_up received")
                elif frame_type == "error":
                    err = _decode_error(frame_data) if frame_data else {}
                    logger.warning("Chatto WS: server error: %s (code=%s)", err.get("message", "unknown"), err.get("code"))
                elif frame_type == "close":
                    msg = ""
                    if frame_data:
                        try:
                            close_fields = _decode_fields(frame_data)
                            msg_raw = _get_first(close_fields, 1)
                            if isinstance(msg_raw, bytes):
                                msg = msg_raw.decode("utf-8", errors="replace")
                        except Exception:
                            pass
                    logger.info("Chatto WS: server sent close: %s", msg or "(no message)")
                    raise ConnectionError(f"Server closed: {msg}")
                elif frame_type == "hello":
                    # Unexpected re-hello, ignore
                    logger.debug("Chatto WS: unexpected hello frame")
                elif frame_type == "subscribed":
                    logger.debug("Chatto WS: re-subscribed confirmation")
                else:
                    logger.debug("Chatto WS: unknown frame type %s", frame_type)
        finally:
            logger.info("Chatto WS: event loop ended")
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

    async def _ping_loop(self, websocket) -> None:
        """Send periodic ping frames for keepalive."""
        try:
            while True:
                await asyncio.sleep(_WS_PING_INTERVAL)
                ping_body = _encode_ping()
                ping_frame = _encode_client_frame_ping(ping_body)
                await websocket.send(ping_frame)
                logger.info("Chatto WS: ping sent")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.info("Chatto WS: ping loop exited: %s", e)
            # WebSocket closed or error — exit silently, the main loop will handle reconnect
            pass

    # ------------------------------------------------------------------ #
    # Event handling
    # ------------------------------------------------------------------ #

    async def _handle_projection_event(self, data: bytes) -> None:
        """Handle a RealtimeProjectionEvent — parse operations for messages."""
        if not data:
            return

        try:
            event = _decode_projection_event(data)
        except (ValueError, IndexError) as e:
            logger.warning("Chatto WS: failed to decode projection event: %s", e)
            return

        # Update resume cursor if provided
        cursor = event.get("resume_cursor")
        if cursor:
            self._resume_cursor = cursor

        operations = event.get("operations", [])
        for op in operations:
            if op.get("type") == "room_timeline_event_upsert":
                await self._handle_timeline_event_upsert(op)
            # Other operation types (room_upsert, room_member_upsert, etc.) are
            # not relevant to message delivery — ignore them.

    async def _handle_timeline_event_upsert(self, op: dict) -> None:
        """Handle a room_timeline_event_upsert operation."""
        room_id = op.get("room_id", "")
        event = op.get("event", {})
        if not event:
            return

        ev_id = str(event.get("id", ""))
        if not ev_id:
            return

        # De-dupe: skip events we've already seen
        if self._is_seen(room_id, ev_id):
            return
        self._mark_seen(room_id, ev_id)

        # Only handle messagePosted events
        posted = event.get("messagePosted")
        if not posted:
            return

        msg = posted.get("message", {})
        if not msg:
            return

        await self._dispatch_message(msg, room_id)

    async def _handle_transient_event(self, data: bytes) -> None:
        """Handle a transient RealtimeEventEnvelope (message_posted, mentions, DMs).

        These are signal-only events — they contain room_id and event_id but NOT
        the message body. We fetch the actual message via REST as a fallback.
        """
        if not data:
            return

        try:
            envelope = _decode_event_envelope(data)
        except (ValueError, IndexError) as e:
            logger.warning("Chatto WS: failed to decode transient event: %s", e)
            return

        event_type = envelope.get("type", "unknown")
        event_data = envelope.get("data", {})

        if event_type == "message_posted":
            room_id = event_data.get("roomId", "")
            event_id = event_data.get("messageEventId", "")
            thread_root = event_data.get("threadRootEventId", "")
            logger.info("Chatto WS: message_posted in room %s, event %s (thread=%s)", room_id, event_id, thread_root or "none")
            if room_id and event_id and not self._is_seen(room_id, event_id):
                await self._fetch_and_dispatch_event(room_id, event_id, thread_root)
        elif event_type == "mention_notification":
            room_id = event_data.get("roomId", "")
            event_id = event_data.get("eventId", "")
            logger.info("Chatto WS: mention notification in room %s for event %s", room_id, event_id)
            if room_id and event_id and not self._is_seen(room_id, event_id):
                await self._fetch_and_dispatch_event(room_id, event_id)
        elif event_type == "new_direct_message_notification":
            room_id = event_data.get("roomId", "")
            event_id = event_data.get("eventId", "")
            logger.info("Chatto WS: new DM notification in room %s for event %s", room_id, event_id)
            if room_id and event_id and not self._is_seen(room_id, event_id):
                await self._fetch_and_dispatch_event(room_id, event_id)
        elif event_type == "user_joined_room":
            room_id = event_data.get("roomId", "")
            actor_id = envelope.get("actorId", "")
            logger.info("Chatto WS: user_joined_room room=%s actor=%s", room_id, actor_id)
            # If WE joined a room (or someone else joined and we should watch it),
            # refresh room list and resubscribe
            if room_id and room_id not in self._watch_room_ids:
                await self._refresh_rooms()
        elif event_type == "room_created":
            room_id = event_data.get("roomId", "")
            logger.info("Chatto WS: room_created room=%s", room_id)
            # A new room was created — check if we should join/watch it
            if room_id and room_id not in self._watch_room_ids:
                await self._refresh_rooms()
        elif event_type == "user_left_room":
            room_id = event_data.get("roomId", "")
            actor_id = envelope.get("actorId", "")
            logger.info("Chatto WS: user_left_room room=%s actor=%s", room_id, actor_id)
            # If WE left a room, stop watching it
            if room_id and actor_id == self._user_id and room_id in self._watch_room_ids:
                self._watch_room_ids.remove(room_id)
                logger.info("Chatto WS: stopped watching room %s (we left)", room_id)
        elif event_type == "message_edited":
            room_id = event_data.get("roomId", "")
            event_id = event_data.get("messageEventId", "")
            logger.info("Chatto WS: message_edited in room %s, event %s", room_id, event_id)
            # Log edit — could re-fetch for context if needed in the future
        elif event_type == "message_retracted":
            room_id = event_data.get("roomId", "")
            event_id = event_data.get("messageEventId", "")
            reason = event_data.get("reason", "")
            logger.info("Chatto WS: message_retracted in room %s, event %s (reason=%s)", room_id, event_id, reason or "none")
            # Mark the message as seen so we don't try to dispatch it later
            if room_id and event_id:
                self._mark_seen(room_id, event_id)
        elif event_type == "session_terminated":
            reason = event_data.get("reason", "")
            logger.warning("Chatto WS: session terminated by server (reason=%s) — forcing reconnect", reason or "none")
            # Close the websocket to trigger reconnect with backoff
            if self._ws_ref:
                try:
                    await self._ws_ref.close()
                except Exception:
                    pass
        else:
            logger.debug("Chatto WS: unknown transient event type: %s", event_type)

    async def _fetch_and_dispatch_event(self, room_id: str, event_id: str, thread_root_event_id: str = "") -> None:
        """Fetch a single event by ID via REST and dispatch it.

        Used as a fallback when the projection_event for a transient
        notification (mention/DM) hasn't arrived yet.
        When thread_root_event_id is set, fetches from the thread timeline
        instead of the room timeline.
        """
        self._mark_seen(room_id, event_id)
        if thread_root_event_id:
            # Thread reply — use GetThreadEvents
            status, resp = await self._rpc(_PATH_GET_THREAD_EVENTS, {
                "roomId": room_id,
                "threadRootEventId": thread_root_event_id,
            })
        else:
            # Regular room message — use GetRoomEvents
            status, resp = await self._rpc(_PATH_GET_ROOM_EVENTS, {"roomId": room_id})
        if status != 200:
            logger.warning("Chatto WS: REST fallback fetch failed for event %s (status=%d)", event_id, status)
            return
        events = resp.get("page", {}).get("events", [])
        for ev in events:
            ev_id = str(ev.get("id", ""))
            if ev_id == event_id:
                posted = ev.get("messagePosted")
                if posted:
                    msg = posted.get("message", {})
                    if msg:
                        # Ensure thread info is set on the message so
                        # _dispatch_message can extract the thread root ID.
                        if thread_root_event_id and not msg.get("thread"):
                            msg["thread"] = {"threadRootEventId": thread_root_event_id}
                        logger.info("Chatto WS: dispatching event %s via REST fallback (thread=%s)", event_id, thread_root_event_id or "none")
                        await self._dispatch_message(msg, room_id)
                    return
        logger.warning("Chatto WS: event %s not found in room %s events (thread=%s)", event_id, room_id, thread_root_event_id or "none")

    async def _refresh_rooms(self) -> None:
        """Re-list rooms and subscribe to any new ones dynamically.

        Called when a room_created or user_joined_room event arrives.
        This avoids requiring a gateway restart to pick up new rooms.
        """
        try:
            status, resp = await self._rpc(_PATH_LIST_ROOMS, {})
            if status != 200:
                logger.warning("Chatto WS: _refresh_rooms ListRooms failed (status=%d)", status)
                return

            rooms = resp.get("rooms", [])
            new_room_ids = []
            for entry in rooms:
                room = entry.get("room", {})
                rid = str(room.get("id", ""))
                if not rid:
                    continue
                name = str(room.get("name", rid))
                kind = str(room.get("kind", ""))
                self._room_names[rid] = name
                self._room_kinds[rid] = kind
                viewer = entry.get("viewerState", {})
                is_member = viewer.get("isMember", False)

                # If we're a member and not already watching, add it
                if is_member and rid not in self._watch_room_ids:
                    new_room_ids.append(rid)

            if not new_room_ids:
                return

            logger.info("Chatto WS: discovered %d new room(s): %s", len(new_room_ids), new_room_ids)

            # Join and seed each new room
            for rid in new_room_ids:
                if self._room_kinds.get(rid) != "ROOM_KIND_DM":
                    await self._join_room(rid)
                self._seen[rid] = OrderedDict()
                await self._seed_room(rid)
                self._watch_room_ids.append(rid)

            # Resubscribe to all rooms (including new ones) via the open websocket
            if self._ws_ref and self._ws_active:
                subscribe_body = _encode_subscribe_events(
                    resume_cursor=self._resume_cursor,
                    retained_room_ids=self._watch_room_ids,
                )
                subscribe_frame = _encode_client_frame_subscribe(subscribe_body)
                await self._ws_ref.send(subscribe_frame)
                logger.info("Chatto WS: resubscribed with %d room(s)", len(self._watch_room_ids))
            else:
                logger.warning("Chatto WS: cannot resubscribe — websocket not active")

        except Exception:
            logger.warning("Chatto WS: _refresh_rooms failed", exc_info=True)

    async def _dispatch_message(self, msg: dict, room_id: str) -> None:
        """Build a MessageEvent and hand it to the base class handler.

        This method is identical to the polling version — it receives a
        message dict (decoded from protobuf) and dispatches it through the
        standard Hermes message pipeline.
        """
        if not self._message_handler:
            return

        actor_id = str(msg.get("actorId", ""))
        # Skip our own messages
        if actor_id == self._user_id:
            return

        # Best-effort: cache the sender's display name for richer message context
        if actor_id and actor_id not in self._user_cache:
            try:
                await self.get_user(actor_id)
            except Exception:
                logger.debug("Chatto: get_user(%s) failed during dispatch", actor_id, exc_info=True)

        body = str(msg.get("body", ""))
        if not body:
            return

        msg_id = str(msg.get("id", ""))
        chat_type = "dm" if self._room_kinds.get(room_id) == "ROOM_KIND_DM" else "group"

        # Mention detection
        is_dm = chat_type == "dm"
        mentioned = False
        if self._user_login:
            mentioned = f"@{self._user_login}" in body
        if self._user_display:
            mentioned = mentioned or f"@{self._user_display}" in body

        if self._require_mention and not is_dm and not mentioned:
            # Allow free-response rooms (like Discord's free_response_channels)
            if room_id not in self._free_response_channels:
                return

        # For DMs, always respond. For rooms with require_mention, only respond when mentioned.
        # Strip the mention from the text for the agent
        text = body
        if mentioned and not is_dm:
            # Remove mention prefix if present
            if self._user_login and text.startswith(f"@{self._user_login}"):
                text = text[len(f"@{self._user_login}"):].lstrip()
            elif self._user_display and text.startswith(f"@{self._user_display}"):
                text = text[len(f"@{self._user_display}"):].lstrip()

        # Resolve user display name from actorLogin or actorDisplayName
        user_name = str(msg.get("actorLogin", "")) or str(msg.get("actorDisplayName", actor_id))

        thread_id = None
        thread_info = msg.get("thread", {})
        if thread_info and str(thread_info.get("threadRootEventId", "")) != msg_id:
            thread_id = str(thread_info.get("threadRootEventId", ""))

        source = self.build_source(
            chat_id=room_id,
            chat_name=self._room_names.get(room_id, room_id),
            chat_type=chat_type,
            user_id=actor_id,
            user_name=user_name,
            thread_id=thread_id,
        )

        created_at_str = str(msg.get("createdAt", ""))
        try:
            timestamp = datetime.fromisoformat(created_at_str.replace("Z", "+00:00")) if created_at_str else datetime.now()
        except (ValueError, TypeError):
            timestamp = datetime.now()

        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=msg_id,
            timestamp=timestamp,
            raw_message=msg,
        )

        await self.handle_message(event)

        # ------------------------------------------------------------------ #
        # Read state & notification dismissal (best-effort, Chatto-unique)
        # ------------------------------------------------------------------ #
        try:
            await self.mark_room_as_read(room_id)
        except Exception:
            logger.debug("Chatto: mark_room_as_read failed for %s", room_id, exc_info=True)
        try:
            await self.dismiss_all_notifications()
        except Exception:
            logger.debug("Chatto: dismiss_all_notifications failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Sending (REST — unchanged from polling version)
    # ------------------------------------------------------------------ #

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a Chatto room.

        Long messages are split into chunks via ``truncate_message`` and
        each chunk is sent as a separate CreateMessage call.  The first
        chunk's message ID is returned as ``message_id``.

        When ``auto_thread`` is enabled and the incoming message was a
        regular room message (not already in a thread), the first chunk is
        sent as a room message and its ID becomes the thread root. Subsequent
        chunks are sent in that thread. This mirrors Discord's auto_thread
        behavior.
        """
        if not content:
            return SendResult(success=False, error="Empty message")

        formatted = self.format_message(content) if hasattr(self, "format_message") else content
        chunks = self.truncate_message(formatted, self.MAX_MESSAGE_LENGTH)

        # Thread support — resolve thread_id once
        # DM rooms don't support threads, so skip threading for DMs
        thread_id = (metadata or {}).get("thread_id")
        if reply_to:
            # reply_to might be the incoming message ID. If we already have
            # thread_id from metadata, keep it (it's the thread root).
            # Only use reply_to as thread_id if we don't already have one.
            if not thread_id:
                thread_id = reply_to
        # Check if this is a DM room — DMs don't support threads
        room_kind = self._room_kinds.get(str(chat_id), "")
        is_dm = room_kind == "ROOM_KIND_DM" or room_kind == "dm"
        if is_dm:
            thread_id = None

        # Auto-thread: by default, Chatto creates a thread for replies to room
        # messages (not DMs, not already in a thread). This keeps conversations
        # organized in the room. Can be disabled via extra.auto_thread=false.
        auto_thread_enabled = os.getenv("CHATTO_AUTO_THREAD", "").strip().lower()
        if auto_thread_enabled:
            auto_thread_enabled = auto_thread_enabled in ("true", "1", "yes")
        else:
            auto_thread_enabled = True  # default: enabled
        use_auto_thread = auto_thread_enabled and not thread_id and not is_dm

        message_ids: List[str] = []
        last_resp: Optional[dict] = None
        last_error: Optional[str] = None
        retryable = False

        for i, chunk in enumerate(chunks):
            body: Dict[str, Any] = {"roomId": str(chat_id), "body": chunk}
            # If we have a thread_id, send in the thread
            if thread_id:
                body["threadRootEventId"] = str(thread_id)

            status, resp = await self._rpc(_PATH_CREATE_MESSAGE, body)
            if status != 200:
                err = resp.get("message") or resp.get("error") or f"HTTP {status}"
                last_error = err
                retryable = status >= 500 or status == 401
                break

            last_resp = resp
            msg = resp.get("message", {})
            msg_id = str(msg.get("id", "")) if msg else ""
            if msg_id:
                self._mark_seen(str(chat_id), msg_id)
                message_ids.append(msg_id)
                self._our_message_ids.add(msg_id)
                # If we sent a message WITHOUT a thread_id, this message could
                # become a thread root if someone replies to it
                if not thread_id:
                    self._our_thread_roots.add(msg_id)
                # Auto-thread: first chunk becomes the thread root,
                # subsequent chunks go in the thread
                if use_auto_thread and i == 0 and not thread_id:
                    thread_id = msg_id

        if last_error and not message_ids:
            return SendResult(success=False, error=last_error, retryable=retryable)

        first_id = message_ids[0] if message_ids else ""

        # ------------------------------------------------------------------ #
        # Thread following (best-effort, Chatto-unique)
        # ------------------------------------------------------------------ #
        if thread_id and message_ids:
            try:
                await self._follow_thread(str(chat_id), str(thread_id))
            except Exception:
                logger.debug("Chatto: _follow_thread failed for room=%s thread=%s",
                             chat_id, thread_id, exc_info=True)

        return SendResult(success=True, message_id=first_id, raw_response=last_resp)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Start a persistent typing indicator for a room.

        Sends a typing ping every 10 seconds (Chatto's indicator likely
        lasts ~8-10s).  The background loop runs until ``stop_typing()``
        is called or the task is cancelled.
        """
        if chat_id in self._typing_tasks:
            return  # already running

        async def _typing_loop() -> None:
            try:
                while True:
                    try:
                        await self._rpc(
                            _PATH_UPDATE_TYPING,
                            {"roomId": str(chat_id), "typing": True},
                            retry=False,
                        )
                    except asyncio.CancelledError:
                        return
                    except Exception:
                        pass
                    await asyncio.sleep(10)
            except asyncio.CancelledError:
                pass
            finally:
                self._typing_tasks.pop(chat_id, None)

        self._typing_tasks[chat_id] = asyncio.create_task(_typing_loop())

    async def stop_typing(self, chat_id: str) -> None:
        """Stop the persistent typing indicator for a room."""
        task = self._typing_tasks.pop(chat_id, None)
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a chat/room."""
        name = self._room_names.get(chat_id, chat_id)
        kind = self._room_kinds.get(chat_id, "")
        chat_type = "dm" if kind == "ROOM_KIND_DM" else "group"
        return {
            "name": name,
            "type": chat_type,
        }

    # ------------------------------------------------------------------ #
    # Reactions
    # ------------------------------------------------------------------ #

    @staticmethod
    def _emoji_to_shortcode(emoji: str) -> str:
        """Convert a unicode emoji to a Chatto shortcode name.

        If the emoji is already a shortcode (no unicode mapping found),
        return it as-is.
        """
        shortcode = _EMOJI_TO_SHORTCODE.get(emoji)
        if shortcode:
            return shortcode
        # Already a shortcode like "thumbsup" — return as-is
        return emoji

    async def send_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Add a reaction to a message via MessageService/AddReaction."""
        shortcode = self._emoji_to_shortcode(emoji)
        body = {
            "roomId": str(chat_id),
            "messageEventId": str(message_id),
            "emoji": shortcode,
        }
        try:
            status, resp = await self._rpc(_PATH_ADD_REACTION, body, retry=False)
            if status == 200:
                return True
            logger.debug(
                "Chatto: AddReaction failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: AddReaction error: %s", e)
            return False

    async def remove_reaction(self, chat_id: str, message_id: str, emoji: str) -> bool:
        """Remove a reaction from a message via MessageService/RemoveReaction."""
        shortcode = self._emoji_to_shortcode(emoji)
        body = {
            "roomId": str(chat_id),
            "messageEventId": str(message_id),
            "emoji": shortcode,
        }
        try:
            status, resp = await self._rpc(_PATH_REMOVE_REACTION, body, retry=False)
            if status == 200:
                return True
            logger.debug(
                "Chatto: RemoveReaction failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: RemoveReaction error: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # Read state management (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def mark_room_as_read(self, room_id: str) -> bool:
        """Mark a room as read via RoomService/MarkRoomAsRead."""
        try:
            status, resp = await self._rpc(
                _PATH_MARK_ROOM_READ, {"roomId": str(room_id)}, retry=False
            )
            if status == 200:
                return True
            logger.debug(
                "Chatto: MarkRoomAsRead failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: MarkRoomAsRead error: %s", e)
            return False

    async def mark_thread_as_read(self, room_id: str, thread_root_event_id: str) -> bool:
        """Mark a thread as read via ThreadService/MarkThreadAsRead."""
        try:
            status, resp = await self._rpc(
                _PATH_MARK_THREAD_READ,
                {"roomId": str(room_id), "threadRootEventId": str(thread_root_event_id)},
                retry=False,
            )
            if status == 200:
                return True
            logger.debug(
                "Chatto: MarkThreadAsRead failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: MarkThreadAsRead error: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # DM initiation (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def start_dm(self, user_id: str) -> Optional[str]:
        """Start a direct message with a user via RoomService/StartDM.

        Returns the room ID on success, or None on failure.
        """
        body: Dict[str, Any] = {"participantIds": [str(user_id)] if user_id else []}
        try:
            status, resp = await self._rpc(_PATH_START_DM, body, retry=True)
            if status != 200:
                logger.debug(
                    "Chatto: StartDM failed (%s): %s",
                    status,
                    resp.get("message") or resp.get("error") or "",
                )
                return None
            room = resp.get("room", {})
            rid = str(room.get("id", "")) if room else ""
            if rid:
                self._room_names[rid] = self._room_names.get(rid, "")
                self._room_kinds[rid] = "ROOM_KIND_DM"
                return rid
            logger.debug("Chatto: StartDM returned no room id: %s", resp)
            return None
        except Exception as e:
            logger.debug("Chatto: StartDM error: %s", e)
            return None

    # ------------------------------------------------------------------ #
    # Thread following (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def _follow_thread(self, room_id: str, thread_root_event_id: str) -> None:
        """Best-effort: follow a thread via ThreadService/FollowThread."""
        try:
            status, resp = await self._rpc(
                _PATH_FOLLOW_THREAD,
                {"roomId": str(room_id), "threadRootEventId": str(thread_root_event_id)},
                retry=False,
            )
            if status != 200:
                logger.debug(
                    "Chatto: FollowThread failed (%s): %s",
                    status,
                    resp.get("message") or resp.get("error") or "",
                )
        except Exception as e:
            logger.debug("Chatto: FollowThread error: %s", e)

    # ------------------------------------------------------------------ #
    # Room creation (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def create_room(
        self,
        name: str,
        description: str = "",
        group_id: str = "",
        universal: bool = True,
    ) -> Optional[str]:
        """Create an ad-hoc room via RoomService/CreateRoom.

        Returns the room ID on success, or None on failure.
        """
        body: Dict[str, Any] = {
            "name": name,
            "description": description,
            "groupId": group_id,
            "universal": universal,
        }
        try:
            status, resp = await self._rpc(_PATH_CREATE_ROOM, body, retry=True)
            if status != 200:
                logger.debug(
                    "Chatto: CreateRoom failed (%s): %s",
                    status,
                    resp.get("message") or resp.get("error") or "",
                )
                return None
            room = resp.get("room", {})
            rid = str(room.get("id", "")) if room else ""
            if rid:
                self._room_names[rid] = name
                self._room_kinds[rid] = "ROOM_KIND_GROUP"
                return rid
            logger.debug("Chatto: CreateRoom returned no room id: %s", resp)
            return None
        except Exception as e:
            logger.debug("Chatto: CreateRoom error: %s", e)
            return None

    # ------------------------------------------------------------------ #
    # Notification dismissal (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def dismiss_all_notifications(self) -> bool:
        """Dismiss all notifications via NotificationService/DismissAllNotifications."""
        try:
            status, resp = await self._rpc(
                _PATH_DISMISS_ALL_NOTIFICATIONS, {}, retry=False
            )
            if status == 200:
                return True
            logger.debug(
                "Chatto: DismissAllNotifications failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: DismissAllNotifications error: %s", e)
            return False

    async def dismiss_notification(self, notification_id: str) -> bool:
        """Dismiss a single notification via NotificationService/DismissNotification."""
        try:
            status, resp = await self._rpc(
                _PATH_DISMISS_NOTIFICATION,
                {"notificationId": str(notification_id)},
                retry=False,
            )
            if status == 200:
                return True
            logger.debug(
                "Chatto: DismissNotification failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: DismissNotification error: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # Message editing and deletion
    # ------------------------------------------------------------------ #

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        new_content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Edit a previously sent message via MessageService/UpdateMessage."""
        body = {
            "roomId": str(chat_id),
            "eventId": str(message_id),
            "body": new_content,
        }
        try:
            status, resp = await self._rpc(_PATH_UPDATE_MESSAGE, body, retry=True)
            if status == 200:
                return True
            logger.debug(
                "Chatto: UpdateMessage failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: UpdateMessage error: %s", e)
            return False

    async def delete_message(
        self,
        chat_id: str,
        message_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Delete a previously sent message via MessageService/DeleteMessage."""
        body = {
            "roomId": str(chat_id),
            "eventId": str(message_id),
        }
        try:
            status, resp = await self._rpc(_PATH_DELETE_MESSAGE, body, retry=True)
            if status == 200:
                return True
            logger.debug(
                "Chatto: DeleteMessage failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: DeleteMessage error: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # Processing lifecycle hooks (reactions-based, like Discord)
    # ------------------------------------------------------------------ #

    def _reactions_enabled(self) -> bool:
        """Check if processing reactions are enabled."""
        return os.getenv("CHATTO_REACTIONS", "true").lower() not in {"false", "0", "no"}

    def _event_room_and_message_id(self, event: MessageEvent) -> Tuple[str, str]:
        """Extract room_id and message_id from a MessageEvent."""
        chat_id = ""
        message_id = str(event.message_id or "")
        source = event.source
        if source:
            chat_id = str(getattr(source, "chat_id", "") or "")
        # Fallback: try raw_message dict
        if not chat_id or not message_id:
            raw = event.raw_message
            if isinstance(raw, dict):
                if not chat_id:
                    chat_id = str(raw.get("roomId", "") or "")
                if not message_id:
                    message_id = str(raw.get("id", "") or "")
        return chat_id, message_id

    async def on_processing_start(self, event: MessageEvent) -> None:
        """Add an 👀 (eyes) reaction to the incoming message."""
        if not self._reactions_enabled():
            return
        chat_id, message_id = self._event_room_and_message_id(event)
        if not chat_id or not message_id:
            return
        await self.send_reaction(chat_id, message_id, "👀")

    async def on_processing_complete(
        self, event: MessageEvent, outcome: ProcessingOutcome
    ) -> None:
        """Swap the 👀 reaction for ✅ (success) or ❌ (failure)."""
        if not self._reactions_enabled():
            return
        chat_id, message_id = self._event_room_and_message_id(event)
        if not chat_id or not message_id:
            return
        # Remove the processing eyes reaction
        await self.remove_reaction(chat_id, message_id, "👀")
        # Add the outcome reaction
        if outcome == ProcessingOutcome.SUCCESS:
            await self.send_reaction(chat_id, message_id, "✅")
        elif outcome == ProcessingOutcome.FAILURE:
            await self.send_reaction(chat_id, message_id, "❌")

    # ------------------------------------------------------------------ #
    # Asset upload (chunked)
    # ------------------------------------------------------------------ #

    async def _upload_asset(self, room_id: str, file_path: str) -> Optional[str]:
        """Upload a file via the chunked AssetUploadService.

        Returns the asset ID on success, or None on failure.
        """
        try:
            with open(file_path, "rb") as f:
                file_data = f.read()
        except Exception as e:
            logger.error("Chatto: failed to read file %s — %s", file_path, e)
            return None

        if not file_data:
            logger.error("Chatto: file %s is empty", file_path)
            return None

        file_size = len(file_data)
        file_name = os.path.basename(file_path)
        mime_type = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        sha256_hash = hashlib.sha256(file_data).hexdigest()

        # Step 1: Create upload session
        create_body = {
            "roomId": room_id,
            "filename": file_name,
            "contentType": mime_type,
            "size": file_size,
            "sha256": sha256_hash,
        }
        status, resp = await self._rpc(_PATH_CREATE_UPLOAD, create_body)
        if status != 200:
            logger.error(
                "Chatto: CreateUpload failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return None

        upload_id = str(resp.get("upload", {}).get("id", ""))
        if not upload_id:
            logger.error("Chatto: CreateUpload returned no upload ID: %s", resp)
            return None

        # Step 2: Upload chunks
        offset = 0
        while offset < file_size:
            chunk = file_data[offset:offset + _UPLOAD_CHUNK_SIZE]
            chunk_b64 = base64.b64encode(chunk).decode("ascii")
            chunk_sha256 = hashlib.sha256(chunk).hexdigest()
            chunk_body = {
                "uploadId": upload_id,
                "offset": offset,
                "content": chunk_b64,
                "chunkSha256": chunk_sha256,
            }
            status, resp = await self._rpc(_PATH_UPLOAD_CHUNK, chunk_body)
            if status != 200:
                logger.error(
                    "Chatto: UploadChunk failed at offset %d (%s): %s",
                    offset,
                    status,
                    resp.get("message") or resp.get("error") or "",
                )
                return None
            offset += len(chunk)

        # Step 3: Complete upload
        complete_body = {"uploadId": upload_id}
        status, resp = await self._rpc(_PATH_COMPLETE_UPLOAD, complete_body)
        if status != 200:
            logger.error(
                "Chatto: CompleteUpload failed (%s): %s",
                status,
                resp.get("message") or resp.get("error") or "",
            )
            return None

        asset_id = str(resp.get("asset", {}).get("id", ""))
        if not asset_id:
            logger.error("Chatto: CompleteUpload returned no asset ID: %s", resp)
            return None

        logger.info("Chatto: uploaded %s as asset %s (%d bytes)", file_name, asset_id, file_size)
        return asset_id

    async def send_image_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a local image file via the chunked upload API."""
        # Validate the path is safe
        safe_path = self.validate_media_delivery_path(file_path)
        if not safe_path:
            logger.warning("Chatto: send_image_file — unsafe path %s", file_path)
            text = "⚠️ Couldn't deliver the image attachment."
            if caption:
                text = f"{caption}\n{text}"
            return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

        asset_id = await self._upload_asset(str(chat_id), safe_path)
        if not asset_id:
            # Fallback to a notice
            text = "⚠️ Couldn't deliver the image attachment."
            if caption:
                text = f"{caption}\n{text}"
            return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

        body: Dict[str, Any] = {
            "roomId": str(chat_id),
            "body": caption or "",
            "attachmentAssetIds": [asset_id],
        }

        thread_id = (metadata or {}).get("thread_id")
        if reply_to:
            thread_id = reply_to
        if thread_id:
            body["threadRootEventId"] = str(thread_id)

        status, resp = await self._rpc(_PATH_CREATE_MESSAGE, body)
        if status != 200:
            err = resp.get("message") or resp.get("error") or f"HTTP {status}"
            return SendResult(success=False, error=err, retryable=status >= 500 or status == 401)

        msg = resp.get("message", {})
        msg_id = str(msg.get("id", "")) if msg else ""
        if msg_id:
            self._mark_seen(str(chat_id), msg_id)
        return SendResult(success=True, message_id=msg_id, raw_response=resp)

    async def send_image(
        self,
        chat_id: str,
        image_url: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send an image to a Chatto room.

        Tries to download the image from the URL and upload it as a native
        attachment.  Falls back to sending the URL as a link (Chatto renders
        link previews) if the download fails.
        """
        # Try downloading and uploading as attachment
        try:
            import tempfile
            import urllib.request as _urllib_request

            # Download to a temp file
            parsed = urlsplit(image_url)
            url_path = parsed.path
            ext = os.path.splitext(url_path)[1] or ".png"
            tmp_fd, tmp_path = tempfile.mkstemp(suffix=ext, prefix="chatto_img_")
            try:
                os.close(tmp_fd)
                req = _urllib_request.Request(image_url, headers={"User-Agent": "Hermes/1.0"})
                ctx = _ssl_context()
                with _urllib_request.urlopen(req, timeout=_HTTP_TIMEOUT, context=ctx) as resp:
                    with open(tmp_path, "wb") as f:
                        f.write(resp.read())

                # Upload as attachment
                result = await self.send_image_file(
                    chat_id, tmp_path, caption=caption,
                    reply_to=reply_to, metadata=metadata,
                )
                if result.success:
                    return result
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        except Exception as e:
            logger.debug("Chatto: send_image download/upload failed, falling back to link: %s", e)

        # Fallback: send as link (Chatto renders link previews)
        text = image_url
        if caption:
            text = f"{caption}\n{image_url}"
        return await self.send(chat_id, text, reply_to=reply_to, metadata=metadata)

    # ------------------------------------------------------------------ #
    # Platform properties
    # ------------------------------------------------------------------ #

    @property
    def platform_name(self) -> str:
        return "chatto"

    @property
    def supports_markdown(self) -> bool:
        return True

    @property
    def supports_reactions(self) -> bool:
        return True

    # ------------------------------------------------------------------ #
    # Member directory — user lookup and mention resolution (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def list_users(self) -> list:
        """List all server members via UserService/ListUsers.

        Returns a list of user dicts. Each dict typically contains
        ``id``, ``login``, and ``displayName`` keys.
        """
        try:
            status, resp = await self._rpc(_PATH_LIST_USERS, {}, retry=True)
            if status != 200:
                logger.debug(
                    "Chatto: ListUsers failed (%s): %s",
                    status,
                    resp.get("message") or resp.get("error") or "",
                )
                return []
            users = resp.get("users", [])
            # Cache all returned users
            for u in users:
                uid = str(u.get("id", ""))
                if uid:
                    self._user_cache[uid] = u
            return users
        except Exception as e:
            logger.debug("Chatto: ListUsers error: %s", e)
            return []

    async def get_user(self, user_id: str) -> Optional[dict]:
        """Get a single user by ID via UserService/GetUser.

        Returns the user dict (containing ``id``, ``login``,
        ``displayName``) or ``None`` on failure. Results are cached in
        ``self._user_cache``.
        """
        if not user_id:
            return None
        # Return cached entry if available
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            status, resp = await self._rpc(
                _PATH_GET_USER, {"userId": str(user_id)}, retry=True
            )
            if status != 200:
                logger.debug(
                    "Chatto: GetUser failed (%s): %s",
                    status,
                    resp.get("message") or resp.get("error") or "",
                )
                return None
            user = resp.get("user")
            if user:
                uid = str(user.get("id", ""))
                if uid:
                    self._user_cache[uid] = user
                return user
            return None
        except Exception as e:
            logger.debug("Chatto: GetUser error: %s", e)
            return None

    async def batch_get_users(self, user_ids: list) -> list:
        """Batch-fetch multiple users via UserService/BatchGetUsers.

        Returns a list of user dicts. Cached entries are reused and only
        uncached IDs are fetched from the server.
        """
        if not user_ids:
            return []
        # Separate cached from uncached
        cached: list = []
        uncached_ids: list = []
        for uid in user_ids:
            uid_str = str(uid)
            if uid_str in self._user_cache:
                cached.append(self._user_cache[uid_str])
            else:
                uncached_ids.append(uid_str)
        if not uncached_ids:
            return cached
        try:
            status, resp = await self._rpc(
                _PATH_BATCH_GET_USERS, {"userIds": uncached_ids}, retry=True
            )
            if status != 200:
                logger.debug(
                    "Chatto: BatchGetUsers failed (%s): %s",
                    status,
                    resp.get("message") or resp.get("error") or "",
                )
                return cached
            fetched = resp.get("users", [])
            for u in fetched:
                uid = str(u.get("id", ""))
                if uid:
                    self._user_cache[uid] = u
            return cached + fetched
        except Exception as e:
            logger.debug("Chatto: BatchGetUsers error: %s", e)
            return cached

    # ------------------------------------------------------------------ #
    # Presence broadcasting (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def set_presence(self, status: str) -> bool:
        """Update the bot's presence status via MyAccountService/UpdatePresence.

        Accepts string values ``"online"``, ``"away"``, ``"dnd"`` (or
        ``"do_not_disturb"``) and maps them to Chatto's integer status
        codes: 1=ONLINE, 2=AWAY, 3=DO_NOT_DISTURB.
        Returns ``True`` on success.
        """
        status_lower = status.lower().strip()
        status_int = _PRESENCE_STATUS_MAP.get(status_lower)
        if status_int is None:
            logger.warning("Chatto: unknown presence status %r", status)
            return False
        try:
            status_code, resp = await self._rpc(
                _PATH_UPDATE_PRESENCE, {"status": status_int}, retry=False
            )
            if status_code == 200:
                logger.debug("Chatto: presence set to %s (%d)", status_lower, status_int)
                return True
            logger.debug(
                "Chatto: UpdatePresence failed (%s): %s",
                status_code,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: UpdatePresence error: %s", e)
            return False

    # ------------------------------------------------------------------ #
    # Custom status messages (Chatto-unique)
    # ------------------------------------------------------------------ #

    async def set_custom_status(self, text: str) -> bool:
        """Set a custom status message via MyAccountService/UpdateCustomStatus.

        The status text is a plain string (max ~100 chars). Useful for
        indicating long-running operations, e.g. ``"Processing..."``.
        Returns ``True`` on success.
        """
        if not text:
            return False
        # Truncate to a reasonable length
        status_text = text.strip()[:100]
        if not status_text:
            return False
        try:
            status_code, resp = await self._rpc(
                _PATH_UPDATE_CUSTOM_STATUS, {"status": status_text}, retry=False
            )
            if status_code == 200:
                logger.debug("Chatto: custom status set to %r", status_text)
                return True
            logger.debug(
                "Chatto: UpdateCustomStatus failed (%s): %s",
                status_code,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: UpdateCustomStatus error: %s", e)
            return False

    async def clear_custom_status(self) -> bool:
        """Clear the custom status message via MyAccountService/DeleteCustomStatus.

        Returns ``True`` on success.
        """
        try:
            status_code, resp = await self._rpc(
                _PATH_DELETE_CUSTOM_STATUS, {}, retry=False
            )
            if status_code == 200:
                logger.debug("Chatto: custom status cleared")
                return True
            logger.debug(
                "Chatto: DeleteCustomStatus failed (%s): %s",
                status_code,
                resp.get("message") or resp.get("error") or "",
            )
            return False
        except Exception as e:
            logger.debug("Chatto: DeleteCustomStatus error: %s", e)
            return False

    @property
    def supports_threads(self) -> bool:
        return True


# --------------------------------------------------------------------------- #
# Plugin registration
# --------------------------------------------------------------------------- #

def check_requirements() -> bool:
    """Check if Chatto is configured."""
    return bool(
        os.getenv("CHATTO_URL", "").strip()
        and os.getenv("CHATTO_LOGIN", "").strip()
        and os.getenv("CHATTO_PASSWORD", "").strip()
    )


def validate_config(config) -> bool:
    """Validate that the platform config has enough info to connect."""
    extra = getattr(config, "extra", {}) or {}
    url = os.getenv("CHATTO_URL") or str(extra.get("url", ""))
    login = os.getenv("CHATTO_LOGIN", "").strip()
    password = os.getenv("CHATTO_PASSWORD", "").strip()
    return bool(url and login and password)


def is_connected(config) -> bool:
    """Check whether Chatto is configured."""
    return validate_config(config)


def _apply_yaml_config(yaml_cfg: dict, chatto_cfg: dict) -> Optional[dict]:
    """Translate config.yaml chatto.extra keys into CHATTO_* env vars."""
    extra = chatto_cfg.get("extra") if isinstance(chatto_cfg.get("extra"), dict) else {}
    mapping = {
        "url": "CHATTO_URL",
        "home_channel": "CHATTO_HOME_CHANNEL",
        "require_mention": "CHATTO_REQUIRE_MENTION",
    }
    for yaml_key, env_key in mapping.items():
        val = extra.get(yaml_key)
        if val is not None and not os.getenv(env_key):
            os.environ[env_key] = str(val).lower() if isinstance(val, bool) else str(val)
    channels = extra.get("channels")
    if isinstance(channels, list) and not os.getenv("CHATTO_CHANNELS"):
        os.environ["CHATTO_CHANNELS"] = ",".join(str(c) for c in channels)
    allowed = extra.get("allowed_users")
    if isinstance(allowed, list) and not os.getenv("CHATTO_ALLOWED_USERS"):
        os.environ["CHATTO_ALLOWED_USERS"] = ",".join(str(u) for u in allowed)
    if "allow_all_users" in extra and not os.getenv("CHATTO_ALLOW_ALL_USERS"):
        os.environ["CHATTO_ALLOW_ALL_USERS"] = str(extra["allow_all_users"]).lower()
    # Return nothing to merge — all config flows through env
    return None


def _env_enablement() -> Optional[dict]:
    """Seed PlatformConfig.extra from env vars for env-only setups."""
    url = os.getenv("CHATTO_URL", "").strip()
    if not url:
        return None
    extra = {"url": url}
    home = os.getenv("CHATTO_HOME_CHANNEL", "").strip()
    if home:
        extra["home_channel"] = home
    channels = os.getenv("CHATTO_CHANNELS", "").strip()
    if channels:
        extra["channels"] = [c.strip() for c in channels.split(",") if c.strip()]
    rm = os.getenv("CHATTO_REQUIRE_MENTION", "").strip().lower()
    if rm:
        extra["require_mention"] = rm in ("true", "1", "yes")
    home_dict = {"home_channel": home} if home else None
    return {"extra": extra, "home_channel": home_dict}


async def _standalone_send(
    base_url: str,
    login: str,
    password: str,
    room_id: str,
    content: str,
    thread_id: Optional[str] = None,
) -> dict:
    """Out-of-process send for cron delivery (no live adapter needed)."""
    token = _auth_login(base_url, login, password)
    if not token:
        return {"success": False, "error": "login failed"}
    body: Dict[str, Any] = {"roomId": room_id, "body": content}
    if thread_id:
        body["threadRootEventId"] = thread_id
    status, resp = _rpc_request(base_url, _PATH_CREATE_MESSAGE, token, body)
    if status == 200:
        return {"success": True, "response": resp}
    return {"success": False, "error": resp.get("message", f"HTTP {status}"), "status": status}


def interactive_setup() -> None:
    """Interactive setup wizard for Chatto."""
    from hermes_cli.gateway import prompt_env, set_env_var

    url = prompt_env("Chatto server URL (e.g. https://chat.example.com):")
    if url:
        set_env_var("CHATTO_URL", url)
    login = prompt_env("Chatto login (username):")
    if login:
        set_env_var("CHATTO_LOGIN", login)
    password = prompt_env("Chatto password:", password=True)
    if password:
        set_env_var("CHATTO_PASSWORD", password)
    channels = prompt_env("Room IDs to watch (comma-separated, or empty for all):")
    if channels:
        set_env_var("CHATTO_CHANNELS", channels)
    home = prompt_env("Home room ID for notifications (or empty):")
    if home:
        set_env_var("CHATTO_HOME_CHANNEL", home)
    allow_all = prompt_env("Allow all users? (true/false):")
    if allow_all:
        set_env_var("CHATTO_ALLOW_ALL_USERS", allow_all)
    print("\n✓ Chatto configured. Restart the gateway to activate.")


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="chatto",
        label="Chatto",
        adapter_factory=lambda cfg: ChattoAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        required_env=["CHATTO_URL", "CHATTO_LOGIN", "CHATTO_PASSWORD"],
        install_hint="Requires a Chatto server. See https://docs.chatto.run",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        cron_deliver_env_var="CHATTO_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        allowed_users_env="CHATTO_ALLOWED_USERS",
        allow_all_env="CHATTO_ALLOW_ALL_USERS",
        max_message_length=_MAX_MESSAGE_LENGTH,
        emoji="💬",
        allow_update_command=True,
        pii_safe=False,
        platform_hint=(
            "You are chatting in Chatto (a self-hosted team chat server). "
            "Markdown IS supported. Users address you by @-mentioning your name "
            "in rooms; direct messages reach you without a mention. "
            "Keep responses conversational."
        ),
    )