"""Gate-release refusals: no ordinary path may release a human approval gate.

SECURITY-CRITICAL. The gate is only worth having if every route that could
move a task off `scheduled` refuses while `gate_state` is set. These tests
enumerate the routes rather than trusting that one guard covers them.

Scope, stated honestly: these prove the ORDINARY paths refuse. They do not
prove authority — `release_plan_gate` still takes a caller-supplied actor
string, and the attestation broker that makes it proof of human intent is a
later commit.
"""

import pytest

from hermes_cli import kanban_db as kb


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
    assert kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1) is True
    return tid


# --- the CLI / slash / cron route ---------------------------------------

def test_unblock_task_refuses_a_gated_task(conn):
    tid = _gated(conn)
    assert kb.unblock_task(conn, tid) is False
    assert kb.gate_state_of(conn, tid) == "plan"
    assert kb.get_task(conn, tid).status == "scheduled"


def test_unblock_refusal_is_audited(conn):
    tid = _gated(conn)
    kb.unblock_task(conn, tid)
    kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert "gate_release_refused" in kinds


def test_repeated_unblock_never_wears_the_gate_down(conn):
    tid = _gated(conn)
    for _ in range(5):
        assert kb.unblock_task(conn, tid) is False
    assert kb.gate_state_of(conn, tid) == "plan"


def test_unblock_still_works_on_an_ordinary_scheduled_task(conn):
    """The guard must not break the feature it protects."""
    tid = kb.create_task(conn, title="t", assignee="a")
    kb.schedule_task(conn, tid, reason="later")
    assert kb.unblock_task(conn, tid) is True
    assert kb.get_task(conn, tid).status in {"ready", "todo"}


def test_unblock_still_works_on_a_blocked_task(conn):
    tid = kb.create_task(conn, title="t", assignee="a")
    kb.block_task(conn, tid, reason="need input", kind="needs_input")
    assert kb.unblock_task(conn, tid) is True


# --- the promote route ---------------------------------------------------

def test_promote_cannot_reach_a_gated_task(conn):
    """promote_task only accepts todo/blocked; a gated task is scheduled."""
    tid = _gated(conn)
    ok, why = kb.promote_task(conn, tid, actor="rick")
    assert ok is False and why is not None
    assert kb.gate_state_of(conn, tid) == "plan"


# --- the dispatcher route ------------------------------------------------

def test_dispatcher_never_spawns_on_a_gated_task(conn):
    tid = _gated(conn)
    spawned = []
    for _ in range(3):
        kb.recompute_ready(conn)
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
    assert spawned == []
    assert not kb.list_runs(conn, tid)
    assert kb.gate_state_of(conn, tid) == "plan"


def test_claim_is_refused_directly(conn):
    tid = _gated(conn)
    assert kb.claim_task(conn, tid) is None
    assert kb.claim_review_task(conn, tid) is None


# --- generic events must not advance a gate ------------------------------

def test_parent_completion_does_not_release_a_gated_child(conn):
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',1,0,1)"
    )
    kb.park_for_plan_approval(conn, child, project_id="p1", revision=1)
    kb.complete_task(conn, parent, result="done")
    kb.recompute_ready(conn)
    assert kb.gate_state_of(conn, child) == "plan"
    assert kb.get_task(conn, child).status == "scheduled"


def test_stale_claim_release_does_not_touch_a_gate(conn):
    tid = _gated(conn)
    kb.release_stale_claims(conn)
    assert kb.gate_state_of(conn, tid) == "plan"


def test_reconcile_and_crash_sweeps_do_not_release_a_gate(conn):
    """A restart-time sweep must not advance a gated task."""
    tid = _gated(conn)
    kb.detect_crashed_workers(conn)
    kb.detect_stale_running(conn)
    kb.reconcile_orphaned_running(conn)
    assert kb.gate_state_of(conn, tid) == "plan"
    assert kb.get_task(conn, tid).status == "scheduled"


# --- only the explicit verb releases it ----------------------------------

def test_only_release_plan_gate_lets_it_out(conn):
    tid = _gated(conn)
    assert kb.unblock_task(conn, tid) is False
    assert kb.promote_task(conn, tid, actor="r")[0] is False
    assert kb.claim_task(conn, tid) is None
    assert kb.gate_state_of(conn, tid) == "plan"
    ok, why = kb.release_plan_gate(conn, tid, decision="approved", actor="rick")
    assert ok is True and why is None
    assert kb.gate_state_of(conn, tid) is None
    assert kb.get_task(conn, tid).status == "ready"


def test_after_release_the_ordinary_paths_work_again(conn):
    tid = _gated(conn)
    kb.release_plan_gate(conn, tid, decision="approved", actor="rick")
    kb.schedule_task(conn, tid, reason="later")
    assert kb.unblock_task(conn, tid) is True


# ---------------------------------------------------------------------------
# Terminal / destructive routes (independent review, findings 1-3, 5)
# ---------------------------------------------------------------------------
#
# Archiving or deleting a gated PARENT is the same bypass as unblocking it:
# both stop it blocking its children. `archived` counts as satisfied in
# _parents_satisfied, and deletion drops the task_links rows entirely.


def _gated_parent_with_child(conn):
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',1,0,1)"
    )
    assert kb.park_for_plan_approval(conn, parent, project_id="p1", revision=1) is True
    return parent, child


def _refusals(conn, task_id, via=None):
    out = [e for e in kb.list_events(conn, task_id) if e.kind == "gate_release_refused"]
    if via is not None:
        out = [e for e in out if (e.payload or {}).get("via") == via]
    return out


# --- archive -------------------------------------------------------------

def test_archive_refuses_a_gated_task(conn):
    tid = _gated(conn)
    assert kb.archive_task(conn, tid) is False


def test_archive_leaves_status_and_gate_unchanged(conn):
    tid = _gated(conn)
    kb.archive_task(conn, tid)
    assert kb.get_task(conn, tid).status == "scheduled"
    assert kb.gate_state_of(conn, tid) == "plan"


def test_archiving_a_gated_parent_does_not_promote_children(conn):
    parent, child = _gated_parent_with_child(conn)
    assert kb.archive_task(conn, parent) is False
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None


def test_archive_refusal_is_audited(conn):
    tid = _gated(conn)
    kb.archive_task(conn, tid)
    assert _refusals(conn, tid, via="archive_task")


def test_archive_still_works_on_an_ungated_task(conn):
    tid = kb.create_task(conn, title="t", assignee="a")
    assert kb.archive_task(conn, tid) is True
    assert kb.get_task(conn, tid).status == "archived"


# --- delete --------------------------------------------------------------

def test_delete_refuses_a_gated_task(conn):
    tid = _gated(conn)
    assert kb.delete_task(conn, tid) is False


def test_deleted_gated_task_remains_present_and_gated(conn):
    tid = _gated(conn)
    kb.delete_task(conn, tid)
    t = kb.get_task(conn, tid)
    assert t is not None and t.status == "scheduled"
    assert kb.gate_state_of(conn, tid) == "plan"


def test_delete_refusal_keeps_dependency_links(conn):
    parent, child = _gated_parent_with_child(conn)
    assert kb.delete_task(conn, parent) is False
    assert kb.parent_ids(conn, child) == [parent]
    assert kb.child_ids(conn, parent) == [child]


def test_deleting_a_gated_parent_does_not_promote_children(conn):
    parent, child = _gated_parent_with_child(conn)
    kb.delete_task(conn, parent)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None


def test_delete_refusal_is_audited(conn):
    tid = _gated(conn)
    kb.delete_task(conn, tid)
    assert _refusals(conn, tid, via="delete_task")


def test_delete_still_works_on_an_ungated_task(conn):
    tid = kb.create_task(conn, title="t", assignee="a")
    assert kb.delete_task(conn, tid) is True
    assert kb.get_task(conn, tid) is None


# --- delete_archived_task (defence in depth) -----------------------------

def test_delete_archived_refuses_a_corrupt_archived_row_that_kept_its_gate(conn):
    """A correctly gated task stays `scheduled` and never reaches this verb.

    A legacy, hand-edited, or corrupted row could be `archived` while still
    carrying gate_state; purging it would drop the gate with the row.
    """
    tid = _gated(conn)
    conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (tid,))
    assert kb.delete_archived_task(conn, tid) is False
    assert kb.get_task(conn, tid) is not None
    assert kb.gate_state_of(conn, tid) == "plan"
    assert _refusals(conn, tid, via="delete_archived_task")


def test_delete_archived_still_works_on_an_ungated_archived_task(conn):
    tid = kb.create_task(conn, title="t", assignee="a")
    assert kb.archive_task(conn, tid) is True
    assert kb.delete_archived_task(conn, tid) is True
    assert kb.get_task(conn, tid) is None


# --- audit consistency ---------------------------------------------------

def test_every_db_refusal_route_emits_the_same_event_kind(conn):
    tid = _gated(conn)
    kb.unblock_task(conn, tid)
    kb.archive_task(conn, tid)
    kb.delete_task(conn, tid)
    vias = {(e.payload or {}).get("via") for e in _refusals(conn, tid)}
    assert {"unblock_task", "archive_task", "delete_task"} <= vias
    for e in _refusals(conn, tid):
        assert (e.payload or {}).get("gate_state") == "plan"


# ---------------------------------------------------------------------------
# Dependency-edge routes (independent review round 2, finding 9)
# ---------------------------------------------------------------------------
#
# Removing an edge whose PARENT is gated is a gate release by another name: the
# parent stays gated but stops blocking the child, and unlink_tasks' own
# recompute_ready then promotes the child.


def test_unlink_refuses_while_the_parent_is_gated(conn):
    parent, child = _gated_parent_with_child(conn)
    assert kb.unlink_tasks(conn, parent, child) is False


def test_unlink_refusal_preserves_the_edge(conn):
    parent, child = _gated_parent_with_child(conn)
    kb.unlink_tasks(conn, parent, child)
    assert kb.parent_ids(conn, child) == [parent]
    assert kb.child_ids(conn, parent) == [child]


def test_unlink_refusal_leaves_the_parent_gated(conn):
    parent, child = _gated_parent_with_child(conn)
    kb.unlink_tasks(conn, parent, child)
    assert kb.get_task(conn, parent).status == "scheduled"
    assert kb.gate_state_of(conn, parent) == "plan"


def test_unlink_refusal_leaves_the_child_unpromoted(conn):
    """The actual bypass: the child must not become claimable."""
    parent, child = _gated_parent_with_child(conn)
    kb.unlink_tasks(conn, parent, child)
    kb.recompute_ready(conn)
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, child) is None


def test_unlink_refusal_is_audited(conn):
    parent, child = _gated_parent_with_child(conn)
    kb.unlink_tasks(conn, parent, child)
    assert _refusals(conn, parent, via="unlink_tasks")


def test_unlink_still_works_for_an_ungated_parent(conn):
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    assert kb.unlink_tasks(conn, parent, child) is True
    assert kb.parent_ids(conn, child) == []


def test_unlink_of_an_ungated_parent_still_promotes_an_eligible_child(conn):
    """The guard must not break the promotion this function exists to do."""
    parent = kb.create_task(conn, title="parent", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[parent])
    assert kb.get_task(conn, child).status == "todo"
    assert kb.unlink_tasks(conn, parent, child) is True
    assert kb.get_task(conn, child).status == "ready"


def test_unlink_of_a_missing_edge_is_unchanged(conn):
    a = kb.create_task(conn, title="a", assignee="a")
    b = kb.create_task(conn, title="b", assignee="a")
    assert kb.unlink_tasks(conn, a, b) is False


# --- documented policy: a gated CHILD's edges are NOT frozen -------------

def test_a_gated_childs_edges_remain_editable_and_release_nothing(conn):
    """Deliberate scope decision, not an oversight.

    Editing a gated child's edges cannot release it — it stays gated either
    way — and release_plan_gate re-evaluates _parents_satisfied at approval
    time, so the human's decision is applied against the graph as it stands
    when they approve.
    """
    other = kb.create_task(conn, title="other", assignee="a")
    child = kb.create_task(conn, title="child", assignee="a", parents=[other])
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p1','s','n',1,0,1)"
    )
    kb.park_for_plan_approval(conn, child, project_id="p1", revision=1)
    assert kb.unlink_tasks(conn, other, child) is True
    assert kb.gate_state_of(conn, child) == "plan"
    assert kb.get_task(conn, child).status == "scheduled"
    assert kb.claim_task(conn, child) is None


def test_linking_a_new_parent_onto_a_gated_task_is_allowed(conn):
    """Adding an edge can only ADD blocking, never remove it."""
    tid = _gated(conn)
    other = kb.create_task(conn, title="other", assignee="a")
    kb.link_tasks(conn, other, tid)
    assert kb.gate_state_of(conn, tid) == "plan"
    assert kb.claim_task(conn, tid) is None
