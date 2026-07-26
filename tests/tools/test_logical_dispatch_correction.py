"""Focused regressions for the single Task-3 correction cycle."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from tools import async_delegation as ad


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    ad._reset_for_tests()
    yield
    ad._reset_for_tests()


def _claimed_authority(board: Path, *, title: str = "bridge target"):
    kb.init_db(board)
    with kb.connect_closing(board) as conn:
        task_id = kb.create_task(
            conn,
            title=title,
            assignee="forge",
            created_by="correction-test",
            initial_status="running",
        )
        assert kb.claim_task(conn, task_id, claimer="bridge-claim") is not None
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.current_run_id is not None
        conn.execute(
            "UPDATE tasks SET workflow_template_id='workflow-v1', current_step_key='implement' WHERE id=?",
            (task_id,),
        )
        conn.commit()
    return ad.BridgeAuthorityContext(
        kanban_db_path=str(board.resolve()),
        workflow_id="workflow-v1",
        step_key="implement",
        step_attempt_id=f"workflow-v1/implement/{task.current_run_id}",
        task_id=task_id,
        run_id=task.current_run_id,
        claim_token="bridge-claim",
        lane="forge",
        route="openai-codex/gpt-5.6-sol",
        owner_id="parent-session",
    )


def _reserve_terminal(authority, result):
    key = ad.canonical_logical_key(authority)
    digest = ad.canonical_json_digest(
        ad.canonical_dispatch_input("bounded receipt", None, "leaf")
    )
    reserved = ad.reserve_logical_delegation(
        logical_key=key,
        input_digest=digest,
        goal="bounded receipt",
        context=None,
        toolsets=None,
        role="leaf",
        model="gpt-5.6-sol",
        authority_context=authority,
    )
    assert reserved["status"] == "reserved"
    assert ad.claim_logical_delegation_launch(key, digest)
    committed = ad.commit_logical_delegation_result(key, digest, result)
    assert committed["state"] == "terminal_unattached"
    return key, digest


def _bridge_counts(board: Path, task_id: str, logical_key: str):
    with sqlite3.connect(board) as conn:
        receipt = conn.execute(
            "SELECT COUNT(*) FROM delegation_receipts WHERE logical_key=?",
            (logical_key,),
        ).fetchone()[0]
        continuation = conn.execute(
            "SELECT COUNT(*) FROM delegation_continuations WHERE logical_key=?",
            (logical_key,),
        ).fetchone()[0]
        events = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='delegation_receipted'",
            (task_id,),
        ).fetchall()
    event = sum(json.loads(payload)["logical_key"] == logical_key for (payload,) in events)
    return {"receipt": receipt, "event": event, "continuation": continuation}


def test_direct_dispatch_without_current_run_claim_emits_no_authoritative_bridge_state(
    tmp_path: Path,
):
    board = tmp_path / "unclaimed.db"
    kb.init_db(board)
    with kb.connect_closing(board) as conn:
        task_id = kb.create_task(
            conn,
            title="unclaimed compatibility target",
            created_by="correction-test",
            initial_status="running",
        )

    goal = "unclaimed direct dispatch"
    logical_key = "compatibility/unclaimed"
    input_digest = ad.canonical_json_digest(
        {"tasks": [{"goal": goal, "context": None, "role": "leaf"}]}
    )
    dispatched = ad.dispatch_logical_delegation(
        logical_key=logical_key,
        input_digest=input_digest,
        goal=goal,
        context=None,
        toolsets=None,
        role="leaf",
        model="local",
        session_key="forge",
        parent_session_id="parent",
        runner=lambda: {"status": "PASS", "summary": "compatibility evidence"},
    )
    assert dispatched["status"] == "dispatched"

    deadline = time.time() + 5
    while time.time() < deadline:
        record = ad.get_logical_delegation(logical_key)
        if record and record["state"] == "terminal_unattached":
            break
        time.sleep(0.01)
    else:
        pytest.fail("direct logical dispatch did not become terminal")

    attached = ad.attach_logical_dispatch_receipt(
        logical_key=logical_key,
        kanban_db_path=board,
        task_id=task_id,
        acknowledge_source=False,
    )

    assert attached["status"] == "non_authoritative"
    with sqlite3.connect(board) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_receipts WHERE logical_key=?",
            (logical_key,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_continuations WHERE logical_key=?",
            (logical_key,),
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='delegation_receipted'",
            (task_id,),
        ).fetchone()[0] == 0


def test_authoritative_reservation_rejects_sha256_prefixed_non_hex_digest(tmp_path: Path):
    authority = _claimed_authority(tmp_path / "malformed-digest.db")
    logical_key = ad.canonical_logical_key(authority)

    rejected = ad.reserve_logical_delegation(
        logical_key=logical_key,
        input_digest="sha256:not-a-canonical-hex-digest",
        goal="malformed digest",
        context=None,
        toolsets=None,
        role="leaf",
        model="local",
        authority_context=authority,
    )

    assert rejected["status"] == "rejected"
    assert ad.get_logical_delegation(logical_key) is None


def test_compatibility_evidence_is_explicitly_non_authoritative_and_cannot_attach(
    tmp_path: Path,
):
    board = tmp_path / "claimed-compatibility.db"
    authority = _claimed_authority(board)
    goal = "retain non-authoritative compatibility evidence"
    logical_key = "compatibility/claimed"
    input_digest = ad.canonical_json_digest(
        {"tasks": [{"goal": goal, "context": None, "role": "leaf"}]}
    )
    dispatched = ad.dispatch_logical_delegation(
        logical_key=logical_key,
        input_digest=input_digest,
        goal=goal,
        context=None,
        toolsets=None,
        role="leaf",
        model="local",
        session_key="forge",
        parent_session_id="parent",
        runner=lambda: {"status": "PASS", "summary": "compatibility evidence"},
    )
    assert dispatched["status"] == "dispatched"

    deadline = time.time() + 5
    while time.time() < deadline:
        record = ad.get_logical_delegation(logical_key)
        if record and record["state"] == "terminal_unattached":
            break
        time.sleep(0.01)
    else:
        pytest.fail("compatibility dispatch did not become terminal")

    attached = ad.attach_logical_dispatch_receipt(
        logical_key=logical_key,
        kanban_db_path=board,
        task_id=authority.task_id,
        acknowledge_source=False,
    )
    record = ad.get_logical_delegation(logical_key)

    assert record is not None
    assert attached["status"] == "non_authoritative"
    assert attached["status"] != "committed"
    assert record["authority"] == {
        "authoritative": False,
        "compatibility_mode": "direct_dispatch",
    }
    assert record["state"] == "terminal_unattached"
    assert record["receipt_id"] is None
    assert record["continuation_id"] is None
    assert _bridge_counts(board, authority.task_id, logical_key) == {
        "receipt": 0,
        "event": 0,
        "continuation": 0,
    }


def test_kanban_receipt_authorization_rejects_task_state_without_current_authority(
    tmp_path: Path,
):
    board = tmp_path / "task-state-only.db"
    kb.init_db(board)
    with kb.connect_closing(board) as conn:
        task_id = kb.create_task(
            conn,
            title="task state is not authority",
            created_by="correction-test",
            initial_status="running",
        )
        result = kb.record_delegation_receipt(
            conn,
            logical_key="task-state-only",
            input_digest="sha256:" + "a" * 64,
            delegation_id="deleg_task_state_only",
            execution_id="exec_task_state_only",
            result_digest="sha256:" + "b" * 64,
            terminal_status="PASS",
            result={"status": "PASS", "summary": "must not authorize"},
            task_id=task_id,
            allow_legacy_running_task=True,
        )

        assert result["status"] == "conflict"
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_receipts WHERE logical_key='task-state-only'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_continuations WHERE logical_key='task-state-only'"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='delegation_receipted'",
            (task_id,),
        ).fetchone()[0] == 0


def test_receipt_requires_current_claim_and_stores_only_bounded_redacted_projection(tmp_path: Path):
    board = tmp_path / "board.db"
    authority = _claimed_authority(board)
    secret = "OPENAI_API_KEY=" + "sk-" + ("x" * 300_000)
    key, digest = _reserve_terminal(
        authority,
        {"status": "PASS", "summary": secret, "private": {"raw": secret}},
    )

    attached = ad.attach_logical_dispatch_receipt(
        logical_key=key,
        kanban_db_path=board,
        task_id=authority.task_id,
        acknowledge_source=False,
    )

    assert attached["status"] == "committed"
    with sqlite3.connect(board) as conn:
        row = conn.execute(
            "SELECT input_digest, result_digest, result_json FROM delegation_receipts WHERE logical_key=?",
            (key,),
        ).fetchone()
    assert row is not None
    assert row[0] == digest
    assert row[1].startswith("sha256:") and len(row[1]) == 71
    assert len(row[2].encode("utf-8")) <= 8192
    assert secret not in row[2]
    projection = json.loads(row[2])
    assert projection["source_pointer"].startswith("state.db:logical_delegations/")
    assert projection["result_digest"] == row[1]

    stale_board = tmp_path / "stale.db"
    stale_authority = _claimed_authority(stale_board, title="stale target")
    stale_key, _ = _reserve_terminal(stale_authority, {"status": "PASS", "summary": "ok"})
    with kb.connect_closing(stale_board) as conn:
        conn.execute(
            "UPDATE tasks SET claim_lock='replacement-claim' WHERE id=?",
            (stale_authority.task_id,),
        )
        conn.commit()
    rejected = ad.attach_logical_dispatch_receipt(
        logical_key=stale_key,
        kanban_db_path=stale_board,
        task_id=stale_authority.task_id,
        acknowledge_source=False,
    )
    assert rejected["status"] == "conflict"
    with sqlite3.connect(stale_board) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_receipts WHERE logical_key=?", (stale_key,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM delegation_continuations WHERE logical_key=?", (stale_key,)
        ).fetchone()[0] == 0


def test_conflict_before_launch_executes_zero_runners():
    original_lock = ad._records_lock
    reached_launch = threading.Event()
    release_launch = threading.Event()
    runner_called = threading.Event()

    class _FirstEntryGate:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.first = True

        def __enter__(self):
            if self.first:
                self.first = False
                reached_launch.set()
                assert release_launch.wait(timeout=5)
            self.wrapped.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.wrapped.release()
            return False

    ad._records_lock = _FirstEntryGate(original_lock)
    dispatch_result = {}

    def _runner():
        runner_called.set()
        return {"status": "PASS", "summary": "must not run"}

    def _dispatch():
        dispatch_result.update(
            ad.dispatch_logical_delegation(
                logical_key="race/key",
                input_digest="sha256:original",
                goal="race",
                context=None,
                toolsets=None,
                role="leaf",
                model="local",
                session_key="forge",
                parent_session_id="parent",
                runner=_runner,
                max_async_children=2,
            )
        )

    thread = threading.Thread(target=_dispatch)
    try:
        thread.start()
        assert reached_launch.wait(timeout=5)
        conflict = ad.reserve_logical_delegation(
            logical_key="race/key",
            input_digest="sha256:conflict",
            goal="conflict",
            context=None,
            toolsets=None,
            role="leaf",
            model="local",
        )
        assert conflict["status"] == "quarantined"
        release_launch.set()
        thread.join(timeout=5)
        assert not thread.is_alive()
        time.sleep(0.05)
        assert dispatch_result["status"] != "dispatched"
        assert not runner_called.is_set()
    finally:
        release_launch.set()
        thread.join(timeout=5)
        ad._records_lock = original_lock


def test_cross_board_attach_cas_mutates_exactly_one_target(tmp_path: Path):
    targets = []
    for label in ("a", "b"):
        board = tmp_path / f"board-{label}.db"
        authority = _claimed_authority(board, title=label)
        targets.append((label, board, authority))

    source_authority = targets[0][2]
    key = ad.canonical_logical_key(source_authority)
    goal = "attach once"
    digest = ad.canonical_json_digest(
        ad.canonical_dispatch_input(goal, None, "leaf")
    )
    dispatched = ad.dispatch_logical_delegation(
        logical_key=key,
        input_digest=digest,
        goal=goal,
        context=None,
        toolsets=None,
        role="leaf",
        model="local",
        session_key="forge",
        parent_session_id="parent",
        runner=lambda: {"status": "PASS", "summary": "one execution"},
        authority_context=source_authority,
    )
    assert dispatched["status"] == "dispatched"
    deadline = time.time() + 5
    while time.time() < deadline:
        row = ad.get_logical_delegation(key)
        if row and row["state"] == "terminal_unattached":
            break
        time.sleep(0.01)
    else:
        pytest.fail("logical dispatch did not become terminal")

    original_get = ad.get_logical_delegation
    barrier = threading.Barrier(2)
    first_reads = set()
    first_reads_lock = threading.Lock()

    def _synchronized_get(logical_key):
        record = original_get(logical_key)
        if threading.current_thread().name.startswith("attach-"):
            ident = threading.get_ident()
            with first_reads_lock:
                first = ident not in first_reads
                first_reads.add(ident)
            if first:
                barrier.wait(timeout=5)
        return record

    ad.get_logical_delegation = _synchronized_get
    results = {}

    def _attach(label, board, authority):
        results[label] = ad.attach_logical_dispatch_receipt(
            logical_key=key,
            kanban_db_path=board,
            task_id=authority.task_id,
            acknowledge_source=False,
        )

    threads = [
        threading.Thread(target=_attach, args=target, name=f"attach-{target[0]}")
        for target in targets
    ]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
    finally:
        ad.get_logical_delegation = original_get

    outcomes = []
    for label, board, authority in targets:
        with sqlite3.connect(board) as conn:
            receipt_count = conn.execute(
                "SELECT COUNT(*) FROM delegation_receipts WHERE logical_key=?",
                (key,),
            ).fetchone()[0]
            event_count = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='delegation_receipted'",
                (authority.task_id,),
            ).fetchone()[0]
            continuation_count = conn.execute(
                "SELECT COUNT(*) FROM delegation_continuations WHERE logical_key=?",
                (key,),
            ).fetchone()[0]
        outcomes.append(
            (
                receipt_count,
                event_count,
                continuation_count,
                results[label]["status"],
            )
        )
    assert sorted(outcomes) == [
        (0, 0, 0, "conflict"),
        (1, 1, 1, "committed"),
    ]


def test_concurrent_attach_refuses_missing_run_claim_before_cas_and_commits_authorized(
    tmp_path: Path,
):
    authorized_board = tmp_path / "authorized.db"
    authorized = _claimed_authority(authorized_board, title="authorized target")
    authorized_key, authorized_digest = _reserve_terminal(
        authorized,
        {"status": "PASS", "summary": "authorized execution"},
    )

    refused_board = tmp_path / "missing-authority.db"
    kb.init_db(refused_board)
    with kb.connect_closing(refused_board) as conn:
        refused_task_id = kb.create_task(
            conn,
            title="missing run and claim",
            assignee="forge",
            created_by="correction-test",
            initial_status="running",
        )
        refused_task = kb.get_task(conn, refused_task_id)
        assert refused_task is not None
        assert refused_task.current_run_id is None
        assert refused_task.claim_lock is None

    refused_key = "compatibility/b3-missing-run-claim"
    refused_goal = "refuse incomplete authority"
    refused_digest = ad.canonical_json_digest(
        ad.canonical_dispatch_input(refused_goal, None, "leaf")
    )
    refused_dispatch = ad.dispatch_logical_delegation(
        logical_key=refused_key,
        input_digest=refused_digest,
        goal=refused_goal,
        context=None,
        toolsets=None,
        role="leaf",
        model="local",
        session_key="forge",
        parent_session_id="parent",
        runner=lambda: {"status": "PASS", "summary": "non-authoritative evidence"},
    )
    assert refused_dispatch["status"] == "dispatched"
    deadline = time.time() + 5
    while time.time() < deadline:
        refused_source = ad.get_logical_delegation(refused_key)
        if refused_source and refused_source["state"] == "terminal_unattached":
            break
        time.sleep(0.01)
    else:
        pytest.fail("incomplete-authority logical dispatch did not become terminal")

    assert authorized_digest == ad.canonical_json_digest(
        ad.canonical_dispatch_input("bounded receipt", None, "leaf")
    )
    assert refused_digest == ad.canonical_json_digest(
        ad.canonical_dispatch_input(refused_goal, None, "leaf")
    )

    barrier = threading.Barrier(2)
    results = {}

    def _attach(label, logical_key, board, task_id):
        barrier.wait(timeout=5)
        results[label] = ad.attach_logical_dispatch_receipt(
            logical_key=logical_key,
            kanban_db_path=board,
            task_id=task_id,
            acknowledge_source=False,
        )

    threads = [
        threading.Thread(
            target=_attach,
            args=("authorized", authorized_key, authorized_board, authorized.task_id),
            name="b3-authorized-attach",
        ),
        threading.Thread(
            target=_attach,
            args=("refused", refused_key, refused_board, refused_task_id),
            name="b3-refused-attach",
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert results["authorized"]["status"] == "committed"
    assert results["refused"]["status"] == "non_authoritative"
    assert sum(result["status"] == "committed" for result in results.values()) == 1
    assert _bridge_counts(authorized_board, authorized.task_id, authorized_key) == {
        "receipt": 1,
        "event": 1,
        "continuation": 1,
    }
    assert _bridge_counts(refused_board, refused_task_id, refused_key) == {
        "receipt": 0,
        "event": 0,
        "continuation": 0,
    }
    refused_source = ad.get_logical_delegation(refused_key)
    assert refused_source is not None
    assert refused_source["state"] == "terminal_unattached"
    assert refused_source["kanban_db_path"] is None
    assert refused_source["task_id"] is None
    assert refused_source["receipt_id"] is None
    assert refused_source["continuation_id"] is None


def test_prune_health_matches_reservation_capacity_at_boundaries():
    limit = 2
    for protected, expected in ((limit - 1, "ok"), (limit, "backpressure"), (limit + 1, "backpressure")):
        with ad._DB_LOCK, ad._connect() as conn:
            conn.execute("DELETE FROM logical_delegations")
        for index in range(protected):
            result = ad.reserve_logical_delegation(
                logical_key=f"threshold/{protected}/{index}",
                input_digest=f"sha256:{protected}-{index}",
                goal="threshold",
                context=None,
                toolsets=None,
                role="leaf",
                model="local",
                max_pending_records=limit + 2,
            )
            assert result["status"] == "reserved"

        health = ad.prune_logical_delegations(
            retention_seconds=0,
            max_records=100,
            max_pending_records=limit,
        )
        reservation = ad.reserve_logical_delegation(
            logical_key=f"threshold/{protected}/next",
            input_digest=f"sha256:{protected}-next",
            goal="next",
            context=None,
            toolsets=None,
            role="leaf",
            model="local",
            max_pending_records=limit,
        )
        assert health["status"] == expected
        assert reservation["status"] == ("reserved" if expected == "ok" else expected)


def test_predecessor_prune_fails_closed_before_deleting_pending_evidence():
    with ad._DB_LOCK, ad._connect() as conn:
        for index in range(60):
            conn.execute(
                """INSERT INTO async_delegations
                   (delegation_id, origin_session, state, dispatched_at,
                    completed_at, updated_at, event_json, result_json,
                    delivery_state, delivery_attempts)
                   VALUES (?, 'b6-rollback', 'completed', ?, ?, ?, '{}', '{}',
                           'pending', 0)""",
                (f"deleg-b6-{index:03d}", index + 1, index + 1, index + 1),
            )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="pending delegation evidence requires a compatible Hermes writer",
    ):
        with sqlite3.connect(ad._db_path()) as conn:
            terminal_count = conn.execute(
                """SELECT COUNT(*) FROM async_delegations
                   WHERE state NOT IN ('running','finalizing')"""
            ).fetchone()[0]
            excess = max(0, terminal_count - 50)
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )

    with sqlite3.connect(ad._db_path()) as conn:
        remaining = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE delivery_state='pending'"""
        ).fetchone()[0]
    assert remaining == 60


def _real_delegate_aggregate(monkeypatch: pytest.MonkeyPatch, statuses: list[str]):
    from tools import delegate_tool as dt
    from tools import delegation_live_log as live_log

    credentials = {
        "model": "local",
        "provider": None,
        "base_url": None,
        "api_key": None,
        "api_mode": None,
        "request_overrides": None,
        "max_output_tokens": None,
        "command": None,
        "args": None,
    }
    parent = SimpleNamespace(
        _delegate_depth=0,
        _interrupt_requested=False,
        _memory_manager=None,
        _active_children=[],
        session_id="parent-session",
        session_estimated_cost_usd=0.0,
        session_cost_source="none",
        session_cost_status="unknown",
    )
    children = [SimpleNamespace(_delegate_role="leaf") for _ in statuses]
    monkeypatch.setattr(dt, "_load_config", lambda: {"workflow_bridge_enabled": False})
    monkeypatch.setattr(dt, "_get_max_concurrent_children", lambda: len(statuses))
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *_a, **_k: credentials)
    monkeypatch.setattr(dt, "_build_child_agent", lambda task_index, **_k: children[task_index])
    monkeypatch.setattr(
        dt,
        "_run_single_child",
        lambda task_index, *_a, **_k: {
            "task_index": task_index,
            "status": statuses[task_index],
            "summary": "result",
            "api_calls": 1,
            "duration_seconds": 0,
            "_child_role": "leaf",
        },
    )
    monkeypatch.setattr(
        live_log,
        "create_live_transcripts",
        lambda *_a, **_k: ("deleg-b5", [], []),
    )
    monkeypatch.setattr(live_log, "update_manifest_statuses", lambda *_a, **_k: None)

    return json.loads(
        dt.delegate_task(
            tasks=[{"goal": f"task-{index}"} for index in range(len(statuses))],
            background=False,
            parent_agent=parent,
        )
    )


def test_real_delegate_aggregate_maps_all_success_to_pass(monkeypatch: pytest.MonkeyPatch):
    aggregate = _real_delegate_aggregate(monkeypatch, ["completed", "completed"])

    assert aggregate["status"] == "PASS"


@pytest.mark.parametrize(
    "statuses",
    [
        ["completed", "failed"],
        ["failed"],
        ["interrupted"],
        ["unknown"],
    ],
    ids=["partial", "failed", "interrupted", "unknown"],
)
def test_real_delegate_aggregate_never_false_passes(
    monkeypatch: pytest.MonkeyPatch, statuses: list[str]
):
    aggregate = _real_delegate_aggregate(monkeypatch, statuses)

    assert aggregate["status"] == "FAILED"


@pytest.mark.parametrize("boundary", ["ai-agent", "registered-tool"])
def test_model_cannot_select_hidden_bridge_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
):
    import run_agent
    from tools import delegate_tool as dt

    board = tmp_path / "bridge-boundary.db"
    expected = _claimed_authority(board)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board))
    monkeypatch.setenv("HERMES_KANBAN_TASK", expected.task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(expected.run_id))
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", expected.claim_token)
    monkeypatch.setenv("HERMES_PROFILE", expected.lane)

    parent = SimpleNamespace(
        _delegate_depth=0,
        session_id=expected.owner_id,
        model=expected.route,
    )
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return "{}"

    monkeypatch.setattr(dt, "delegate_task", _capture)
    model_args = {
        "goal": "bounded bridge",
        "logical_dispatch_key": "model-selected-key",
        "logical_input_digest": "sha256:model-selected-digest",
        "bridge_authority_context": {"task_id": "model-selected-task"},
        "authority_context": {"claim_token": "model-selected-claim"},
    }

    if boundary == "ai-agent":
        run_agent.AIAgent._dispatch_delegate_task(parent, model_args)  # type: ignore[arg-type]
    else:
        dt.registry._tools["delegate_task"].handler(model_args, parent_agent=parent)

    assert captured["bridge_authority_context"] == expected
    assert "logical_dispatch_key" not in captured
    assert "logical_input_digest" not in captured
    assert "bridge_authority_context" not in dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    assert "authority_context" not in dt.DELEGATE_TASK_SCHEMA["parameters"]["properties"]
