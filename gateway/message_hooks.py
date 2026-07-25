"""Narrow immutable contracts for plugin handling of ordinary gateway messages."""

from __future__ import annotations

import inspect
import weakref
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource
from hermes_constants import GATEWAY_MESSAGE_HOOK_API_VERSION


DeliveryStatus = Literal["sent", "failed", "unknown"]


@dataclass(frozen=True, slots=True)
class GatewayDeliveryReceipt:
    """Truthful platform-neutral result of a route-bound host send."""

    status: DeliveryStatus
    message_id: Optional[str] = None


@dataclass(frozen=True, slots=True)
class GatewayMessageEvent:
    """Immutable normalized snapshot of an inbound user message."""

    text: str
    message_id: Optional[str]
    message_type: str
    timestamp: str
    media_urls: tuple[str, ...]
    media_types: tuple[str, ...]
    mentioned_user_ids: tuple[str, ...] | None
    mentions_room: bool | None
    reply_to_message_id: Optional[str]
    reply_to_text: Optional[str]
    reply_to_author_id: Optional[str]
    reply_to_author_name: Optional[str]
    reply_to_is_own_message: bool

    @classmethod
    def from_event(cls, event: MessageEvent) -> "GatewayMessageEvent":
        timestamp = getattr(event, "timestamp", None)
        timestamp_text = timestamp.isoformat() if hasattr(timestamp, "isoformat") else ""
        message_type = getattr(getattr(event, "message_type", None), "value", None)
        metadata = getattr(event, "metadata", None)
        raw_mentioned_user_ids = (
            metadata.get("mentioned_user_ids") if isinstance(metadata, dict) else None
        )
        mentioned_user_ids = None
        if isinstance(raw_mentioned_user_ids, (list, tuple)) and all(
            isinstance(item, str) and item for item in raw_mentioned_user_ids
        ):
            mentioned_user_ids = tuple(raw_mentioned_user_ids)
        raw_mentions_room = metadata.get("mentions_room") if isinstance(metadata, dict) else None
        mentions_room = raw_mentions_room if isinstance(raw_mentions_room, bool) else None
        return cls(
            text=str(getattr(event, "text", "") or ""),
            message_id=_optional_text(getattr(event, "message_id", None)),
            message_type=str(message_type or ""),
            timestamp=timestamp_text,
            media_urls=tuple(str(item) for item in (getattr(event, "media_urls", None) or ())),
            media_types=tuple(str(item) for item in (getattr(event, "media_types", None) or ())),
            mentioned_user_ids=mentioned_user_ids,
            mentions_room=mentions_room,
            reply_to_message_id=_optional_text(
                getattr(event, "reply_to_message_id", None)
            ),
            reply_to_text=_optional_text(getattr(event, "reply_to_text", None)),
            reply_to_author_id=_optional_text(
                getattr(event, "reply_to_author_id", None)
            ),
            reply_to_author_name=_optional_text(
                getattr(event, "reply_to_author_name", None)
            ),
            reply_to_is_own_message=bool(
                getattr(event, "reply_to_is_own_message", False)
            ),
        )


@dataclass(frozen=True, slots=True)
class GatewayMessageRoute:
    """Immutable normalized route for the current inbound source."""

    session_key: str
    platform: str
    profile: Optional[str]
    scope_id: Optional[str]
    chat_id: str
    chat_name: Optional[str]
    chat_type: str
    thread_id: Optional[str]
    user_id: Optional[str]
    user_name: Optional[str]

    @classmethod
    def from_source(
        cls,
        source: SessionSource,
        *,
        session_key: str,
    ) -> "GatewayMessageRoute":
        platform = getattr(getattr(source, "platform", None), "value", None)
        profile = _optional_text(getattr(source, "profile", None))
        if profile is None:
            from hermes_cli.profiles import get_active_profile_name

            profile = _optional_text(get_active_profile_name())
        return cls(
            session_key=str(session_key or ""),
            platform=str(platform or ""),
            profile=profile,
            scope_id=_optional_text(getattr(source, "scope_id", None)),
            chat_id=str(getattr(source, "chat_id", "") or ""),
            chat_name=_optional_text(getattr(source, "chat_name", None)),
            chat_type=str(getattr(source, "chat_type", "") or ""),
            thread_id=_optional_text(getattr(source, "thread_id", None)),
            user_id=_optional_text(getattr(source, "user_id", None)),
            user_name=_optional_text(getattr(source, "user_name", None)),
        )


class GatewayDelivery:
    """Capability that can send only to the route captured by its host callback."""

    __slots__ = ("__weakref__",)

    def __init__(self, send_callback: Callable[[str], Awaitable[Any] | Any]) -> None:
        if not callable(send_callback):
            raise TypeError("send_callback must be callable")
        _DELIVERY_SEND_CALLBACKS[self] = send_callback

    async def send(self, content: str) -> GatewayDeliveryReceipt:
        """Send text to the bound source and normalize the native acknowledgement."""
        try:
            send_callback = _DELIVERY_SEND_CALLBACKS.get(self)
            if send_callback is None:
                return GatewayDeliveryReceipt(status="failed")
            native_result = send_callback(str(content))
            if inspect.isawaitable(native_result):
                native_result = await native_result
        except Exception:
            return GatewayDeliveryReceipt(status="unknown")

        if not isinstance(native_result, SendResult):
            return GatewayDeliveryReceipt(status="unknown")
        if native_result.success is False:
            return GatewayDeliveryReceipt(status="failed")
        message_id = _optional_text(native_result.message_id)
        if native_result.success is True and message_id:
            return GatewayDeliveryReceipt(status="sent", message_id=message_id)
        return GatewayDeliveryReceipt(status="unknown")


_DELIVERY_SEND_CALLBACKS: weakref.WeakKeyDictionary[
    GatewayDelivery,
    Callable[[str], Awaitable[Any] | Any],
] = weakref.WeakKeyDictionary()


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None
