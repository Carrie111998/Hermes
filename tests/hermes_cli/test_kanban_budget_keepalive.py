"""LS-2777: iteration-budget Remoko keep-alive."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_budget_keepalive as keepalive
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _running_task(conn, title="keep alive", assignee="cole"):
    tid = kb.create_task(conn, title=title, assignee=assignee)
    task = kb.get_task(conn, tid)
    if task.status != "ready":
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?",
            (tid,),
        )
        conn.commit()
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    return claimed


def _events(conn, task_id, kind=None):
    if kind:
        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (task_id, kind),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
    return [row["kind"] for row in rows]


def _failures(conn, task_id):
    row = conn.execute(
        "SELECT consecutive_failures, status, block_kind, last_failure_error "
        "FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    return row


def test_schema_creates_budget_decisions_table(kanban_home):
    conn = kb.connect()
    try:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "kanban_budget_decisions" in names
        assert "kanban_objectives" not in names
        assert "kanban_objective_units" not in names
    finally:
        conn.close()


def test_first_burn_parks_without_death_counter(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        before = _failures(conn, task.id)
        decision = keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        after = _failures(conn, task.id)
        assert after["consecutive_failures"] == before["consecutive_failures"] == 0
        assert after["status"] == "blocked"
        assert after["block_kind"] == "needs_input"
        assert _events(conn, task.id, "budget_exhausted") == ["budget_exhausted"]
        assert "gave_up" not in _events(conn, task.id)
        assert "timed_out" not in _events(conn, task.id)
        assert len(client.ask_calls) == 1
        card = client.ask_calls[0]
        assert card["question"] == "Keep this work going?"
        assert card["choices"] == list(keepalive.CHOICES)
        assert card["choices"][0] == "Give 90 more"
        assert card["risk"] == "medium"
        assert card["external_id"] == f"obj-{task.id}-budget-1"
        assert decision.request_id == "req-budget-1"
        assert decision.status == "pending"
        run = conn.execute(
            "SELECT outcome, status FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task.id,),
        ).fetchone()
        assert run["outcome"] == "budget_exhausted"
        assert run["status"] == "budget_exhausted"
    finally:
        conn.close()


def test_second_burn_while_pending_is_noop(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        first = keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        # Simulate another worker finishing the same burn.
        conn.execute(
            "UPDATE tasks SET status='running' WHERE id=?",
            (task.id,),
        )
        conn.commit()
        second = keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        assert second.request_id == first.request_id == "req-budget-1"
        assert second.external_id == first.external_id
        assert len(client.ask_calls) == 1
        assert _events(conn, task.id, "budget_exhausted") == ["budget_exhausted"]
        assert _failures(conn, task.id)["consecutive_failures"] == 0
    finally:
        conn.close()


def test_give_90_more_requeues_and_adds_turns(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        result = keepalive.consume_budget_decision(
            conn, task.id, "Give 90 more", remoko_client=client
        )
        assert result["ok"] is True
        assert result["extra_turns"] == 90
        assert keepalive.worker_max_iterations_value(90, base_max_turns=90) == 180
        assert result["max_iterations"] == keepalive.worker_max_iterations_value(90)
        decision = keepalive.get_decision(conn, task.id)
        assert decision.status == "granted"
        assert decision.policy == "extend_repeat"
        assert decision.extra_turns == 90
        row = _failures(conn, task.id)
        assert row["status"] == "ready"
        assert row["consecutive_failures"] == 0
        assert keepalive.worker_max_iterations_value(decision.extra_turns, base_max_turns=90) == 180
        assert client.report_calls[-1]["outcome"] == "completed"
        assert client.mark_calls
    finally:
        conn.close()


def test_park_it_keeps_blocked_needs_input(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        first = keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        result = keepalive.consume_budget_decision(
            conn, task.id, "Park it", remoko_client=client
        )
        assert result["ok"] is True
        row = _failures(conn, task.id)
        assert row["status"] == "blocked"
        assert row["block_kind"] == "needs_input"
        decision = keepalive.get_decision(conn, task.id)
        assert decision.status == "parked"
        assert decision.request_id == first.request_id
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        assert len(client.ask_calls) == 1
    finally:
        conn.close()


def test_duplicate_exhaustion_does_not_create_second_remoko(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        assert len(client.ask_calls) == 1
        assert len({call["external_id"] for call in client.ask_calls}) == 1
    finally:
        conn.close()


def test_pending_survives_reconnect_and_does_not_autoresume(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        tid = task.id
    finally:
        conn.close()

    # Simulated gateway restart: new connection, same board file.
    conn = kb.connect()
    try:
        decision = keepalive.get_decision(conn, tid)
        assert decision is not None
        assert decision.status == "pending"
        assert decision.request_id == "req-budget-1"
        row = _failures(conn, tid)
        assert row["status"] == "blocked"
        assert row["block_kind"] == "needs_input"
        # Reconcile must not flip pending → ready.
        keepalive.reconcile_budget_keepalive(conn, remoko_client=client)
        assert keepalive.get_decision(conn, tid).status == "pending"
        assert _failures(conn, tid)["status"] == "blocked"
        # Consume still revalidates.
        result = keepalive.consume_budget_decision(
            conn, tid, "Give 90 more", remoko_client=client
        )
        assert result["ok"] is True
        assert _failures(conn, tid)["status"] == "ready"
    finally:
        conn.close()


def test_failure_limit_does_not_autokill_first_budget_burn(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        conn.execute("UPDATE tasks SET max_retries = 1 WHERE id=?", (task.id,))
        conn.commit()
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        assert "gave_up" not in _events(conn, task.id)
        assert _failures(conn, task.id)["consecutive_failures"] == 0
        assert keepalive.get_decision(conn, task.id).status == "pending"
    finally:
        conn.close()


def test_wall_clock_timeout_does_not_enter_keepalive(kanban_home, monkeypatch):
    conn = kb.connect()
    try:
        task = _running_task(conn)
        now_minus = 10_000
        conn.execute(
            "UPDATE tasks SET worker_pid=4242, max_runtime_seconds=1, "
            "started_at=? WHERE id=?",
            (now_minus, task.id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid=4242, started_at=? WHERE id=?",
            (now_minus, task.current_run_id),
        )
        conn.commit()
        monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
        timed = kb.enforce_max_runtime(conn, signal_fn=lambda *_a, **_k: None)
        assert task.id in timed
        assert keepalive.get_decision(conn, task.id) is None
        assert "budget_exhausted" not in _events(conn, task.id)
        assert "timed_out" in _events(conn, task.id)
        assert _failures(conn, task.id)["consecutive_failures"] == 1
    finally:
        conn.close()


def test_stale_tap_rejected_when_task_completed(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        # complete_task requires a non-blocked path; force done after the burn.
        conn.execute(
            "UPDATE tasks SET status='done', completed_at=strftime('%s','now') WHERE id=?",
            (task.id,),
        )
        conn.commit()
        result = keepalive.consume_budget_decision(
            conn, task.id, "Give 90 more", remoko_client=client
        )
        assert result["ok"] is False
        assert result.get("stale") is True
        decision = keepalive.get_decision(conn, task.id)
        assert decision.extra_turns == 0
        assert decision.status == "pending"
        assert client.report_calls[-1]["outcome"] == "failed"
    finally:
        conn.close()


def test_after_three_grants_next_card_recommends_park(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        for _ in range(3):
            keepalive.consume_budget_decision(
                conn, task.id, "Give 90 more", remoko_client=client
            )
            # Reclaim so the next burn can park a running card.
            conn.execute(
                "UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?",
                (task.id,),
            )
            conn.commit()
            kb.claim_task(conn, task.id)
            keepalive.record_kanban_budget_exhausted(
                conn, task.id, budget_used=90, budget_max=90, remoko_client=client
            )
        last = client.ask_calls[-1]
        assert last["recommendation"].startswith("Park it")
        assert "three extra bursts" in last["context"].lower()
        assert last["choices"][0] == "Give 90 more"
        assert last["external_id"] == f"obj-{task.id}-budget-4"
        assert keepalive.recommended_choice(3) == "Park it"
    finally:
        conn.close()


def test_give_90_once_then_next_burn_autoparks(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        keepalive.consume_budget_decision(
            conn, task.id, "Give 90 once", remoko_client=client
        )
        asks_after_grant = len(client.ask_calls)
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?",
            (task.id,),
        )
        conn.commit()
        kb.claim_task(conn, task.id)
        decision = keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=180, budget_max=180, remoko_client=client
        )
        assert decision.status == "parked"
        assert decision.policy == "park"
        assert len(client.ask_calls) == asks_after_grant
        row = _failures(conn, task.id)
        assert row["status"] == "blocked"
        assert row["block_kind"] == "needs_input"
    finally:
        conn.close()


def test_send_failure_leaves_pending_empty_request_and_reconcile_retries(kanban_home):
    conn = kb.connect()
    failing = keepalive.RecordingRemokoClient()
    failing.fail_ask = True
    try:
        task = _running_task(conn)
        decision = keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=failing
        )
        assert decision.status == "pending"
        assert not decision.request_id
        retry = keepalive.RecordingRemokoClient()
        keepalive.reconcile_budget_keepalive(conn, remoko_client=retry)
        refreshed = keepalive.get_decision(conn, task.id)
        assert refreshed.status == "pending"
        assert refreshed.request_id == "req-budget-1"
        assert _failures(conn, task.id)["status"] == "blocked"
    finally:
        conn.close()


def test_cli_lock_honors_kanban_env_ahead_of_config():
    env = {
        "HERMES_KANBAN_TASK": "t_abc",
        "HERMES_KANBAN_MAX_ITERATIONS": "180",
        "HERMES_MAX_ITERATIONS": "40",
    }
    assert keepalive.effective_kanban_max_iterations(env) == 180
    assert keepalive.effective_kanban_max_iterations({"HERMES_MAX_ITERATIONS": "40"}) is None
    assert keepalive.effective_kanban_max_iterations(
        {"HERMES_KANBAN_TASK": "t_abc"}
    ) is None


def test_sidecar_used_for_subagent_even_with_kanban_env(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    client = keepalive.RecordingRemokoClient()
    agent = SimpleNamespace(_subagent_id="child-99", session_id="sess-child")
    decision = keepalive.record_iteration_budget_exhausted(
        task_id="t_parent",
        budget_used=50,
        budget_max=50,
        agent=agent,
        remoko_client=client,
    )
    assert decision is not None
    assert decision.store == "sidecar"
    assert decision.task_id == "child-99"
    assert decision.external_id == "obj-child-99-budget-1"
    assert len(client.ask_calls) == 1
    conn = kb.connect()
    try:
        assert keepalive.get_decision(conn, "t_parent") is None
    finally:
        conn.close()


def test_bot_chat_uses_sidecar(kanban_home):
    client = keepalive.RecordingRemokoClient()
    agent = SimpleNamespace(
        _subagent_id=None,
        session_id="sess-bot",
        _session_title_hint="Bot Chat",
        _session_db=None,
    )
    decision = keepalive.record_iteration_budget_exhausted(
        budget_used=90,
        budget_max=90,
        agent=agent,
        remoko_client=client,
    )
    assert decision.store == "sidecar"
    assert decision.task_id == "sess-bot"
    again = keepalive.record_iteration_budget_exhausted(
        budget_used=90,
        budget_max=90,
        agent=agent,
        remoko_client=client,
    )
    assert again.request_id == decision.request_id
    assert len(client.ask_calls) == 1


def test_regular_cli_does_not_open_keepalive(kanban_home):
    agent = SimpleNamespace(
        _subagent_id=None,
        session_id="sess-cli",
        _session_title_hint="something else",
        _session_db=None,
    )
    assert (
        keepalive.record_iteration_budget_exhausted(
            budget_used=90, budget_max=90, agent=agent
        )
        is None
    )


def test_blocking_decision_skips_dispatch(kanban_home):
    conn = kb.connect()
    client = keepalive.RecordingRemokoClient()
    try:
        task = _running_task(conn)
        keepalive.record_kanban_budget_exhausted(
            conn, task.id, budget_used=90, budget_max=90, remoko_client=client
        )
        # A stray unblock must not let the dispatcher spawn.
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?",
            (task.id,),
        )
        conn.commit()
        assert keepalive.budget_decision_blocks_dispatch(conn, task.id) is True
    finally:
        conn.close()


def test_pytest_default_client_does_not_send():
    assert isinstance(keepalive.default_remoko_client(), keepalive.NullRemokoClient)


def test_reconcile_consumes_answered_sidecar(kanban_home):
    client = keepalive.RecordingRemokoClient()
    subject = "child-sidecar-90"
    keepalive.save_sidecar(
        keepalive.BudgetDecision(
            task_id=subject,
            request_id="req-sidecar-90",
            external_id="obj-child-sidecar-90-budget-1",
            status=keepalive.STATUS_PENDING,
            extra_turns=0,
            store="sidecar",
        )
    )
    client.answers["req-sidecar-90"] = "Give 90 more"
    conn = kb.connect()
    try:
        result = keepalive.reconcile_budget_keepalive(conn, remoko_client=client)
        assert result["sidecar_consumed"] == 1
        assert result["sidecar_retried"] == 0
        assert result["consumed"] == 0
    finally:
        conn.close()
    refreshed = keepalive.load_sidecar(subject)
    assert refreshed is not None
    assert refreshed.extra_turns == 90
    assert refreshed.status == "granted"
    assert client.get_calls
    assert client.get_calls[0]["request_id"] == "req-sidecar-90"


def test_overlapping_kanban_burns_send_one_remoko(kanban_home):
    conn = kb.connect()
    try:
        task = _running_task(conn)
        tid = task.id
    finally:
        conn.close()

    in_ask = threading.Event()
    release_send = threading.Event()

    class SlowClient(keepalive.RecordingRemokoClient):
        def ask_question(self, **kwargs):
            in_ask.set()
            assert release_send.wait(timeout=5)
            return super().ask_question(**kwargs)

    client = SlowClient()
    first_result: list[keepalive.BudgetDecision] = []
    errors: list[BaseException] = []

    def first_burn():
        c = kb.connect()
        try:
            first_result.append(
                keepalive.record_kanban_budget_exhausted(
                    c, tid, budget_used=90, budget_max=90, remoko_client=client
                )
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            c.close()

    worker = threading.Thread(target=first_burn)
    worker.start()
    assert in_ask.wait(timeout=5)

    c2 = kb.connect()
    try:
        second = keepalive.record_kanban_budget_exhausted(
            c2, tid, budget_used=90, budget_max=90, remoko_client=client
        )
    finally:
        c2.close()

    release_send.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert len(client.ask_calls) == 1
    assert first_result[0].request_id == "req-budget-1"
    assert second.status == "pending"
    conn = kb.connect()
    try:
        live = keepalive.get_decision(conn, tid)
        assert live is not None
        assert live.request_id == "req-budget-1"
        assert live.status == "pending"
        assert live.external_id == f"obj-{tid}-budget-1"
    finally:
        conn.close()


def test_overlapping_sidecar_burns_send_one_remoko(kanban_home):
    in_ask = threading.Event()
    release_send = threading.Event()

    class SlowClient(keepalive.RecordingRemokoClient):
        def ask_question(self, **kwargs):
            in_ask.set()
            assert release_send.wait(timeout=5)
            return super().ask_question(**kwargs)

    client = SlowClient()
    subject = "child-race-1"
    first_result: list[keepalive.BudgetDecision] = []
    errors: list[BaseException] = []

    def first_burn():
        try:
            first_result.append(
                keepalive.record_sidecar_budget_exhausted(
                    subject, budget_used=50, budget_max=50, remoko_client=client
                )
            )
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=first_burn)
    worker.start()
    assert in_ask.wait(timeout=5)
    second = keepalive.record_sidecar_budget_exhausted(
        subject, budget_used=50, budget_max=50, remoko_client=client
    )
    release_send.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert errors == []
    assert len(client.ask_calls) == 1
    assert first_result[0].request_id == "req-budget-1"
    assert second.status == "pending"
    live = keepalive.load_sidecar(subject)
    assert live is not None
    assert live.request_id == "req-budget-1"
    assert live.status == "pending"
    assert live.external_id == "obj-child-race-1-budget-1"
