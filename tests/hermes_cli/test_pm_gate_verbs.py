"""Plan-gate verbs and transitions (Option B: scheduled parking + gate_state).

These pin the properties that make the gate hold without adding a status:
a gated task is never claimed, never auto-promoted, and never satisfies a
child's dependency; only the explicit release verb lets it out.
"""

import pytest

from hermes_cli import kanban_db as kb


def _attest(project_id="p1", revision=1, body="the plan", decision="approved"):
    """A real broker-produced attestation via a stub TTY (fresh nonce each call)."""
    from hermes_cli import approval_broker as ab

    # Minted the way a future authenticated adapter will. There is no local
    # approval surface any more; these tests are about what release_plan_gate
    # does with an attestation, which is unchanged.
    return ab.issue_attestation_for_adapter(
        project_id=project_id, revision=revision, plan_body=body,
        decision=decision, surface="test-adapter", operator_display="tester",
    )


@pytest.fixture
def conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    yield c
    c.close()


def _gated(conn, title="work"):
    tid = kb.create_task(conn, title=title, assignee="a")
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',1,0,1)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO pm_plans "
        "(project_id, revision, body, proposed_at, root_task_id)"
        " VALUES ('p1', 1, 'the plan', 1, ?)", (tid,)
    )
    assert kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1) is True
    return tid


# --- parking -------------------------------------------------------------

def test_park_sets_scheduled_and_gate_state(conn):
    tid = _gated(conn)
    t = kb.get_task(conn, tid)
    assert t.status == "scheduled"
    assert kb.gate_state_of(conn, tid) == "plan"


def test_park_emits_a_human_notification_event(conn):
    tid = _gated(conn)
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "plan_awaiting_approval" in kinds


def test_double_park_is_a_noop_not_an_error(conn):
    tid = _gated(conn)
    assert kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1) is False
    assert kb.gate_state_of(conn, tid) == "plan"


def test_park_clears_any_claim(conn):
    tid = _gated(conn)
    t = kb.get_task(conn, tid)
    assert t.claim_lock is None and t.worker_pid is None


# --- the gate holds ------------------------------------------------------

def test_gated_task_cannot_be_claimed(conn):
    tid = _gated(conn)
    assert kb.claim_task(conn, tid) is None


def test_gated_task_is_not_auto_promoted(conn):
    tid = _gated(conn)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, tid).status == "scheduled"
    assert kb.gate_state_of(conn, tid) == "plan"


def test_gated_task_is_not_dispatched(conn):
    tid = _gated(conn)
    spawned = []
    kb.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 4242)
    assert spawned == []
    assert not kb.list_runs(conn, tid)


def test_gated_parent_does_not_satisfy_a_child(conn):
    parent = _gated(conn, title="parent")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None


# --- release -------------------------------------------------------------

def test_approve_releases_to_ready_when_parents_are_satisfied(conn):
    tid = _gated(conn)
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is True and why is None
    assert kb.get_task(conn, tid).status == "ready"
    assert kb.gate_state_of(conn, tid) is None


def test_approve_lands_in_todo_while_a_parent_is_open(conn):
    """Releasing a gate must never jump a dependency."""
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',1,0,1)"
    )
    conn.execute(
        "INSERT OR IGNORE INTO pm_plans "
        "(project_id, revision, body, proposed_at, root_task_id)"
        " VALUES ('p1', 1, 'the plan', 1, ?)", (child,)
    )
    kb.park_for_plan_approval(conn, child, project_id="p1", revision=1)
    ok, _ = kb.release_plan_gate(conn, child, attestation=_attest())
    assert ok is True
    assert kb.get_task(conn, child).status == "todo"


def test_reject_returns_the_task_to_triage(conn):
    tid = _gated(conn)
    ok, why = kb.release_plan_gate(
        conn, tid, attestation=_attest(decision="rejected"), reason="scope too wide"
    )
    assert ok is True and why is None
    assert kb.get_task(conn, tid).status == "triage"
    assert kb.gate_state_of(conn, tid) is None


def test_release_records_the_decision_event(conn):
    tid = _gated(conn)
    kb.release_plan_gate(conn, tid, attestation=_attest())
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "plan_approved" in kinds


def test_release_refuses_an_ungated_task(conn):
    tid = kb.create_task(conn, title="t", assignee="a")
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "not parked" in why


def test_release_refuses_a_missing_task(conn):
    ok, why = kb.release_plan_gate(conn, "t_nope", attestation=_attest())
    assert ok is False and "not found" in why


def test_an_unknown_decision_is_refused_at_the_broker(conn):
    """The decision now lives on the attestation, so it is validated at mint."""
    from hermes_cli import approval_broker as ab
    with pytest.raises(ValueError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="maybe")


def test_second_release_fails_safely(conn):
    tid = _gated(conn)
    assert kb.release_plan_gate(conn, tid, attestation=_attest())[0] is True
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and why is not None
    assert kb.get_task(conn, tid).status == "ready"


def test_stale_plan_revision_fails_safely_and_stays_gated(conn):
    """Now enforced automatically — the caller cannot opt out."""
    tid = _gated(conn)
    a = _attest()
    conn.execute("UPDATE pm_projects SET plan_revision = 2 WHERE id = 'p1'")
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is False and "stale plan" in why
    assert kb.gate_state_of(conn, tid) == "plan"
    assert kb.get_task(conn, tid).status == "scheduled"



def test_matching_revision_is_accepted(conn):
    tid = _gated(conn)
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is True and why is None



def test_ordinary_scheduled_task_still_behaves_normally(conn):
    tid = kb.create_task(conn, title="t", assignee="a")
    assert kb.schedule_task(conn, tid, reason="later") is True
    assert kb.get_task(conn, tid).status == "scheduled"
    assert kb.gate_state_of(conn, tid) is None
    assert kb.unblock_task(conn, tid) is True
    assert kb.get_task(conn, tid).status in {"ready", "todo"}


def test_gate_transitions_do_not_touch_failure_counters(conn):
    tid = _gated(conn)
    before = kb.get_task(conn, tid)
    kb.release_plan_gate(conn, tid, attestation=_attest(decision="rejected"))
    conn.execute("UPDATE pm_projects SET plan_revision = 2 WHERE id='p1'")
    conn.execute(
        "INSERT INTO pm_plans "
        "(project_id, revision, body, proposed_at, root_task_id)"
        " VALUES ('p1', 2, 'revised plan', 2, ?)", (tid,)
    )
    kb.park_for_plan_approval(conn, tid, project_id="p1", revision=2)
    kb.release_plan_gate(conn, tid, attestation=_attest(revision=2, body="revised plan"))
    after = kb.get_task(conn, tid)
    assert after.consecutive_failures == before.consecutive_failures == 0
    assert after.block_recurrences == before.block_recurrences == 0
    assert after.status != "triage"
