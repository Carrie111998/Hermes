"""Canonical Telegram notifications for master-task lifecycle events.

The kanban task lifecycle remains the single durable authority for task
terminal state. This module only maps authoritative lifecycle rows into concise
operator-facing Telegram messages and keeps an in-process dedup cache for
consent notifications.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from agent.redact import redact_sensitive_text


_LOCAL_PATH_RE = re.compile(
    r"(?<![\w:/])(?:/(?:Users|home|private|tmp|var|etc|workspace)/[^\s,;]+|"
    r"[A-Za-z]:\\[^\s,;]+)"
)


@dataclass(frozen=True)
class MasterTaskEvent:
    event_type: str
    task_name: str
    task_id: str
    status: str
    summary: str = ""
    useful_verification: str = ""
    next_action: str = ""
    consent_permission: str = ""
    consent_reason: str = ""
    consent_request_id: str = ""
    dedup_key: str = ""
    source_kind: str = ""
    board: str = ""
    is_terminal: bool = False
    authoritative: bool = True


@dataclass(frozen=True)
class Decision:
    SHOULD_NOTIFY: bool
    EVENT_TYPE: str
    TASK_NAME: str
    STATUS: str
    SUMMARY: str
    USEFUL_VERIFICATION: str
    NEXT_ACTION: str
    DEDUP_KEY: str
    MESSAGE: str
    SKIP_REASON: str = ""


@dataclass(frozen=True)
class DeliveryResult:
    DELIVERED: bool
    EVENT_TYPE: str
    DEDUP_KEY: str
    MESSAGE: str
    SKIP_REASON: str = ""


def _clean_text(value: Any, *, limit: int = 200) -> str:
    text = redact_sensitive_text(
        "" if value is None else str(value),
        force=True,
        redact_url_credentials=True,
    )
    text = _LOCAL_PATH_RE.sub("[local path]", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _task_label(task_name: str, task_id: str) -> str:
    cleaned_name = _clean_text(task_name, limit=120)
    if cleaned_name and cleaned_name != task_id:
        return f"{cleaned_name} ({task_id})"
    return task_id


def _kanban_verification(kind: str, event_id: Optional[int], board: str) -> str:
    board_name = board or "default"
    if event_id is None:
        return f"Sự kiện `{kind}` đã được ghi vào kanban board `{board_name}`."
    return f"Sự kiện `{kind}` #{event_id} đã được ghi vào kanban board `{board_name}`."


def _blocked_next_action(source_kind: str) -> str:
    if source_kind == "changes_requested":
        return "Mở Hermes để xem feedback review và tiếp tục cùng task hiện tại."
    if source_kind == "review_requested":
        return "Mở Hermes để review task này hoặc phản hồi trực tiếp trên task."
    if source_kind == "block_loop_detected":
        return "Task đã vào triage vì lặp lại cùng blocker; anh cần quyết định thủ công trong Hermes."
    return "Mở Hermes để gỡ blocker hoặc cung cấp đúng input còn thiếu."


def _failure_next_action(event_type: str) -> str:
    if event_type == "TASK_TIMED_OUT":
        return "Mở Hermes để xem log, tăng runtime nếu cần, rồi resume cùng task này."
    return "Mở Hermes để xem log, sửa nguyên nhân lỗi, rồi resume cùng task này."


def _approval_permission_level(approval_data: dict) -> str:
    if approval_data.get("smart_denied"):
        return "một lần"
    if approval_data.get("allow_permanent", True):
        return "một lần / phiên / luôn"
    if approval_data.get("allow_session", True):
        return "một lần / phiên"
    return "một lần"


def _event_dedup_key(task_id: str, event_id: Optional[int], kind: str) -> str:
    suffix = str(event_id) if event_id is not None else "unknown"
    return f"kanban:{task_id}:{suffix}:{kind}"


def build_master_task_event_from_kanban(
    task: Any,
    event: Any,
    *,
    board_slug: Optional[str] = None,
) -> Optional[MasterTaskEvent]:
    """Map a kanban task event into canonical operator-notification semantics."""
    if event is None:
        return None
    source_kind = str(getattr(event, "kind", "") or "").strip()
    task_id = str(getattr(event, "task_id", "") or "").strip()
    if not task_id:
        return None
    task_name = ""
    task_status = source_kind
    task_result = ""
    if task is not None:
        task_name = str(getattr(task, "title", "") or "").strip()
        task_status = str(getattr(task, "status", "") or task_status).strip()
        task_result = str(getattr(task, "result", "") or "").strip()
    task_name = task_name or task_id
    payload = getattr(event, "payload", None) or {}
    event_id = getattr(event, "id", None)
    dedup_key = _event_dedup_key(task_id, event_id, source_kind)
    verification = _kanban_verification(source_kind, event_id, board_slug or "")

    if source_kind == "completed":
        summary = _clean_text(payload.get("summary") or task_result or "Không có tóm tắt ngắn.", limit=220)
        return MasterTaskEvent(
            event_type="TASK_COMPLETED",
            task_name=task_name,
            task_id=task_id,
            status="completed",
            summary=summary,
            useful_verification=verification,
            next_action="Anh có thể kiểm tra kết quả hoặc giao bước tiếp theo.",
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            is_terminal=True,
            authoritative=True,
        )
    if source_kind == "blocked":
        summary = _clean_text(
            payload.get("reason") or payload.get("summary") or "Task đang bị chặn và cần thao tác tiếp theo.",
            limit=220,
        )
        return MasterTaskEvent(
            event_type="TASK_BLOCKED",
            task_name=task_name,
            task_id=task_id,
            status=task_status or "blocked",
            summary=summary,
            useful_verification=verification,
            next_action=_blocked_next_action(source_kind),
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            authoritative=True,
        )
    if source_kind == "review_requested":
        summary = _clean_text(
            payload.get("summary") or "Task đã xong phần triển khai và đang chờ review.",
            limit=220,
        )
        return MasterTaskEvent(
            event_type="TASK_WAITING_OPERATOR",
            task_name=task_name,
            task_id=task_id,
            status="review_requested",
            summary=summary,
            useful_verification=verification,
            next_action=_blocked_next_action(source_kind),
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            authoritative=True,
        )
    if source_kind == "changes_requested":
        reviewer = _clean_text(payload.get("reviewer") or "", limit=48)
        implementer = _clean_text(payload.get("implementer") or "", limit=48)
        summary = _clean_text(
            payload.get("reason") or "Review yêu cầu chỉnh sửa trước khi tiếp tục.",
            limit=180,
        )
        provenance = []
        if reviewer:
            provenance.append(f"reviewer @{reviewer}")
        if implementer:
            provenance.append(f"implementer @{implementer}")
        if provenance:
            summary = f"{summary} ({', '.join(provenance)})"
        return MasterTaskEvent(
            event_type="TASK_BLOCKED",
            task_name=task_name,
            task_id=task_id,
            status=str(payload.get("status") or task_status or "blocked"),
            summary=summary,
            useful_verification=verification,
            next_action=_blocked_next_action(source_kind),
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            authoritative=True,
        )
    if source_kind == "block_loop_detected":
        reason = payload.get("reason") or "Task đã lặp lại cùng blocker."
        recurrences = payload.get("recurrences")
        recurrence_note = f" (lặp {recurrences} lần)" if recurrences else ""
        summary = _clean_text(f"{reason}{recurrence_note}", limit=220)
        return MasterTaskEvent(
            event_type="TASK_BLOCKED",
            task_name=task_name,
            task_id=task_id,
            status="triage",
            summary=summary,
            useful_verification=verification,
            next_action=_blocked_next_action(source_kind),
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            authoritative=True,
        )
    if source_kind == "gave_up":
        trigger_outcome = str(payload.get("trigger_outcome") or "").strip()
        event_type = "TASK_TIMED_OUT" if trigger_outcome == "timed_out" else "TASK_FAILED"
        status = "timed_out" if trigger_outcome == "timed_out" else "failed"
        summary = _clean_text(
            payload.get("error")
            or payload.get("reason")
            or "Task đã dừng retry sau nhiều lần lỗi liên tiếp.",
            limit=220,
        )
        return MasterTaskEvent(
            event_type=event_type,
            task_name=task_name,
            task_id=task_id,
            status=status,
            summary=summary,
            useful_verification=verification,
            next_action=_failure_next_action(event_type),
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            is_terminal=True,
            authoritative=True,
        )
    if source_kind == "timed_out":
        summary = _clean_text(
            f"Worker vượt quá giới hạn runtime và dispatcher sẽ thử lại (limit={payload.get('limit_seconds') or '?' }s).",
            limit=220,
        )
        return MasterTaskEvent(
            event_type="TASK_TIMED_OUT",
            task_name=task_name,
            task_id=task_id,
            status="retrying",
            summary=summary,
            useful_verification=verification,
            next_action="Không cần thông báo Telegram terminal ở bước retry trung gian này.",
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            authoritative=False,
        )
    if source_kind == "crashed":
        summary = _clean_text(
            "Worker đã crash và dispatcher sẽ thử lại trước khi coi task là thất bại.",
            limit=220,
        )
        return MasterTaskEvent(
            event_type="TASK_FAILED",
            task_name=task_name,
            task_id=task_id,
            status="retrying",
            summary=summary,
            useful_verification=verification,
            next_action="Không dùng crash attempt-level làm authority cho master-task completion.",
            dedup_key=dedup_key,
            source_kind=source_kind,
            board=board_slug or "",
            authoritative=False,
        )
    return None


def build_master_task_consent_event(approval_data: dict) -> Optional[MasterTaskEvent]:
    """Build a passive Telegram WAITING_OPERATOR notification from approval data."""
    master_task = approval_data.get("master_task")
    if not isinstance(master_task, dict):
        return None
    task_id = str(master_task.get("task_id") or "").strip()
    if not task_id:
        return None
    task_name = str(master_task.get("task_name") or task_id).strip() or task_id
    request_id = str(approval_data.get("request_id") or "").strip()
    dedup_key = f"consent:{request_id or task_id}"
    description = _clean_text(approval_data.get("description") or "Hành động cần quyền thủ công.", limit=180)
    command = _clean_text(approval_data.get("command") or "", limit=180)
    summary = command or description or "Có thao tác đang chờ anh cấp quyền."
    verification = (
        f"Yêu cầu quyền #{request_id[:8]} đang chờ trong UI."
        if request_id
        else "Yêu cầu quyền đang chờ trong UI."
    )
    return MasterTaskEvent(
        event_type="TASK_WAITING_OPERATOR",
        task_name=task_name,
        task_id=task_id,
        status="waiting_operator",
        summary=summary,
        useful_verification=verification,
        next_action="Mở Hermes để Allow / Deny.",
        consent_permission=_approval_permission_level(approval_data),
        consent_reason=description,
        consent_request_id=request_id,
        dedup_key=dedup_key,
        source_kind="approval",
        authoritative=True,
    )


class CanonicalNotificationRouter:
    """Decide and format canonical Telegram notifications for master tasks."""

    def __init__(self) -> None:
        self._delivered_keys: set[str] = set()

    def has_delivered(self, dedup_key: str) -> bool:
        return bool(dedup_key) and dedup_key in self._delivered_keys

    def remember_delivery(self, dedup_key: str) -> None:
        if dedup_key:
            self._delivered_keys.add(dedup_key)

    def decide(self, event: MasterTaskEvent) -> Decision:
        if not event.authoritative:
            return Decision(
                SHOULD_NOTIFY=False,
                EVENT_TYPE=event.event_type,
                TASK_NAME=event.task_name,
                STATUS=event.status,
                SUMMARY=event.summary,
                USEFUL_VERIFICATION=event.useful_verification,
                NEXT_ACTION=event.next_action,
                DEDUP_KEY=event.dedup_key,
                MESSAGE="",
                SKIP_REASON="subprocess_telemetry_only",
            )
        if self.has_delivered(event.dedup_key):
            return Decision(
                SHOULD_NOTIFY=False,
                EVENT_TYPE=event.event_type,
                TASK_NAME=event.task_name,
                STATUS=event.status,
                SUMMARY=event.summary,
                USEFUL_VERIFICATION=event.useful_verification,
                NEXT_ACTION=event.next_action,
                DEDUP_KEY=event.dedup_key,
                MESSAGE="",
                SKIP_REASON="duplicate_delivery",
            )
        message = self._format_message(event)
        return Decision(
            SHOULD_NOTIFY=bool(message),
            EVENT_TYPE=event.event_type,
            TASK_NAME=event.task_name,
            STATUS=event.status,
            SUMMARY=event.summary,
            USEFUL_VERIFICATION=event.useful_verification,
            NEXT_ACTION=event.next_action,
            DEDUP_KEY=event.dedup_key,
            MESSAGE=message,
            SKIP_REASON="" if message else "no_message",
        )

    def route(self, event: MasterTaskEvent, telegram_sender) -> DeliveryResult:
        decision = self.decide(event)
        if not decision.SHOULD_NOTIFY:
            return DeliveryResult(
                DELIVERED=False,
                EVENT_TYPE=decision.EVENT_TYPE,
                DEDUP_KEY=decision.DEDUP_KEY,
                MESSAGE=decision.MESSAGE,
                SKIP_REASON=decision.SKIP_REASON,
            )
        telegram_sender(decision.MESSAGE)
        self.remember_delivery(decision.DEDUP_KEY)
        return DeliveryResult(
            DELIVERED=True,
            EVENT_TYPE=decision.EVENT_TYPE,
            DEDUP_KEY=decision.DEDUP_KEY,
            MESSAGE=decision.MESSAGE,
        )

    def _format_message(self, event: MasterTaskEvent) -> str:
        task_line = f"Task: {_task_label(event.task_name, event.task_id)}"
        if event.event_type == "TASK_COMPLETED":
            lines = [
                "✅ Task đã hoàn tất",
                task_line,
                f"Kết quả: {event.summary or 'Không có tóm tắt ngắn.'}",
                f"Xác minh: {event.useful_verification}",
            ]
            if event.next_action:
                lines.append(f"Tiếp theo: {event.next_action}")
            return "\n".join(lines)
        if event.event_type == "TASK_BLOCKED":
            lines = [
                "⛔ Task đang bị chặn",
                task_line,
                f"Blocker: {event.summary or 'Cần thao tác tiếp theo từ anh.'}",
            ]
            if event.next_action:
                lines.append(f"Cần anh: {event.next_action}")
            return "\n".join(lines)
        if event.event_type == "TASK_TIMED_OUT":
            lines = [
                "⏱ Task đã hết thời gian và đã dừng retry",
                task_line,
                f"Lý do: {event.summary or 'Task đã hết thời gian sau nhiều lần thử.'}",
                f"Xác minh: {event.useful_verification}",
            ]
            if event.next_action:
                lines.append(f"Tiếp theo: {event.next_action}")
            return "\n".join(lines)
        if event.event_type == "TASK_FAILED":
            lines = [
                "❌ Task đã thất bại",
                task_line,
                f"Lý do: {event.summary or 'Task đã dừng retry sau nhiều lỗi.'}",
                f"Xác minh: {event.useful_verification}",
            ]
            if event.next_action:
                lines.append(f"Tiếp theo: {event.next_action}")
            return "\n".join(lines)
        if event.event_type == "TASK_WAITING_OPERATOR":
            if event.consent_request_id:
                return "\n".join(
                    [
                        "🔐 Hermes đang chờ anh cấp quyền",
                        task_line,
                        f"Action: {event.summary or 'Có thao tác cần xác nhận.'}",
                        f"Permission: {event.consent_permission or 'một lần'}",
                        f"Reason: {event.consent_reason or event.summary or 'Cần quyền thủ công trong UI.'}",
                        event.next_action or "Mở Hermes để Allow / Deny.",
                    ]
                )
            header = (
                "👀 Task đang chờ anh review"
                if event.source_kind == "review_requested"
                else "👀 Task đang chờ anh xử lý"
            )
            lines = [
                header,
                task_line,
                f"Kết quả: {event.summary or 'Task đang chờ thao tác tiếp theo từ anh.'}",
                f"Xác minh: {event.useful_verification}",
            ]
            if event.next_action:
                lines.append(f"Tiếp theo: {event.next_action}")
            return "\n".join(lines)
        return ""


def get_canonical_notification_router(owner: Any) -> CanonicalNotificationRouter:
    router = getattr(owner, "_master_task_notification_router", None)
    if isinstance(router, CanonicalNotificationRouter):
        return router
    router = CanonicalNotificationRouter()
    try:
        setattr(owner, "_master_task_notification_router", router)
    except Exception:
        pass
    return router
