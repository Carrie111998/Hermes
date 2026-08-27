"""Attestation broker and the artifact-bound release path.

WHAT THIS SUITE PROVES: an approval cannot be produced from an agent context,
forged, replayed, expired, applied to a different project or revision, applied
after the plan text changed, or redeemed for a decision the human did not
confirm — and that a valid decision updates the approval ledger, the plan row,
the task gate and the event atomically, or not at all.

WHAT IT DOES NOT PROVE: that a deliberately adversarial process running as the
same OS user cannot approve. Hermes agents run as the user with terminal access;
a process that allocates a pty and drives the prompt defeats the TTY
constructor. That is a stated limit of the design.

NOTE: the earlier version of this file tested plan_binding_hash() in isolation
and called that "binding". It was not: nothing verified that release_plan_gate
CHECKED the hash, and it did not. Every binding test here goes through the
release path.
"""

import time

import pytest

from hermes_cli import approval_broker as ab
from hermes_cli import kanban_db as kb


class FakeTTY:
    name = "/dev/tty"

    def __init__(self, typed):
        self._typed = typed
        self.written = []
        self.closed = False

    def write(self, s):
        self.written.append(s)

    def flush(self):
        pass

    def readline(self):
        return self._typed + "\n"

    def close(self):
        self.closed = True


def _tty(typed):
    return lambda: FakeTTY(typed)


def _no_tty():
    def opener():
        raise OSError(6, "Device not configured")
    return opener


REAL_BODY = "REAL PLAN: do the thing"


def _attest(project_id="p1", revision=1, body=REAL_BODY, decision="approved",
            typed=None):
    return ab.for_plan_decision(
        project_id=project_id, revision=revision, plan_body=body,
        decision=decision,
        _tty_opener=_tty(typed if typed is not None
                         else ab.CONFIRM_PHRASES.get(decision, "?")),
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


# =========================== broker construction ===========================

def test_direct_construction_is_refused():
    with pytest.raises(ab.ApprovalProvenanceError):
        ab.Attestation(
            subject="plan:p1:1", binding_hash="h", decision="approved",
            surface="cli-tty", operator_display="r", os_user="r", os_uid=1,
            host_id="h", tty_path=None, issued_at=int(time.time()), nonce="n",
        )


@pytest.mark.parametrize("var,val", [
    ("HERMES_KANBAN_TASK", "t_abc"),
    ("HERMES_KANBAN_RUN_ID", "7"),
    ("HERMES_CRON_SESSION", "1"),
    ("HERMES_DELEGATED_CHILD_CONTEXT", "1"),
    ("HERMES_SESSION_SOURCE", "kanban"),
    ("HERMES_SESSION_PLATFORM", "telegram"),
])
def test_agent_contexts_are_denied(monkeypatch, var, val):
    monkeypatch.setenv(var, val)
    with pytest.raises(ab.ApprovalProvenanceError):
        _attest()


def test_delegated_child_contextvar_is_denied():
    from agent.delegation_context import delegated_child_context
    with delegated_child_context():
        with pytest.raises(ab.ApprovalProvenanceError):
            _attest()


def test_provenance_is_checked_before_the_tty_is_touched(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc")
    tty = FakeTTY("approve")
    with pytest.raises(ab.ApprovalProvenanceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved", _tty_opener=lambda: tty)
    assert tty.written == []


def test_no_controlling_terminal_is_refused():
    with pytest.raises(ab.ApprovalSurfaceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved", _tty_opener=_no_tty())


def test_wrong_phrase_is_refused():
    with pytest.raises(ab.ApprovalSurfaceError):
        _attest(typed="yes")


def test_approve_phrase_cannot_confirm_a_rejection():
    """Each decision has its own phrase; they are not interchangeable."""
    with pytest.raises(ab.ApprovalSurfaceError):
        _attest(decision="rejected", typed="approve")


def test_the_prompt_names_the_decision_project_and_revision():
    tty = FakeTTY("approve")
    ab.for_plan_decision(project_id="p1", revision=7, plan_body="b",
                         decision="approved", _tty_opener=lambda: tty)
    text = "".join(tty.written)
    assert "APPROVE" in text and "p1" in text and "7" in text


def test_the_rejection_prompt_says_reject():
    tty = FakeTTY("reject")
    ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                         decision="rejected", _tty_opener=lambda: tty)
    assert "REJECT" in "".join(tty.written)


def test_prompt_is_written_to_the_tty_not_stdout(capsys):
    tty = FakeTTY("approve")
    ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                         decision="approved", _tty_opener=lambda: tty)
    assert "APPROVE" not in capsys.readouterr().out


def test_tty_is_closed_even_on_refusal():
    tty = FakeTTY("no")
    with pytest.raises(ab.ApprovalSurfaceError):
        ab.for_plan_decision(project_id="p1", revision=1, plan_body="b",
                             decision="approved", _tty_opener=lambda: tty)
    assert tty.closed


def test_unknown_decision_is_rejected():
    with pytest.raises(ValueError):
        _attest(decision="maybe")


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


def test_a_worker_cannot_mint_the_attestation_it_would_need(conn, monkeypatch):
    tid = _seed(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    with pytest.raises(ab.ApprovalProvenanceError):
        _attest()
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
