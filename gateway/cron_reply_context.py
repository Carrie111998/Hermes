"""Short-lived context for replies to cron deliveries."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_STORE_PATH = get_hermes_home() / "gateway" / "cron_reply_contexts.json"
_MAX_CONTENT_CHARS = 4000
_DEFAULT_MAX_AGE_SECONDS = 6 * 60 * 60
_MAX_RECORDS = 500


def _normalize_chat_id(platform: str, chat_id: str) -> str:
    chat = str(chat_id)
    if platform.lower() == "teams" and ";messageid=" in chat:
        return chat.split(";messageid=", 1)[0]
    return chat


def _record_key(platform: str, chat_id: str, thread_id: Optional[str]) -> str:
    platform_name = platform.lower()
    chat = _normalize_chat_id(platform_name, chat_id)
    return f"{platform_name}::{chat}::{thread_id or ''}"


def _load_records() -> dict[str, dict[str, Any]]:
    try:
        if not _STORE_PATH.exists():
            return {}
        raw = json.loads(_STORE_PATH.read_text(encoding="utf-8"))
        records = raw.get("contexts", raw) if isinstance(raw, dict) else {}
        if not isinstance(records, dict):
            return {}
        return {
            str(key): value
            for key, value in records.items()
            if isinstance(value, dict)
        }
    except Exception:
        logger.debug("Failed to load cron reply context store", exc_info=True)
        return {}


def _write_records(records: dict[str, dict[str, Any]]) -> None:
    payload = {
        "version": 1,
        "contexts": records,
    }
    _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _STORE_PATH.with_suffix(_STORE_PATH.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(_STORE_PATH)


def _prune_records(
    records: dict[str, dict[str, Any]],
    *,
    now: float,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
    max_records: Optional[int] = None,
) -> dict[str, dict[str, Any]]:
    """Drop malformed/expired entries and retain only the newest bounded set."""
    if max_records is None:
        max_records = _MAX_RECORDS
    fresh: list[tuple[str, dict[str, Any], float]] = []
    for key, record in records.items():
        try:
            updated_at = float(record.get("updated_at", 0))
        except (TypeError, ValueError):
            continue
        if now - updated_at <= max_age_seconds:
            fresh.append((key, record, updated_at))

    fresh.sort(key=lambda item: item[2], reverse=True)
    return {
        key: record
        for key, record, _updated_at in fresh[:max_records]
    }


def record_cron_reply_context(
    platform: str,
    chat_id: str,
    content: str,
    *,
    thread_id: Optional[str] = None,
    message_id: Optional[str] = None,
    job_id: Optional[str] = None,
) -> None:
    """Persist cron delivery content for a future reply in the same chat/thread."""
    text = str(content or "").strip()
    thread = str(thread_id).strip() if thread_id else ""
    if not text or not thread:
        return
    platform_name = str(platform).lower()
    chat = _normalize_chat_id(platform_name, str(chat_id))
    now = time.time()
    record = {
        "platform": platform_name,
        "chat_id": chat,
        "thread_id": thread,
        "message_id": str(message_id) if message_id else None,
        "job_id": str(job_id) if job_id else None,
        "content": text[:_MAX_CONTENT_CHARS],
        "updated_at": now,
    }
    records = _prune_records(_load_records(), now=now)
    records[_record_key(platform_name, chat, thread)] = record
    _write_records(_prune_records(records, now=now))


def find_cron_reply_context(
    platform: str,
    chat_id: str,
    *,
    thread_id: Optional[str] = None,
    max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS,
) -> Optional[dict[str, Any]]:
    """Return recent cron context for an explicit incoming reply, if any."""
    thread = str(thread_id).strip() if thread_id else ""
    if not thread:
        return None

    platform_name = str(platform).lower()
    chat = _normalize_chat_id(platform_name, str(chat_id))
    records = _load_records()
    now = time.time()

    def _fresh(record: dict[str, Any]) -> bool:
        try:
            return now - float(record.get("updated_at", 0)) <= max_age_seconds
        except (TypeError, ValueError):
            return False

    exact = records.get(_record_key(platform_name, chat, thread))
    if exact and _fresh(exact):
        return exact
    return None
