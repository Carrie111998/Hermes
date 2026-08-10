"""Bundled Telegram finance-recording plugin for Hermes Agent."""
from __future__ import annotations

import asyncio
import os
from typing import Any

from .parser import parse_finance_message
from .service import FinanceProcessor
from .sheets import FinanceSheetAdapter, build_adapter_from_env

_PROCESSOR: FinanceProcessor | None = None
_TEST_ADAPTER: FinanceSheetAdapter | None = None


def set_test_adapter(adapter: FinanceSheetAdapter | None) -> None:
    """Inject an adapter for tests; pass None to restore env-based construction."""
    global _TEST_ADAPTER, _PROCESSOR
    _TEST_ADAPTER = adapter
    _PROCESSOR = None


def get_processor() -> FinanceProcessor:
    global _PROCESSOR
    if _PROCESSOR is None:
        adapter = _TEST_ADAPTER if _TEST_ADAPTER is not None else build_adapter_from_env()
        _PROCESSOR = FinanceProcessor(adapter, timezone=os.getenv("FINANCE_TIMEZONE", "Asia/Tokyo") or "Asia/Tokyo")
    return _PROCESSOR


def _allowed_chat_ids() -> set[str]:
    raw = os.getenv("FINANCE_ALLOWED_TELEGRAM_CHAT_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def _is_telegram_event(event: Any) -> bool:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    value = getattr(platform, "value", platform)
    return str(value).lower() == "telegram"


def _chat_id(event: Any) -> str | None:
    source = getattr(event, "source", None)
    chat_id = getattr(source, "chat_id", None)
    return str(chat_id) if chat_id is not None else None


def _message_id(event: Any) -> str:
    mid = getattr(event, "message_id", None)
    if mid is None:
        metadata = getattr(event, "metadata", {}) or {}
        mid = metadata.get("message_id") or metadata.get("telegram_message_id")
    return str(mid or "")


def _send_reply(gateway: Any, event: Any, reply: str) -> None:
    if not reply:
        return
    adapter = None
    getter = getattr(gateway, "_adapter_for_source", None)
    if callable(getter):
        adapter = getter(getattr(event, "source", None))
    if adapter is None:
        source = getattr(event, "source", None)
        platform = getattr(source, "platform", None)
        adapter = getattr(gateway, "adapters", {}).get(platform)
    send = getattr(adapter, "send", None) if adapter is not None else None
    if not callable(send):
        return
    result = send(_chat_id(event), reply)
    if asyncio.iscoroutine(result):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(result)
        except RuntimeError:
            asyncio.run(result)


def on_pre_gateway_dispatch(event: Any, gateway: Any = None, session_store: Any = None, **_: Any) -> dict[str, str] | None:
    """Intercept allowed Telegram finance messages before normal agent dispatch."""
    del session_store
    if not _is_telegram_event(event):
        return {"action": "allow"}
    chat_id = _chat_id(event)
    allowed = _allowed_chat_ids()
    if not chat_id or not allowed or chat_id not in allowed:
        return {"action": "allow"}
    text = getattr(event, "text", "") or ""
    # Parse first so unrelated messages continue to Hermes normally.
    if parse_finance_message(text, now=getattr(event, "timestamp", None), timezone=os.getenv("FINANCE_TIMEZONE", "Asia/Tokyo") or "Asia/Tokyo") is None:
        return {"action": "allow"}
    try:
        processor = get_processor()
    except Exception as exc:
        if gateway is not None:
            _send_reply(gateway, event, f"財務記録の初期化に失敗しました。成功として扱いません。エラー: {exc}")
        return {"action": "skip", "reason": "finance_records_init_failed"}
    result = processor.process(
        text=text,
        telegram_chat_id=chat_id,
        telegram_message_id=_message_id(event),
        received_at=getattr(event, "timestamp", None),
    )
    if result.handled:
        if gateway is not None and result.reply:
            _send_reply(gateway, event, result.reply)
        return {"action": "skip", "reason": "finance_records_handled"}
    return {"action": "allow"}


def register(ctx) -> None:
    ctx.register_hook("pre_gateway_dispatch", on_pre_gateway_dispatch)
