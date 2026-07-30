"""Grizzly's narrow Telegram-to-Control-Plane callback bridge.

Only opaque ``od:`` callbacks are consumed.  The callback contains no action,
decision, or authorization data: the loopback action service resolves it
against its server-owned, message-bound registry and returns the receipt that
is mirrored into the same bot-owned Telegram card.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

_CALLBACK_RE = re.compile(r"^od:[A-Za-z0-9_-]{16,60}$")
_CARD_REF_RE = re.compile(r"^TGC-[0-9a-f]{16}$")
_RECEIPT_RE = re.compile(r"^ACT-[0-9a-f]{12}$")
_ACTIONS = frozenset({"approve", "reject", "request_changes"})


def _default_dependencies() -> tuple[Callable[[], Any], Callable[[dict], str]]:
    from grover_runtime.action_service_client import (
        ActionServiceClient,
        render_shadow_card,
    )

    return ActionServiceClient, render_shadow_card


async def _answer(query: Any, text: str) -> None:
    try:
        await query.answer(text=text, show_alert=True)
    except Exception:
        logger.debug("Telegram typed-action alert failed", exc_info=True)


async def handle_typed_action_callback(
    query: Any,
    *,
    is_authorized: Callable[..., bool],
    client_factory: Callable[[], Any] | None = None,
    render_receipt: Callable[[dict], str] | None = None,
) -> bool:
    """Resolve one opaque typed callback; return whether it was consumed.

    ``act:`` and all other model-authored callback text deliberately return
    ``False``.  Once an ``od:`` prefix is observed, every malformed,
    unauthorized, unavailable, or invalid path is consumed and fails closed so
    it can never fall through to generic model dispatch.
    """

    data = str(getattr(query, "data", "") or "")
    if not data.startswith("od:"):
        return False

    message = getattr(query, "message", None)
    user = getattr(query, "from_user", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    thread_id = getattr(message, "message_thread_id", None)
    user_id = str(getattr(user, "id", "") or "")
    user_label = " ".join(
        str(getattr(user, "first_name", None) or "Principal").split()
    )[:60]

    if not _CALLBACK_RE.fullmatch(data):
        await _answer(query, "Could not record this decision. Nothing was executed.")
        return True

    try:
        authorized = is_authorized(
            user_id,
            chat_id=chat_id,
            chat_type=str(getattr(chat, "type", "") or "") or None,
            thread_id=str(thread_id) if thread_id is not None else None,
            user_name=getattr(user, "first_name", None),
        )
    except Exception:
        logger.exception("Telegram typed-action authorization check failed closed")
        await _answer(query, "Could not verify authorization. Nothing was executed.")
        return True
    if not authorized:
        await _answer(query, "You are not authorized to resolve this action.")
        return True

    decision_recorded = False
    try:
        if client_factory is None or render_receipt is None:
            default_factory, default_renderer = _default_dependencies()
            client_factory = client_factory or default_factory
            render_receipt = render_receipt or default_renderer
        if chat_id is None or message_id is None or not user_id:
            raise RuntimeError("typed callback is not bound to a Telegram message")

        client = client_factory()
        result = await asyncio.to_thread(
            client.resolve_callback,
            data,
            str(chat_id),
            str(message_id),
            user_id,
            user_label,
        )
        card_ref = str(result.get("card_ref") or "")
        receipt_id = str(result.get("receipt_id") or "")
        if (
            result.get("mode") != "shadow"
            or result.get("action") not in _ACTIONS
            or not _CARD_REF_RE.fullmatch(card_ref)
            or not _RECEIPT_RE.fullmatch(receipt_id)
        ):
            raise RuntimeError("typed action service returned an invalid receipt")
        decision_recorded = True

        pending_card = await asyncio.to_thread(
            client.pending_card,
            card_ref,
            chat_id=str(chat_id),
            thread_id=str(thread_id) if thread_id is not None else None,
            message_id=str(message_id),
            receipt_id=receipt_id,
            action=str(result["action"]),
        )
        if pending_card is None:
            raise RuntimeError("typed action receipt projection is pending")
        receipt_html = render_receipt(pending_card)
        await query.edit_message_text(
            text=receipt_html,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=None,
        )
        await asyncio.to_thread(client.mirrored, card_ref, receipt_id)
        await _answer(query, "Recorded in SHADOW. Nothing was executed.")
    except Exception:
        logger.exception("Telegram typed-action callback failed closed")
        if decision_recorded:
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                logger.debug(
                    "Committed typed-action buttons need cleanup", exc_info=True
                )
            await _answer(
                query,
                "Recorded in SHADOW; receipt sync pending. Nothing was executed.",
            )
        else:
            await _answer(
                query, "Could not record this decision. Nothing was executed."
            )
    return True
