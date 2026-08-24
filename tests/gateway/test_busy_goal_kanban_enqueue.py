"""Busy /goal requests become durable Native Kanban work without interruption."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_LANGUAGE", "ko")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb

    kb._INITIALIZED_PATHS.clear()
    kb.init_db(board="default")
    yield home
    kb._INITIALIZED_PATHS.clear()


def _event(text: str, *, message_id: str = "msg-1") -> MessageEvent:
    return MessageEvent(
        text=text,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            user_name="tester",
            chat_type="dm",
            profile="worker-a",
        ),
        message_id=message_id,
    )


def _runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._handle_goal_command = AsyncMock(return_value="legacy-control")
    runner._active_profile_name = lambda: "worker-a"
    return runner


@pytest.mark.asyncio
async def test_busy_new_goal_is_enqueued_once_and_reports_ready(kanban_home):
    from hermes_cli import kanban_db as kb

    runner = _runner()
    event = _event("/goal 새 보고서 작성")

    first = await runner._busy_goal_command(event, "telegram:u1:c1", event.source)
    second = await runner._busy_goal_command(event, "telegram:u1:c1", event.source)

    conn = kb.connect(board="default")
    try:
        tasks = kb.list_tasks(conn)
    finally:
        conn.close()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.title == "새 보고서 작성"
    assert task.body == "새 보고서 작성"
    assert task.assignee == "worker-a"
    assert task.status == "ready"
    assert task.goal_mode is True
    assert task.id in first
    assert "상태: 대기" in first
    assert "현재 작업은 그대로 진행 중입니다." in first
    assert "새 요청은 작업판에 등록했습니다." in first
    assert second == first
    runner._handle_goal_command.assert_not_awaited()

    conn = kb.connect(board="default")
    try:
        subs = kb.list_notify_subs(conn, task.id)
    finally:
        conn.close()
    assert len(subs) == 1
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "c1"
    assert subs[0]["user_id"] == "u1"
    assert subs[0]["notifier_profile"] == "worker-a"
    assert subs[0]["delivery_mode"] == "notify"


@pytest.mark.asyncio
async def test_concurrent_redelivery_still_creates_exactly_one_task(kanban_home):
    from hermes_cli import kanban_db as kb

    runner = _runner()
    event = _event("/goal 새 보고서 작성", message_id="same-update")

    replies = await asyncio.gather(
        *(runner._busy_goal_command(event, "telegram:u1:c1", event.source) for _ in range(8))
    )

    conn = kb.connect(board="default")
    try:
        tasks = kb.list_tasks(conn)
    finally:
        conn.close()

    assert len(tasks) == 1
    assert all(tasks[0].id in reply for reply in replies)


@pytest.mark.asyncio
async def test_distinct_messages_without_platform_ids_are_not_collapsed(kanban_home):
    from hermes_cli import kanban_db as kb

    runner = _runner()
    first = _event("/goal 같은 요청", message_id="")
    second = _event("/goal 같은 요청", message_id="")

    await runner._busy_goal_command(first, "telegram:u1:c1", first.source)
    await runner._busy_goal_command(second, "telegram:u1:c1", second.source)

    conn = kb.connect(board="default")
    try:
        tasks = kb.list_tasks(conn)
    finally:
        conn.close()

    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_busy_goal_control_commands_keep_legacy_behavior(kanban_home):
    runner = _runner()
    event = _event("/goal pause")

    result = await runner._busy_goal_command(event, "telegram:u1:c1", event.source)

    assert result == "legacy-control"
    runner._handle_goal_command.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_busy_goal_enqueue_failure_is_korean_and_keeps_current_goal(
    kanban_home, monkeypatch
):
    from hermes_cli import kanban_db as kb

    runner = _runner()
    event = _event("/goal 새 보고서 작성", message_id="msg-fail")

    monkeypatch.setattr(kb, "create_task", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("locked")))

    result = await runner._busy_goal_command(event, "telegram:u1:c1", event.source)

    assert "현재 작업은 그대로 진행 중입니다." in result
    assert "새 요청을 작업판에 등록하지 못했습니다." in result
    assert "locked" in result
    runner._handle_goal_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_subscription_failure_rolls_back_new_task(kanban_home, monkeypatch):
    from hermes_cli import kanban_db as kb

    runner = _runner()
    monkeypatch.setattr(
        kb,
        "add_notify_sub",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("sub failed")),
    )

    result = await runner._busy_goal_command(
        _event("/goal 원자 등록", message_id="msg-atomic"),
        "telegram:u1:c1",
        _event("/goal 원자 등록", message_id="msg-atomic").source,
    )

    conn = kb.connect(board="default")
    try:
        assert kb.list_tasks(conn) == []
    finally:
        conn.close()
    assert "등록하지 못했습니다" in result
    assert "sub failed" in result


@pytest.mark.asyncio
async def test_archived_message_redelivery_does_not_create_or_resubscribe(
    kanban_home,
):
    from hermes_cli import kanban_db as kb

    runner = _runner()
    event = _event("/goal 한 번만 등록", message_id="msg-archived")
    first = await runner._busy_goal_command(event, "telegram:u1:c1", event.source)

    conn = kb.connect(board="default")
    try:
        task = kb.list_tasks(conn)[0]
        assert kb.archive_task(conn, task.id)
        kb.remove_notify_sub(
            conn,
            task_id=task.id,
            platform="telegram",
            chat_id="c1",
        )
    finally:
        conn.close()

    second = await runner._busy_goal_command(event, "telegram:u1:c1", event.source)

    conn = kb.connect(board="default")
    try:
        tasks = kb.list_tasks(conn, include_archived=True)
        subs = kb.list_notify_subs(conn, task.id)
    finally:
        conn.close()
    assert len(tasks) == 1
    assert tasks[0].id == task.id
    assert tasks[0].status == "archived"
    assert task.id in first and task.id in second
    assert "상태: 보관됨" in second
    assert subs == []


@pytest.mark.asyncio
async def test_redelivery_prefers_active_legacy_duplicate_with_same_timestamp(
    kanban_home,
):
    from hermes_cli import kanban_db as kb

    runner = _runner()
    event = _event("/goal 과거 중복 복구", message_id="msg-legacy-duplicate")
    await runner._busy_goal_command(event, "telegram:u1:c1", event.source)

    conn = kb.connect(board="default")
    try:
        archived = kb.list_tasks(conn)[0]
        assert kb.archive_task(conn, archived.id)
        active_id = kb.create_task(
            conn,
            title="과거 레이스의 활성 중복",
            assignee="worker-a",
            idempotency_key=archived.idempotency_key,
            goal_mode=True,
        )
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET created_at = ? WHERE id IN (?, ?)",
                (archived.created_at, archived.id, active_id),
            )
    finally:
        conn.close()

    result = await runner._busy_goal_command(event, "telegram:u1:c1", event.source)

    conn = kb.connect(board="default")
    try:
        active = kb.get_task(conn, active_id)
        subs = kb.list_notify_subs(conn, active_id)
    finally:
        conn.close()
    assert active is not None and active.status == "ready"
    assert active_id in result
    assert "상태: 대기" in result
    assert len(subs) == 1


@pytest.mark.asyncio
async def test_busy_goal_ack_preserves_english_locale(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_LANGUAGE", "en")
    runner = _runner()

    result = await runner._busy_goal_command(
        _event("/goal write a report", message_id="msg-en"),
        "telegram:u1:c1",
        _event("/goal write a report", message_id="msg-en").source,
    )

    assert "The current work is still in progress." in result
    assert "The new request was added to the board." in result
