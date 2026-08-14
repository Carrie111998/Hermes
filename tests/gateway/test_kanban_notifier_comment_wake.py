import asyncio

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []
        self.handled = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})

    async def handle_message(self, event):
        self.handled.append(event)


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter, profile="origin"):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    runner._active_profile_name = lambda: profile
    return runner


def _commentable_task(
    *,
    block_kind="needs_input",
    status="blocked",
    delivery_mode="notify+wake",
):
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Awaiting decision",
            assignee="worker",
            session_id="origin-session",
        )
        if block_kind == "needs_input" and status == "blocked":
            assert kb.block_task(conn, task_id, reason="Need approval", kind=block_kind)
        elif block_kind == "dependency" and status == "blocked":
            # block_task deliberately routes dependency waits to ``todo``.
            # Preserve that event/API behavior, then model a legacy persisted
            # dependency-blocked row that the watcher must not wake.
            assert kb.block_task(conn, task_id, reason="Waiting on parent", kind=block_kind)
            conn.execute(
                "UPDATE tasks SET status = 'blocked' WHERE id = ?", (task_id,)
            )
            conn.commit()
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, block_kind = ? WHERE id = ?",
                (status, block_kind, task_id),
            )
            conn.commit()
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="origin",
            delivery_mode=delivery_mode,
        )
        return task_id
    finally:
        conn.close()


def _task_and_sub(task_id):
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(
            conn, notifier_profiles={"origin"}, include_unowned=False,
        )
        return kb.get_task(conn, task_id), [sub for sub in subs if sub["task_id"] == task_id]
    finally:
        conn.close()


def _set_comment_wake_config(monkeypatch, *, enabled=True, overrides_mute=False):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {
            "kanban": {
                "human_comment_wake": enabled,
                "human_comment_wake_overrides_mute": overrides_mute,
            },
        },
    )


def test_human_comment_wakes_once_silently_and_preserves_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    task_id = _commentable_task()
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "human", "Approve the proposed approach.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert len(adapter.handled) == 1
    wake = adapter.handled[0]
    assert "read the task's block reason and latest comments" in wake.text
    assert "newest human comment as the decision" in wake.text
    assert "record a resolution comment" in wake.text
    assert "THEN unblock the task" in wake.text
    assert wake.source.profile == "origin"
    task, subs = _task_and_sub(task_id)
    assert task.status == "blocked"
    assert task.block_kind == "needs_input"
    assert len(subs) == 1

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 1


def test_push_comment_wake_failure_retries_before_advancing_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    task_id = _commentable_task()
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "human", "Approve it.")
    finally:
        conn.close()

    class FlakyAdapter(RecordingAdapter):
        async def handle_message(self, event):
            self.handled.append(event)
            if len(self.handled) == 1:
                raise RuntimeError("transient wake failure")

    adapter = FlakyAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 1

    runner._running = True
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
    assert len(adapter.handled) == 2


def test_push_comment_wakes_without_task_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    task_id = _commentable_task()
    conn = kb.connect()
    try:
        conn.execute("UPDATE tasks SET session_id = NULL WHERE id = ?", (task_id,))
        conn.commit()
        kb.add_comment(conn, task_id, "human", "Approve it.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.handled) == 1


@pytest.mark.parametrize(
    ("author", "block_kind", "status"),
    [
        (" worker ", "needs_input", "blocked"),
        (" origin ", "needs_input", "blocked"),
        ("human", "needs_input", "ready"),
        ("human", "dependency", "blocked"),
    ],
    ids=("worker-comment", "origin-resolution", "not-blocked", "dependency-block"),
)
def test_comment_wake_requires_human_needs_input_block(
    tmp_path, monkeypatch, author, block_kind, status,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    task_id = _commentable_task(block_kind=block_kind, status=status)
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, author, "A comment")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []


def test_secondary_profile_resolution_comment_does_not_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    task_id = _commentable_task()
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "origin", "Resolution recorded.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter, profile="default")
    runner._profile_adapters = {"origin": {Platform.TELEGRAM: adapter}}
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.sent == []
    assert adapter.handled == []


def test_newer_resolution_comment_suppresses_older_human_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    task_id = _commentable_task()
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "human", "Approve it.")
        kb.add_comment(conn, task_id, "origin", "Resolution recorded.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.handled == []


def test_mixed_comments_wake_once_for_newest_human_comment(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    task_id = _commentable_task()
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "worker", "I am still blocked.")
        kb.add_comment(conn, task_id, "first-human", "Do not use this decision.")
        kb.add_comment(conn, task_id, "origin", "Resolution recorded.")
        kb.add_comment(conn, task_id, "latest-human", "Use this decision.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert len(adapter.handled) == 1
    wake = adapter.handled[0].text
    assert "latest-human" in wake
    assert "first-human" not in wake


def test_terminal_and_human_comment_share_one_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="Awaiting decision",
            assignee="worker",
            session_id="origin-session",
        )
        kb.add_notify_sub(
            conn,
            task_id=task_id,
            platform="telegram",
            chat_id="chat-1",
            notifier_profile="origin",
            delivery_mode="notify+wake",
        )
        assert kb.block_task(conn, task_id, reason="Need approval", kind="needs_input")
        kb.add_comment(conn, task_id, "decision-maker", "Proceed with option B.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert "blocked" in adapter.sent[0]["text"]
    assert len(adapter.handled) == 1
    wake = adapter.handled[0].text
    assert "blocked" in wake
    assert "decision-maker" in wake


def test_human_comment_wake_config_off_suppresses_wake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    _set_comment_wake_config(monkeypatch, enabled=False, overrides_mute=True)
    kb.init_db()
    task_id = _commentable_task()
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "human", "Approve it.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []


def test_comment_wake_respects_muted_subscription_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    _set_comment_wake_config(monkeypatch, enabled=True, overrides_mute=False)
    kb.init_db()
    task_id = _commentable_task(delivery_mode="notify")
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "human", "Approve it.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert adapter.handled == []


def test_comment_wake_override_wakes_muted_subscription(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "comments.db"))
    _set_comment_wake_config(monkeypatch, enabled=True, overrides_mute=True)
    kb.init_db()
    task_id = _commentable_task(delivery_mode="notify")
    conn = kb.connect()
    try:
        kb.add_comment(conn, task_id, "human", "Approve it.")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert adapter.sent == []
    assert len(adapter.handled) == 1
