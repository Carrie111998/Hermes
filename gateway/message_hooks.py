"""Narrow immutable contracts for plugin handling of ordinary gateway messages."""

from __future__ import annotations

import asyncio
import inspect
import weakref
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from gateway.platforms.base import MessageEvent, NativeDeliveryAck, SendResult
from gateway.session import SessionSource
from hermes_constants import GATEWAY_MESSAGE_HOOK_API_VERSION


DeliveryStatus = Literal["sent", "failed", "unknown"]
GATEWAY_DELIVERY_CAPABILITY_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class GatewayDeliveryReceipt:
    """Truthful platform-neutral result of a route-bound host send."""

    status: DeliveryStatus
    platform: Optional[str] = None
    room_id: Optional[str] = None
    profile: Optional[str] = None
    self_actor_id: Optional[str] = None
    effect_kind: Optional[Literal["send", "reply", "react"]] = None
    submitted_content: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    target_message_id: Optional[str] = None
    reaction: Optional[str] = None
    reaction_operation: Optional[Literal["add", "remove"]] = None
    message_id: Optional[str] = None
    effect_id: Optional[str] = None


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
    wake: asyncio.Event
    participant_settled: asyncio.Event
    binding: _DeliveryBinding
    worker: Optional[asyncio.Task[None]] = None
    terminal_callback: Optional[Callable[[Any], None]] = None
    revoked: bool = False
    consumed: bool = False
    invocation_started: bool = False


@dataclass(frozen=True, slots=True)
class _DeliveryBinding:
    platform: str
    room_id: str
    profile: str
    self_actor_id: Optional[str]
    source_message_id: Optional[str]


class GatewayDelivery:
    """One-shot, route-bound delivery facade for trusted in-process plugins.

    This narrows ordinary plugin use to the captured route and does not expose
    adapters or native clients as API. It is not a sandbox against hostile
    Python code running in the same interpreter.
    """

    __slots__ = ("__weakref__",)

    def __init__(
        self,
        send_callback: Callable[[str], Awaitable[Any] | Any],
        *,
        reply_callback: Callable[[str], Awaitable[Any] | Any] | None = None,
        react_callback: Callable[[str, str], Awaitable[Any] | Any] | None = None,
        platform: str,
        room_id: str,
        profile: str,
        self_actor_id: Optional[str],
        source_message_id: Optional[str],
        on_terminal: Callable[[Any], None] | None = None,
        hold_until_participant_settled: bool = False,
    ) -> None:
        if not callable(send_callback):
            raise TypeError("send_callback must be callable")
        channel = _DeliveryChannel(
            queue=asyncio.Queue(maxsize=1),
            wake=asyncio.Event(),
            participant_settled=asyncio.Event(),
            binding=_DeliveryBinding(
                platform=str(platform),
                room_id=str(room_id),
                profile=str(profile),
                self_actor_id=_optional_text(self_actor_id),
                source_message_id=_optional_text(source_message_id),
            ),
            terminal_callback=on_terminal,
        )
        if not hold_until_participant_settled:
            channel.participant_settled.set()
        _DELIVERY_CHANNELS[self] = channel
        worker = asyncio.create_task(
            _delivery_worker(
                channel,
                send_callback,
                reply_callback=reply_callback,
                react_callback=react_callback,
            )
        )
        channel.worker = worker
        worker.add_done_callback(
            lambda task, delivery_ref=weakref.ref(self), worker_channel=channel: (
                _finalize_delivery_worker(task, delivery_ref, worker_channel)
            )
        )

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
            channel.wake.set()
        except asyncio.QueueFull:
            return GatewayDeliveryReceipt(status="failed")
        return await future

    def _set_terminal_callback(
        self,
        callback: Callable[[Any], None],
    ) -> None:
        """Bind host cleanup after registration, including terminal races."""
        channel = _DELIVERY_CHANNELS.get(self)
        if channel is None:
            callback(self)
            return
        channel.terminal_callback = callback

    def _release_participant_hold(self) -> None:
        """Start the unused-capability TTL after participant handling."""
        channel = _DELIVERY_CHANNELS.get(self)
        if channel is not None:
            channel.participant_settled.set()

    def _revoke(self) -> None:
        """Begin invalidation synchronously; async boundaries must await settle."""
        self._begin_revocation()

    async def _settle_revocation(self) -> None:
        """Invalidate, then wait for any in-flight native effect to settle.

        This is the full lifecycle boundary for a single capability: after
        it returns, no revoked native invocation is still live. A caller
        unwinding inside the delivery worker's own callback frame skips the
        wait to avoid self-await deadlock.
        """
        worker = self._begin_revocation()
        if worker is None:
            return
        await _await_delivery_settlement(
            worker, owner=asyncio.current_task()
        )

    def _begin_revocation(self) -> Optional["asyncio.Task[None]"]:
        """Prevent new invocations, then return the worker to await, if any.

        The revocation flag is applied in one synchronous segment, so this is
        the commit side of the
        revocation/invocation race: a request whose native invocation has
        not started never starts. A native invocation that already won is not
        cancelled: adapter cancellation is not proof that a detached or
        transport-owned effect was aborted. The returned worker is therefore
        retained as the settlement handle and the lifecycle boundary waits
        for the adapter callback to finish before returning. A worker that
        has already finished is returned as well so callers settle uniformly.
        """
        channel = _DELIVERY_CHANNELS.get(self)
        if channel is None:
            return None
        channel.revoked = True
        worker = channel.worker
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
                channel.wake.set()
            except (RuntimeError, asyncio.QueueFull):
                pass
        return worker


_DELIVERY_CHANNELS: weakref.WeakKeyDictionary[
    GatewayDelivery,
    _DeliveryChannel,
] = weakref.WeakKeyDictionary()


async def _await_delivery_settlement(
    worker: asyncio.Task[None],
    *,
    owner: Optional[asyncio.Task[Any]] = None,
) -> None:
    """Wait for a delivery worker's native invocation to finish.

    ``worker`` is awaited shielded so cancellation of the lifecycle path
    cannot detach the settlement wait and let the boundary return while
    the native effect is still live. When the awaiting lifecycle path is
    itself cancelled mid-wait, propagation is deferred: the cancellation
    is consumed and the shield-await is re-entered until the worker
    settles, and only then is ``CancelledError`` re-raised so the caller
    observes cancellation strictly after no revoked native effect remains
    live. The only legitimate escape is the worker awaiting itself (host
    lifecycle code unwinding inside the delivery callback frame), which
    would otherwise self-deadlock.
    """
    if worker.done():
        return
    if owner is not None and owner is worker:
        return
    waiter = asyncio.current_task()
    deferred = False
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            if worker.done():
                # The worker itself completed as cancelled (revocation's own
                # cancellation request won the race): the invocation is
                # settled and the boundary may proceed. Preserve a concurrent
                # outer cancellation rather than mistaking it for the worker's.
                if waiter is not None and waiter.cancelling():
                    deferred = True
                    waiter.uncancel()
                break
            # The awaiting lifecycle path was cancelled while the worker is
            # still live. Consume the cancellation so it cannot detach the
            # settlement wait, keep shield-awaiting until the worker
            # settles, and re-raise afterwards.
            deferred = True
            if waiter is not None and waiter.cancelling():
                waiter.uncancel()
    if deferred:
        raise asyncio.CancelledError()


async def _delivery_worker(
    channel: _DeliveryChannel,
    send_callback: Callable[[str], Awaitable[Any] | Any],
    *,
    reply_callback: Callable[[str], Awaitable[Any] | Any] | None,
    react_callback: Callable[[str, str], Awaitable[Any] | Any] | None,
) -> None:
    if not channel.participant_settled.is_set():
        wake_waiter = asyncio.create_task(channel.wake.wait())
        settled_waiter = asyncio.create_task(channel.participant_settled.wait())
        waiters = {wake_waiter, settled_waiter}
        done: set[asyncio.Task[bool]] = set()
        try:
            done, _pending = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        for waiter in done:
            waiter.result()
    try:
        if not channel.wake.is_set():
            await asyncio.wait_for(
                channel.wake.wait(),
                timeout=GATEWAY_DELIVERY_CAPABILITY_TTL_SECONDS,
            )
    except TimeoutError:
        channel.revoked = True
        channel.consumed = True
        return
    try:
        request = channel.queue.get_nowait()
    except asyncio.QueueEmpty:
        channel.revoked = True
        channel.consumed = True
        return
    if channel.revoked:
        if not request.future.done():
            request.future.set_result(GatewayDeliveryReceipt(status="failed"))
        return
    try:
        # Revocation and native invocation share one synchronous commit
        # boundary. Once this flag is set the host deliberately does not
        # cancel the adapter callback: cancellation cannot prove that an
        # already-submitted external effect was aborted.
        channel.invocation_started = True
        if request.kind == "reply":
            if not callable(reply_callback):
                native_result = SendResult(
                    success=False,
                    native_delivery_non_occurrence_attested=True,
                )
            else:
                native_result = reply_callback(request.content)
        elif request.kind == "react":
            if not callable(react_callback):
                native_result = SendResult(
                    success=False,
                    native_delivery_non_occurrence_attested=True,
                )
            else:
                native_result = react_callback(request.content, request.operation)
        else:
            native_result = send_callback(request.content)
        if inspect.isawaitable(native_result):
            native_result = await native_result
        # If revocation arrived after invocation started, the effect may have
        # committed. Awaiting the callback settles the adapter-owned operation,
        # but attribution remains unknown rather than fabricated failure.
        if channel.revoked:
            if not request.future.done():
                request.future.set_result(GatewayDeliveryReceipt(status="unknown"))
            return
    except asyncio.CancelledError:
        # Core revocation never cancels an invocation that already started.
        # If an unrelated owner cancels this worker, native non-occurrence is
        # unprovable after invocation and the only truthful receipt is unknown.
        if not request.future.done():
            request.future.set_result(
                GatewayDeliveryReceipt(
                    status="unknown" if channel.invocation_started else "failed"
                )
            )
        return
    except Exception:
        receipt = GatewayDeliveryReceipt(status="unknown")
    else:
        receipt = _normalize_delivery_receipt(native_result, request, channel.binding)
        if channel.revoked and receipt.status == "sent":
            receipt = GatewayDeliveryReceipt(status="unknown")
    if not request.future.done():
        request.future.set_result(receipt)


def _normalize_delivery_receipt(
    native_result: Any,
    request: _DeliveryRequest,
    binding: _DeliveryBinding,
) -> GatewayDeliveryReceipt:
    if not isinstance(native_result, SendResult):
        return GatewayDeliveryReceipt(status="unknown")
    if native_result.success is False:
        return GatewayDeliveryReceipt(
            status=(
                "failed"
                if native_result.native_delivery_non_occurrence_attested is True
                else "unknown"
            )
        )
    if native_result.success is not True or binding.self_actor_id is None:
        return GatewayDeliveryReceipt(status="unknown")
    raw_response = native_result.raw_response
    if isinstance(raw_response, dict):
        delivered_chunks = raw_response.get("delivered_chunks")
        total_chunks = raw_response.get("total_chunks")
        if raw_response.get("partial_overflow") is True or (
            isinstance(delivered_chunks, int)
            and isinstance(total_chunks, int)
            and delivered_chunks < total_chunks
        ):
            return GatewayDeliveryReceipt(status="unknown")
    ack = native_result.native_delivery_ack
    if not isinstance(ack, NativeDeliveryAck):
        return GatewayDeliveryReceipt(status="unknown")
    expected_kind = {"send": "send", "reply": "reply", "react": "react"}[request.kind]
    expected_reply = binding.source_message_id if request.kind == "reply" else None
    expected_target = binding.source_message_id if request.kind == "react" else None
    expected_content = request.content if request.kind != "react" else None
    expected_reaction = request.content if request.kind == "react" else None
    expected_operation = request.operation if request.kind == "react" else None
    if (
        ack.platform != binding.platform
        or ack.room_id != binding.room_id
        or ack.self_actor_id != binding.self_actor_id
        or ack.effect_kind != expected_kind
        or ack.submitted_content != expected_content
        or ack.reply_to_message_id != expected_reply
        or ack.target_message_id != expected_target
        or ack.reaction != expected_reaction
        or ack.reaction_operation != expected_operation
    ):
        return GatewayDeliveryReceipt(status="unknown")
    if request.kind == "react":
        if (
            ack.message_id is not None
            or not ack.effect_id
            or not ack.effect_id.startswith(f"{binding.platform}:reaction:")
        ):
            return GatewayDeliveryReceipt(status="unknown")
    else:
        native_message_id = _optional_text(native_result.message_id)
        if (
            not native_message_id
            or ack.message_id != native_message_id
            or ack.effect_id != native_message_id
            or native_message_id == binding.source_message_id
        ):
            return GatewayDeliveryReceipt(status="unknown")
    return GatewayDeliveryReceipt(
        status="sent",
        platform=ack.platform,
        room_id=ack.room_id,
        profile=binding.profile,
        self_actor_id=ack.self_actor_id,
        effect_kind=ack.effect_kind,
        submitted_content=ack.submitted_content,
        reply_to_message_id=ack.reply_to_message_id,
        target_message_id=ack.target_message_id,
        reaction=ack.reaction,
        reaction_operation=ack.reaction_operation,
        message_id=ack.message_id,
        effect_id=ack.effect_id,
    )


def _consume_delivery_worker_result(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        pass


def _finalize_delivery_worker(
    task: asyncio.Task[None],
    delivery_ref: "weakref.ReferenceType[GatewayDelivery]",
    channel: _DeliveryChannel,
) -> None:
    """Close one terminal capability and release every host registry edge."""
    _consume_delivery_worker_result(task)
    channel.revoked = True
    channel.consumed = True
    delivery = delivery_ref()
    if delivery is None:
        return
    _DELIVERY_CHANNELS.pop(delivery, None)
    callback = channel.terminal_callback
    channel.terminal_callback = None
    if callback is not None:
        try:
            callback(delivery)
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
