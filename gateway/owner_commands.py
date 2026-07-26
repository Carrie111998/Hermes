"""Private owner-principal and pre-model owner-command boundary.

Owner identity is intentionally separate from gateway authorization.  The
configured set is read from the secret-scoped ``HERMES_OWNER_PRINCIPALS`` value
as comma-separated ``platform:user-id`` entries, is bounded, and is never
serialized or logged by this module.

Request/session bindings are opaque HMACs tied to this process epoch.  They are
not durable replay keys; a future host integration must provide durable replay
protection.  The callback contract is deliberately synchronous and may return
only ``"accepted"`` or ``"duplicate"``.

Hermes plugins and all other Python loaded into the gateway process are trusted
in-process code.  The private provenance machinery prevents accidental/public
API forgery and detects mutation between adapter intake and interception; it is
not a sandbox or isolation boundary against a malicious plugin with arbitrary
Python access.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, FrozenSet, Optional
from urllib.parse import urlsplit


_MAX_OWNERS = 16
_MAX_OWNER_ENTRY_LENGTH = 320
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_WATCH_QUERY_RE = re.compile(r"^v=([A-Za-z0-9_-]{11})$")
_COMMAND_PREFIX = "/youtube-probe"
_PROCESS_EPOCH_KEY = secrets.token_bytes(32)


@dataclass(frozen=True, slots=True)
class OwnerConfig:
    """Bounded owner set with no public identifier-bearing representation."""

    _principals: FrozenSet[str] = field(default_factory=frozenset, repr=False)

    @property
    def enabled(self) -> bool:
        return bool(self._principals)

    @property
    def count(self) -> int:
        return len(self._principals)

    def __repr__(self) -> str:
        return f"OwnerConfig(enabled={self.enabled}, count={self.count})"


@dataclass(frozen=True, slots=True)
class _OwnerPrincipal:
    platform: str
    request_binding: str = field(repr=False)
    session_binding: str = field(repr=False)

    def __repr__(self) -> str:
        return f"_OwnerPrincipal(platform={self.platform!r}, bindings=<redacted>)"


@dataclass(frozen=True, slots=True)
class _OwnerCommandRequest:
    principal: _OwnerPrincipal = field(repr=False)
    video_url: str = field(repr=False)
    request_binding: str = field(repr=False)
    session_binding: str = field(repr=False)

    def __repr__(self) -> str:
        return "_OwnerCommandRequest(<redacted>)"


@dataclass(frozen=True, slots=True)
class _InboundOwnerProvenance:
    _digest: str = field(repr=False)
    _owner_command_candidate: bool = field(repr=False)

    def __repr__(self) -> str:
        return "_InboundOwnerProvenance(<redacted>)"


def _load_owner_config(raw: Optional[str]) -> OwnerConfig:
    """Parse the secret-scoped owner set without exposing rejected values."""
    if not isinstance(raw, str) or not raw.strip():
        return OwnerConfig()
    parsed: set[str] = set()
    entries = raw.split(",")
    if len(entries) > _MAX_OWNERS:
        return OwnerConfig()
    for entry in entries:
        value = entry.strip()
        if not value or len(value) > _MAX_OWNER_ENTRY_LENGTH or ":" not in value:
            return OwnerConfig()
        platform, user_id = value.split(":", 1)
        platform = platform.strip().lower()
        user_id = user_id.strip()
        if (
            not platform
            or not user_id
            or platform in {"api_server", "webhook", "relay", "local"}
            or "*" in user_id
            or any(ch.isspace() for ch in platform)
        ):
            return OwnerConfig()
        parsed.add(_owner_fingerprint(platform, user_id))
    if len(parsed) > _MAX_OWNERS:
        return OwnerConfig()
    return OwnerConfig(frozenset(parsed))


def _opaque_binding(kind: str, *parts: object) -> str:
    payload = "\x1f".join([kind, *(str(part or "") for part in parts)])
    return hmac.new(
        _PROCESS_EPOCH_KEY, payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def _owner_fingerprint(platform: str, user_id: str) -> str:
    return _opaque_binding("owner", platform, user_id)


def _canonical_youtube_url(text: str) -> Optional[str]:
    if (
        not isinstance(text, str)
        or not text.startswith("https://")
        or any(ch.isspace() for ch in text)
    ):
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.fragment
    ):
        return None
    if parsed.netloc == "www.youtube.com":
        if parsed.path != "/watch":
            return None
        query_match = _WATCH_QUERY_RE.fullmatch(parsed.query)
        if query_match is None:
            return None
        video_id = query_match.group(1)
        canonical = f"https://www.youtube.com/watch?v={video_id}"
        return canonical if text == canonical else None
    if parsed.netloc == "youtu.be":
        if parsed.query or not parsed.path.startswith("/"):
            return None
        video_id = parsed.path[1:]
        if "/" in video_id or not _VIDEO_ID_RE.fullmatch(video_id):
            return None
        canonical = f"https://youtu.be/{video_id}"
        return canonical if text == canonical else None
    return None


def _is_owner_command_candidate(text: object) -> bool:
    return isinstance(text, str) and (
        text == _COMMAND_PREFIX
        or (
            text.startswith(_COMMAND_PREFIX)
            and len(text) > len(_COMMAND_PREFIX)
            and text[len(_COMMAND_PREFIX)].isspace()
        )
    )


def _normalized_name(value: object) -> str:
    raw = getattr(value, "name", None)
    if raw is None:
        raw = getattr(value, "value", value)
    return str(raw or "").lower()


def _provenance_facts(event: Any) -> Optional[tuple[object, ...]]:
    """Return validated transport facts, or ``None`` when provenance is unsafe."""
    source = getattr(event, "source", None)
    raw = getattr(event, "raw_message", None)
    platform = getattr(getattr(source, "platform", None), "value", "")
    if (
        source is None
        or raw is None
        or getattr(source, "chat_type", None) != "dm"
        or not getattr(source, "user_id", None)
        or getattr(source, "is_bot", False) is True
        or getattr(event, "reply_to_message_id", None)
        or getattr(event, "reply_to_text", None)
    ):
        return None

    if platform == "telegram":
        author = getattr(raw, "from_user", None)
        chat = getattr(raw, "chat", None)
        author_id = str(getattr(author, "id", ""))
        raw_message_id = str(getattr(raw, "message_id", ""))
        chat_id = str(getattr(chat, "id", ""))
        chat_type = _normalized_name(getattr(chat, "type", None))
        if not (
            author is not None
            and chat is not None
            and bool(author_id and raw_message_id and chat_id)
            and author_id == str(source.user_id)
            and raw_message_id == str(getattr(event, "message_id", ""))
            and chat_id == str(source.chat_id)
            and chat_id == author_id
            and chat_type in {"private", "chattype.private"}
            and getattr(author, "is_bot", False) is False
            and getattr(raw, "forward_origin", None) is None
            and getattr(raw, "forward_date", None) is None
            and getattr(raw, "sender_chat", None) is None
            and getattr(raw, "via_bot", None) is None
            and getattr(raw, "is_automatic_forward", False) is False
            and getattr(raw, "reply_to_message", None) is None
        ):
            return None
        raw_facts: tuple[object, ...] = (
            author_id,
            raw_message_id,
            chat_id,
            chat_type,
            False,  # author is not a bot
            False,  # not forwarded/automatic/via-bot/sender-chat
            False,  # not a reply
        )
    elif platform == "discord":
        author = getattr(raw, "author", None)
        channel = getattr(raw, "channel", None)
        author_id = str(getattr(author, "id", ""))
        raw_message_id = str(getattr(raw, "id", ""))
        channel_id = str(getattr(channel, "id", ""))
        msg_type = getattr(raw, "type", None)
        msg_type_name = _normalized_name(msg_type)
        channel_type = _normalized_name(getattr(channel, "type", None))
        channel_class = type(channel).__name__.lower() if channel is not None else ""
        is_private_channel = channel_class == "dmchannel" or channel_type in {
            "private",
            "dm",
            "channeltype.private",
        }
        if not (
            author is not None
            and channel is not None
            and bool(author_id and raw_message_id and channel_id)
            and author_id == str(source.user_id)
            and raw_message_id == str(getattr(event, "message_id", ""))
            and channel_id == str(source.chat_id)
            and getattr(author, "bot", False) is False
            and getattr(raw, "guild", None) is None
            and getattr(channel, "guild", None) is None
            and is_private_channel
            and getattr(raw, "webhook_id", None) is None
            and getattr(raw, "reference", None) is None
            and not (getattr(raw, "message_snapshots", None) or [])
            and msg_type_name in {"default", "messagetype.default"}
        ):
            return None
        raw_facts = (
            author_id,
            raw_message_id,
            channel_id,
            channel_type,
            channel_class,
            msg_type_name,
            False,  # author is not a bot
            False,  # no webhook/reference/forward snapshot/guild
        )
    else:
        return None

    return (
        platform,
        str(source.user_id),
        str(source.chat_id),
        str(getattr(event, "message_id", "")),
        getattr(event, "text", None),
        getattr(source, "chat_type", None),
        getattr(source, "is_bot", False) is True,
        getattr(source, "delivered_via_upstream_relay", False) is True,
        getattr(event, "internal", False) is True,
        bool(getattr(event, "reply_to_message_id", None)),
        bool(getattr(event, "reply_to_text", None)),
        bool(getattr(event, "media_urls", None)),
        bool(getattr(event, "channel_context", None)),
        raw_facts,
    )


def _provenance_digest(event: Any) -> Optional[str]:
    facts = _provenance_facts(event)
    if facts is None:
        return None
    encoded = json.dumps(facts, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return hmac.new(
        _PROCESS_EPOCH_KEY, b"provenance\0" + encoded, hashlib.sha256
    ).hexdigest()


def _stamp_direct_human_event(event: Any) -> None:
    """Privately bind an intake event once to its exact validated provenance."""
    if getattr(event, "_owner_provenance", None) is not None:
        return
    digest = _provenance_digest(event)
    if digest is not None:
        event._owner_provenance = _InboundOwnerProvenance(
            digest,
            _is_owner_command_candidate(getattr(event, "text", None)),
        )


def _has_valid_provenance(event: Any) -> bool:
    provenance = getattr(event, "_owner_provenance", None)
    if not isinstance(provenance, _InboundOwnerProvenance):
        return False
    current = _provenance_digest(event)
    return current is not None and hmac.compare_digest(provenance._digest, current)


def _handle_owner_command(
    event: Any,
    config: OwnerConfig,
    callback: Optional[Callable[[_OwnerCommandRequest], object]],
    *,
    session_key: str,
    rewritten: bool = False,
) -> tuple[bool, Optional[str]]:
    """Synchronously decide and consume an owner command before model dispatch."""
    text = getattr(event, "text", None)
    provenance = getattr(event, "_owner_provenance", None)
    if provenance is not None and not _has_valid_provenance(event):
        if _is_owner_command_candidate(text) or (
            isinstance(provenance, _InboundOwnerProvenance)
            and provenance._owner_command_candidate
        ):
            return True, "Owner command unavailable."
        return False, None
    if not _is_owner_command_candidate(text):
        return False, None

    parts = text.split(" ") if isinstance(text, str) else []
    if len(parts) != 2 or not parts[1] or any(not part for part in parts):
        return True, "Usage: /youtube-probe <HTTPS YouTube video URL>"
    video_url = _canonical_youtube_url(parts[1])
    if video_url is None:
        return True, "Usage: /youtube-probe <HTTPS YouTube video URL>"

    source = getattr(event, "source", None)
    platform = getattr(getattr(source, "platform", None), "value", "")
    user_id = getattr(source, "user_id", None)
    eligible = bool(
        config.enabled
        and not rewritten
        and source is not None
        and getattr(event, "internal", False) is False
        and _has_valid_provenance(event)
        and getattr(source, "chat_type", None) == "dm"
        and getattr(source, "is_bot", False) is False
        and getattr(source, "delivered_via_upstream_relay", False) is False
        and not getattr(event, "media_urls", None)
        and not getattr(event, "channel_context", None)
        and _owner_fingerprint(platform, str(user_id or "")) in config._principals
    )
    if not eligible:
        return True, "Owner command unavailable."

    request_binding = _opaque_binding(
        "request", platform, user_id, getattr(event, "message_id", None), video_url
    )
    session_binding = _opaque_binding("session", platform, user_id, session_key)
    principal = _OwnerPrincipal(
        platform=platform,
        request_binding=request_binding,
        session_binding=session_binding,
    )
    if callback is None:
        return True, "Owner command service unavailable."

    try:
        result = callback(
            _OwnerCommandRequest(
                principal=principal,
                video_url=video_url,
                request_binding=request_binding,
                session_binding=session_binding,
            )
        )
        if inspect.isawaitable(result):
            if inspect.iscoroutine(result):
                result.close()
            return True, "Owner command service unavailable."
    except Exception:
        return True, "Owner command service unavailable."

    if result == "accepted":
        return True, "YouTube probe accepted."
    if result == "duplicate":
        return True, "YouTube probe already accepted."
    return True, "Owner command service unavailable."
