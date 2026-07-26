"""Narrow immutable contracts for plugin handling of ordinary gateway messages."""

from __future__ import annotations

import asyncio
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
    coverage_gaps: tuple[str, ...] = ()

    @classmethod
    def from_event(cls, event: MessageEvent) -> "GatewayMessageEvent":
        gaps: set[str] = set()
        timestamp = getattr(event, "timestamp", None)
        timestamp_text = timestamp.isoformat() if hasattr(timestamp, "isoformat") else ""
        message_type = getattr(getattr(event, "message_type", None), "value", None)
        metadata = getattr(event, "metadata", None)
        raw_mentioned_user_ids = (
            metadata.get("mentioned_user_ids") if isinstance(metadata, dict) else None
        )
        mentioned_user_ids = None
        if isinstance(raw_mentioned_user_ids, (list, tuple)):
            mention_values = raw_mentioned_user_ids[:129]
            if len(raw_mentioned_user_ids) > 128:
                gaps.add("mention-count-overflow")
            if all(isinstance(item, str) and item for item in mention_values[:128]):
                mentioned_user_ids = tuple(
                    _bounded_text(
                        item,
                        512,
                        "mention-id-overflow",
                        gaps,
                    )
                    for item in mention_values[:128]
                )
        raw_mentions_room = metadata.get("mentions_room") if isinstance(metadata, dict) else None
        mentions_room = raw_mentions_room if isinstance(raw_mentions_room, bool) else None
        media_urls = _bounded_items(
            getattr(event, "media_urls", None),
            count_limit=16,
            item_limit=4_096,
            count_gap="media-count-overflow",
            item_gap="media-url-overflow",
            gaps=gaps,
        )
        media_types = _bounded_items(
            getattr(event, "media_types", None),
            count_limit=16,
            item_limit=256,
            count_gap="media-count-overflow",
            item_gap="media-type-overflow",
            gaps=gaps,
        )
        return cls(
            text=_bounded_text(
                getattr(event, "text", "") or "",
                65_536,
                "text-overflow",
                gaps,
            ),
            message_id=_bounded_optional_text(
                getattr(event, "message_id", None),
                512,
                "message-id-overflow",
                gaps,
            ),
            message_type=_bounded_text(
                message_type or "",
                64,
                "message-type-overflow",
                gaps,
            ),
            timestamp=_bounded_text(
                timestamp_text,
                128,
                "timestamp-overflow",
                gaps,
            ),
            media_urls=media_urls,
            media_types=media_types,
            mentioned_user_ids=mentioned_user_ids,
            mentions_room=mentions_room,
            reply_to_message_id=_bounded_optional_text(
                getattr(event, "reply_to_message_id", None),
                512,
                "reply-message-id-overflow",
                gaps,
            ),
            reply_to_text=_bounded_optional_text(
                getattr(event, "reply_to_text", None),
                16_384,
                "reply-text-overflow",
                gaps,
            ),
            reply_to_author_id=_bounded_optional_text(
                getattr(event, "reply_to_author_id", None),
                512,
                "reply-author-id-overflow",
                gaps,
            ),
            reply_to_author_name=_bounded_optional_text(
                getattr(event, "reply_to_author_name", None),
                4_096,
                "reply-author-name-overflow",
                gaps,
            ),
            reply_to_is_own_message=bool(
                getattr(event, "reply_to_is_own_message", False)
            ),
            coverage_gaps=tuple(sorted(gaps)),
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
    coverage_gaps: tuple[str, ...] = ()

    @classmethod
    def from_source(
        cls,
        source: SessionSource,
        *,
        session_key: str,
    ) -> "GatewayMessageRoute":
        gaps: set[str] = set()
        platform = getattr(getattr(source, "platform", None), "value", None)
        profile_value = getattr(source, "profile", None)
        profile = _optional_text(profile_value)
        if profile is None:
            from hermes_cli.profiles import get_active_profile_name

            profile = _optional_text(get_active_profile_name())
        return cls(
            session_key=_bounded_text(
                session_key or "", 2_048, "session-key-overflow", gaps
            ),
            platform=_bounded_text(
                platform or "", 64, "platform-overflow", gaps
            ),
            profile=_bounded_optional_text(
                profile, 256, "profile-overflow", gaps
            ),
            scope_id=_bounded_optional_text(
                getattr(source, "scope_id", None), 512, "scope-id-overflow", gaps
            ),
            chat_id=_bounded_text(
                getattr(source, "chat_id", "") or "",
                512,
                "chat-id-overflow",
                gaps,
            ),
            chat_name=_bounded_optional_text(
                getattr(source, "chat_name", None),
                4_096,
                "chat-name-overflow",
                gaps,
            ),
            chat_type=_bounded_text(
                getattr(source, "chat_type", "") or "",
                64,
                "chat-type-overflow",
                gaps,
            ),
            thread_id=_bounded_optional_text(
                getattr(source, "thread_id", None),
                512,
                "thread-id-overflow",
                gaps,
            ),
            user_id=_bounded_optional_text(
                getattr(source, "user_id", None), 512, "user-id-overflow", gaps
            ),
            user_name=_bounded_optional_text(
                getattr(source, "user_name", None),
                4_096,
                "user-name-overflow",
                gaps,
            ),
            coverage_gaps=tuple(sorted(gaps)),
        )


@dataclass(slots=True)
class _DeliveryRequest:
    kind: Literal["send", "reply", "react"]
    content: str
    operation: str
    future: asyncio.Future[GatewayDeliveryReceipt]


@dataclass(slots=True)
class _DeliveryChannel:
    queue: asyncio.Queue[_DeliveryRequest]
    revoked: bool = False
    consumed: bool = False


class GatewayDelivery:
    """Opaque, one-shot capability bound to a host-owned delivery worker."""

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        send_callback: Callable[[str], Awaitable[Any] | Any],
        *,
        reply_callback: Callable[[str], Awaitable[Any] | Any] | None = None,
        react_callback: Callable[[str, str], Awaitable[Any] | Any] | None = None,
    ) -> None:
        if not callable(send_callback):
            raise TypeError("send_callback must be callable")
        channel = _DeliveryChannel(queue=asyncio.Queue(maxsize=1))
        _DELIVERY_CHANNELS[self] = channel
        worker = asyncio.create_task(
            _delivery_worker(
                channel,
                send_callback,
                reply_callback=reply_callback,
                react_callback=react_callback,
            )
        )
        worker.add_done_callback(_consume_delivery_worker_result)

    async def send(self, content: str) -> GatewayDeliveryReceipt:
        """Send text to the bound source and normalize the native acknowledgement."""
        return await self._submit("send", str(content), "")

    async def reply(self, content: str) -> GatewayDeliveryReceipt:
        """Reply only to the native message captured by this capability."""
        return await self._submit("reply", str(content), "")

    async def react(
        self,
        reaction: str,
        *,
        operation: str = "add",
    ) -> GatewayDeliveryReceipt:
        """Add/remove one reaction on the captured native message."""
        if operation not in {"add", "remove"}:
            return GatewayDeliveryReceipt(status="failed")
        return await self._submit("react", str(reaction), operation)

    async def _submit(
        self,
        kind: Literal["send", "reply", "react"],
        content: str,
        operation: str,
    ) -> GatewayDeliveryReceipt:
        channel = _DELIVERY_CHANNELS.get(self)
        if channel is None or channel.revoked or channel.consumed:
            return GatewayDeliveryReceipt(status="failed")
        channel.consumed = True
        future = asyncio.get_running_loop().create_future()
        try:
            channel.queue.put_nowait(
                _DeliveryRequest(
                    kind=kind,
                    content=content,
                    operation=operation,
                    future=future,
                )
            )
        except asyncio.QueueFull:
            return GatewayDeliveryReceipt(status="failed")
        return await future

    def _revoke(self) -> None:
        """Invalidate this capability synchronously at a host lifecycle boundary."""
        channel = _DELIVERY_CHANNELS.get(self)
        if channel is not None:
            channel.revoked = True
            if not channel.consumed:
                channel.consumed = True
                try:
                    future = asyncio.get_running_loop().create_future()
                    channel.queue.put_nowait(
                        _DeliveryRequest(
                            kind="send",
                            content="",
                            operation="",
                            future=future,
                        )
                    )
                except (RuntimeError, asyncio.QueueFull):
                    pass


_DELIVERY_CHANNELS: weakref.WeakKeyDictionary[
    GatewayDelivery,
    _DeliveryChannel,
] = weakref.WeakKeyDictionary()


async def _delivery_worker(
    channel: _DeliveryChannel,
    send_callback: Callable[[str], Awaitable[Any] | Any],
    *,
    reply_callback: Callable[[str], Awaitable[Any] | Any] | None,
    react_callback: Callable[[str, str], Awaitable[Any] | Any] | None,
) -> None:
    request: _DeliveryRequest | None = None
    while request is None:
        try:
            request = channel.queue.get_nowait()
        except asyncio.QueueEmpty:
            if channel.revoked:
                return
            # Polling deliberately leaves no queue waiter that can be traversed
            # from the public capability into this host task's callback frame.
            await asyncio.sleep(0.01)
    if channel.revoked:
        if not request.future.done():
            request.future.set_result(GatewayDeliveryReceipt(status="failed"))
        return
    try:
        # No suspension occurs between the authoritative revocation check and
        # native invocation. A boundary that wins first therefore prevents the
        # adapter call; a boundary after invocation can only make its outcome
        # ambiguous, never fabricate success.
        if request.kind == "reply":
            if not callable(reply_callback):
                native_result = SendResult(success=False)
            else:
                native_result = reply_callback(request.content)
        elif request.kind == "react":
            if not callable(react_callback):
                native_result = SendResult(success=False)
            else:
                native_result = react_callback(request.content, request.operation)
        else:
            native_result = send_callback(request.content)
        if inspect.isawaitable(native_result):
            native_result = await native_result
    except Exception:
        receipt = GatewayDeliveryReceipt(status="unknown")
    else:
        receipt = _normalize_delivery_receipt(native_result)
        if channel.revoked and receipt.status == "sent":
            receipt = GatewayDeliveryReceipt(status="unknown")
    if not request.future.done():
        request.future.set_result(receipt)


def _normalize_delivery_receipt(native_result: Any) -> GatewayDeliveryReceipt:
    if not isinstance(native_result, SendResult):
        return GatewayDeliveryReceipt(status="unknown")
    if native_result.success is False:
        return GatewayDeliveryReceipt(status="failed")
    message_id = _optional_text(native_result.message_id)
    if native_result.success is True and message_id:
        return GatewayDeliveryReceipt(status="sent", message_id=message_id)
    return GatewayDeliveryReceipt(status="unknown")


def _consume_delivery_worker_result(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


def _optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _bounded_text(
    value: Any,
    byte_limit: int,
    gap: str,
    gaps: set[str],
) -> str:
    text = str(value)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= byte_limit:
        return text
    gaps.add(gap)
    return encoded[:byte_limit].decode("utf-8", errors="ignore")


def _bounded_optional_text(
    value: Any,
    byte_limit: int,
    gap: str,
    gaps: set[str],
) -> Optional[str]:
    if value is None:
        return None
    text = _bounded_text(value, byte_limit, gap, gaps)
    return text if text else None


def _bounded_items(
    values: Any,
    *,
    count_limit: int,
    item_limit: int,
    count_gap: str,
    item_gap: str,
    gaps: set[str],
) -> tuple[str, ...]:
    if not values:
        return ()
    items = list(values[: count_limit + 1]) if isinstance(values, (list, tuple)) else []
    if not isinstance(values, (list, tuple)):
        gaps.add(count_gap)
        return ()
    if len(values) > count_limit:
        gaps.add(count_gap)
    return tuple(
        _bounded_text(item, item_limit, item_gap, gaps)
        for item in items[:count_limit]
    )
