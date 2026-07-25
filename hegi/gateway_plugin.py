"""Telegram pre-dispatch hook that routes professor approval replies to HEGI."""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from typing import Any

from .approval import process_pending_approvals
from .config import load_config
from .memory import DraftGate, MCPMemoryBackend, parse_approval_command
from .state import StateStore


_PIPELINE_LOCK = threading.Lock()
_PIPELINE_THREAD: threading.Thread | None = None
_PIPELINE_WAKE = threading.Event()
_LOGGER = logging.getLogger(__name__)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses standalone recovery
    fcntl = None  # type: ignore[assignment]


def _pipeline_loop(config: Any) -> None:
    from .pipeline import HegiPipeline

    pipeline = HegiPipeline(config)
    poll_seconds = max(
        1, int(config.section("daemon").get("poll_seconds", 60))
    )
    while True:
        _run_pipeline_cycle(config, pipeline)
        _PIPELINE_WAKE.wait(poll_seconds)
        _PIPELINE_WAKE.clear()


def _run_pipeline_cycle(config: Any, pipeline: Any) -> bool:
    """Run one cycle only while owning the standalone daemon's lock."""
    if fcntl is None:
        return False
    lock_path = config.state_db.parent / "daemon.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="ascii") as lock_stream:
        try:
            fcntl.flock(
                lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
            )
        except BlockingIOError:
            return False
        try:
            try:
                pipeline.run_once(dry_run=False)
            except Exception as exc:
                _LOGGER.exception("embedded HEGI pipeline cycle failed")
                pipeline.state.add_dead_letter(
                    "gateway_pipeline", {}, str(exc)
                )
            try:
                process_pending_approvals(config)
            except Exception as exc:
                _LOGGER.exception("embedded HEGI approval cycle failed")
                pipeline.state.add_dead_letter(
                    "gateway_approval", {}, str(exc)
                )
        finally:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
    return True


def _ensure_pipeline_worker(config: Any) -> None:
    global _PIPELINE_THREAD
    if fcntl is None:
        return
    with _PIPELINE_LOCK:
        if _PIPELINE_THREAD is not None and _PIPELINE_THREAD.is_alive():
            return
        _PIPELINE_THREAD = threading.Thread(
            target=_pipeline_loop,
            args=(config,),
            name="hegi-gateway-pipeline",
            daemon=True,
        )
        _PIPELINE_THREAD.start()


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
    _ensure_pipeline_worker(config)
    _PIPELINE_WAKE.set()


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
    source = getattr(event, "source", None)
    if (
        not config.enabled
        or source is None
        or str(getattr(source, "chat_id", "")) != config.chat_id
    ):
        return None
    _ensure_pipeline_worker(config)
    state = StateStore(config.state_db)
    command = parse_approval_command(text, memory.get("commands"))
    if command is None:
        return None
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
    config = load_config()
    StateStore(config.state_db)
    _ensure_pipeline_worker(config)
    _PIPELINE_WAKE.set()
    context.register_hook("pre_gateway_dispatch", intercept_telegram_approval)
