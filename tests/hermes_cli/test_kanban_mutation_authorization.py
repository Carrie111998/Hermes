import contextlib
import json
import os
import signal
import subprocess
import time

import pytest

from hermes_cli import kanban_db as kb
from tests.attempt_fence_helpers import (
    isolated_home,
    logical_board_snapshot,
    process_tuple,
    registered_current_process,
)


def test_write_txn_token_resets_after_commit(isolated_home):
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            token = kb._active_write_txn_token(conn)
            assert token
        with pytest.raises(RuntimeError, match="active write_txn"):
            kb._active_write_txn_token(conn)
    finally:
        conn.close()


def test_write_txn_token_resets_after_rollback(isolated_home):
    conn = kb.connect()
    try:
        with pytest.raises(RuntimeError, match="boom"):
            with kb.write_txn(conn):
                assert kb._active_write_txn_token(conn)
                raise RuntimeError("boom")
        with pytest.raises(RuntimeError, match="active write_txn"):
            kb._active_write_txn_token(conn)
    finally:
        conn.close()


def test_nested_same_connection_inherits_token_without_inner_commit(isolated_home):
    conn = kb.connect()
    try:
        with kb.write_txn(conn):
            outer = kb._active_write_txn_token(conn)
            with kb.write_txn(conn, allow_nested=True):
                assert kb._active_write_txn_token(conn) == outer
            assert conn.in_transaction
            assert kb._active_write_txn_token(conn) == outer
    finally:
        conn.close()


@pytest.mark.macos_only
def test_authorization_cannot_be_reused_in_later_transaction(
    registered_current_process,
):
    fixture = registered_current_process
    prepared = kb._discover_current_worker_registration()
    with kb.write_txn(fixture.conn):
        auth = kb._authorize_mutation_locked(
            fixture.conn,
            target_task_ids=(fixture.task_id,),
            prepared_provenance=prepared,
        )
    before = logical_board_snapshot(fixture.conn)
    with kb.write_txn(fixture.conn):
        with pytest.raises(kb.StaleAttemptError):
            kb._authorize_mutation_locked(
                fixture.conn,
                target_task_ids=(fixture.task_id,),
                prepared_provenance=prepared,
                existing=auth,
            )
    assert logical_board_snapshot(fixture.conn) == before


@pytest.fixture
def outside_registered_group(isolated_home):
    leader = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
    identity = kb._darwin_process_identity(leader.pid)
    assert identity is not None
    conn = kb.connect()
    from tests.attempt_fence_helpers import create_bound_attempt

    task_id, claimed, raw_fence = create_bound_attempt(
        conn,
        leader_identity=identity,
    )
    try:
        yield conn, task_id, claimed, raw_fence
    finally:
        conn.close()
        try:
            os.killpg(identity.pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader.wait(timeout=5)


@pytest.mark.macos_only
@pytest.mark.parametrize(
    "operation",
    ["comment", "complete", "block", "heartbeat", "link", "unlink"],
)
def test_outside_group_public_mutation_has_zero_delta(
    outside_registered_group,
    operation,
):
    conn, task_id, claimed, _raw_fence = outside_registered_group
    other_id = kb.create_task(conn, title="other")
    if operation == "unlink":
        conn.execute(
            "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (task_id, other_id),
        )
        conn.commit()
    before = logical_board_snapshot(conn)

    with pytest.raises(kb.FencedTargetError):
        if operation == "comment":
            kb.add_comment(conn, task_id, "outside", "late")
        elif operation == "complete":
            kb.complete_task(
                conn,
                task_id,
                result="late",
                expected_run_id=claimed.current_run_id,
            )
        elif operation == "block":
            kb.block_task(
                conn,
                task_id,
                reason="late",
                expected_run_id=claimed.current_run_id,
            )
        elif operation == "heartbeat":
            kb.heartbeat_claim(conn, task_id)
        elif operation == "link":
            kb.link_tasks(conn, task_id, other_id)
        else:
            kb.unlink_tasks(conn, task_id, other_id)
    assert logical_board_snapshot(conn) == before


@pytest.mark.macos_only
@pytest.mark.parametrize(
    "operation",
    ["assign", "model", "reasoning", "archive", "schedule", "notify"],
)
def test_outside_group_lifecycle_mutation_has_zero_delta(
    outside_registered_group,
    operation,
):
    conn, task_id, _claimed, _raw_fence = outside_registered_group
    before = logical_board_snapshot(conn)
    with pytest.raises(kb.FencedTargetError):
        if operation == "assign":
            kb.assign_task(conn, task_id, "yonatan")
        elif operation == "model":
            kb.set_model_override(conn, task_id, "gpt-5")
        elif operation == "reasoning":
            kb.set_reasoning_effort(conn, task_id, "high")
        elif operation == "archive":
            kb.archive_task(conn, task_id)
        elif operation == "schedule":
            kb.schedule_task(conn, task_id, reason="late")
        else:
            kb.add_notify_sub(
                conn,
                task_id=task_id,
                platform="test",
                chat_id="foreign",
            )
    assert logical_board_snapshot(conn) == before


@pytest.mark.macos_only
def test_outside_group_attachment_fails_before_filesystem_or_db_delta(
    outside_registered_group,
):
    conn, task_id, _claimed, _raw_fence = outside_registered_group
    attachment_dir = kb.task_attachments_dir(task_id)
    assert not attachment_dir.exists()
    before = logical_board_snapshot(conn)
    with pytest.raises(kb.FencedTargetError):
        kb.store_attachment_bytes(conn, task_id, "proof.txt", b"must not land")
    assert not attachment_dir.exists()
    assert logical_board_snapshot(conn) == before


@pytest.mark.macos_only
def test_registered_worker_control_plane_denied_after_env_removal(
    registered_current_process,
    monkeypatch,
):
    fixture = registered_current_process
    other_id = kb.create_task(fixture.conn, title="claim target", assignee="dor-coo")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    before = logical_board_snapshot(fixture.conn)
    with pytest.raises(kb.RegisteredWorkerControlPlaneError):
        kb.claim_task(fixture.conn, other_id, claimer="late-worker")
    assert logical_board_snapshot(fixture.conn) == before


@pytest.mark.macos_only
def test_registered_worker_cannot_delete_its_registration_row(
    registered_current_process,
):
    fixture = registered_current_process
    before = logical_board_snapshot(fixture.conn)
    with pytest.raises(kb.RegisteredWorkerControlPlaneError):
        kb.delete_task(fixture.conn, fixture.task_id)
    assert logical_board_snapshot(fixture.conn) == before


@pytest.mark.macos_only
def test_registered_worker_cannot_mutate_another_unfenced_task(
    registered_current_process,
):
    fixture = registered_current_process
    other_id = kb.create_task(fixture.conn, title="worker-created child")
    before = logical_board_snapshot(fixture.conn)
    with pytest.raises(kb.FencedTargetError):
        kb.add_comment(fixture.conn, other_id, "dor-coo", "cross-task")
    assert logical_board_snapshot(fixture.conn) == before


@pytest.mark.macos_only
def test_registered_worker_filesystem_control_plane_has_zero_delta(
    registered_current_process,
    monkeypatch,
):
    fixture = registered_current_process
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_CLAIM_LOCK", raising=False)
    current_path = kb.current_board_path()
    before_current = current_path.read_bytes() if current_path.exists() else None
    metadata_path = kb.board_metadata_path("blocked-by-fence")
    assert not metadata_path.exists()

    with pytest.raises(kb.RegisteredWorkerControlPlaneError):
        kb.set_current_board("blocked-by-fence")
    with pytest.raises(kb.RegisteredWorkerControlPlaneError):
        kb.write_board_metadata("blocked-by-fence", name="must not land")

    after_current = current_path.read_bytes() if current_path.exists() else None
    assert after_current == before_current
    assert not metadata_path.exists()


@pytest.mark.macos_only
def test_registered_worker_cannot_open_other_board_without_env(
    isolated_home,
    monkeypatch,
):
    other_path = isolated_home / "kanban" / "boards" / "other" / "kanban.db"
    kb.init_db(other_path)
    own_conn = kb.connect()
    identity = kb._darwin_process_identity(os.getpgid(0))
    assert identity is not None
    from tests.attempt_fence_helpers import create_bound_attempt

    create_bound_attempt(own_conn, leader_identity=identity)
    own_conn.close()
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    before = other_path.read_bytes()
    with pytest.raises(kb.CrossBoardWorkerMutationError):
        kb.connect(other_path)
    assert other_path.read_bytes() == before


@pytest.mark.macos_only
def test_registered_group_connects_existing_own_board_without_migration(
    registered_current_process,
):
    fixture = registered_current_process
    fixture.conn.close()
    conn = kb.connect(fixture.board_path)
    try:
        comment_id = kb.add_comment(conn, fixture.task_id, "dor-coo", "authorized")
        assert comment_id > 0
    finally:
        conn.close()


@pytest.mark.macos_only
def test_worker_graph_mutation_requires_own_attempt_as_an_endpoint(
    registered_current_process,
):
    fixture = registered_current_process
    parent_id = kb.create_task(fixture.conn, title="unrelated parent")
    child_id = kb.create_task(fixture.conn, title="unrelated child")
    fixture.conn.execute(
        "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
        (parent_id, child_id),
    )
    fixture.conn.commit()
    before = logical_board_snapshot(fixture.conn)

    with pytest.raises(kb.FencedTargetError):
        kb.unlink_tasks(fixture.conn, parent_id, child_id)
    with pytest.raises(kb.FencedTargetError):
        kb.link_tasks(fixture.conn, child_id, parent_id)

    assert logical_board_snapshot(fixture.conn) == before


@pytest.mark.macos_only
@pytest.mark.parametrize("own_is_parent", [True, False])
@pytest.mark.parametrize("operation", ["link", "unlink"])
def test_worker_graph_mutation_allows_own_attempt_endpoint_only(
    registered_current_process,
    own_is_parent,
    operation,
):
    fixture = registered_current_process
    other_id = kb.create_task(fixture.conn, title="attempt-related node")
    parent_id, child_id = (
        (fixture.task_id, other_id) if own_is_parent else (other_id, fixture.task_id)
    )
    if operation == "unlink":
        fixture.conn.execute(
            "INSERT INTO task_links(parent_id, child_id) VALUES (?, ?)",
            (parent_id, child_id),
        )
        fixture.conn.commit()
        assert kb.unlink_tasks(fixture.conn, parent_id, child_id) is True
        assert (
            fixture.conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                (parent_id, child_id),
            ).fetchone()
            is None
        )
    else:
        kb.link_tasks(fixture.conn, parent_id, child_id)
        assert (
            fixture.conn.execute(
                "SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                (parent_id, child_id),
            ).fetchone()
            is not None
        )


def test_unlink_and_ready_recompute_roll_back_together(isolated_home, monkeypatch):
    conn = kb.connect()
    try:
        parent_id = kb.create_task(conn, title="parent")
        child_id = kb.create_task(conn, title="child")
        kb.link_tasks(conn, parent_id, child_id)
        before = logical_board_snapshot(conn)

        def fail_recompute(*_args, **_kwargs):
            raise RuntimeError("forced recompute failure")

        monkeypatch.setattr(kb, "recompute_ready", fail_recompute)
        with pytest.raises(RuntimeError, match="forced recompute failure"):
            kb.unlink_tasks(conn, parent_id, child_id)
        assert logical_board_snapshot(conn) == before
    finally:
        conn.close()


@pytest.mark.macos_only
@pytest.mark.parametrize("operation", ["archive", "schedule"])
def test_worker_terminal_lifecycle_preserves_exact_attempt_tuple(
    registered_current_process,
    operation,
):
    fixture = registered_current_process
    before_task = process_tuple(kb.get_task(fixture.conn, fixture.task_id))
    before_run = tuple(
        fixture.conn.execute(
            "SELECT claim_lock, worker_pid, worker_pgid, worker_identity, worker_fence "
            "FROM task_runs WHERE id=?",
            (fixture.claimed.current_run_id,),
        ).fetchone()
    )

    if operation == "archive":
        assert kb.archive_task(fixture.conn, fixture.task_id) is True
        expected_status = "archived"
    else:
        assert (
            kb.schedule_task(
                fixture.conn,
                fixture.task_id,
                reason="authorized terminal park",
                expected_run_id=fixture.claimed.current_run_id,
            )
            is True
        )
        expected_status = "scheduled"

    task = kb.get_task(fixture.conn, fixture.task_id)
    assert task.status == expected_status
    assert process_tuple(task) == before_task
    run = fixture.conn.execute(
        "SELECT claim_lock, worker_pid, worker_pgid, worker_identity, worker_fence, "
        "ended_at FROM task_runs WHERE id=?",
        (fixture.claimed.current_run_id,),
    ).fetchone()
    assert tuple(run)[:5] == before_run
    assert run["ended_at"] is not None


def test_claim_review_authorization_and_mutation_use_one_transaction(isolated_home):
    conn = kb.connect()
    try:
        task_id = kb.create_task(conn, title="review target")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task_id,))
        conn.commit()
        statements = []
        conn.set_trace_callback(statements.append)
        assert kb.claim_review_task(conn, task_id, claimer="reviewer") is not None
        begins = [sql for sql in statements if sql.strip().upper() == "BEGIN IMMEDIATE"]
        assert len(begins) == 1
    finally:
        conn.set_trace_callback(None)
        conn.close()


@pytest.mark.macos_only
def test_claim_review_has_no_registration_race_between_gate_and_write(
    isolated_home,
    monkeypatch,
):
    conn = kb.connect()
    try:
        review_id = kb.create_task(conn, title="review target")
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review_id,))
        worker_id = kb.create_task(conn, title="worker registration")
        claimed = kb.claim_task(conn, worker_id, claimer="fixture:claim")
        conn.commit()
        identity = kb._darwin_process_identity(os.getpgid(0))
        assert identity is not None
        raw_fence = json.dumps(
            {
                "run_id": claimed.current_run_id,
                "claim_lock": claimed.claim_lock,
                "host": kb._host_id(),
                "leader_pid": identity.pid,
                "worker_pgid": identity.pgid,
                "worker_identity": identity.token,
                "reason": "race",
                "created_at": int(time.time()),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        real_write_txn = kb.write_txn
        calls = 0
        injected = False

        @contextlib.contextmanager
        def adversarial_write_txn(target_conn, *args, **kwargs):
            nonlocal calls, injected
            calls += 1
            if calls == 2:
                injected = True
                target_conn.execute(
                    "UPDATE tasks SET worker_pid=?, worker_pgid=?, worker_identity=?, "
                    "worker_fence=? WHERE id=?",
                    (identity.pid, identity.pgid, identity.token, raw_fence, worker_id),
                )
                target_conn.execute(
                    "UPDATE task_runs SET worker_pid=?, worker_pgid=?, worker_identity=?, "
                    "worker_fence=? WHERE id=?",
                    (
                        identity.pid,
                        identity.pgid,
                        identity.token,
                        raw_fence,
                        claimed.current_run_id,
                    ),
                )
                target_conn.commit()
            with real_write_txn(target_conn, *args, **kwargs):
                yield

        monkeypatch.setattr(kb, "write_txn", adversarial_write_txn)
        try:
            result = kb.claim_review_task(conn, review_id, claimer="reviewer")
        except kb.RegisteredWorkerControlPlaneError:
            result = None
        if injected:
            assert result is None
            assert kb.get_task(conn, review_id).status == "review"
        else:
            assert calls == 1
            assert result is not None
    finally:
        conn.close()


@pytest.mark.macos_only
def test_registered_worker_connect_rejects_missing_terminal_reaper_schema(
    registered_current_process,
):
    fixture = registered_current_process
    board_path = fixture.board_path
    fixture.conn.execute("DROP TABLE terminal_fence_reap_state")
    fixture.conn.commit()
    fixture.conn.close()
    before = board_path.read_bytes()
    with pytest.raises(kb.AttemptFenceCapabilityError):
        kb.connect(board_path)
    assert board_path.read_bytes() == before


def _task_event_snapshot(conn, task_id):
    task = tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    events = [
        tuple(row)
        for row in conn.execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY id", (task_id,)
        )
    ]
    return task, events


@pytest.mark.macos_only
@pytest.mark.parametrize("operation", ["unlink", "complete", "archive"])
def test_worker_nested_ready_continuation_is_bounded_to_affected_child(
    registered_current_process,
    operation,
):
    fixture = registered_current_process
    child_id = kb.create_task(fixture.conn, title="affected child")
    kb.link_tasks(fixture.conn, fixture.task_id, child_id)
    unrelated_id = kb.create_task(fixture.conn, title="unrelated eligible")
    fixture.conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (unrelated_id,))
    fixture.conn.commit()
    unrelated_before = _task_event_snapshot(fixture.conn, unrelated_id)

    if operation == "unlink":
        assert kb.unlink_tasks(fixture.conn, fixture.task_id, child_id) is True
    elif operation == "complete":
        assert (
            kb.complete_task(
                fixture.conn,
                fixture.task_id,
                result="done",
                expected_run_id=fixture.claimed.current_run_id,
            )
            is True
        )
    else:
        assert kb.archive_task(fixture.conn, fixture.task_id) is True

    assert kb.get_task(fixture.conn, child_id).status == "ready"
    assert _task_event_snapshot(fixture.conn, unrelated_id) == unrelated_before


def test_decompose_nested_ready_continuation_is_bounded_to_created_graph(
    isolated_home,
):
    conn = kb.connect()
    try:
        root_id = kb.create_task(conn, title="triage root")
        unrelated_id = kb.create_task(conn, title="unrelated eligible")
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (root_id,))
        conn.execute("UPDATE tasks SET status='todo' WHERE id=?", (unrelated_id,))
        conn.commit()
        unrelated_before = _task_event_snapshot(conn, unrelated_id)

        child_ids = kb.decompose_triage_task(
            conn,
            root_id,
            root_assignee="dor-coo",
            children=[{"title": "bounded child", "assignee": "yonatan"}],
        )

        assert child_ids and kb.get_task(conn, child_ids[0]).status == "ready"
        assert _task_event_snapshot(conn, unrelated_id) == unrelated_before
    finally:
        conn.close()


def test_operator_recompute_ready_remains_board_wide(isolated_home):
    conn = kb.connect()
    try:
        first_id = kb.create_task(conn, title="first eligible")
        second_id = kb.create_task(conn, title="second eligible")
        conn.execute(
            "UPDATE tasks SET status='todo' WHERE id IN (?, ?)",
            (first_id, second_id),
        )
        conn.commit()

        assert kb.recompute_ready(conn) == 2
        assert kb.get_task(conn, first_id).status == "ready"
        assert kb.get_task(conn, second_id).status == "ready"
    finally:
        conn.close()
