import inspect
import os
import tempfile
from pathlib import Path

from gateway.master_task_notifications import (
    CanonicalNotificationRouter,
    build_master_task_consent_event,
    build_master_task_event_from_kanban,
)
from hermes_cli import kanban_db as kb
from tools.approval import _with_master_task_metadata


_NOTIFIER_KINDS = (
    "completed",
    "blocked",
    "gave_up",
    "crashed",
    "timed_out",
    "review_requested",
    "changes_requested",
    "block_loop_detected",
)


class RecordingTelegramSender:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def __call__(self, message: str) -> None:
        self.messages.append(message)


def _init_kanban_home(monkeypatch, db_name: str) -> Path:
    hermes_home = Path(
        tempfile.mkdtemp(prefix="hermes-master-task-notify-")
    ).resolve()
    hermes_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    db_path = hermes_home / "kanban-tests" / db_name
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    return db_path


def _claim_events(task_id: str, *, chat_id: str = "chat-1") -> list[kb.Event]:
    conn = kb.connect()
    try:
        _old, _new, events = kb.claim_unseen_events_for_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id=chat_id,
            kinds=_NOTIFIER_KINDS,
        )
        return events
    finally:
        conn.close()


def _task(task_id: str):
    conn = kb.connect()
    try:
        return kb.get_task(conn, task_id)
    finally:
        conn.close()


def test_success_completed_delivery_once_with_human_title(monkeypatch):
    _init_kanban_home(monkeypatch, "success.db")
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Triển khai thông báo release", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1")
        assert kb.complete_task(conn, task_id, summary="Đã cập nhật router và chạy pytest xanh.")
    finally:
        conn.close()

    event = _claim_events(task_id)[0]
    router = CanonicalNotificationRouter()
    sender = RecordingTelegramSender()
    routed = router.route(
        build_master_task_event_from_kanban(_task(task_id), event, board_slug="default"),
        sender,
    )

    assert routed.DELIVERED is True
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert message.startswith("✅ Task đã hoàn tất")
    assert "Triển khai thông báo release" in message
    assert "Đã cập nhật router và chạy pytest xanh." in message
    assert f"(t_" in message
    assert f"Task: {task_id}" not in message

    routed_again = router.route(
        build_master_task_event_from_kanban(_task(task_id), event, board_slug="default"),
        sender,
    )
    assert routed_again.DELIVERED is False
    assert routed_again.SKIP_REASON == "duplicate_delivery"
    assert len(sender.messages) == 1


def test_child_retry_events_do_not_prematurely_complete_master_task(monkeypatch):
    _init_kanban_home(monkeypatch, "child-retry.db")
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Build artifact bundle", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1")
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, "timed_out", {"limit_seconds": 120})
    finally:
        conn.close()

    router = CanonicalNotificationRouter()
    sender = RecordingTelegramSender()
    timed_out_event = _claim_events(task_id)[0]
    timed_out_result = router.route(
        build_master_task_event_from_kanban(_task(task_id), timed_out_event, board_slug="default"),
        sender,
    )
    assert timed_out_result.DELIVERED is False
    assert timed_out_result.SKIP_REASON == "subprocess_telemetry_only"
    assert sender.messages == []

    conn = kb.connect()
    try:
        assert kb.complete_task(conn, task_id, summary="Artifact cuối cùng đã build thành công sau lần retry.")
    finally:
        conn.close()

    completed_event = _claim_events(task_id)[0]
    completed_result = router.route(
        build_master_task_event_from_kanban(_task(task_id), completed_event, board_slug="default"),
        sender,
    )
    assert completed_result.DELIVERED is True
    assert len(sender.messages) == 1
    assert "Artifact cuối cùng đã build thành công sau lần retry." in sender.messages[0]


def test_consent_waiting_operator_dedups_by_request_id_and_resumes_same_task(monkeypatch):
    _init_kanban_home(monkeypatch, "consent.db")
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Publish changelog",
            assignee="worker",
            session_id="agent:main:telegram:dm:chat-1",
        )
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "17")
    approval_data = _with_master_task_metadata(
        {
            "command": "scp ./dist/app.tar.gz production:/srv/releases/",
            "description": "Deploy production artifact",
            "allow_permanent": False,
            "allow_session": True,
        }
    )
    approval_data["request_id"] = "req-master-task-001"

    router = CanonicalNotificationRouter()
    sender = RecordingTelegramSender()
    consent_event = build_master_task_consent_event(approval_data)
    consent_result = router.route(consent_event, sender)

    assert consent_result.DELIVERED is True
    assert len(sender.messages) == 1
    assert sender.messages[0].startswith("🔐 Hermes đang chờ anh cấp quyền")
    assert "Publish changelog" in sender.messages[0]
    assert "Permission: một lần / phiên" in sender.messages[0]
    assert "Mở Hermes để Allow / Deny." in sender.messages[0]
    assert "Allow Once" not in sender.messages[0]

    consent_again = router.route(consent_event, sender)
    assert consent_again.DELIVERED is False
    assert consent_again.SKIP_REASON == "duplicate_delivery"
    assert len(sender.messages) == 1

    conn = kb.connect()
    try:
        assert kb.complete_task(conn, task_id, summary="Đã publish changelog sau khi anh cấp quyền.")
    finally:
        conn.close()
    completed_event = _claim_events(task_id)[0]
    completed_master_event = build_master_task_event_from_kanban(
        _task(task_id), completed_event, board_slug="default"
    )
    assert completed_master_event.task_id == consent_event.task_id
    router.route(completed_master_event, sender)
    assert len(sender.messages) == 2


def test_blocked_and_final_failure_mapping_stay_canonical(monkeypatch):
    _init_kanban_home(monkeypatch, "blocked-failure.db")
    conn = kb.connect()
    try:
        blocked_task_id = kb.create_task(conn, title="Rotate prod key", assignee="worker")
        kb.add_notify_sub(conn, task_id=blocked_task_id, platform="telegram", chat_id="chat-1")
        assert kb.block_task(conn, blocked_task_id, reason="Cần production token", kind="needs_input")

        failed_task_id = kb.create_task(conn, title="Run long migration", assignee="worker")
        kb.add_notify_sub(conn, task_id=failed_task_id, platform="telegram", chat_id="chat-1")
        with kb.write_txn(conn):
            kb._append_event(conn, failed_task_id, "crashed", {"pid": 999})
            kb._append_event(
                conn,
                failed_task_id,
                "gave_up",
                {"error": "worker crashed 3 lần liên tiếp", "trigger_outcome": "crashed"},
            )
    finally:
        conn.close()

    router = CanonicalNotificationRouter()
    sender = RecordingTelegramSender()

    blocked_event = _claim_events(blocked_task_id)[0]
    blocked_result = router.route(
        build_master_task_event_from_kanban(_task(blocked_task_id), blocked_event, board_slug="default"),
        sender,
    )
    assert blocked_result.DELIVERED is True
    assert sender.messages[0].startswith("⛔ Task đang bị chặn")
    assert "Cần production token" in sender.messages[0]

    failed_events = _claim_events(failed_task_id)
    crashed_result = router.route(
        build_master_task_event_from_kanban(_task(failed_task_id), failed_events[0], board_slug="default"),
        sender,
    )
    assert crashed_result.DELIVERED is False
    assert crashed_result.SKIP_REASON == "subprocess_telemetry_only"

    gave_up_result = router.route(
        build_master_task_event_from_kanban(_task(failed_task_id), failed_events[1], board_slug="default"),
        sender,
    )
    assert gave_up_result.DELIVERED is True
    assert sender.messages[1].startswith("❌ Task đã thất bại")
    assert "worker crashed 3 lần liên tiếp" in sender.messages[1]


def test_claim_cursor_prevents_duplicate_delivery_after_reconnect(monkeypatch):
    _init_kanban_home(monkeypatch, "dedup.db")
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="Generate SBOM", assignee="worker")
        kb.add_notify_sub(conn, task_id=task_id, platform="telegram", chat_id="chat-1")
        assert kb.complete_task(conn, task_id, summary="Đã xuất SBOM vào artifacts.")
    finally:
        conn.close()

    first_router = CanonicalNotificationRouter()
    sender = RecordingTelegramSender()
    first_events = _claim_events(task_id)
    assert len(first_events) == 1
    first_router.route(
        build_master_task_event_from_kanban(_task(task_id), first_events[0], board_slug="default"),
        sender,
    )
    assert len(sender.messages) == 1

    second_router = CanonicalNotificationRouter()
    second_events = _claim_events(task_id)
    assert second_events == []
    assert len(sender.messages) == 1
    assert isinstance(second_router, CanonicalNotificationRouter)


def test_gateway_approval_sync_has_passive_telegram_master_task_branch():
    import gateway.run as gateway_run

    source = inspect.getsource(gateway_run)
    assert "build_master_task_consent_event" in source
    assert "Failed to send Telegram master-task consent notification" in source
