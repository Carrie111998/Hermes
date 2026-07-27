import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace


from agent import kanban_handoff_scope as handoff_scope
from gateway.config import Platform
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class ArtifactRecordingAdapter(RecordingAdapter):
    def __init__(self):
        super().__init__()
        self.documents = []
        self.images = []
        self.videos = []

    def extract_local_files(self, _text):
        raise AssertionError(
            "managed DB prose must use the no-filesystem path lexer"
        )

    async def send_document(self, *, chat_id, file_path, metadata=None):
        self.documents.append(file_path)

    async def send_multiple_images(self, *, chat_id, images, metadata=None):
        self.images.extend(item[0] for item in images)

    async def send_video(self, *, chat_id, video_path, metadata=None):
        self.videos.append(video_path)


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _bind_managed_task(conn, task_id, workspace: Path):
    workspace_root = workspace.resolve(strict=True)
    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(workspace_root)
    identity = {
        "platform": "telegram",
        "scope_id": "tenant-1",
        "chat_type": "group",
        "chat_id": "chat-1",
        "thread_id": "",
        "user_id": "user-1",
        "notifier_profile": "default",
        "session_key": "session-1",
    }
    worker_policy = {
        "schema": 2,
        "enabled": True,
        "soft_iteration_limit": 4,
        "max_handoffs": 1,
        "max_iterations": 90,
        "failure_limit": 1,
        "allowed_workspace_roots": [str(workspace_root)],
        "validation_error": None,
    }
    task_policy = {
        "schema": 1,
        "authorized": True,
        "origin": identity,
        "matched_origin": {
            key: identity[key]
            for key in ("platform", "chat_type", "chat_id", "user_id")
        },
        "worker_policy": worker_policy,
    }
    assert kb.add_control_binding(
        conn,
        binding_id=f"binding-{task_id}",
        task_id=task_id,
        short_handoff_policy=task_policy,
        **identity,
    ) is True


def _phase1_origin(workspace: Path) -> dict:
    workspace_root = str(workspace.resolve(strict=True))
    identity = {
        "platform": "telegram",
        "scope_id": "tenant-1",
        "chat_type": "group",
        "chat_id": "chat-1",
        "thread_id": "",
        "user_id": "user-1",
        "notifier_profile": "default",
        "session_key": "session-1",
        "message_id": "create-phase1",
        "operation_slot": "slash",
    }
    config = {
        "agent": {"max_turns": 90},
        "kanban": {
            "failure_limit": 1,
            "short_task_handoff": {
                "enabled": True,
                "soft_iteration_limit": 4,
                "max_handoffs": 1,
                "allowed_workspace_roots": [workspace_root],
                "allowed_origins": [
                    {
                        "platform": "telegram",
                        "chat_type": "group",
                        "chat_id": "chat-1",
                        "user_id": "user-1",
                    }
                ],
            },
        },
    }
    decision = handoff_scope.decide_gateway_origin(config, identity)
    assert decision["authorized"] is True
    identity["short_handoff_policy"] = decision["task_policy_json"]
    return identity


def _set_managed_worker_env(
    conn, monkeypatch, task, workspace: Path, *, review: bool
):
    policy = dict(kb._task_short_handoff_worker_policy(conn, task.id))
    if review:
        policy["enabled"] = False
        policy["inactive_reason"] = kb._SHORT_TASK_REVIEW_INACTIVE_REASON
    monkeypatch.setenv("HERMES_KANBAN_TASK", task.id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", str(task.claim_lock))
    monkeypatch.setenv(
        "HERMES_KANBAN_SHORT_TASK_HANDOFF_POLICY",
        json.dumps(policy, sort_keys=True, separators=(",", ":")),
    )
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(workspace.resolve()))
    monkeypatch.setenv(
        "HERMES_KANBAN_MANAGED_LANE",
        "review" if review else "implementation",
    )
    monkeypatch.setenv("HERMES_KANBAN_REVIEW_MODE", "1" if review else "0")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP", "1")
    monkeypatch.setenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_VERIFIED", "1")
    monkeypatch.delenv("HERMES_KANBAN_MANAGED_BOOTSTRAP_ERROR", raising=False)


def _complete_managed_task_through_review(conn, task_id, workspace, monkeypatch):
    implementation = kb.claim_task(conn, task_id, claimer="impl")
    if implementation is None:
        implementation = kb.get_task(conn, task_id)
    assert implementation is not None
    assert implementation.current_run_id is not None
    assert kb._set_worker_pid(conn, task_id, os.getpid())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET handoff_safety_required = 1 WHERE id = ?",
            (int(implementation.current_run_id),),
        )
    implementation = kb.get_task(conn, task_id)
    _set_managed_worker_env(
        conn, monkeypatch, implementation, workspace, review=False
    )
    assert kb.submit_task_for_review(
        conn,
        task_id,
        summary="implementation ready for review",
        expected_run_id=implementation.current_run_id,
        expected_worker_pid=os.getpid(),
    )
    with monkeypatch.context() as release_patch:
        release_patch.setattr(
            kb, "_exit_gate_release_reason", lambda _row: "test_exit"
        )
        kb.release_handoff_exit_gates(conn)
    assert conn.execute(
        "SELECT 1 FROM task_exit_gates WHERE child_task_id = ? "
        "AND released_at IS NULL",
        (task_id,),
    ).fetchone() is None
    reviewer = kb.claim_review_task(conn, task_id, claimer="reviewer")
    assert reviewer is not None
    assert kb._set_worker_pid(conn, task_id, os.getpid())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET handoff_safety_required = 1 WHERE id = ?",
            (int(reviewer.current_run_id),),
        )
    reviewer = kb.get_task(conn, task_id)
    _set_managed_worker_env(conn, monkeypatch, reviewer, workspace, review=True)
    from tools import managed_file_tools

    assert "plain handoff summary" in managed_file_tools.read_file_tool(
        "evidence.txt"
    )
    assert kb.complete_task(
        conn,
        task_id,
        summary="plain handoff summary",
        expected_run_id=reviewer.current_run_id,
        expected_worker_pid=os.getpid(),
    )


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    db_path = tmp_path / "profile-no-fallback.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_managed_artifact_consumer_rejects_malicious_db_paths(
    tmp_path, monkeypatch
):
    """Stored event/result paths cannot make the notifier read outside cwd."""
    db_path = tmp_path / "managed-artifact.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("safe")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    escaped_link = workspace / "escaped.txt"
    escaped_link.symlink_to(outside)

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="malicious stored metadata",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace),
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1"
        )
        # Simulate a historical/forged row that predates the producer guard.
        kb.complete_task(
            conn,
            tid,
            summary=f"outside summary {outside}",
            result=f"outside result {outside}",
            metadata={
                "artifacts": [
                    str(outside),
                    "../outside.txt",
                    str(escaped_link),
                ]
            },
        )
        _bind_managed_task(conn, tid, workspace)
    finally:
        conn.close()

    adapter = ArtifactRecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    assert adapter.documents == []
    assert adapter.images == []
    assert adapter.videos == []

    # A genuinely in-workspace file still reaches the native uploader through
    # the exact same consumer gate.
    runner = _make_runner(adapter)
    asyncio.run(
        runner._deliver_kanban_artifacts(
            adapter=adapter,
            chat_id="chat-1",
            metadata={},
            event_payload={"artifacts": [str(inside)]},
            task=SimpleNamespace(id=tid, result=None),
            managed_short_task=True,
            workspace_path=str(workspace),
        )
    )
    assert adapter.documents == [str(inside.resolve())]


def test_managed_max_retries_one_sends_only_final_chinese_failure(
    tmp_path, monkeypatch
):
    """The first terminal failure never promises a retry that cannot occur."""
    db_path = tmp_path / "managed-final-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="single failure",
            assignee="worker",
            max_retries=1,
            workspace_kind="dir",
            workspace_path=str(workspace.resolve()),
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1"
        )
        _bind_managed_task(conn, tid, workspace)
        conn.execute(
            "UPDATE tasks SET status='blocked', consecutive_failures=1, "
            "last_failure_error='synthetic failure' WHERE id=?",
            (tid,),
        )
        kb._append_event(
            conn,
            tid,
            kind="crashed",
            payload={"error": "synthetic failure", "failures": 1},
        )
        kb._append_event(
            conn,
            tid,
            kind="gave_up",
            payload={
                "error": "synthetic failure",
                "failures": 1,
                "effective_limit": 1,
                "trigger_outcome": "crashed",
                "exit_pending": True,
                "exit_gate": "g_unconfirmed",
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "已停止继续推进" in text
    assert "还未确认" in text
    assert "不会自动重试" in text
    assert "synthetic failure" not in text
    assert "已安全停止" not in text
    assert tid not in text
    assert "will retry" not in text.lower()
    assert "gave up" not in text.lower()


def test_managed_completion_and_blocked_notifications_are_plain_chinese(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "managed-plain-notices.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    conn = kb.connect()
    try:
        completed_tid = kb.create_task(
            conn,
            title="managed completion",
            assignee="worker",
            workspace_kind="dir",
                workspace_path=str(workspace.resolve()),
                validation_class=kb.PHASE1_FILE_REVIEW_VALIDATION_CLASS,
                control_origin=_phase1_origin(workspace),
            )
        blocked_tid = kb.create_task(
            conn,
            title="managed blocked",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace.resolve()),
        )
        for tid in (completed_tid, blocked_tid):
            kb.add_notify_sub(
                conn, task_id=tid, platform="telegram", chat_id="chat-1"
            )
        (workspace / "evidence.txt").write_text(
            "plain handoff summary\n", encoding="utf-8"
        )
        monkeypatch.setattr(
            kb, "_short_task_handoff_dispatch_enabled", lambda: True
        )
        _complete_managed_task_through_review(
            conn, completed_tid, workspace, monkeypatch
        )
        _bind_managed_task(conn, blocked_tid, workspace)
        kb.block_task(conn, blocked_tid, reason="需要人工确认")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    texts = [item["text"] for item in adapter.sent]
    assert len(texts) == 2
    assert any("这项短任务已完成" in text for text in texts)
    assert any("已通过独立复核" in text for text in texts)
    assert any("这项短任务已暂停" in text for text in texts)
    assert all("Kanban" not in text for text in texts)
    assert all(completed_tid not in text for text in texts)
    assert all(blocked_tid not in text for text in texts)


def test_managed_emergency_brake_notification_is_honest_when_exit_unconfirmed(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "managed-emergency-notice.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="managed emergency brake",
            assignee="worker",
            workspace_kind="dir",
            workspace_path=str(workspace.resolve()),
        )
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-1"
        )
        _bind_managed_task(conn, tid, workspace)
        conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='needs_input' "
            "WHERE id=?",
            (tid,),
        )
        kb._append_event(
            conn,
            tid,
            kind="blocked",
            payload={
                "reason": "短任务自动接力已关闭或设置不可用；任务已暂停派发，当前步骤是否完全停止还需确认。",
                "kind": "needs_input",
                "source": "short_task_policy_emergency_brake",
                "policy_disabled": True,
                "identity_verified": False,
                "exit_pending": True,
            },
        )
    finally:
        conn.close()

    adapter = RecordingAdapter()
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter)))

    assert len(adapter.sent) == 1
    text = adapter.sent[0]["text"]
    assert "还不能确认当前步骤已经完全停止" in text
    assert "不会开始下一步" in text
    assert "已安全暂停" not in text
    assert tid not in text
    assert "Kanban" not in text
