"""Minimal Zulip inbound platform adapter for Hermes.

Consumes Zulip ``message`` events from any channel/topic the configured bot
can read and delegates through the existing gateway MessageEvent path.

Workspace assumption (RFC P-XX, M0): the configured Zulip workspace contains
only Master and the Hermes bot, so every non-bot stream message is treated as
a direct instruction.  An explicit ``@Hermes`` mention is still tolerated and
is stripped before the body reaches the runtime; messages without a mention
are dispatched as-is.  The adapter does NOT mirror messages, create or move
topics, or persist credentials.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 30.0
_RECONNECT_DELAY_SECONDS = 2.0
_LONGPOLL_READ_TIMEOUT_BUFFER_SECONDS = 5.0
_DEFAULT_LONGPOLL_TIMEOUT_SECONDS = 90.0
# Keep Zulip aligned with the gateway's established safe outbound cap.  The
# delivery router delegates full content only to adapters that chunk natively.
_ZULIP_SAFE_MESSAGE_LENGTH = 4000
_ZULIP_EXECUTIVE_SUMMARY_THRESHOLD = 1200
_WIDE_TABLE_COLUMN_COUNT = 4
_WIDE_TABLE_CELL_LENGTH = 80


def _split_table_cells(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _is_table_separator(line: str) -> bool:
    cells = _split_table_cells(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _is_narrative_column(header: str, values: list[str]) -> bool:
    return (
        header.strip().lower() in {"analysis", "comment", "comments", "note", "notes", "insight", "insights", "分析", "备注", "说明", "洞察"}
        or any(len(value) > _WIDE_TABLE_CELL_LENGTH for value in values)
    )


def _render_wide_table(headers: list[str], rows: list[list[str]]) -> str:
    columns = list(zip(*rows)) if rows else []
    narrative_indexes = {
        index for index, header in enumerate(headers)
        if _is_narrative_column(header, list(columns[index]) if index < len(columns) else [])
    }
    metric_indexes = [index for index in range(len(headers)) if index not in narrative_indexes]
    metric_lines = ["### Key Metrics", "", "| Metric | Value |", "| --- | --- |"]
    for row_index, row in enumerate(rows, 1):
        for index in metric_indexes:
            metric = headers[index] if len(rows) == 1 else f"Row {row_index} · {headers[index]}"
            metric_lines.append(f"| {metric} | {row[index]} |")

    analysis_lines: list[str] = []
    for row_index, row in enumerate(rows, 1):
        for index in sorted(narrative_indexes):
            label = headers[index] if len(rows) == 1 else f"Row {row_index} · {headers[index]}"
            analysis_lines.append(f"- **{label}:** {row[index]}")
    if analysis_lines:
        return "\n".join(metric_lines + ["", "### Analysis", ""] + analysis_lines)
    return "\n".join(metric_lines)


def _format_wide_tables(content: str) -> str:
    lines = content.splitlines(keepends=False)
    output: list[str] = []
    index = 0
    changed = False
    while index < len(lines):
        if (
            index + 2 < len(lines)
            and "|" in lines[index]
            and _is_table_separator(lines[index + 1])
            and "|" in lines[index + 2]
        ):
            headers = _split_table_cells(lines[index])
            rows: list[list[str]] = []
            cursor = index + 2
            while cursor < len(lines) and "|" in lines[cursor] and lines[cursor].strip():
                row = _split_table_cells(lines[cursor])
                if len(row) != len(headers):
                    break
                rows.append(row)
                cursor += 1
            is_wide = (
                len(headers) > _WIDE_TABLE_COLUMN_COUNT
                or any(len(cell) > _WIDE_TABLE_CELL_LENGTH for row in rows for cell in row)
            )
            if is_wide:
                output.extend(_render_wide_table(headers, rows).splitlines())
                changed = True
            else:
                output.extend(lines[index:cursor])
            index = cursor
            continue
        output.append(lines[index])
        index += 1
    if not changed:
        return content
    rendered = "\n".join(output)
    return rendered + ("\n" if content.endswith("\n") else "")


def _has_executive_summary(content: str) -> bool:
    return bool(re.search(r"^#{1,3}\s*(executive summary|summary|tl;dr|执行摘要|摘要)\b", content, flags=re.IGNORECASE | re.MULTILINE))


def _build_executive_summary(content: str) -> str:
    matches = re.finditer(r"^#{1,3}\s+(Overall Status|Key Findings|Risks|Recommended Actions|状态|关键发现|风险|建议行动)\s*$\n(?:\s*[-*]\s+(.+?)\s*$)", content, flags=re.IGNORECASE | re.MULTILINE)
    items = [(match.group(1), match.group(2)) for match in matches]
    if not items:
        return ""
    return "\n".join(["## Executive Summary"] + [f"- **{heading}:** {detail}" for heading, detail in items[:4]])


def _format_health_report(content: str) -> str:
    pattern = re.compile(r"^\|\s*Component\s*\|\s*Status\s*\|\s*Detail\s*\|\s*\n\|[-| :]+\|\s*\n((?:\|.*\|\s*\n?)+)", re.IGNORECASE | re.MULTILINE)
    def replace(match: re.Match[str]) -> str:
        lines = [line for line in match.group(1).splitlines() if line.strip()]
        rendered = ["### Health Status", ""]
        for line in lines:
            cells = _split_table_cells(line)
            if len(cells) != 3:
                return match.group(0)
            component, status, detail = cells
            icon = "✅" if status.lower() in {"healthy", "ok", "pass", "passed"} else "⚠️" if status.lower() in {"warning", "warn", "degraded"} else "❌" if status.lower() in {"failed", "fail", "error", "down"} else "⚠️"
            rendered.append(f"- {icon} **{component}** — {status}" + (f" · {detail}" if detail else ""))
        return "\n".join(rendered) + "\n"
    return pattern.sub(replace, content)


def _polish_zulip_presentation(content: str) -> str:
    content = re.sub(r"^#{1,3}\s+[^\n]+\n(?:[ \t]*\n)+(?=#{1,3}\s)", "", content, flags=re.MULTILINE)
    content = re.sub(r"^(\s*)[*•]\s+", r"\1- ", content, flags=re.MULTILINE)
    def number(match: re.Match[str]) -> str:
        raw = match.group(1)
        if raw.endswith("%"):
            return f"{float(raw[:-1]):.2f}%"
        value = float(raw)
        return f"{value:,.0f}" if value.is_integer() else f"{value:.2f}"
    content = re.sub(r"(?<=:\s)(\d+(?:\.\d+)?%?)(?=\s*$)", number, content, flags=re.MULTILINE)
    return re.sub(r"(?<!\A)\n{2,}(?=##\s)", "\n\n---\n\n", content)


def _format_zulip_presentation(content: str) -> str:
    """Apply Zulip readability formatting while preserving fenced code."""
    parts = re.split(r"(```[\s\S]*?```)", content)
    rendered = "".join(
        part if part.startswith("```") else _polish_zulip_presentation(_format_health_report(_format_wide_tables(part)))
        for part in parts
    )
    if len(rendered) < _ZULIP_EXECUTIVE_SUMMARY_THRESHOLD or _has_executive_summary(rendered):
        return rendered
    summary = _build_executive_summary(rendered)
    return f"{summary}\n\n{rendered}" if summary else rendered


def _split_zulip_body(content: str, limit: int) -> list[str]:
    """Split text on paragraph/newline/sentence boundaries, then safely by char."""
    chunks: list[str] = []
    remaining = content
    while len(remaining) > limit:
        candidate = remaining[:limit]
        paragraph_split = candidate.rfind("\n\n")
        if paragraph_split > 0:
            paragraph_split += 2
        if paragraph_split >= limit // 2:
            split_at = paragraph_split
        else:
            split_at = candidate.rfind("\n")
            if split_at > 0:
                split_at += 1
            else:
                sentence_ends = [candidate.rfind(mark) for mark in "。！？.!?"]
                split_at = max(sentence_ends) + 1
                if split_at <= 0:
                    split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    chunks.append(remaining)
    return chunks


def _split_zulip_message(content: str, limit: int = _ZULIP_SAFE_MESSAGE_LENGTH) -> list[str]:
    """Return ordered Zulip-safe messages, preserving content exactly.

    Short content is returned unchanged.  Long content gains a compact fullwidth
    sequence label; capacity is recomputed until labels and bodies fit together.
    """
    if len(content) <= limit:
        return [content]

    body_limit = limit - len("（1/1）\n")
    chunks: list[str] = []
    for _ in range(4):
        chunks = _split_zulip_body(content, body_limit)
        prefix_len = len(f"（{len(chunks)}/{len(chunks)}）\n")
        next_body_limit = limit - prefix_len
        if next_body_limit == body_limit:
            break
        body_limit = next_body_limit
    return [f"（{index}/{len(chunks)}）\n{chunk}" for index, chunk in enumerate(chunks, 1)]


def _credentials() -> tuple[str, str, str]:
    """Read the established Zulip credentials without persisting them."""
    return (
        os.getenv("ZULIP_URL", "").rstrip("/"),
        os.getenv("ZULIP_BOT_EMAIL", ""),
        os.getenv("ZULIP_API_KEY", ""),
    )


def check_requirements() -> bool:
    """The adapter requires exactly the existing Zulip credential set."""
    return all(_credentials())


def validate_config(_config: Any) -> bool:
    return check_requirements()


def _env_enablement() -> Optional[dict[str, Any]]:
    """Enable the bundled plugin only when all existing credentials are present."""
    if not check_requirements():
        return None
    return {"enabled": True}


def extract_mention_text(content: str, bot_full_name: str) -> Optional[str]:
    """Return text after a strict Zulip mention of the configured bot.

    Zulip stores a normal mention as Markdown (for example ``@**Hermes**``).
    Accept the plain form as well for API-version tolerance, but never treat a
    bare occurrence of the name as a mention.
    """
    name = (bot_full_name or "").strip()
    if not name:
        return None
    mention = rf"@(?:\*\*)?{re.escape(name)}(?:\*\*)?(?=$|[\s,，:：])"
    matched = re.search(mention, content or "", flags=re.IGNORECASE)
    if matched is None:
        return None
    text = (content[matched.end():] if content else "").strip()
    return text or None


def extract_direct_text(content: str, bot_full_name: str) -> str:
    """Return the dispatchable body for a direct-conversation Zulip message.

    Always returns a string (never ``None``) so the gateway always receives a
    payload.  Behaviour:

    * If the message begins with a strict Zulip mention of the bot, the
      mention is stripped and the remainder is returned (empty -> empty).
    * Otherwise the original content is returned untouched.
    """
    raw = content or ""
    stripped = extract_mention_text(raw, bot_full_name)
    if stripped is None:
        return raw.strip()
    return stripped


class _ZulipAPI:
    """Small async REST client which deliberately never logs credentials."""

    def __init__(self, base_url: str, email: str, api_key: str) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            auth=(email, api_key),
            follow_redirects=False,
            headers={"User-Agent": "Hermes-Agent-Zulip/1.0"},
        )

    async def get(
        self,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        timeout: float | httpx.Timeout = _REQUEST_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        response = await self._http.get(path, params=params, timeout=timeout)
        return self._payload(response)

    async def post(self, path: str, *, data: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        response = await self._http.post(path, data=data, timeout=_REQUEST_TIMEOUT_SECONDS)
        return self._payload(response)

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _payload(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Zulip returned an invalid JSON response")
        return payload


class ZulipAdapter(BasePlatformAdapter):
    """Long-poll Zulip ``message`` events and reply in the originating topic."""

    MAX_MESSAGE_LENGTH: int = _ZULIP_SAFE_MESSAGE_LENGTH
    splits_long_messages = True

    def __init__(self, config: Any, **_kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("zulip"))
        # Bind existing session machinery to the originating Zulip topic.
        config.extra.setdefault("thread_sessions_per_user", True)
        self._client: Optional[_ZulipAPI] = None
        self._listener_task: Optional[asyncio.Task[None]] = None
        self._queue_id: Optional[str] = None
        self._last_event_id: Optional[int] = None
        self._bot_user_id: Optional[int] = None
        self._bot_full_name = ""
        self._longpoll_timeout_seconds: Optional[float] = None

    @property
    def name(self) -> str:
        return "Zulip"

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        base_url, email, api_key = _credentials()
        if not all((base_url, email, api_key)):
            self._set_fatal_error("config_missing", "ZULIP_URL, ZULIP_BOT_EMAIL, and ZULIP_API_KEY are required", retryable=False)
            return False
        try:
            self._client = _ZulipAPI(base_url, email, api_key)
            profile = await self._client.get("/api/v1/users/me")
            self._bot_user_id = int(profile["user_id"])
            self._bot_full_name = str(profile["full_name"]).strip()
            if not self._bot_full_name:
                raise RuntimeError("Zulip bot profile did not include a full name")
            await self._register_queue()
        except (KeyError, TypeError, ValueError, httpx.HTTPError, RuntimeError) as exc:
            await self._close_client()
            self._set_fatal_error("connect_failed", str(exc), retryable=True)
            return False
        self._listener_task = asyncio.create_task(self._listen(), name="zulip-event-listener")
        self._mark_connected()
        logger.info("Zulip: event listener connected")
        return True

    async def disconnect(self) -> None:
        self._mark_disconnected()
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        self._listener_task = None
        await self._close_client()
        self._queue_id = None
        self._last_event_id = None

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        topic = str((metadata or {}).get("thread_id") or "").strip()
        if self._client is None:
            return SendResult(success=False, error="Zulip adapter is not connected")
        if not topic:
            return SendResult(success=False, error="Zulip reply requires the originating topic")
        segments = _split_zulip_message(_format_zulip_presentation(content), self.MAX_MESSAGE_LENGTH)
        message_ids: list[str] = []
        for index, segment in enumerate(segments, 1):
            try:
                payload = await self._client.post(
                    "/api/v1/messages",
                    data={"type": "stream", "to": chat_id, "topic": topic, "content": segment},
                )
                if payload.get("result") == "error":
                    raise RuntimeError(str(payload.get("msg", "Zulip send failed")))
                message_ids.append(str(payload["id"]))
            except (KeyError, httpx.HTTPError, RuntimeError) as exc:
                logger.error(
                    "Zulip send failed at segment %d/%d (stream=%s, topic=%s): %s",
                    index,
                    len(segments),
                    chat_id,
                    topic,
                    exc,
                )
                return SendResult(
                    success=False,
                    error=f"Zulip segment {index}/{len(segments)} failed: {exc}",
                    raw_response={"sent_message_ids": message_ids},
                )
        return SendResult(
            success=True,
            message_id=message_ids[-1],
            raw_response={"message_ids": message_ids},
        )

    async def send_typing(self, chat_id: str, metadata: Optional[dict[str, Any]] = None) -> None:
        # Zulip's REST API has no typing indicator for channel messages.
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "channel", "chat_id": chat_id}

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.close()
            self._client = None

    async def _register_queue(self) -> None:
        if self._client is None:
            raise RuntimeError("Zulip client is unavailable")
        payload = await self._client.post(
            "/api/v1/register",
            data={"event_types": json.dumps(["message"]), "fetch_event_types": json.dumps([])},
        )
        if payload.get("result") == "error":
            raise RuntimeError(str(payload.get("msg", "Zulip queue registration failed")))
        self._queue_id = str(payload["queue_id"])
        self._last_event_id = int(payload["last_event_id"])
        longpoll_timeout = payload.get("event_queue_longpoll_timeout_seconds")
        self._longpoll_timeout_seconds = float(longpoll_timeout) if longpoll_timeout is not None else None

    async def _listen(self) -> None:
        while self.is_connected:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                logger.warning(
                    "Zulip: event listener retrying after %s: %r",
                    type(exc).__name__,
                    exc,
                )
                await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
            except Exception:
                logger.exception("Zulip: unexpected event listener error")
                await asyncio.sleep(_RECONNECT_DELAY_SECONDS)

    async def _poll_once(self) -> None:
        if self._client is None or self._queue_id is None or self._last_event_id is None:
            raise RuntimeError("Zulip event queue is not initialized")
        longpoll_timeout = self._longpoll_timeout_seconds
        if longpoll_timeout is None:
            longpoll_timeout = _DEFAULT_LONGPOLL_TIMEOUT_SECONDS
        timeout = httpx.Timeout(
            longpoll_timeout + _LONGPOLL_READ_TIMEOUT_BUFFER_SECONDS,
            connect=_REQUEST_TIMEOUT_SECONDS,
            write=_REQUEST_TIMEOUT_SECONDS,
            pool=_REQUEST_TIMEOUT_SECONDS,
        )
        payload = await self._client.get(
            "/api/v1/events",
            params={"queue_id": self._queue_id, "last_event_id": self._last_event_id},
            timeout=timeout,
        )
        if payload.get("result") == "error":
            if payload.get("code") == "BAD_EVENT_QUEUE_ID":
                await self._register_queue()
                return
            raise RuntimeError(str(payload.get("msg", "Zulip event poll failed")))
        for event in payload.get("events", []):
            if not isinstance(event, dict):
                continue
            if isinstance(event.get("id"), int):
                self._last_event_id = event["id"]
            await self._handle_event(event)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        # Only plain ``message`` events are dispatched.  Everything else
        # (reactions, presence, typing indicators, edits, deletes, ...) is
        # ignored so the gateway never wakes up for non-message signals.
        if event.get("type") != "message":
            return
        message = event.get("message")
        if not isinstance(message, dict) or message.get("type") != "stream":
            return
        # Self-reply guard: the bot's own outbound messages echo back through
        # the long-poll queue as ordinary ``message`` events.  Use the
        # authoritative Zulip sender_id rather than scraping message text.
        if self._bot_user_id is not None and message.get("sender_id") == self._bot_user_id:
            return
        text = extract_direct_text(str(message.get("content") or ""), self._bot_full_name)
        if not text:
            # Allow empty-content messages (e.g. attachment-only posts) to pass
            # through only when they carry attachments; pure empty messages
            # produce no useful runtime input and are dropped.
            if not message.get("attachments"):
                return
        stream = str(message.get("display_recipient") or "").strip()
        topic = str(message.get("subject") or message.get("topic") or "").strip()
        message_id = message.get("id")
        sender_id = message.get("sender_id")
        if not stream or not topic or not isinstance(message_id, int) or sender_id is None:
            return
        source = self.build_source(
            chat_id=stream,
            chat_name=stream,
            chat_type="channel",
            user_id=str(sender_id),
            user_name=str(message.get("sender_full_name") or message.get("sender_email") or sender_id),
            thread_id=topic,
            chat_topic=topic,
            message_id=str(message_id),
        )
        timestamp = message.get("timestamp")
        event_time = datetime.fromtimestamp(timestamp, tz=timezone.utc) if isinstance(timestamp, (int, float)) else datetime.now(timezone.utc)
        await self.handle_message(
            MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                raw_message=message,
                message_id=str(message_id),
                timestamp=event_time,
            )
        )


def register(ctx: Any) -> None:
    """Register the Zulip adapter as a bundled platform plugin."""
    ctx.register_platform(
        name="zulip",
        label="Zulip",
        adapter_factory=lambda cfg: ZulipAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["ZULIP_URL", "ZULIP_BOT_EMAIL", "ZULIP_API_KEY"],
        env_enablement_fn=_env_enablement,
        allowed_users_env="ZULIP_ALLOWED_USERS",
        allow_all_env="ZULIP_ALLOW_ALL_USERS",
        max_message_length=_ZULIP_SAFE_MESSAGE_LENGTH,
        emoji="🟩",
        platform_hint=(
            "You are replying in a Zulip channel topic.\n"
            "Zulip is a long-term knowledge workspace, not a real-time chat.\n\n"
            "Prefer outcome-oriented responses.\n\n"
            "Rules:\n"
            "1. Start with conclusion, status, or answer. Do not narrate process.\n"
            "2. For analysis tasks: put TL;DR or verdict first, evidence and details after.\n"
            "3. For debugging: ROOT CAUSE -> EVIDENCE -> FIX -> VERIFICATION.\n"
            "4. Hide internal execution details:\n"
            "   token counts, model names, tool calls, retries, fallback chains, policy details.\n"
            "5. Avoid step-by-step narration unless explicitly requested.\n"
            "6. Use 'Master' only when directly addressing the user, not in reports.\n"
            "7. Long responses should use Markdown headings and a short summary first.\n"
            "8. Stay within the current Zulip topic."
        ),
    )
