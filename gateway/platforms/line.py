#!/usr/bin/env python3
"""
Hermes Agent adapter for LINE Messaging API.

Connects Hermes Agent to LINE via the Messaging API webhook + push/reply pattern.
Starts a local HTTP server to receive webhooks from LINE Platform.

Supports bidirectional communication:
- Inbound: LINE webhook → message queue → Agent processing
- Outbound: Reply API (within 24h) or Push API (anytime) → LINE user

.. deprecated::
    This gateway-embedded adapter is superseded by the plugin version at
    ``plugins/platforms/line/adapter.py`` (``LineAdapter``).  The plugin
    version provides richer features (group member name resolution, media
    handling, postback buttons, retry logic).  This module will be kept as a
    thin shim during the migration period and removed once the plugin is the
    sole LINE adapter.  Do not add new features here — extend the plugin
    instead.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import socket as _socket
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from gateway.config import Platform, PlatformConfig, LINE_WEBHOOK_EVENTS_MAX
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.platforms.helpers import MessageDeduplicator

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

LINE_API_BASE_URL = "https://api.line.me/v2/bot"
LINE_PUSH_MESSAGE_EP = f"{LINE_API_BASE_URL}/message/push"
LINE_REPLY_MESSAGE_EP = f"{LINE_API_BASE_URL}/message/reply"
LINE_MULTICAST_EP = f"{LINE_API_BASE_URL}/message/multicast"
LINE_GET_MEMBER_COUNT_EP = f"{LINE_API_BASE_URL}/group/{{group_id}}/members/count"

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8645
DEFAULT_WEBHOOK_PATH = "/webhook/line"


def check_line_requirements() -> bool:
    """Check if LINE adapter dependencies are available."""
    if not AIOHTTP_AVAILABLE:
        return False
    return bool(os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip())


class LineAdapter(BasePlatformAdapter):
    """Hermes Agent adapter for LINE Messaging API.

    Supports bidirectional communication:
    - **Inbound**: LINE webhook → message queue → Agent processing
    - **Outbound**: Reply API (within 24h window) or Push API (anytime)
    - **Async push**: ``push_message()`` for Agent-initiated notifications
    """

    MAX_MESSAGE_LENGTH = 5000  # LINE text message max (officially 5000 chars)
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.LINE)
        extra = config.extra or {}
        self.channel_access_token = str(
            config.token or extra.get("channel_access_token")
            or os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
        ).strip()
        self.channel_secret = str(
            extra.get("channel_secret")
            or os.getenv("LINE_CHANNEL_SECRET", "")
        ).strip()

        # HTTP server config
        self._host = str(extra.get("host") or DEFAULT_HOST)
        self._port = int(extra.get("port") or DEFAULT_PORT)
        self._path = str(extra.get("path") or DEFAULT_WEBHOOK_PATH)

        # Reply token store: message_id -> {"token": str, "inserted_at": float}
        # LINE reply tokens expire in ~30s; entries are cleaned up after 25s.
        self._reply_tokens: Dict[str, dict] = {}
        self._REPLY_TOKEN_TTL = 30.0
        self._REPLY_TOKEN_CLEANUP_AT = 25.0

        # DM / group access control
        self.dm_policy = str(
            extra.get("dm_policy") or os.getenv("LINE_DM_POLICY", "open")
        ).strip().lower()
        self.group_policy = str(
            extra.get("group_policy") or os.getenv("LINE_GROUP_POLICY", "open")
        ).strip().lower()
        self.allow_from = _coerce_list(
            extra.get("allow_from") or os.getenv("LINE_ALLOWED_USERS", "")
        )
        self.group_allow_from = _coerce_list(
            extra.get("group_allow_from") or os.getenv("LINE_GROUP_ALLOWED_USERS", "")
        )

        # HTTP client for LINE API
        self._session: Optional[Any] = None  # aiohttp.ClientSession
        # HTTP server for webhooks
        self._app: Optional[Any] = None  # web.Application
        self._runner: Optional[Any] = None  # web.AppRunner
        self._site: Optional[Any] = None  # web.TCPSite
        self._poll_task: Optional[asyncio.Task] = None
        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._background_tasks: Set[asyncio.Task] = set()

        # Async push queue: enables Agent-initiated messages independent of
        # webhook triggers (e.g., scheduled notifications, async results).
        self._send_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._send_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        self._dedup = MessageDeduplicator(ttl_seconds=300)

        # Retry / dead-letter config
        self._MAX_DISPATCH_RETRIES = 3
        self._DEAD_LETTER_QUEUE_MAX_SIZE = 1000
        self._DEAD_LETTER_QUEUE_PRUNE_AT = 100  # keep only the most recent N entries
        self._dead_letter_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._retry_counts: Dict[str, tuple] = {}  # event_id -> (count, inserted_at)
        self._retry_lock = asyncio.Lock()

        # Member display name cache: (chat_type, chat_id, user_id) -> (display_name, inserted_at)
        self._member_name_cache: Dict[tuple, tuple] = {}
        self._MEMBER_NAME_CACHE_TTL = 3600.0  # 1 hour
        self._MEMBER_NAME_CACHE_MAX_SIZE = 500  # evict oldest 25% when exceeded
        self._MEMBER_API_TIMEOUT = 3.0  # max seconds for member profile lookups

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Start HTTP server for LINE webhooks and verify API credentials."""
        if not self.channel_access_token:
            message = "LINE startup failed: LINE_CHANNEL_ACCESS_TOKEN is required"
            self._set_fatal_error("line_missing_token", message, retryable=False)
            logger.warning("[%s] %s", self.name, message)
            return False

        if not AIOHTTP_AVAILABLE:
            self._set_fatal_error("line_no_aiohttp", "aiohttp not installed", retryable=False)
            return False

        import aiohttp
        timeout = aiohttp.ClientTimeout(total=30)
        self._session = aiohttp.ClientSession(trust_env=True, timeout=timeout)

        # Verify API credentials
        try:
            await self._line_api_get(f"{LINE_API_BASE_URL}/info")
            logger.info("[%s] LINE API credentials verified", self.name)
        except Exception as exc:
            logger.warning("[%s] LINE API credential check failed (continuing): %s", self.name, exc)

        # Port conflict check
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", self._port))
            logger.error("[%s] Port %d already in use", self.name, self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass

        # Start HTTP server
        try:
            self._app = web.Application()
            self._app.router.add_get("/health", self._handle_health)
            self._app.router.add_post(self._path, self._handle_webhook)
            self._runner = web.AppRunner(self._app)
            await self._runner.setup()
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
            # Start the poll loop which drains the message queue.
            # The queue buffers webhook events until _message_handler is set
            # by the GatewayRunner (see _poll_loop guard below).
            self._poll_task = asyncio.create_task(self._poll_loop())
            # Start the async push sender for Agent-initiated messages.
            self._send_task = asyncio.create_task(self._send_loop())
            # Start periodic cleanup of in-memory caches.
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._mark_connected()
            logger.info(
                "[%s] HTTP server listening on %s:%d%s",
                self.name, self._host, self._port, self._path,
            )
            return True
        except Exception as exc:
            message = f"Failed to start HTTP server: {exc}"
            self._set_fatal_error("line_server_error", message, retryable=True)
            logger.error("[%s] %s", self.name, message)
            return False

    async def disconnect(self) -> None:
        """Shutdown HTTP server and HTTP client."""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._send_task:
            self._send_task.cancel()
            try:
                await self._send_task
            except asyncio.CancelledError:
                pass
            self._send_task = None

        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None

        if self._site:
            await self._site.stop()
            self._site = None
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._member_name_cache.clear()
        self._retry_counts.clear()
        # Drain the dead-letter queue
        while not self._dead_letter_queue.empty():
            try:
                self._dead_letter_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        self._mark_disconnected()
        logger.info("[%s] Disconnected", self.name)

    # ------------------------------------------------------------------
    # HTTP server handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: Any) -> Any:
        """Health check endpoint."""
        return web.Response(text="ok")

    async def _handle_webhook(self, request: Any) -> Any:
        """Receive LINE webhook POST and enqueue message events.

        Handles:
        - text messages (processed → Agent reply)
        - follow/unfollow events
        - postback events
        - member join/leave events
        - other events (logged and acknowledged)
        """
        signature = request.headers.get("X-Line-Signature", "")
        body = await request.read()

        try:
            body_json = json.loads(body)
        except json.JSONDecodeError:
            logger.error("[%s] Failed to decode JSON from webhook body", self.name)
            return web.Response(status=400, text="invalid json")

        events_data = body_json.get("events", [])

        # LINE Developers console verification sends empty events array.
        # Return 200 immediately so the console check passes.
        if not events_data:
            logger.debug("[%s] Received webhook with empty events (verification ping)", self.name)
            return web.Response(text="ok")

        # Signature verification (only for real events)
        if self.channel_secret:
            if not signature or not self._verify_signature(body, signature):
                logger.error("[%s] Invalid webhook signature", self.name)
                return web.Response(status=403, text="invalid signature")
        else:
            # Reject unauthenticated webhooks unless explicitly opted-in
            # for development (LINE_SKIP_SIGNATURE_VERIFY=true).
            skip = os.getenv("LINE_SKIP_SIGNATURE_VERIFY", "").lower() in {"true", "1", "yes"}
            if skip:
                logger.warning(
                    "[%s] Channel secret not set; signature verification SKIPPED "
                    "(LINE_SKIP_SIGNATURE_VERIFY=true — insecure, do not use in production)",
                    self.name,
                )
            else:
                logger.error("[%s] Channel secret not set; rejecting webhook (set LINE_CHANNEL_SECRET or LINE_SKIP_SIGNATURE_VERIFY=true for dev)", self.name)
                return web.Response(status=403, text="channel_secret required")

        count = 0
        if len(events_data) > LINE_WEBHOOK_EVENTS_MAX:
            logger.warning(
                "[%s] Webhook contains %d events (limit %d); processing first %d",
                self.name, len(events_data), LINE_WEBHOOK_EVENTS_MAX, LINE_WEBHOOK_EVENTS_MAX,
            )
            events_data = events_data[:LINE_WEBHOOK_EVENTS_MAX]
        for item in events_data:
            if not isinstance(item, dict):
                continue
            processed = await self._process_webhook_event(item)
            for event in processed:
                await self._message_queue.put(event)
                count += 1

        logger.debug("[%s] Enqueued %d message event(s) from webhook", self.name, count)
        return web.Response(text="ok")

    async def _poll_loop(self) -> None:
        """Drain the message queue and dispatch to the gateway runner.

        Guards against dispatching before the GatewayRunner has set
        ``_message_handler`` (which happens after ``connect()`` returns).
        While the handler is not yet set, events are re-queued with a
        short delay so no inbound message is lost.
        """
        while True:
            event = await self._message_queue.get()
            # If the gateway runner hasn't installed the handler yet,
            # re-queue the event and wait briefly rather than dropping it.
            if self._message_handler is None:
                logger.debug("[%s] _message_handler not set yet; re-queuing event", self.name)
                await self._message_queue.put(event)
                await asyncio.sleep(0.5)
                continue
            await self._dispatch_with_retry(event)

    async def _dispatch_with_retry(self, event: MessageEvent) -> None:
        """Dispatch a single event with retry + dead-letter fallback."""
        event_id = getattr(event, "message_id", None) or id(event)
        async with self._retry_lock:
            entry = self._retry_counts.get(event_id)
            retry_count = entry[0] if entry else 0

        if retry_count >= self._MAX_DISPATCH_RETRIES:
            logger.error("[%s] Event %s failed after %d retries — moving to dead-letter queue",
                         self.name, event_id, self._MAX_DISPATCH_RETRIES)
            # Cap queue size to prevent unbounded growth
            while self._dead_letter_queue.qsize() >= self._DEAD_LETTER_QUEUE_MAX_SIZE:
                try:
                    self._dead_letter_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            await self._dead_letter_queue.put({
                "event": event,
                "retry_count": retry_count,
                "reason": "max_retries_exceeded",
            })
            async with self._retry_lock:
                self._retry_counts.pop(event_id, None)
            return

        try:
            task = asyncio.create_task(self.handle_message(event))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
            await task  # Propagate exception to caller
        except Exception:
            async with self._retry_lock:
                self._retry_counts[event_id] = (retry_count + 1, time.monotonic())
            logger.warning("[%s] Dispatch failed for event %s (attempt %d/%d), scheduling retry",
                           self.name, event_id, retry_count + 1, self._MAX_DISPATCH_RETRIES,
                           exc_info=True)
            # Re-enqueue with a small delay to avoid tight retry loops
            await asyncio.sleep(1)
            await self._message_queue.put(event)

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a LINE user/group/room.

        Uses the **Reply API** when a fresh ``reply_to`` token is available
        (within ~24 h of the user's message), otherwise falls back to the
        **Push API**.

        Args:
            chat_id: The user/group/room ID to send to.
            content: Message text (will be split if over ``MAX_MESSAGE_LENGTH``).
            reply_to: Original message ID to use as reply token, if available.
            metadata: Ignored on LINE (present for interface compatibility).

        Returns:
            ``SendResult`` with ``success`` and ``message_id``.
        """
        if not self._session or not self.channel_access_token:
            return SendResult(success=False, error="Not connected or token missing")

        try:
            chunks = self._split_text(content)
            last_message_id: Optional[str] = None

            # Prefer push message (reliable); use reply only when we have a
            # fresh reply_token cached for the triggering message.
            # Enforce TTL cleanup for the referenced token and all expired entries.
            reply_token: Optional[str] = None
            if reply_to:
                entry = self._reply_tokens.pop(reply_to, None)
                if entry:
                    now = time.monotonic()
                    if now - entry["inserted_at"] <= self._REPLY_TOKEN_TTL:
                        reply_token = entry["token"]
                    else:
                        logger.debug("[%s] reply_token for %s expired (%.1fs old)",
                                     self.name, reply_to, now - entry["inserted_at"])
            self._clear_expired_reply_tokens()
            self._cleanup_member_name_cache()

            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue

                # Only the first chunk can use replyToken; subsequent chunks
                # must be push messages.
                if i == 0 and reply_token:
                    payload = {
                        "replyToken": reply_token,
                        "messages": [{"type": "text", "text": chunk}],
                    }
                    endpoint = LINE_REPLY_MESSAGE_EP
                else:
                    payload = {
                        "to": chat_id,
                        "messages": [{"type": "text", "text": chunk}],
                    }
                    endpoint = LINE_PUSH_MESSAGE_EP

                await self._line_api_post(endpoint, payload)

                # LINE reply endpoint returns empty body on success; generate
                # a client-side id for tracking.
                last_message_id = f"line-{secrets.token_hex(8)}-{i}"

                # Throttle between chunks to avoid rate limits
                if i < len(chunks) - 1:
                    await asyncio.sleep(0.5)

            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.error("[%s] send failed to=%s: %s", self.name, chat_id, exc)
            return SendResult(success=False, error=str(exc))

    async def push_message(
        self,
        chat_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Enqueue an asynchronous push message to a LINE user/group/room.

        Unlike ``send()`` – which tries the Reply API first – this method
        **always uses the Push API**, making it suitable for Agent-initiated
        notifications that arrive independently of a user message
        (e.g., scheduled reminders, background task results).

        Multiple calls are batched through an internal queue (``_send_queue``)
        and dispatched by a background sender task (``_send_loop``), so callers
        can ``await`` the enqueue without blocking on the HTTP round-trip.

        Args:
            chat_id: LINE user/group/room ID.
            content: Message text (split internally if over ``MAX_MESSAGE_LENGTH``).
            metadata: Ignored on LINE (present for interface compatibility).

        Returns:
            ``SendResult`` — ``success=True`` if the payload was enqueued.
        """
        if not self._session or not self.channel_access_token:
            return SendResult(success=False, error="Not connected or token missing")

        try:
            chunks = self._split_text(content)
            for chunk in chunks:
                if not chunk.strip():
                    continue
                await self._send_queue.put({
                    "to": chat_id,
                    "messages": [{"type": "text", "text": chunk}],
                })
            return SendResult(success=True)
        except Exception as exc:
            logger.error("[%s] push_message failed to=%s: %s",
                         self.name, chat_id, exc)
            return SendResult(success=False, error=str(exc))

    def _clear_expired_reply_tokens(self) -> None:
        """Remove reply_token entries older than the cleanup threshold."""
        now = time.monotonic()
        expired = [
            mid for mid, entry in self._reply_tokens.items()
            if now - entry["inserted_at"] > self._REPLY_TOKEN_CLEANUP_AT
        ]
        for mid in expired:
            del self._reply_tokens[mid]
        if expired:
            logger.debug("[%s] Cleaned up %d expired reply_token(s)", self.name, len(expired))

    # ------------------------------------------------------------------
    # Async push sender (background task)
    # ------------------------------------------------------------------

    async def _send_loop(self) -> None:
        """Background task that drains the async push queue and calls the LINE Push API.

        Runs until ``disconnect()`` cancels the task.  Failures are logged but
        do not crash the loop — individual messages are not retried (the caller
        can re-enqueue if needed).
        """
        while True:
            payload = await self._send_queue.get()
            try:
                await self._line_api_post(LINE_PUSH_MESSAGE_EP, payload)
                logger.debug("[%s] Push message sent to %s", self.name, payload.get("to"))
            except Exception as exc:
                logger.error("[%s] Push message to %s failed: %s",
                             self.name, payload.get("to"), exc)
            self._cleanup_member_name_cache()

    # ------------------------------------------------------------------
    # Periodic cleanup
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Background task that periodically prunes stale in-memory state.

        Runs every ``_CLEANUP_INTERVAL`` seconds.  Prevents unbounded growth
        of reply token cache, member name cache, retry-count dictionary, and
        the dead-letter queue.
        """
        CLEANUP_INTERVAL = 300.0  # 5 minutes
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL)
            try:
                self._clear_expired_reply_tokens()
                self._cleanup_member_name_cache()
                self._cleanup_retry_counts()
                self._prune_dead_letter_queue()
            except Exception:
                logger.debug(
                    "[%s] Periodic cleanup failed", self.name, exc_info=True,
                )

    def _cleanup_retry_counts(self) -> None:
        """Remove retry-count entries older than ``_RETRY_COUNT_TTL``.

        Entries are keyed by ``event_id`` and store ``(count, inserted_at)``.
        Normally entries are popped on successful dispatch or after
        ``_MAX_DISPATCH_RETRIES`` failures.  Entries that remain past the
        TTL (e.g. from dropped or lost events) are swept here to prevent
        unbounded dictionary growth.
        """
        RETRY_COUNT_TTL = 3600.0  # 1 hour
        now = time.monotonic()
        expired = [
            eid for eid, (_, inserted_at) in self._retry_counts.items()
            if now - inserted_at > RETRY_COUNT_TTL
        ]
        for eid in expired:
            self._retry_counts.pop(eid, None)
        if expired:
            logger.debug(
                "[%s] Cleaned up %d stale retry-count entry(s)",
                self.name, len(expired),
            )

    def _prune_dead_letter_queue(self) -> None:
        """Keep only the most recent entries in the dead-letter queue.

        The queue exists for diagnostics.  When it exceeds
        ``_DEAD_LETTER_QUEUE_PRUNE_AT`` entries, oldest items are
        drained until the size is back within the limit.
        """
        q = self._dead_letter_queue
        prune_at = self._DEAD_LETTER_QUEUE_PRUNE_AT
        while q.qsize() > prune_at:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        if q.qsize() > 0:
            logger.debug(
                "[%s] Dead-letter queue size=%d (pruned to %d)",
                self.name, q.qsize(), prune_at,
            )

    # ------------------------------------------------------------------
    # Formatting (platform-specific override)
    # ------------------------------------------------------------------

    def format_message(self, content: str) -> str:
        """Format a message for LINE.

        LINE does not use Markdown, so this is a no-op pass-through.
        Subclasses can override to add LINE-specific formatting
        (e.g., converting [[b]]…[[/b]] to Unicode bold characters).
        """
        return content

    # ------------------------------------------------------------------
    # Webhook event processing
    # ------------------------------------------------------------------

    async def _process_webhook_event(self, event_data: Dict[str, Any]) -> List[MessageEvent]:
        """Convert a single LINE Webhook event to Hermes MessageEvent(s).

        Handles:
        - ``message`` events (text only for now)
        - ``follow`` / ``unfollow`` events
        - ``postback`` events
        - ``memberJoined`` / ``memberLeft`` events
        - Other event types are logged and acknowledged (200 OK).
        """
        event_type = event_data.get("type")
        source = event_data.get("source", {})
        sender_id = source.get("userId")

        chat_id: Optional[str] = None
        chat_type: Optional[str] = None

        source_type = source.get("type")
        if source_type == "user":
            chat_type = "dm"
            chat_id = sender_id
        elif source_type == "group":
            chat_type = "group"
            chat_id = source.get("groupId")
        elif source_type == "room":
            chat_type = "room"
            chat_id = source.get("roomId")

        # ---- Non-message events -------------------------------------------------

        if event_type == "follow":
            logger.info("[%s] User %s followed the bot (source: %s)",
                        self.name, sender_id, source_type)
            return []

        if event_type == "unfollow":
            logger.info("[%s] User %s unfollowed the bot (source: %s)",
                        self.name, sender_id, source_type)
            return []

        if event_type == "postback":
            # Postback data arrives in event.get("postback", {}).get("data")
            postback_data = event_data.get("postback", {}).get("data", "")
            logger.info("[%s] Postback from %s: %s (data=%r)",
                        self.name, sender_id, source_type, postback_data)
            return []

        if event_type == "memberJoined":
            joined = event_data.get("joined", {}).get("members", [])
            logger.info("[%s] %d member(s) joined %s %s",
                        self.name, len(joined), chat_type or "chat", chat_id)
            return []

        if event_type == "memberLeft":
            left = event_data.get("left", {}).get("members", [])
            logger.info("[%s] %d member(s) left %s %s",
                        self.name, len(left), chat_type or "chat", chat_id)
            return []

        # ---- Message events -----------------------------------------------------

        if event_type != "message":
            logger.debug("[%s] Unhandled event type %r from %s; acknowledged",
                         self.name, event_type, sender_id)
            return []

        if not chat_id or not sender_id:
            logger.warning("[%s] Skipping event with missing source: %s", self.name, event_data)
            return []

        # Access control: DM policy
        if chat_type == "dm":
            if self.allow_from:
                if sender_id not in self.allow_from:
                    logger.warning("[%s] DM from %s rejected (not in allow_from)", self.name, sender_id)
                    return []
            elif self.dm_policy == "closed":
                logger.warning("[%s] DM from %s rejected (dm_policy=closed, no allow_from)", self.name, sender_id)
                return []

        # Access control: group/room policy
        if chat_type in ("group", "room"):
            if self.group_policy == "disabled":
                logger.debug("[%s] Message from %s in %s ignored (group_policy=disabled)",
                             self.name, sender_id, chat_type)
                return []
            if self.group_allow_from and sender_id not in self.group_allow_from:
                logger.debug("[%s] Message from %s in %s ignored (not in group_allow_from)",
                             self.name, sender_id, chat_type)
                return []

        message = event_data.get("message", {})
        message_type = message.get("type")
        message_id = message.get("id")
        timestamp_ms = event_data.get("timestamp")

        if message_type != "text":
            # Non-text messages (image, video, sticker, location, audio, file)
            # are logged but not forwarded to the Agent yet.
            logger.debug("[%s] Non-text message type %r from %s in %s (message_id=%s)",
                         self.name, message_type, sender_id, chat_type, message_id)
            return []

        text = message.get("text", "")
        if not text:
            return []

        # Dedup
        if self._dedup.is_duplicate(message_id):
            logger.debug("[%s] Skipping duplicate message: %s", self.name, message_id)
            return []

        reply_token = event_data.get("replyToken")
        if reply_token:
            self._reply_tokens[message_id] = {
                "token": reply_token,
                "inserted_at": time.monotonic(),
            }

        # Resolve sender display name for group/room messages
        user_name: str = sender_id or ""
        if chat_type in ("group", "room") and sender_id and chat_id:
            display_name = await self._get_member_display_name(
                chat_type, chat_id, sender_id
            )
            if display_name:
                user_name = display_name

        source_info = self.build_source(
            chat_id=chat_id,
            chat_type=chat_type,
            user_id=sender_id,
            user_name=user_name,
        )

        return [MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source_info,
            raw_message=event_data,
            message_id=message_id,
            reply_to_message_id=None,
            timestamp=datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc) if timestamp_ms else datetime.now(timezone.utc),
        )]

    # ------------------------------------------------------------------
    # Required abstract methods
    # ------------------------------------------------------------------

    async def _get_member_display_name(
        self, chat_type: str, chat_id: str, user_id: str
    ) -> Optional[str]:
        """Resolve a member's display name in a group or room.

        Cached in-memory with a TTL to avoid stale display names and
        prevent repeated API calls for the same member.
        """
        if chat_type not in ("group", "room") or not chat_id or not user_id:
            return None

        cache_key = (chat_type, chat_id, user_id)
        now = time.monotonic()
        if cache_key in self._member_name_cache:
            cached_name, inserted_at = self._member_name_cache[cache_key]
            if now - inserted_at <= self._MEMBER_NAME_CACHE_TTL:
                return cached_name
            # Expired — evict and re-fetch below
            del self._member_name_cache[cache_key]

        try:
            endpoint = (
                f"{LINE_API_BASE_URL}/group/{chat_id}/member/{user_id}"
                if chat_type == "group"
                else f"{LINE_API_BASE_URL}/room/{chat_id}/member/{user_id}"
            )
            profile = await asyncio.wait_for(
                self._line_api_get(endpoint),
                timeout=self._MEMBER_API_TIMEOUT,
            )
            display_name = profile.get("displayName")
            if display_name:
                self._member_name_cache[cache_key] = (display_name, time.monotonic())
                self._evict_oldest_if_needed()
            return display_name
        except (Exception, asyncio.TimeoutError):
            logger.debug(
                "[%s] Failed to resolve member name for %s in %s %s",
                self.name, user_id, chat_type, chat_id,
            )
            return None

    def _cleanup_member_name_cache(self) -> None:
        """Remove member name cache entries older than the TTL."""
        now = time.monotonic()
        expired = [
            key for key, (_, inserted_at) in self._member_name_cache.items()
            if now - inserted_at > self._MEMBER_NAME_CACHE_TTL
        ]
        for key in expired:
            del self._member_name_cache[key]
        if expired:
            logger.debug(
                "[%s] Cleaned up %d expired member name cache entry(s)",
                self.name, len(expired),
            )

    def _evict_oldest_if_needed(self) -> None:
        """Evict the oldest 25% of entries when cache exceeds the max size."""
        if len(self._member_name_cache) <= self._MEMBER_NAME_CACHE_MAX_SIZE:
            return
        evict_count = max(1, self._MEMBER_NAME_CACHE_MAX_SIZE // 4)
        sorted_entries = sorted(
            self._member_name_cache.items(), key=lambda kv: kv[1][1]
        )
        for key, _ in sorted_entries[:evict_count]:
            del self._member_name_cache[key]
        logger.debug(
            "[%s] Evicted %d oldest member name cache entry(s) (size=%d, max=%d)",
            self.name, evict_count,
            len(self._member_name_cache), self._MEMBER_NAME_CACHE_MAX_SIZE,
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get information about a LINE chat (user / group / room).

        Uses the well-known LINE ID prefix to determine chat type:
        ``U`` = user (DM), ``C`` = group, ``R`` = room.
        """
        prefix = (chat_id or "")[:1]
        try:
            if prefix == "C":
                # Group: fetch group summary for the group name
                summary = await self._line_api_get(
                    f"{LINE_API_BASE_URL}/group/{chat_id}/summary"
                )
                return {
                    "name": summary.get("groupName", chat_id),
                    "type": "group",
                }
            elif prefix == "R":
                # Room: no summary endpoint available; infer from prefix
                return {"name": chat_id, "type": "room"}
            else:
                # User / DM: fetch profile
                profile = await self._line_api_get(
                    f"{LINE_API_BASE_URL}/profile/{chat_id}"
                )
                return {
                    "name": profile.get("displayName", chat_id),
                    "type": "dm",
                }
        except Exception:
            chat_type = {"U": "dm", "C": "group", "R": "room"}.get(prefix, "unknown")
            return {"name": chat_id, "type": chat_type}

    # ------------------------------------------------------------------
    # Signature verification
    # ------------------------------------------------------------------

    def _verify_signature(self, body: bytes, signature: str) -> bool:
        """Verify LINE Webhook request signature (HMAC-SHA256, Base64)."""
        if not self.channel_secret:
            return False
        mac = hmac.new(
            self.channel_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        )
        expected = base64.b64encode(mac.digest()).decode("utf-8")
        return hmac.compare_digest(expected, signature)

    # ------------------------------------------------------------------
    # HTTP helpers (LINE API client)
    # ------------------------------------------------------------------

    async def _line_api_post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a LINE Messaging API POST request."""
        if not self._session:
            raise RuntimeError("Not connected to LINE API")

        headers = {
            "Authorization": f"Bearer {self.channel_access_token}",
            "Content-Type": "application/json",
        }
        async with self._session.post(endpoint, json=payload, headers=headers) as resp:
            if not resp.ok:
                try:
                    err = await resp.json()
                    msg = err.get("message", resp.reason)
                except Exception:
                    msg = await resp.text()
                raise RuntimeError(f"LINE API POST {endpoint} failed ({resp.status}): {msg}")
            try:
                return await resp.json()
            except Exception:
                logger.debug(
                    "[%s] LINE API POST %s returned non-JSON response (status %d)",
                    self.name, endpoint, resp.status,
                )
                return {}

    async def _line_api_get(self, endpoint: str) -> Dict[str, Any]:
        """Execute a LINE Messaging API GET request."""
        if not self._session:
            raise RuntimeError("Not connected to LINE API")

        headers = {"Authorization": f"Bearer {self.channel_access_token}"}
        async with self._session.get(endpoint, headers=headers) as resp:
            if not resp.ok:
                try:
                    err = await resp.json()
                    msg = err.get("message", resp.reason)
                except Exception:
                    msg = await resp.text()
                raise RuntimeError(f"LINE API GET {endpoint} failed ({resp.status}): {msg}")
            try:
                return await resp.json()
            except Exception:
                logger.debug(
                    "[%s] LINE API GET %s returned non-JSON response (status %d)",
                    self.name, endpoint, resp.status,
                )
                return {}

    # ------------------------------------------------------------------
    # Text splitting
    # ------------------------------------------------------------------

    def _split_text(self, content: str) -> List[str]:
        """Split content into chunks within LINE's message length limit."""
        if len(content) <= self.MAX_MESSAGE_LENGTH:
            return [content]
        chunks = []
        for i in range(0, len(content), self.MAX_MESSAGE_LENGTH):
            chunks.append(content[i : i + self.MAX_MESSAGE_LENGTH])
        return [c for c in chunks if c.strip()]


# ------------------------------------------------------------------
# Utility
# ------------------------------------------------------------------

def _coerce_list(value: Any) -> List[str]:
    """Coerce a comma-separated string or list into a list of strings."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []
