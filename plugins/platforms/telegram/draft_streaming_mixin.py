"""Telegram c5 mixin for ``TelegramAdapter``.

Extracted verbatim from ``plugins/platforms/telegram/adapter.py`` as part
of the god-file decomposition campaign. Draft streaming cluster: native ``sendMessageDraft`` streaming,
capability probing, and the thread-fallback control-send helper.

Behavior-neutral: every method is lifted character-for-character from
``TelegramAdapter``. ``self.*`` calls resolve unchanged via the MRO, and
``DraftStreamingMixin`` precedes ``BasePlatformAdapter`` in the bases so
resolution order is what it was when these methods sat on the class.

Three details keep the lift observationally identical:

* ``logger`` is bound by explicit name rather than ``__name__``, so records
  emitted from these methods keep the logger name
  ``"plugins.platforms.telegram.adapter"``. ``getLogger`` returns the same
  singleton object the adapter module holds.
* ``InlineKeyboardButton``/``InlineKeyboardMarkup``/``ParseMode`` are
  imported under the same ``ImportError`` guard the adapter uses, falling
  back to ``Any``/``None``. This module deliberately does not enable
  postponed annotation evaluation, matching the adapter, so the
  annotations on the lifted signatures are evaluated exactly as before.
* Shared adapter helpers are re-imported from their home module, exactly
  as the adapter imports them.
"""

import logging

from typing import Any, Dict, Optional

from gateway.platforms.base import SendResult, utf16_len

from plugins.platforms.telegram.telegram_ids import normalize_telegram_chat_id

from plugins.platforms.telegram.adapter import _redact_telegram_error_text

try:
    from telegram.constants import ParseMode
except ImportError:  # pragma: no cover - mirrors the adapter's import guard
    ParseMode = None

# Bind the adapter's logger by name so log records lifted with these methods
# are emitted under exactly the name they were before.
logger = logging.getLogger("plugins.platforms.telegram.adapter")


class DraftStreamingMixin:
    """c5 cluster lifted verbatim from ``TelegramAdapter``."""

    def supports_draft_streaming(
        self,
        chat_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Telegram supports sendMessageDraft for private chats only.

        Bot API 9.5 (March 2026) opened ``sendMessageDraft`` to all bots
        unconditionally for private (DM) chats.  Groups, supergroups, and
        channels still rely on the edit-based path.

        We additionally require ``self._bot`` to expose ``send_message_draft``
        (added to python-telegram-bot in 22.6); older PTB installs gracefully
        fall back to the edit path even on DMs.
        """
        if not self._bot or not hasattr(self._bot, "send_message_draft"):
            return False
        return (chat_type or "").lower() in {"dm", "private"}

    async def send_draft(
        self,
        chat_id: str,
        draft_id: int,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Stream a partial message via Telegram's native draft API.

        Uses ``sendRichMessageDraft`` (Bot API 10.1) with the raw markdown when
        rich messages are enabled and supported, otherwise the plain-text
        ``sendMessageDraft``. The Bot API animates the preview when the same
        ``draft_id`` is reused across consecutive calls in the same chat.  When
        the response finishes, the caller sends the final text via the normal
        ``send`` path; the draft preview clears naturally on the client
        (Telegram has no Bot API to "promote" a draft to a real message — the
        final ``sendMessage``/``sendRichMessage`` is what the user receives in
        their history).
        """
        if not self._bot:
            return SendResult(success=False, error="not_connected")

        # Rich draft fast-path (Bot API 10.1 sendRichMessageDraft): render the
        # streaming preview with the same raw markdown the final
        # sendRichMessage will persist, so the animated draft matches the final
        # message. Any failure degrades to the legacy plain-text draft below.
        if self._should_attempt_rich_draft(content):
            if await self._try_send_rich_draft(chat_id, draft_id, content, metadata):
                # Drafts have no message_id; report success without one.
                return SendResult(success=True, message_id=None)

        if not hasattr(self._bot, "send_message_draft"):
            return SendResult(success=False, error="api_unavailable")

        # Trim to the same UTF-16 budget the platform enforces on regular
        # sends.  Drafts have the same length contract as messages.
        text = content if len(content) <= self.MAX_MESSAGE_LENGTH else \
            self.truncate_message(content, self.MAX_MESSAGE_LENGTH, len_fn=utf16_len)[0]

        thread_id = self._metadata_thread_id(metadata)

        # Apply the same MarkdownV2 conversion the regular ``send`` path uses
        # so the animated draft preview renders with identical formatting to
        # the final message.  Without this, the draft streams as raw text and
        # the final ``sendMessage`` (which DOES use MarkdownV2) snaps into
        # formatted output, producing a jarring visual shift at the end of the
        # response.  We try MarkdownV2 first and fall back to plain text if a
        # malformed escape would be rejected — mirroring the (True, False)
        # retry the streaming send loop uses — so a single bad token never
        # kills draft streaming for the whole response.
        for use_markdown in (True, False):
            kwargs: Dict[str, Any] = {
                "chat_id": normalize_telegram_chat_id(chat_id),
                "draft_id": int(draft_id),
                "text": self.format_message(text) if use_markdown else text,
            }
            if use_markdown:
                kwargs["parse_mode"] = ParseMode.MARKDOWN_V2
            if thread_id is not None:
                kwargs["message_thread_id"] = thread_id

            try:
                ok = await self._bot.send_message_draft(**kwargs)
                if ok:
                    # Drafts have no message_id; we report success without one
                    # so the caller knows the animation frame landed.
                    return SendResult(success=True, message_id=None)
                return SendResult(success=False, error="draft_rejected")
            except Exception as e:
                # A MarkdownV2 parse failure (BadRequest "can't parse entities")
                # is recoverable: retry once as plain text.  Any other failure
                # (chat doesn't allow drafts, transient hiccup) — or a failure
                # on the plain-text attempt — propagates to the caller, which
                # treats it as "fall back to edit-based for this response".
                if use_markdown and self._is_bad_request_error(e):
                    logger.debug(
                        "[%s] sendMessageDraft MarkdownV2 rejected, retrying "
                        "as plain text (chat=%s draft_id=%s): %s",
                        self.name, chat_id, draft_id, _redact_telegram_error_text(e),
                    )
                    continue
                logger.debug(
                    "[%s] sendMessageDraft failed (chat=%s draft_id=%s): %s",
                    self.name, chat_id, draft_id, e,
                )
                return SendResult(success=False, error=_redact_telegram_error_text(e))

        return SendResult(success=False, error="draft_rejected")

    async def _send_message_with_thread_fallback(self, **kwargs):
        """Send a Telegram message, retrying once without message_thread_id
        if Telegram returns 'Message thread not found'.

        Used for control-style sends (approval prompts, model picker,
        update prompts) that can carry a stale thread_id from a DM
        reply chain.  The streaming send loop has its own equivalent
        (PR #3390) at the body of ``send``; this helper applies the
        same retry pattern to the non-streaming control paths.
        """
        if not self._bot:
            raise RuntimeError("Not connected")

        message_thread_id = kwargs.get("message_thread_id")
        try:
            return await self._bot.send_message(**kwargs)
        except Exception as send_err:
            if (
                message_thread_id is not None
                and self._is_bad_request_error(send_err)
                and self._is_thread_not_found_error(send_err)
            ):
                logger.warning(
                    "[%s] Thread %s not found for control message, retrying without message_thread_id",
                    self.name,
                    message_thread_id,
                )
                # Same prune as the streaming send path — the
                # control-message retry tells us the topic is gone,
                # so the binding row in state.db must go too
                # (#31501).
                self._prune_stale_dm_topic_binding(
                    kwargs.get("chat_id"), message_thread_id,
                )
                retry_kwargs = dict(kwargs)
                retry_kwargs.pop("message_thread_id", None)
                return await self._bot.send_message(**retry_kwargs)
            raise
