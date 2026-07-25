"""Telegram pre-dispatch hook that routes professor approval replies to HEGI."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import re
from typing import Any

from .approval import process_pending_approvals
from .config import load_config
from .memory import DraftGate, MCPMemoryBackend, parse_approval_command
from .state import StateStore


_APPROVAL_EXECUTOR = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="hegi-approval-worker"
)


def _platform_name(event: Any) -> str:
    platform = getattr(getattr(event, "source", None), "platform", "")
    return str(getattr(platform, "value", platform)).lower()


def _schedule_reply(gateway: Any, event: Any, text: str) -> None:
    try:
        adapter = gateway._adapter_for_source(event.source)
        if adapter is None:
            return
        kwargs: dict[str, Any] = {}
        message_id = getattr(event, "message_id", None)
        if message_id:
            kwargs["reply_to"] = str(message_id)
        asyncio.get_running_loop().create_task(
            adapter.send(str(event.source.chat_id), text, **kwargs)
        )
    except Exception:
        return


def _meeting_id(state: StateStore, event: Any) -> str | None:
    reply_id = getattr(event, "reply_to_message_id", None)
    if reply_id:
        matched = state.meeting_for_report_message(str(reply_id))
        if matched:
            return matched
    text = str(getattr(event, "text", "") or "")
    explicit = re.search(
        r"\bmeeting[_\s-]*id\s*[:=]\s*([A-Za-z0-9._:-]+)",
        text,
        flags=re.IGNORECASE,
    )
    return explicit.group(1) if explicit else None


def _process_pending_background(config: Any) -> None:
    _APPROVAL_EXECUTOR.submit(process_pending_approvals, config)


def intercept_telegram_approval(
    *, event: Any, gateway: Any, session_store: Any = None, **_kwargs: Any
) -> dict[str, str] | None:
    del session_store
    if _platform_name(event) != "telegram":
        return None
    text = str(getattr(event, "text", "") or "")
    try:
        config = load_config()
    except Exception:
        return None
    memory = config.section("memory")
    command = parse_approval_command(text, memory.get("commands"))
    if command is None:
        return None
    source = getattr(event, "source", None)
    if (
        not config.enabled
        or source is None
        or str(getattr(source, "chat_id", "")) != config.chat_id
    ):
        return None
    state = StateStore(config.state_db)
    meeting_id = _meeting_id(state, event)
    if not meeting_id:
        _schedule_reply(gateway, event, "처리할 HEGI 회의록을 찾지 못했습니다.")
        return {"action": "skip", "reason": "hegi-no-meeting"}
    backend = MCPMemoryBackend(
        read_server=str(memory.get("read_server", "memory-forest-read")),
        search_tool=str(memory.get("search_tool", "")),
        draft_server=str(memory.get("draft_server", "")),
        draft_tool=str(memory.get("draft_tool", "")),
    )
    gate = DraftGate(
        state,
        backend,
        professor_user_ids=[
            str(item) for item in memory.get("professor_user_ids", [])
        ],
    )
    message_id = str(getattr(event, "message_id", "") or "")
    user_id = str(getattr(source, "user_id", "") or "")
    try:
        approved = gate.approve(
            meeting_id=meeting_id,
            text=text,
            user_id=user_id,
            platform_message_id=message_id or None,
            canonical_command=command,
        )
        if approved == "reject":
            state.mark_meeting_rejected(meeting_id)
            _schedule_reply(
                gateway,
                event,
                f"HEGI 기억 생성을 취소했습니다.\n회의: {meeting_id}",
            )
            return {"action": "skip", "reason": "hegi-rejected"}
        if not message_id:
            raise ValueError("Telegram message ID가 없어 승인을 영속화할 수 없습니다.")
        project = str(memory.get("default_project", "")).strip()
        if not project:
            raise ValueError("memory.default_project가 설정되지 않았습니다.")
        if not state.enqueue_approval_job(
            meeting_id=meeting_id,
            platform_message_id=message_id,
            project=project,
        ):
            raise ValueError("이미 처리 중이거나 완료된 승인 메시지입니다.")
    except Exception as exc:
        _schedule_reply(gateway, event, f"HEGI 승인 거부: {exc}")
        return {"action": "skip", "reason": "hegi-approval-denied"}

    _schedule_reply(
        gateway,
        event,
        "HEGI 교수 승인 이벤트를 접수했습니다.\n"
        f"회의: {meeting_id}\n"
        + (
            "Memory Forest 재검색, Draft 검증, approve/commit 및 "
            "validate/audit/index/backup을 순서대로 수행합니다."
            if approved in {"remember", "approve"}
            else "Memory Forest를 다시 검색한 뒤 pending STM Draft만 생성합니다."
        ),
    )
    _process_pending_background(config)
    return {"action": "skip", "reason": "hegi-approval-queued"}


def register(context: Any) -> None:
    # Run additive state migrations at plugin load, before the first live
    # approval event reaches the worker.
    StateStore(load_config().state_db)
    context.register_hook("pre_gateway_dispatch", intercept_telegram_approval)
