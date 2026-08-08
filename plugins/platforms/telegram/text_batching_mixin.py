"""Text-message aggregation methods for ``TelegramAdapter``.

Extracted from ``plugins/platforms/telegram/adapter.py`` as part of the
god-file decomposition campaign, following the same mechanical mixin lift
that produced ``gateway/authz_mixin.py`` and the Telegram authorization
mixin (PR #75742). This mixin holds the c8 cluster: buffering rapid successive text updates from the same session and dispatching them as one aggregated event after a quiet period, with adaptive delay tiers for Telegram client-side splits.

Behavior-neutral: every method is lifted verbatim from ``TelegramAdapter``.
Class attributes (``_SPLIT_THRESHOLD``, ``_TEXT_BATCH_FAST_LEN``, ``_TEXT_BATCH_FAST_DELAY_S``, ``_TEXT_BATCH_SHORT_LEN``, ``_TEXT_BATCH_SHORT_DELAY_S``) stay on ``TelegramAdapter`` and
resolve via ``self.*`` / ``cls.*`` through the MRO, exactly as before the
lift, and ``TextBatchingMixin`` precedes ``BasePlatformAdapter`` in the bases.

``logger`` is bound by explicit name so records emitted from these methods
keep the logger name ``"plugins.platforms.telegram.adapter"``. ``Message``
is imported under the same ``ImportError`` guard the adapter uses, falling
back to ``Any``; like the adapter, this module does not enable postponed
annotation evaluation.
"""

import asyncio
import logging
from typing import Any

from gateway.platforms.base import MessageEvent

logger = logging.getLogger("plugins.platforms.telegram.adapter")

class TextBatchingMixin:
    """Text-message aggregation cluster lifted verbatim from ``TelegramAdapter``."""

    # ------------------------------------------------------------------
    # Text message aggregation (handles Telegram client-side splits)
    # ------------------------------------------------------------------

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching.

        Applies the installed topic-recovery hook first so DM-topic batches
        coalesce on (and dispatch to) the recovered lane rather than the
        raw inbound ``message_thread_id`` Telegram may have attached.
        """
        from gateway.session import build_session_key
        self._apply_topic_recovery(event)
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            profile=event.source.profile,
        )

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer a text event and reset the flush timer.

        When Telegram splits a long user message into multiple updates,
        they arrive within a few hundred milliseconds.  This method
        concatenates them and waits for a short quiet period before
        dispatching the combined message.
        """
        if self._should_drop_delayed_delivery():
            logger.debug("[Telegram] Dropping text batch enqueue after disconnect started")
            return

        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            # Append text from the follow-up chunk
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            # Merge any media that might be attached
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)

        # Cancel any pending flush and restart the timer
        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(
            self._flush_text_batch(key)
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text.

        Uses a longer delay when the latest chunk is near Telegram's 4096-char
        split point, since a continuation chunk is almost certain.
        """
        current_task = asyncio.current_task()
        try:
            # Adaptive delay tiers:
            #  - last chunk ≥ _SPLIT_THRESHOLD: a continuation is almost
            #    certain → wait the longer split delay.
            #  - total accumulated text ≤ _TEXT_BATCH_FAST_LEN (~320 cp):
            #    short message → cap delay at _TEXT_BATCH_FAST_DELAY_S
            #    so the agent sees the text near-instantly.
            #  - total ≤ _TEXT_BATCH_SHORT_LEN (~1024 cp):
            #    medium → cap at _TEXT_BATCH_SHORT_DELAY_S.
            #  - otherwise: use the configured cap.
            # Tiers compose with operator overrides via the env-var-driven
            # ``_text_batch_delay_seconds`` (e.g. an operator who sets the
            # cap below 0.18s gets that lower number on every tier).
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            total_len = len(getattr(pending, "text", "") or "") if pending else 0
            if last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds
            elif total_len <= self._TEXT_BATCH_FAST_LEN:
                delay = min(self._text_batch_delay_seconds, self._TEXT_BATCH_FAST_DELAY_S)
            elif total_len <= self._TEXT_BATCH_SHORT_LEN:
                delay = min(self._text_batch_delay_seconds, self._TEXT_BATCH_SHORT_DELAY_S)
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            if self._should_drop_delayed_delivery():
                logger.debug("[Telegram] Dropping text batch flush after disconnect started")
                return
            logger.info(
                "[Telegram] Flushing text batch %s (%d chars)",
                key, len(event.text or ""),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)
