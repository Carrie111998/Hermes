"""Attestation broker and the artifact-bound release path.

WHAT THIS SUITE PROVES: an approval cannot be produced from an agent context,
forged, replayed, expired, applied to a different project or revision, applied
after the plan text changed, or redeemed for a decision the human did not
confirm — and that a valid decision updates the approval ledger, the plan row,
the task gate and the event atomically, or not at all.

THE SURFACE IS GONE, THE MACHINERY IS NOT. There is no local approval surface
any more: ``for_plan_decision`` fails closed because no separately authenticated
adapter is configured. Everything below the surface — binding, decision, nonce,
expiry, replay, atomicity — is unchanged and still proved here, because a future
authenticated adapter consumes exactly this machinery. Attestations are minted
directly through ``issue_attestation_for_adapter``, which is what such an adapter
will call; these tests exercise the consumer, not the (absent) surface.

NOTE: the earlier version of this file tested plan_binding_hash() in isolation
and called that "binding". It was not: nothing verified that release_plan_gate
CHECKED the hash, and it did not. Every binding test here goes through the
release path.
"""

import time

import pytest

from hermes_cli import approval_broker as ab
from hermes_cli import kanban_db as kb


REAL_BODY = "REAL PLAN: do the thing"


def _attest(project_id="p1", revision=1, body=REAL_BODY, decision="approved"):
    """Mint an attestation the way a future authenticated adapter will.

    Not a bypass of the surface — there is no surface. These tests are about
    what ``release_plan_gate`` does with an attestation, which is the part that
    survives the move to an external approval domain.
    """
    return ab.issue_attestation_for_adapter(
        project_id=project_id, revision=revision, plan_body=body,
        decision=decision, surface="test-adapter", operator_display="tester",
    )


@pytest.fixture
def conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_CRON_SESSION",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_SESSION_SOURCE",
                "HERMES_SESSION_PLATFORM"):
        monkeypatch.delenv(var, raising=False)


def _seed(conn, project_id="p1", revision=1, body=REAL_BODY, root=True):
    tid = kb.create_task(conn, title="work", assignee="a")
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES (?,?,?,?,0,1)", (project_id, project_id, "n", revision),
    )
    conn.execute(
        "INSERT INTO pm_plans (project_id, revision, body, proposed_at, root_task_id)"
        " VALUES (?,?,?,?,?)",
        (project_id, revision, body, 1, tid if root else None),
    )
    assert kb.park_for_plan_approval(
        conn, tid, project_id=project_id, revision=revision
    ) is True
    return tid


def _plan(conn, project_id="p1", revision=1):
    return conn.execute(
        "SELECT approved_at, approved_by, rejected_at, reject_reason "
        "FROM pm_plans WHERE project_id = ? AND revision = ?",
        (project_id, revision),
    ).fetchone()


def _approvals(conn):
    return conn.execute("SELECT COUNT(*) c FROM pm_approvals").fetchone()["c"]


def _refusal_reasons(conn, tid):
    return [
        (e.payload or {}).get("reason")
        for e in kb.list_events(conn, tid)
        if e.kind == "gate_release_refused"
    ]


# ================== no local approval authority ============================

def test_direct_construction_is_refused():
    """The dataclass still refuses to be built outside this module."""
    with pytest.raises(ab.ApprovalProvenanceError):
        ab.Attestation(
            subject="plan:p1:1", binding_hash="x", decision="approved",
            surface="cli-tty", operator_display="me", os_user="me", os_uid=1,
            host_id="h", tty_path="/dev/tty", issued_at=int(time.time()),
            nonce="n",
        )


def test_no_adapter_is_configured():
    """The default, and the reason every approval below fails."""
    assert ab.resolve_plan_approval_adapter() is None


def test_for_plan_decision_fails_closed():
    with pytest.raises(ab.NoApprovalSurfaceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved")


def test_the_refusal_explains_that_this_is_deliberate():
    """An operator hitting this must not think it is a misconfiguration."""
    with pytest.raises(ab.NoApprovalSurfaceError) as exc:
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved")
    message = str(exc.value)
    assert "no separately authenticated approval surface" in message
    assert "deliberate" in message


def test_rejection_also_fails_closed():
    """Reject is a gate release too, and gets no local authority either."""
    with pytest.raises(ab.NoApprovalSurfaceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="rejected")


def test_no_surface_error_is_still_an_approval_surface_error():
    """Callers that already handle a refusal keep working."""
    assert issubclass(ab.NoApprovalSurfaceError, ab.ApprovalSurfaceError)
    assert issubclass(ab.ApprovalSurfaceError, PermissionError)


def test_the_broker_exposes_no_tty_surface_at_all():
    """The forgeable surface is removed, not merely disabled.

    A disabled code path can be re-enabled by configuration or by a caller that
    passes the right argument. Absence cannot.
    """
    for gone in ("_open_controlling_tty", "_ControllingTTY", "_plan_banner",
                 "_controlling_tty_origin_reason"):
        assert not hasattr(ab, gone), f"{gone} still exists"
    import inspect
    assert "_tty_opener" not in inspect.signature(ab.for_plan_decision).parameters


def test_unknown_decision_is_rejected():
    with pytest.raises(ValueError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="maybe")
    with pytest.raises(ValueError):
        ab.issue_attestation_for_adapter(
            project_id="p1", revision=1, plan_body="b", decision="maybe",
            surface="s", operator_display="o")


# ============== advisory provenance (explicitly NOT the boundary) ===========

@pytest.mark.parametrize("var,val", [
    ("HERMES_KANBAN_TASK", "t1"),
    ("HERMES_KANBAN_RUN_ID", "r1"),
    ("HERMES_CRON_SESSION", "1"),
    ("HERMES_DELEGATED_CHILD_CONTEXT", "1"),
    ("HERMES_SESSION_SOURCE", "kanban"),
])
def test_agent_contexts_are_denied_by_the_advisory_check(monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    with pytest.raises(ab.ApprovalProvenanceError):
        ab.deny_agent_provenance()


def test_delegated_child_contextvar_is_denied():
    from agent.delegation_context import delegated_child_context

    with delegated_child_context():
        with pytest.raises(ab.ApprovalProvenanceError):
            ab.deny_agent_provenance()


def test_the_advisory_check_is_not_what_refuses_an_approval():
    """The refusal must not depend on recognising the caller.

    In a clean context the advisory check passes — and the approval is still
    refused. That is the property the whole redesign rests on: nothing is being
    detected, there is simply nowhere to approve.
    """
    ab.deny_agent_provenance()          # clean context: does not raise
    with pytest.raises(ab.NoApprovalSurfaceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved")


# ===================== binding, enforced at the gate =======================

def test_attestation_for_another_project_cannot_release(conn):
    tid = _seed(conn)
    a = _attest(project_id="unrelated-project", revision=99, body="UNRELATED")
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is False and "different plan" in why
    assert kb.gate_state_of(conn, tid) == "plan"
    assert _approvals(conn) == 0
    assert _plan(conn)["approved_at"] is None


def test_attestation_for_another_revision_cannot_release(conn):
    tid = _seed(conn)
    a = _attest(revision=2)
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is False and "different plan" in why
    assert kb.gate_state_of(conn, tid) == "plan"


def test_right_subject_wrong_body_hash_cannot_release(conn):
    tid = _seed(conn)
    a = _attest(body="A DIFFERENT PLAN")
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is False and "plan text changed" in why
    assert kb.gate_state_of(conn, tid) == "plan"
    assert _approvals(conn) == 0


def test_editing_the_plan_after_issuance_invalidates_the_attestation(conn):
    tid = _seed(conn)
    a = _attest()
    conn.execute("UPDATE pm_plans SET body = ? WHERE project_id='p1'", ("EDITED",))
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is False and "plan text changed" in why
    assert kb.gate_state_of(conn, tid) == "plan"


def test_advancing_the_project_revision_refuses_without_any_caller_hint(conn):
    """The stale check is authoritative, not an optional caller argument."""
    tid = _seed(conn)
    a = _attest()
    conn.execute("UPDATE pm_projects SET plan_revision = 2 WHERE id='p1'")
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is False and "stale plan" in why
    assert kb.gate_state_of(conn, tid) == "plan"


def test_plan_bound_to_another_root_task_is_refused(conn):
    tid = _seed(conn)
    other = kb.create_task(conn, title="other", assignee="a")
    conn.execute("UPDATE pm_plans SET root_task_id = ? WHERE project_id='p1'", (other,))
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "different root task" in why


def test_missing_plan_row_is_refused(conn):
    tid = _seed(conn)
    conn.execute("DELETE FROM pm_plans WHERE project_id='p1'")
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "no plan row" in why


def test_missing_project_is_refused(conn):
    tid = _seed(conn)
    conn.execute("DELETE FROM pm_projects WHERE id='p1'")
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "not found" in why


def test_malformed_gate_event_fails_closed(conn):
    tid = _seed(conn)
    conn.execute(
        "UPDATE task_events SET payload = ? WHERE task_id = ? "
        "AND kind = 'plan_awaiting_approval'", ("{not json", tid),
    )
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "malformed" in why


# ===================== decision binding ====================================

def test_a_rejection_attestation_rejects_and_cannot_approve(conn):
    tid = _seed(conn)
    ok, why = kb.release_plan_gate(
        conn, tid, attestation=_attest(decision="rejected"), reason="too broad"
    )
    assert ok is True and why is None
    assert kb.get_task(conn, tid).status == "triage"
    row = _plan(conn)
    assert row["rejected_at"] is not None and row["reject_reason"] == "too broad"
    assert row["approved_at"] is None


def test_the_decision_comes_from_the_attestation_not_the_caller(conn):
    """There is no decision= parameter to disagree with the human's choice."""
    import inspect
    assert "decision" not in inspect.signature(kb.release_plan_gate).parameters


def test_expected_revision_is_no_longer_an_overridable_parameter():
    import inspect
    assert "expected_revision" not in inspect.signature(kb.release_plan_gate).parameters


# ===================== atomicity ===========================================

def test_a_valid_approval_updates_all_four_stores(conn):
    tid = _seed(conn)
    a = _attest()
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is True and why is None
    assert kb.gate_state_of(conn, tid) is None
    assert kb.get_task(conn, tid).status == "ready"
    row = _plan(conn)
    assert row["approved_at"] is not None and row["approved_by"]
    assert row["rejected_at"] is None
    appr = conn.execute(
        "SELECT decision, subject FROM pm_approvals WHERE nonce = ?", (a.nonce,)
    ).fetchone()
    assert appr["decision"] == "approved" and appr["subject"] == "plan:p1:1"
    assert "plan_approved" in [e.kind for e in kb.list_events(conn, tid)]


def test_a_second_decision_on_the_same_plan_is_refused(conn):
    tid = _seed(conn)
    assert kb.release_plan_gate(conn, tid, attestation=_attest())[0] is True
    kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1)
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "already has a decision" in why


def test_a_replayed_attestation_is_refused_and_changes_nothing(conn):
    first = _seed(conn)
    a = _attest()
    assert kb.release_plan_gate(conn, first, attestation=a)[0] is True

    second = kb.create_task(conn, title="other", assignee="a")
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES ('p2','p2','n',1,0,1)"
    )
    conn.execute(
        "INSERT INTO pm_plans (project_id, revision, body, proposed_at)"
        " VALUES ('p2',1,?,1)", (REAL_BODY,),
    )
    kb.park_for_plan_approval(conn, second, project_id="p2", revision=1)
    before = _approvals(conn)
    ok, why = kb.release_plan_gate(conn, second, attestation=a)
    assert ok is False
    assert kb.gate_state_of(conn, second) == "plan"
    assert _approvals(conn) == before
    assert _plan(conn, "p2")["approved_at"] is None


def test_an_expired_attestation_changes_nothing(conn):
    tid = _seed(conn)
    a = _attest()
    object.__setattr__(a, "issued_at", int(time.time()) - ab.DEFAULT_TTL_SECONDS - 5)
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    assert ok is False and "expired" in why
    assert kb.gate_state_of(conn, tid) == "plan"
    assert _approvals(conn) == 0
    assert _plan(conn)["approved_at"] is None


def test_every_binding_failure_leaves_all_three_stores_unchanged(conn):
    """Task gate, approval ledger and plan row must move together or not at all."""
    tid = _seed(conn)
    for att in (
        _attest(project_id="other", revision=1),
        _attest(revision=5),
        _attest(body="DIFFERENT"),
    ):
        ok, _ = kb.release_plan_gate(conn, tid, attestation=att)
        assert ok is False
        assert kb.gate_state_of(conn, tid) == "plan"
        assert kb.get_task(conn, tid).status == "scheduled"
        assert _approvals(conn) == 0
        assert _plan(conn)["approved_at"] is None
        assert _plan(conn)["rejected_at"] is None


def test_a_concurrent_gate_release_rolls_the_approval_back(conn):
    """CAS failure on the task must undo the approval row and plan update."""
    tid = _seed(conn)
    conn.execute("UPDATE tasks SET gate_state = NULL WHERE id = ?", (tid,))
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False
    assert _approvals(conn) == 0
    assert _plan(conn)["approved_at"] is None


# ===================== auditing ============================================

def test_refusals_are_audited(conn):
    tid = _seed(conn)
    kb.release_plan_gate(conn, tid, attestation=_attest(body="DIFFERENT"))
    assert any("plan text changed" in (r or "") for r in _refusal_reasons(conn, tid))


def test_expiry_refusal_is_audited(conn):
    tid = _seed(conn)
    a = _attest()
    object.__setattr__(a, "issued_at", int(time.time()) - ab.DEFAULT_TTL_SECONDS - 5)
    kb.release_plan_gate(conn, tid, attestation=a)
    assert any("expired" in (r or "") for r in _refusal_reasons(conn, tid))


def test_events_never_carry_the_attestation_nonce(conn):
    tid = _seed(conn)
    a = _attest()
    kb.release_plan_gate(conn, tid, attestation=_attest(body="DIFFERENT"))
    kb.release_plan_gate(conn, tid, attestation=a)
    blob = repr([e.payload for e in kb.list_events(conn, tid)])
    assert a.nonce not in blob


def test_a_worker_cannot_obtain_an_approval(conn, monkeypatch):
    """A worker gets no approval — and not because it was recognised.

    The previous version of this test set HERMES_KANBAN_TASK and asserted the
    mint refused, i.e. it proved a heuristic fired. That heuristic was erasable
    (``env -u HERMES_KANBAN_TASK``), so the test proved less than it looked.
    Now the worker is refused because no approval surface exists, which no
    environment change can alter — asserted below both with and without the
    marker set.
    """
    tid = _seed(conn)
    for marker_set in (True, False):
        if marker_set:
            monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        else:
            monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        with pytest.raises(ab.NoApprovalSurfaceError):
            ab.for_plan_decision(project_id="p1", revision=1,
                                 plan_body=REAL_BODY, decision="approved")
    assert kb.gate_state_of(conn, tid) == "plan"


# ============ round-3 findings: fail-open conditions ========================


def _unbound_seed(conn, project_id="p1", revision=1, body=REAL_BODY):
    """A plan row deliberately left with root_task_id NULL."""
    tid = kb.create_task(conn, title="work", assignee="a")
    conn.execute(
        "INSERT INTO pm_projects (id, slug, name, plan_revision, archived, created_at)"
        " VALUES (?,?,?,?,0,1)", (project_id, project_id, "n", revision),
    )
    conn.execute(
        "INSERT INTO pm_plans (project_id, revision, body, proposed_at, root_task_id)"
        " VALUES (?,?,?,1,NULL)", (project_id, revision, body),
    )
    return tid


# --- P1: an absent root binding is not a satisfied one --------------------

def test_a_null_root_task_binding_is_refused(conn):
    """`if x and x != y` made NULL mean "skip verification"."""
    tid = _unbound_seed(conn)
    # Park, then clear the binding park would have set, to model a plan row
    # that reached the gate without an authoritative task link.
    kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1)
    conn.execute("UPDATE pm_plans SET root_task_id = NULL WHERE project_id='p1'")

    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "no root task binding" in why
    assert kb.gate_state_of(conn, tid) == "plan"
    assert kb.get_task(conn, tid).status == "scheduled"
    assert _approvals(conn) == 0
    row = _plan(conn)
    assert row["approved_at"] is None and row["rejected_at"] is None


def test_an_empty_root_task_binding_is_refused(conn):
    tid = _unbound_seed(conn)
    kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1)
    conn.execute("UPDATE pm_plans SET root_task_id = '' WHERE project_id='p1'")
    ok, why = kb.release_plan_gate(conn, tid, attestation=_attest())
    assert ok is False and "no root task binding" in why
    assert kb.gate_state_of(conn, tid) == "plan"


def test_a_null_root_refusal_is_audited_without_the_nonce(conn):
    tid = _unbound_seed(conn)
    kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1)
    conn.execute("UPDATE pm_plans SET root_task_id = NULL WHERE project_id='p1'")
    a = _attest()
    kb.release_plan_gate(conn, tid, attestation=a)
    assert any("root task binding" in (r or "") for r in _refusal_reasons(conn, tid))
    assert a.nonce not in repr([e.payload for e in kb.list_events(conn, tid)])


def test_parking_binds_an_unbound_plan_to_the_task(conn):
    """The normal flow populates the binding, so valid approvals still work."""
    tid = _unbound_seed(conn)
    assert kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1) is True
    row = conn.execute(
        "SELECT root_task_id FROM pm_plans WHERE project_id='p1'"
    ).fetchone()
    assert row["root_task_id"] == tid
    assert kb.release_plan_gate(conn, tid, attestation=_attest())[0] is True


def test_parking_refuses_a_plan_already_bound_elsewhere(conn):
    tid = _unbound_seed(conn)
    other = kb.create_task(conn, title="other", assignee="a")
    conn.execute(
        "UPDATE pm_plans SET root_task_id = ? WHERE project_id='p1'", (other,)
    )
    assert kb.park_for_plan_approval(conn, tid, project_id="p1", revision=1) is False
    assert kb.gate_state_of(conn, tid) is None


# --- P2: expiry must be evaluated under the write lock --------------------

def test_an_attestation_that_expires_while_waiting_for_the_lock_is_refused(
    conn, monkeypatch
):
    """Valid at entry, expired by the time the write lock is acquired.

    Models a slow writer ahead of us: the pre-lock check passed, the wait
    outlasted the TTL, and the transaction then authorised anyway.
    """
    tid = _seed(conn)
    a = _attest()

    real_time = time.time
    state = {"advanced": False}

    def fake_time():
        # First call inside the transaction jumps past the TTL, exactly as a
        # long wait behind another writer would.
        if not state["advanced"]:
            state["advanced"] = True
            return real_time() + ab.DEFAULT_TTL_SECONDS + 60
        return real_time() + ab.DEFAULT_TTL_SECONDS + 60

    monkeypatch.setattr(kb.time, "time", fake_time)
    ok, why = kb.release_plan_gate(conn, tid, attestation=a)
    monkeypatch.undo()

    assert ok is False and "expired" in why
    assert kb.gate_state_of(conn, tid) == "plan"
    assert kb.get_task(conn, tid).status == "scheduled"
    assert _approvals(conn) == 0
    row = _plan(conn)
    assert row["approved_at"] is None and row["rejected_at"] is None
    assert any("expired" in (r or "") for r in _refusal_reasons(conn, tid))


def test_a_valid_approval_uses_one_clock_reading_for_every_timestamp(conn):
    tid = _seed(conn)
    assert kb.release_plan_gate(conn, tid, attestation=_attest())[0] is True
    appr = conn.execute("SELECT created_at FROM pm_approvals").fetchone()
    plan = _plan(conn)
    assert appr["created_at"] == plan["approved_at"]
