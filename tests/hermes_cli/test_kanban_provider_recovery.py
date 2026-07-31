"""Durable, exact-run provider recovery substrate tests."""

from __future__ import annotations

import concurrent.futures
import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.worker_supervisor import AttemptExit


@pytest.fixture
def recovery_db(tmp_path: Path) -> tuple[Path, object]:
    db_path = tmp_path / "kanban.db"
    conn = kb.connect(db_path)
    try:
        yield db_path, conn
    finally:
        conn.close()
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _running(conn, *, title: str, session_id: str, assignee: str = "alpha"):
    task_id = kb.create_task(conn, title=title, assignee=assignee)
    task = kb.claim_task(conn, task_id, claimer=f"test:{title}")
    assert task is not None and task.current_run_id is not None
    conn.execute(
        "UPDATE tasks SET session_id = ? WHERE id = ? AND current_run_id = ?",
        (session_id, task_id, task.current_run_id),
    )
    return task_id, int(task.current_run_id)


def _terminal_evidence(
    task_id: str,
    run_id: int,
    session_id: str,
    *,
    classification: str = "transient_provider",
) -> AttemptExit:
    return AttemptExit(
        task_id=task_id,
        run_id=run_id,
        session_id=session_id,
        worktree=Path.cwd().resolve(),
        attempt=1,
        pid=123,
        exit_code=1,
        classification=classification,
        events=(),
        owned_processes=(),
    )


def _proof(
    scope,
    *,
    stable_proof_id: str = "proof-001",
    observed_at: int = 1_900_000_000,
    kind=None,
):
    return kb.ProviderRecoveryProof(
        stable_proof_id=stable_proof_id,
        scope=scope,
        kind=kind or kb.ProviderRecoveryProofKind.LIVE_REQUEST_SUCCEEDED,
        provider_observed_at=observed_at,
        publisher_id="provider-runtime",
        publisher_version="1.2.3",
    )


def test_scope_is_immutable_strict_and_normalized():
    scope = kb.ProviderRecoveryScope(" Alpha ", " OPEN-ROUTER ", 7)

    assert scope == kb.ProviderRecoveryScope("alpha", "openrouter", 7)
    with pytest.raises(dataclasses.FrozenInstanceError):
        scope.provider = "other"
    for args in (("", "openrouter", 1), ("alpha", "", 1), ("alpha", "auto", 1)):
        with pytest.raises(ValueError):
            kb.ProviderRecoveryScope(*args)
    for generation in (None, "1", 0, -1, True):
        with pytest.raises((TypeError, ValueError)):
            kb.ProviderRecoveryScope("alpha", "openrouter", generation)


def test_register_waiter_requires_current_running_exact_typed_provider_evidence(recovery_db):
    _, conn = recovery_db
    scope = kb.ProviderRecoveryScope("alpha", "openrouter", 1)
    task_id, run_id = _running(conn, title="eligible", session_id="session-1")
    evidence = _terminal_evidence(task_id, run_id, "session-1")

    assert kb.register_provider_recovery_wait(
        conn, evidence=evidence, scope=scope, waiting_at=100
    )
    assert not kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(task_id, run_id + 1, "session-1"),
        scope=scope,
        waiting_at=101,
    )
    assert not kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(task_id, run_id, "wrong-session"),
        scope=scope,
        waiting_at=101,
    )
    assert not kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(
            task_id, run_id, "session-1", classification="rate_limited"
        ),
        scope=scope,
        waiting_at=101,
    )

    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,))
    assert not kb.register_provider_recovery_wait(
        conn, evidence=evidence, scope=scope, waiting_at=102
    )

    row = conn.execute("SELECT * FROM provider_recovery_waits").fetchone()
    assert (row["task_id"], row["run_id"], row["session_id"]) == (
        task_id,
        run_id,
        "session-1",
    )
    assert row["waiting_at"] == 100  # idempotent registration never replaces it


def test_real_supervised_exit_registers_exact_scoped_waiter(monkeypatch, tmp_path):
    root = tmp_path / "hermes"
    (root / "profiles" / "alpha").mkdir(parents=True)
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    board_db = tmp_path / "kanban.db"
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(board_db))
    kb.init_db(board_db)
    conn = kb.connect(board_db)
    try:
        task_id = kb.create_task(
            conn,
            title="scoped transient",
            assignee="alpha",
            workspace_kind="dir",
            workspace_path=str(workspace),
            initial_status="running",
        )
        task = kb.claim_task(conn, task_id)
        assert task is not None and task.current_run_id is not None
    finally:
        conn.close()

    class FakeSupervisor:
        def start(self, identity, _launch, *, on_exit, **_kwargs):
            on_exit(
                AttemptExit(
                    task_id=identity.task_id,
                    run_id=identity.run_id,
                    session_id=identity.session_id,
                    worktree=identity.worktree,
                    attempt=1,
                    pid=123,
                    exit_code=75,
                    classification="transient_provider",
                    events=(
                        {
                            "kind": "terminal",
                            "profile": "alpha",
                            "provider": "openrouter",
                            "credential_generation": 7,
                        },
                    ),
                    owned_processes=(),
                )
            )
            return SimpleNamespace(pid=123)

    monkeypatch.setattr(
        kb, "_dispatcher_worker_supervisor", lambda **_kwargs: FakeSupervisor()
    )

    assert kb._start_supervised_worker(task, str(workspace)) == 123
    with kb.connect(board_db) as check:
        row = check.execute("SELECT * FROM provider_recovery_waits").fetchone()
        assert row is not None
        assert (
            row["task_id"],
            row["run_id"],
            row["session_id"],
            row["profile"],
            row["provider"],
            row["credential_generation"],
        ) == (
            task_id,
            task.current_run_id,
            kb.get_task(check, task_id).session_id,
            "alpha",
            "openrouter",
            7,
        )


def test_one_event_fans_out_only_to_all_exact_scope_waiters(recovery_db):
    _, conn = recovery_db
    target = kb.ProviderRecoveryScope("alpha", "openrouter", 4)
    identities = [
        _running(conn, title=f"task-{index}", session_id=f"session-{index}")
        for index in range(5)
    ]
    scopes = (
        target,
        kb.ProviderRecoveryScope("ALPHA", "OPEN-ROUTER", 4),
        kb.ProviderRecoveryScope("beta", "openrouter", 4),
        kb.ProviderRecoveryScope("alpha", "anthropic", 4),
        kb.ProviderRecoveryScope("alpha", "openrouter", 5),
    )
    for index, ((task_id, run_id), scope) in enumerate(zip(identities, scopes)):
        assert kb.register_provider_recovery_wait(
            conn,
            evidence=_terminal_evidence(task_id, run_id, f"session-{index}"),
            scope=scope,
            waiting_at=100,
        )

    event = kb.publish_provider_recovery_event(conn, _proof(target, observed_at=101))
    deliveries = kb.list_provider_recovery_deliveries(conn, event_id=event.id)

    assert {
        (delivery.task_id, delivery.run_id, delivery.session_id)
        for delivery in deliveries
    } == {
        (identities[0][0], identities[0][1], "session-0"),
        (identities[1][0], identities[1][1], "session-1"),
    }
    assert {delivery.state for delivery in deliveries} == {
        kb.ProviderRecoveryDeliveryState.PENDING
    }


def test_dispatcher_consumes_delivery_and_signals_only_the_matching_run(
    recovery_db, monkeypatch
):
    db_path, conn = recovery_db
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    scope = kb.ProviderRecoveryScope("alpha", "openrouter", 4)
    task_id, run_id = _running(
        conn, title="dispatcher-consumer", session_id="session-consumer"
    )
    assert kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(task_id, run_id, "session-consumer"),
        scope=scope,
        waiting_at=100,
    )
    event = kb.publish_provider_recovery_event(
        conn, _proof(scope, observed_at=101)
    )
    signalled = []

    def signal(
        exact_task_id,
        reason,
        *,
        board=None,
        expected_run_id=None,
        expected_session_id=None,
    ):
        signalled.append(
            (
                exact_task_id,
                expected_run_id,
                expected_session_id,
                reason,
                board,
            )
        )
        return True

    monkeypatch.setattr(kb, "signal_worker_recovery", signal)

    kb.dispatch_once(conn, max_spawn=0)

    assert signalled == [
        (task_id, run_id, "session-consumer", "provider_recovered", None)
    ]
    deliveries = kb.list_provider_recovery_deliveries(conn, event_id=event.id)
    assert [delivery.state for delivery in deliveries] == [
        kb.ProviderRecoveryDeliveryState.DELIVERED
    ]


def test_stable_proof_publication_is_idempotent_and_collision_safe(recovery_db):
    _, conn = recovery_db
    scope = kb.ProviderRecoveryScope("alpha", "openrouter", 2)
    task_id, run_id = _running(conn, title="one", session_id="session-one")
    assert kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(task_id, run_id, "session-one"),
        scope=scope,
        waiting_at=100,
    )
    proof = _proof(scope, observed_at=101)

    first = kb.publish_provider_recovery_event(conn, proof)
    second = kb.publish_provider_recovery_event(conn, proof)

    assert second == first
    assert conn.execute("SELECT COUNT(*) FROM provider_recovery_events").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM provider_recovery_deliveries").fetchone()[0] == 1
    with pytest.raises(ValueError, match="stable proof id"):
        kb.publish_provider_recovery_event(
            conn,
            _proof(scope, stable_proof_id=proof.stable_proof_id, observed_at=102),
        )


def test_delivery_claims_are_exclusive_and_expired_leases_are_reclaimable(recovery_db):
    db_path, conn = recovery_db
    scope = kb.ProviderRecoveryScope("alpha", "openrouter", 3)
    task_id, run_id = _running(conn, title="lease", session_id="session-lease")
    assert kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(task_id, run_id, "session-lease"),
        scope=scope,
        waiting_at=100,
    )
    event = kb.publish_provider_recovery_event(conn, _proof(scope, observed_at=101))

    def compete(consumer_id: str):
        with kb.connect(db_path) as contender:
            return kb.claim_provider_recovery_deliveries(
                contender,
                consumer_id=consumer_id,
                limit=1,
                lease_seconds=10,
                now=200,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(compete, ("dispatcher-a", "dispatcher-b")))

    assert sorted(len(result) for result in results) == [0, 1]
    winner = next(result[0].claim_owner for result in results if result)
    assert winner in {"dispatcher-a", "dispatcher-b"}
    with kb.connect(db_path) as contender:
        assert kb.claim_provider_recovery_deliveries(
            contender,
            consumer_id="dispatcher-c",
            limit=1,
            lease_seconds=10,
            now=209,
        ) == []
        reclaimed = kb.claim_provider_recovery_deliveries(
            contender,
            consumer_id="dispatcher-c",
            limit=1,
            lease_seconds=10,
            now=210,
        )
    assert [(item.event_id, item.claim_owner) for item in reclaimed] == [
        (event.id, "dispatcher-c")
    ]


def test_delivered_and_stale_marks_are_exact_run_cas(recovery_db):
    _, conn = recovery_db
    scope = kb.ProviderRecoveryScope("alpha", "openrouter", 5)
    first = _running(conn, title="deliver", session_id="session-deliver")
    second = _running(conn, title="stale", session_id="session-stale")
    for (task_id, run_id), session_id in zip(
        (first, second), ("session-deliver", "session-stale")
    ):
        assert kb.register_provider_recovery_wait(
            conn,
            evidence=_terminal_evidence(task_id, run_id, session_id),
            scope=scope,
            waiting_at=100,
        )
    event = kb.publish_provider_recovery_event(conn, _proof(scope, observed_at=101))
    claimed = kb.claim_provider_recovery_deliveries(
        conn, consumer_id="dispatcher", limit=10, lease_seconds=30, now=200
    )
    assert len(claimed) == 2

    assert not kb.mark_provider_recovery_delivery_delivered(
        conn,
        event_id=event.id,
        task_id=first[0],
        run_id=first[1] + 999,
        session_id="session-deliver",
        consumer_id="dispatcher",
        now=201,
    )
    assert kb.mark_provider_recovery_delivery_delivered(
        conn,
        event_id=event.id,
        task_id=first[0],
        run_id=first[1],
        session_id="session-deliver",
        consumer_id="dispatcher",
        now=201,
    )
    assert not kb.mark_provider_recovery_delivery_stale(
        conn,
        event_id=event.id,
        task_id=second[0],
        run_id=second[1],
        session_id="session-stale",
        consumer_id="dispatcher",
        now=201,
    )

    assert kb.reclaim_task(conn, second[0], reason="test successor")
    successor = kb.claim_task(conn, second[0], claimer="test:successor")
    assert successor is not None and successor.current_run_id != second[1]
    conn.execute(
        "UPDATE tasks SET session_id = ? WHERE id = ? AND current_run_id = ?",
        ("session-successor", second[0], successor.current_run_id),
    )
    assert kb.mark_provider_recovery_delivery_stale(
        conn,
        event_id=event.id,
        task_id=second[0],
        run_id=second[1],
        session_id="session-stale",
        consumer_id="dispatcher",
        now=202,
    )
    states = {
        row["task_id"]: row["state"]
        for row in conn.execute("SELECT task_id, state FROM provider_recovery_deliveries")
    }
    assert states == {first[0]: "delivered", second[0]: "stale"}


def test_exact_wait_cleanup_cannot_remove_successor_registration(recovery_db):
    _, conn = recovery_db
    scope = kb.ProviderRecoveryScope("alpha", "openrouter", 6)
    task_id, old_run_id = _running(conn, title="successor", session_id="old-session")
    assert kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(task_id, old_run_id, "old-session"),
        scope=scope,
        waiting_at=100,
    )

    assert kb.reclaim_task(conn, task_id, reason="test successor")
    old_wait = conn.execute(
        "SELECT 1 FROM provider_recovery_waits WHERE task_id = ? AND run_id = ?",
        (task_id, old_run_id),
    ).fetchone()
    assert old_wait is None

    successor = kb.claim_task(conn, task_id, claimer="test:successor")
    assert successor is not None and successor.current_run_id is not None
    new_run_id = int(successor.current_run_id)
    conn.execute(
        "UPDATE tasks SET session_id = ? WHERE id = ? AND current_run_id = ?",
        ("new-session", task_id, new_run_id),
    )
    assert kb.register_provider_recovery_wait(
        conn,
        evidence=_terminal_evidence(task_id, new_run_id, "new-session"),
        scope=scope,
        waiting_at=200,
    )

    assert not kb.close_provider_recovery_wait(
        conn, task_id=task_id, run_id=old_run_id, session_id="old-session"
    )
    waits = conn.execute(
        "SELECT run_id, session_id FROM provider_recovery_waits WHERE task_id = ?",
        (task_id,),
    ).fetchall()
    assert [(row["run_id"], row["session_id"]) for row in waits] == [
        (new_run_id, "new-session")
    ]


def test_proof_api_and_storage_have_no_secret_payload_surface(recovery_db):
    _, conn = recovery_db
    secret = "Bearer sk-super-secret-value"
    scope = kb.ProviderRecoveryScope("alpha", "openrouter", 8)

    for forbidden in (
        {"kind": "retry_after"},
        {"kind": "elapsed_timeout"},
        {"kind": "credential_file_changed"},
        {"kind": "manual_timestamp"},
    ):
        with pytest.raises((TypeError, ValueError)):
            _proof(scope, **forbidden)
    with pytest.raises((TypeError, ValueError)):
        kb.ProviderRecoveryScope("alpha", "openrouter", secret)
    with pytest.raises(TypeError):
        kb.ProviderRecoveryProof(
            stable_proof_id="proof-secret-attempt",
            scope=scope,
            kind=kb.ProviderRecoveryProofKind.LIVE_VALIDATION_SUCCEEDED,
            provider_observed_at=101,
            publisher_id="provider-runtime",
            publisher_version="1.2.3",
            response_body=secret,
        )

    event = kb.publish_provider_recovery_event(
        conn,
        _proof(
            scope,
            kind=kb.ProviderRecoveryProofKind.LIVE_VALIDATION_SUCCEEDED,
            observed_at=101,
        ),
    )
    schema = "\n".join(
        row["sql"] or ""
        for row in conn.execute(
            "SELECT sql FROM sqlite_master WHERE name LIKE 'provider_recovery_%'"
        )
    )
    rows = "\n".join(
        repr(tuple(row))
        for table in (
            "provider_recovery_waits",
            "provider_recovery_events",
            "provider_recovery_deliveries",
        )
        for row in conn.execute(f"SELECT * FROM {table}")
    )
    representations = "\n".join((repr(scope), repr(event), schema, rows)).lower()

    assert secret.lower() not in representations
    for forbidden_name in (
        "credential_value",
        "credential_hash",
        "auth_header",
        "request_body",
        "response_body",
        "token",
    ):
        assert forbidden_name not in schema.lower()
