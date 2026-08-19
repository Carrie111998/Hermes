"""Durable delegation supervision contract — 16-case integration matrix."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_supervisor as sup
from hermes_cli import kanban_supervision_contract as contract
from hermes_cli.kanban_budget_keepalive import (
    RecordingRemokoClient,
    record_kanban_budget_exhausted,
)
from tests.hermes_cli.test_kanban_supervisor import FakeRemoko, _init_git_head


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_OBJECTIVE_ID", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _force_ready(conn, task_id: str) -> None:
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL WHERE id=?",
            (task_id,),
        )


def _seed_child_proof(
    conn, oid: str, child_id: str, *, head: str | None = None, workspace=None,
) -> str:
    if workspace is not None:
        repo = Path(workspace)
        if (repo / ".git").exists():
            import subprocess

            live = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True,
            ).stdout.strip()
        else:
            live = _init_git_head(repo)
        if head is None:
            head = live
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id=?",
            (str(repo), child_id),
        )
    if not head:
        head = "a" * 40
    sup.upsert_unit(
        conn,
        objective_id=oid,
        kind="kanban",
        ref=child_id,
        status="pending",
        terminal_predicate="task_done_with_proof",
        proof={"type": "exact_run", "head": head, "verdict": "pass"},
    )
    packet = contract.build_canonical_evidence(conn, child_id, objective_id=oid)
    return contract.canonical_evidence_hash(packet)


def _seed_durable_origin(conn, oid: str, task_id: str, *, chat_id: str = "origin-live") -> None:
    conn.execute(
        "UPDATE kanban_objectives SET origin_platform=?, origin_chat_id=?, "
        "origin_session_key=? WHERE id=?",
        ("webui", chat_id, chat_id, oid),
    )
    kb.add_notify_sub(
        conn, task_id=task_id, platform="webui",
        chat_id=chat_id, delivery_mode="notify+wake",
        delivery_metadata={"session_key": chat_id},
    )


def _graph(conn, *, parent_assignee="default", child_assignee="cole"):
    parent = kb.create_task(conn, title="supervisor", assignee=parent_assignee)
    child = kb.create_task(
        conn, title="descendant", assignee=child_assignee, parents=[parent],
    )
    oid = sup.ensure_objective(conn, parent)
    return parent, child, oid


def test_child_self_close(kanban_home, monkeypatch):
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        kb.complete_task(conn, parent, summary="parent launched")
        kb.recompute_ready(conn)
        _force_ready(conn, child)
        claimed = kb.claim_task(conn, child)
        assert claimed is not None
        monkeypatch.setenv("HERMES_KANBAN_TASK", child)
        from tools import kanban_tools as kt

        out = json.loads(kt._handle_complete({"summary": "child finished"}))
        assert out.get("ok") is True
        assert kb.get_task(conn, child).status == "done"
        assert not contract.task_has_live_claim(conn, child)


def test_parent_verified_descendant_close(kanban_home, monkeypatch):
    with kb.connect() as conn:
        a = kb.create_task(conn, title="root-a", assignee="default")
        b = kb.create_task(conn, title="root-b", assignee="default")
        mid = kb.create_task(conn, title="mid", assignee="cole", parents=[a, b])
        leaf = kb.create_task(conn, title="leaf", assignee="cole", parents=[mid])
        assert contract.is_graph_descendant(conn, a, leaf)
        assert contract.is_graph_descendant(conn, b, leaf)
        assert contract.is_graph_descendant(conn, mid, leaf)
        assert not contract.is_graph_descendant(conn, leaf, a)
        oid = sup.ensure_objective(conn, a)
        digest = _seed_child_proof(conn, oid, leaf, workspace=kanban_home.parent / "repo-leaf")
        issued = contract.issue_descendant_grant(
            conn,
            objective_id=oid,
            supervisor_task_id=a,
            descendant_task_id=leaf,
            transition="complete",
            evidence_hash=digest,
            caller_task_id=a,
        )
        assert issued["ok"] is True
        again = contract.issue_descendant_grant(
            conn,
            objective_id=oid,
            supervisor_task_id=a,
            descendant_task_id=leaf,
            transition="complete",
            evidence_hash=digest,
            caller_task_id=a,
        )
        assert again["ok"] is True
        assert again["grant_id"] == issued["grant_id"]
        closed = contract.reconcile_descendant(
            conn,
            supervisor_task_id=a,
            descendant_task_id=leaf,
            transition="complete",
            evidence_hash=digest,
            caller_task_id=a,
            objective_id=oid,
        )
        assert closed["ok"] is True
        assert closed["consumed"] is True
        assert kb.get_task(conn, leaf).status == "done"
        assert kb.get_task(conn, a).status != "done"
        replay = contract.reconcile_descendant(
            conn,
            supervisor_task_id=a,
            descendant_task_id=leaf,
            transition="complete",
            evidence_hash=digest,
            caller_task_id=a,
            objective_id=oid,
        )
        assert replay["ok"] is False
        assert "consumed" in replay["error"]


def test_unrelated_worker_rejected(kanban_home, monkeypatch):
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        stranger = kb.create_task(conn, title="stranger", assignee="turing")
        digest = _seed_child_proof(conn, oid, child)
        monkeypatch.setenv("HERMES_KANBAN_TASK", stranger)
        denied = contract.issue_descendant_grant(
            conn,
            objective_id=oid,
            supervisor_task_id=parent,
            descendant_task_id=child,
            transition="complete",
            evidence_hash=digest,
            caller_task_id=parent,
        )
        assert denied["ok"] is False
        assert "scoped to task" in denied["error"]
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        from tools import kanban_tools as kt

        out = json.loads(kt._handle_complete({
            "task_id": child, "summary": "HIJACK",
        }))
        assert out.get("ok") is not True
        expected = (
            f"worker is scoped to task {parent}; refusing to mutate {child}"
        )
        assert expected in out.get("error", "")
        assert kb.get_task(conn, child).status != "done"


def test_child_process_exit_before_result(kanban_home, monkeypatch):
    with kb.connect() as conn:
        parent, _child, oid = _graph(conn)
        kb.block_task(conn, parent, reason="waiting on child", kind="needs_input")
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        monkeypatch.setenv("HERMES_OBJECTIVE_ID", oid)
    sup.note_delegate_spawn(subagent_id="sa-exit", owner_profile="cole")
    sup.note_delegate_stop(subagent_id="sa-exit")
    with kb.connect() as conn:
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units["sa-exit"]["status"] == "awaiting_verification"
        proof = json.loads(units["sa-exit"]["proof"])
        assert proof["classification"] == contract.CLASS_NO_OUTPUT
        assert proof["verified"] is False
        assert not sup.objective_is_complete(conn, oid)
        assert kb.get_task(conn, parent).status != "blocked"


def test_timeout_requeue_continuation(kanban_home):
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        kb.complete_task(conn, parent, summary="launched")
        kb.recompute_ready(conn)
        _force_ready(conn, child)
        assert kb.claim_task(conn, child) is not None
        result = contract.requeue_after_timeout(conn, child)
        assert result["ok"] is True
        assert kb.get_task(conn, child).status == "ready"
        assert not contract.task_has_live_claim(conn, child)
        unit = next(u for u in sup.list_units(conn, oid) if u["ref"] == child)
        assert unit["next_gate"] == "timeout_requeue"


def test_parent_budget_burn_while_child_active(kanban_home):
    remoko = RecordingRemokoClient()
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        _force_ready(conn, parent)
        assert kb.claim_task(conn, parent) is not None
        record_kanban_budget_exhausted(
            conn, parent, budget_used=90, budget_max=90, remoko_client=remoko,
        )
        assert kb.get_task(conn, child).status != "done"
        assert not sup.objective_is_complete(conn, oid)
        assert len(remoko.ask_calls) == 1
        record_kanban_budget_exhausted(
            conn, parent, budget_used=90, budget_max=90, remoko_client=remoko,
        )
        assert len(remoko.ask_calls) == 1


def test_missing_origin_session_after_remoko(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        parent, _child, oid = _graph(conn)
        rid = sup.request_owner_blocker(
            conn,
            objective_id=oid,
            task_id=parent,
            decision_key="external_blocker",
            purpose="Need an owner choice.",
            choices=["Retry now", "Fix credentials", "Reroute owner", "Stop"],
            remoko=remoko,
        )
        assert rid
        conn.execute(
            "UPDATE kanban_objectives SET origin_platform=NULL, origin_chat_id=NULL, "
            "origin_session_key=NULL WHERE id=?",
            (oid,),
        )
        conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=?", (parent,))
        assert contract.missing_origin_after_remoko(conn, oid) is True


def test_direct_fallback_answer_wakes_durable_owner(kanban_home):
    with kb.connect() as conn:
        parent, _child, oid = _graph(conn)
        kb.add_notify_sub(
            conn, task_id=parent, platform="webui",
            chat_id="origin-live", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "origin-live"},
        )
        kb.block_task(conn, parent, reason="owner fork", kind="needs_input")
        assert kb.get_task(conn, parent).status == "blocked"
        result = contract.ingest_direct_fallback_answer(
            conn, objective_id=oid, task_id=parent, answer="Retry now",
        )
        assert result["ok"] is True
        assert kb.get_task(conn, parent).status in {"ready", "todo"}
        kinds = [e.kind for e in kb.list_events(conn, parent)]
        assert "status" in kinds
        obj = sup.get_objective(conn, oid)
        assert obj["status"] == "open"


def test_stale_transcript_not_pending_after_processed(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        parent, _child, oid = _graph(conn)
        rid = sup.request_owner_blocker(
            conn,
            objective_id=oid,
            task_id=parent,
            decision_key="review_cap",
            purpose="cap",
            choices=list(contract.REVIEW_CAP_CHOICES),
            remoko=remoko,
        )
        assert rid
        remoko.requests[rid]["status"] = "pending"
        contract.mark_owner_blocker_processed(
            conn, objective_id=oid, request_id=rid,
        )
        assert contract.owner_blocker_is_pending(conn, oid) is False


def test_same_and_different_profile_parent_child(kanban_home):
    with kb.connect() as conn:
        same_p, same_c, same_oid = _graph(conn, parent_assignee="cole", child_assignee="cole")
        diff_p, diff_c, diff_oid = _graph(conn, parent_assignee="default", child_assignee="cole")
        for parent, child, oid in (
            (same_p, same_c, same_oid),
            (diff_p, diff_c, diff_oid),
        ):
            digest = _seed_child_proof(
                conn, oid, child, workspace=kanban_home.parent / "repo-profiles",
            )
            issued = contract.issue_descendant_grant(
                conn,
                objective_id=oid,
                supervisor_task_id=parent,
                descendant_task_id=child,
                transition="complete",
                evidence_hash=digest,
                caller_task_id=parent,
            )
            assert issued["ok"] is True, issued
            closed = contract.reconcile_descendant(
                conn,
                supervisor_task_id=parent,
                descendant_task_id=child,
                transition="complete",
                evidence_hash=digest,
                caller_task_id=parent,
                objective_id=oid,
            )
            assert closed["ok"] is True
            assert kb.get_task(conn, child).status == "done"


def test_bot_chat_success_failure_malformed_no_output(kanban_home, monkeypatch):
    assert contract.classify_terminal_result(None) == contract.CLASS_NO_OUTPUT
    assert contract.classify_terminal_result("") == contract.CLASS_NO_OUTPUT
    assert contract.classify_terminal_result("not-json") == contract.CLASS_MALFORMED
    assert contract.classify_terminal_result({"ok": False}) == contract.CLASS_FAILURE
    assert contract.classify_terminal_result({"status": "fail"}) == contract.CLASS_FAILURE
    assert contract.classify_terminal_result(
        {"ok": True, "blockers": ["p1"]}
    ) == contract.CLASS_FAILURE
    assert contract.classify_terminal_result({"ok": True, "status": "pass"}) == contract.CLASS_SUCCESS

    with kb.connect() as conn:
        parent = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, parent)
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        monkeypatch.setenv("HERMES_OBJECTIVE_ID", oid)
        kb.block_task(conn, parent, reason="wait", kind="needs_input")
    cases = {
        "sess-success": {"ok": True, "status": "pass"},
        "sess-fail": {"ok": False, "status": "fail"},
        "sess-bad": "????",
        "sess-empty": None,
    }
    for sid, result in cases.items():
        sup.note_bot_chat_handoff(session_id=sid, title="Bot Chat")
        sup.note_bot_chat_complete(session_id=sid, owner_profile="jude", result=result)
    with kb.connect() as conn:
        bots = [u for u in sup.list_units(conn, oid) if u["kind"] == "bot_chat"]
        assert len(bots) == 4
        by_ref = {u["ref"].split(":")[-1]: u for u in bots}
        assert all(u["status"] == "awaiting_verification" for u in bots)
        for sid, expected in (
            ("sess-success", contract.CLASS_SUCCESS),
            ("sess-fail", contract.CLASS_FAILURE),
            ("sess-bad", contract.CLASS_MALFORMED),
            ("sess-empty", contract.CLASS_NO_OUTPUT),
        ):
            proof = json.loads(by_ref[sid]["proof"])
            assert proof["classification"] == expected
            digest = contract.canonical_evidence_hash(contract.process_exit_evidence(proof))
            verified = contract.verify_process_exit(
                conn, kind="bot_chat", ref=sid, evidence_hash=digest,
            )
            assert verified["ok"] is True
            if expected == contract.CLASS_SUCCESS:
                assert verified["status"] == "done"
            else:
                assert verified["status"] in {"failed", "pending"}
                assert verified["verified"] is False
        assert not sup.objective_is_complete(conn, oid)


def test_judge_fail_correction_exact_head_pass(kanban_home):
    old = "a" * 40
    new = "b" * 40
    moved = "c" * 40
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id=?",
            ("/tmp/fake-repo", child),
        )
        first = contract.record_review_verdict(
            conn, task_id=child, verdict="fail", head=old, blockers=["p1"],
        )
        assert first["review_cap"] is False
        stale = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=old,
            current_head=new, git_head_fn=lambda _p: new,
        )
        assert stale["invalidated"] is True
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units[child]["status"] != "done"
        passed = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=new,
            current_head=new, git_head_fn=lambda _p: new,
        )
        assert passed["verdict"] == "pass"
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units[child]["status"] == "done"
        moved_ids = sup.invalidate_stale_reviews(
            conn, git_head_fn=lambda path: moved if path == "/tmp/fake-repo" else None,
        )
        assert child in moved_ids
        assert not sup.objective_is_complete(conn, oid)


def test_review_cap_one_remoko_then_resume(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        _seed_durable_origin(conn, oid, parent)
        last = None
        for i in range(5):
            last = contract.record_review_verdict(
                conn, task_id=child, verdict="fail", head=f"h{i}",
                blockers=["p1"], remoko=remoko,
            )
        assert last["review_cap"] is True
        assert last["request_id"]
        assert len(remoko.calls) == 1
        assert remoko.calls[0]["choices"] == list(contract.REVIEW_CAP_CHOICES)
        again = contract.record_review_verdict(
            conn, task_id=child, verdict="fail", head="h5",
            blockers=["p1"], remoko=remoko,
        )
        assert again["request_id"] == last["request_id"]
        assert len(remoko.calls) == 1
        remoko.answer(last["request_id"], "Add 5 reviews")
        policy = contract.apply_review_cap_answer(conn, child, "Add 5 reviews")
        assert policy == "add5"
        resumed = contract.record_review_verdict(
            conn, task_id=child, verdict="fail", head="h6",
            blockers=["p1"], remoko=remoko,
        )
        assert resumed["review_cap"] is False
        assert len(remoko.calls) == 1


def test_retries_crash_no_duplicate_child_review_request(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        parent = kb.create_task(
            conn, title="parent", assignee="default",
            idempotency_key="obj-supervision",
        )
        again = kb.create_task(
            conn, title="parent", assignee="default",
            idempotency_key="obj-supervision",
        )
        assert parent == again
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[parent],
            idempotency_key="obj-supervision-child",
        )
        assert kb.create_task(
            conn, title="child", assignee="cole", parents=[parent],
            idempotency_key="obj-supervision-child",
        ) == child
        oid = sup.ensure_objective(conn, parent)
        digest = _seed_child_proof(
            conn, oid, child, workspace=kanban_home.parent / "repo-retries",
        )
        first = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest, caller_task_id=parent,
        )
        second = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest, caller_task_id=parent,
        )
        assert first["grant_id"] == second["grant_id"]
        rid1 = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=parent,
            decision_key="review_cap", purpose="cap",
            choices=list(contract.REVIEW_CAP_CHOICES), remoko=remoko,
        )
        rid2 = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=parent,
            decision_key="review_cap", purpose="cap",
            choices=list(contract.REVIEW_CAP_CHOICES), remoko=remoko,
        )
        assert rid1 == rid2
        assert len(remoko.calls) == 1


def test_external_blocker_one_remoko_parks(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        parent, _child, oid = _graph(conn)
        rid = sup.request_owner_blocker(
            conn,
            objective_id=oid,
            task_id=parent,
            decision_key="guard_blocker_auth",
            purpose="Delegated work hit a wall that needs an owner decision.",
            choices=["Retry now", "Fix credentials", "Reroute owner", "Stop"],
            remoko=remoko,
        )
        assert rid
        obj = sup.get_objective(conn, oid)
        assert obj["status"] == "blocked_owner"
        assert kb.get_task(conn, parent).status == "blocked"
        assert sup.request_owner_blocker(
            conn,
            objective_id=oid,
            task_id=parent,
            decision_key="guard_blocker_auth",
            purpose="Delegated work hit a wall that needs an owner decision.",
            remoko=remoko,
        ) == rid
        assert len(remoko.calls) == 1
        assert not sup.objective_is_complete(conn, oid)


def test_e2e_objective_done_only_after_all_gates_survives_reopen(kanban_home, monkeypatch):
    remoko = FakeRemoko()
    live = _init_git_head(kanban_home.parent / "repo")
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id IN (?, ?)",
            (str(kanban_home.parent / "repo"), parent, child),
        )
        kb.add_notify_sub(
            conn, task_id=parent, platform="webui",
            chat_id="origin-live", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "origin-live"},
        )
        monkeypatch.setenv("HERMES_KANBAN_TASK", parent)
        monkeypatch.setenv("HERMES_OBJECTIVE_ID", oid)
        kb.block_task(conn, parent, reason="wait", kind="needs_input")
    sup.note_delegate_spawn(subagent_id="sa-e2e", owner_profile="cole")
    sup.note_delegate_stop(subagent_id="sa-e2e", result={"ok": False, "status": "fail"})

    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units["sa-e2e"]["status"] == "awaiting_verification"
        proof = json.loads(units["sa-e2e"]["proof"])
        digest = contract.canonical_evidence_hash(contract.process_exit_evidence(proof))
        verified = contract.verify_process_exit(
            conn, kind="delegate_task", ref="sa-e2e", evidence_hash=digest,
        )
        assert verified["status"] == "failed"
        contract.record_review_verdict(
            conn, task_id=child, verdict="fail", head="0" * 40, blockers=["p1"],
        )
        contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=live, current_head=live,
        )
        for i in range(5):
            last = contract.record_review_verdict(
                conn, task_id=parent, verdict="fail", head=f"p{i}",
                blockers=["p1"], remoko=remoko,
            )
        assert last["review_cap"] is True
        remoko.answer(last["request_id"], "Add 5 reviews")
        contract.apply_review_cap_answer(conn, parent, "Add 5 reviews")
        contract.record_review_verdict(
            conn, task_id=parent, verdict="pass", head=live, current_head=live,
        )
        digest = _seed_child_proof(conn, oid, child, head=live)
        issued = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest, caller_task_id=parent,
        )
        assert issued["ok"] is True
        closed = contract.reconcile_descendant(
            conn, supervisor_task_id=parent, descendant_task_id=child,
            transition="complete", evidence_hash=digest,
            caller_task_id=parent, objective_id=oid,
        )
        assert closed["ok"] is True
        kb.complete_task(conn, parent, summary="supervisor done")
        assert not sup.objective_is_complete(conn, oid)

    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units[child]["status"] == "done"
        assert units["sa-e2e"]["status"] == "failed"
        assert not sup.objective_is_complete(conn, oid)
        # Correction of the failed child process, then exact-head pass.
        success_proof = {
            "terminal": "process_exit",
            "classification": contract.CLASS_SUCCESS,
            "result": {"ok": True, "status": "pass"},
            "child_status": "completed",
            "verified": False,
        }
        sup.upsert_unit(
            conn, objective_id=oid, kind="delegate_task", ref="sa-e2e",
            status="awaiting_verification", proof=success_proof,
            terminal_predicate="child_completed",
        )
        digest = contract.canonical_evidence_hash(
            contract.process_exit_evidence(success_proof)
        )
        assert contract.verify_process_exit(
            conn, kind="delegate_task", ref="sa-e2e", evidence_hash=digest,
        )["status"] == "done"
        status = sup.reconcile_objective(conn, oid)
        assert status == "done"
        assert sup.objective_is_complete(conn, oid)
        assert len(remoko.calls) == 1


def test_failed_run_without_canonical_proof_cannot_issue_or_consume_grant(kanban_home, monkeypatch):
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        now = int(__import__("time").time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, error) "
                "VALUES (?, 'failed', 'failed', ?, ?, 'boom')",
                (child, now - 10, now),
            )
            conn.execute(
                "UPDATE tasks SET status='blocked', current_run_id=NULL, "
                "claim_lock=NULL, claim_expires=NULL WHERE id=?",
                (child,),
            )
        packet = contract.build_canonical_evidence(conn, child, objective_id=oid)
        assert packet.get("run_id")
        assert contract.persisted_proof_present(packet) is False
        issued = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=contract.canonical_evidence_hash(packet),
            caller_task_id=parent,
        )
        assert issued["ok"] is False
        assert kb.get_task(conn, child).status != "done"


def _shared_descendant_two_objectives(conn, workspace):
    """Same descendant in two objectives; A has the newest exact proof, B has none."""
    parent_a = kb.create_task(conn, title="supervisor-a", assignee="default")
    parent_b = kb.create_task(conn, title="supervisor-b", assignee="default")
    child = kb.create_task(
        conn, title="shared-descendant", assignee="cole",
        parents=[parent_a, parent_b],
        workspace_kind="dir", workspace_path=str(workspace),
    )
    oid_a = sup.ensure_objective(conn, parent_a)
    oid_b = sup.ensure_objective(conn, parent_b)
    sup.upsert_unit(conn, objective_id=oid_b, kind="kanban", ref=child, status="pending")
    digest_a = _seed_child_proof(conn, oid_a, child, workspace=workspace)
    return parent_a, parent_b, child, oid_a, oid_b, digest_a


def _unit_proof(conn, oid: str, ref: str) -> dict:
    units = {u["ref"]: u for u in sup.list_units(conn, oid)}
    raw = (units.get(ref) or {}).get("proof") or "{}"
    return json.loads(raw) if raw else {}


def test_foreign_objective_proof_cannot_issue_grant(kanban_home):
    workspace = kanban_home.parent / "repo-obj-scope-issue"
    with kb.connect() as conn:
        parent_a, parent_b, child, oid_a, oid_b, digest_a = (
            _shared_descendant_two_objectives(conn, workspace)
        )
        issued_b = contract.issue_descendant_grant(
            conn, objective_id=oid_b, supervisor_task_id=parent_b,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest_a, caller_task_id=parent_b,
        )
        assert issued_b["ok"] is False
        assert "no persisted" in str(issued_b.get("error") or "")
        assert kb.get_task(conn, child).status != "done"
        assert _unit_proof(conn, oid_b, child).get("verdict") != "pass"
        assert _unit_proof(conn, oid_a, child).get("verdict") == "pass"
        issued_a = contract.issue_descendant_grant(
            conn, objective_id=oid_a, supervisor_task_id=parent_a,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest_a, caller_task_id=parent_a,
        )
        assert issued_a["ok"] is True


def test_foreign_objective_proof_cannot_satisfy_consume_or_read(kanban_home):
    workspace = kanban_home.parent / "repo-obj-scope-consume"
    with kb.connect() as conn:
        _parent_a, parent_b, child, oid_a, oid_b, digest_a = (
            _shared_descendant_two_objectives(conn, workspace)
        )
        packet_a = contract.build_canonical_evidence(conn, child, objective_id=oid_a)
        packet_b = contract.build_canonical_evidence(conn, child, objective_id=oid_b)
        empty = contract.build_canonical_evidence(conn, child, objective_id="")
        assert packet_a.get("objective_id") == oid_a
        assert contract.persisted_proof_present(packet_a) is True
        assert packet_b.get("objective_id") == oid_b
        assert packet_b.get("proof") == {}
        assert packet_b.get("head") is None
        assert packet_b.get("verdict") is None
        assert contract.persisted_proof_present(packet_b) is False
        assert empty.get("proof") == {}
        assert contract.persisted_proof_present(empty) is False
        contract.ensure_contract_tables(conn)
        now = int(__import__("time").time())
        with kb.write_txn(conn):
            conn.execute(
                """
                INSERT INTO kanban_reconcile_grants (
                    id, objective_id, supervisor_task_id, descendant_task_id,
                    transition, evidence_hash, consumed_at, created_at
                ) VALUES (?, ?, ?, ?, 'complete', ?, NULL, ?)
                """,
                ("rg_cross_obj", oid_b, parent_b, child, digest_a, now),
            )
        closed_b = contract.reconcile_descendant(
            conn, supervisor_task_id=parent_b, descendant_task_id=child,
            transition="complete", evidence_hash=digest_a,
            caller_task_id=parent_b, objective_id=oid_b,
        )
        assert closed_b["ok"] is False
        err = str(closed_b.get("error") or "")
        assert "no persisted" in err or "does not match" in err
        assert "already consumed" not in err
        assert "no issued" not in err
        assert kb.get_task(conn, child).status != "done"
        planted = conn.execute(
            "SELECT consumed_at FROM kanban_reconcile_grants WHERE id = ?",
            ("rg_cross_obj",),
        ).fetchone()
        assert planted["consumed_at"] is None
        assert _unit_proof(conn, oid_b, child).get("verdict") != "pass"
        assert _unit_proof(conn, oid_a, child).get("verdict") == "pass"


def test_cross_bound_objective_supervisor_issue_consume_read_fail_closed(kanban_home):
    """A-proof + B-supervisor fails closed; same-objective A and B stay valid."""
    workspace = kanban_home.parent / "repo-cross-bind"
    with kb.connect() as conn:
        parent_a, parent_b, child, oid_a, oid_b, digest_a = (
            _shared_descendant_two_objectives(conn, workspace)
        )
        assert contract.supervisor_owns_objective(conn, parent_a, oid_a) is True
        assert contract.supervisor_owns_objective(conn, parent_b, oid_b) is True
        assert contract.supervisor_owns_objective(conn, parent_b, oid_a) is False
        assert contract.supervisor_owns_objective(conn, parent_a, oid_b) is False

        packet_cross = contract.build_canonical_evidence(
            conn, child, objective_id=oid_a, supervisor_task_id=parent_b,
        )
        assert packet_cross.get("proof") == {}
        assert packet_cross.get("head") is None
        assert packet_cross.get("verdict") is None
        assert contract.persisted_proof_present(packet_cross) is False

        issued_cross = contract.issue_descendant_grant(
            conn, objective_id=oid_a, supervisor_task_id=parent_b,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest_a, caller_task_id=parent_b,
        )
        assert issued_cross["ok"] is False
        err_issue = str(issued_cross.get("error") or "")
        assert "owning objective" in err_issue
        assert kb.get_task(conn, child).status != "done"

        contract.ensure_contract_tables(conn)
        now = int(__import__("time").time())
        with kb.write_txn(conn):
            conn.execute(
                """
                INSERT INTO kanban_reconcile_grants (
                    id, objective_id, supervisor_task_id, descendant_task_id,
                    transition, evidence_hash, consumed_at, created_at
                ) VALUES (?, ?, ?, ?, 'complete', ?, NULL, ?)
                """,
                ("rg_cross_bind", oid_a, parent_b, child, digest_a, now),
            )
        closed_cross = contract.reconcile_descendant(
            conn, supervisor_task_id=parent_b, descendant_task_id=child,
            transition="complete", evidence_hash=digest_a,
            caller_task_id=parent_b, objective_id=oid_a,
        )
        assert closed_cross["ok"] is False
        err_consume = str(closed_cross.get("error") or "")
        assert "owning objective" in err_consume
        assert "already consumed" not in err_consume
        assert kb.get_task(conn, child).status != "done"
        planted = conn.execute(
            "SELECT consumed_at FROM kanban_reconcile_grants WHERE id = ?",
            ("rg_cross_bind",),
        ).fetchone()
        assert planted["consumed_at"] is None

        packet_a = contract.build_canonical_evidence(
            conn, child, objective_id=oid_a, supervisor_task_id=parent_a,
        )
        assert contract.persisted_proof_present(packet_a) is True
        issued_a = contract.issue_descendant_grant(
            conn, objective_id=oid_a, supervisor_task_id=parent_a,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest_a, caller_task_id=parent_a,
        )
        assert issued_a["ok"] is True

        digest_b = _seed_child_proof(conn, oid_b, child, workspace=workspace)
        packet_b = contract.build_canonical_evidence(
            conn, child, objective_id=oid_b, supervisor_task_id=parent_b,
        )
        assert contract.persisted_proof_present(packet_b) is True
        issued_b = contract.issue_descendant_grant(
            conn, objective_id=oid_b, supervisor_task_id=parent_b,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest_b, caller_task_id=parent_b,
        )
        assert issued_b["ok"] is True
        closed_a = contract.reconcile_descendant(
            conn, supervisor_task_id=parent_a, descendant_task_id=child,
            transition="complete", evidence_hash=digest_a,
            caller_task_id=parent_a, objective_id=oid_a,
        )
        assert closed_a["ok"] is True
        assert closed_a["consumed"] is True
        assert kb.get_task(conn, child).status == "done"
        closed_b = contract.reconcile_descendant(
            conn, supervisor_task_id=parent_b, descendant_task_id=child,
            transition="complete", evidence_hash=digest_b,
            caller_task_id=parent_b, objective_id=oid_b,
        )
        assert closed_b["ok"] is True
        assert closed_b["consumed"] is True
        assert kb.get_task(conn, child).status == "done"


def test_missing_head_review_fails_closed(kanban_home):
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        missing_submitted = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=None, current_head="abc",
        )
        assert missing_submitted.get("ok") is False
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units.get(child, {}).get("status") != "done"
        missing_live = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head="abc", current_head=None,
            git_head_fn=lambda _p: None,
        )
        assert missing_live.get("ok") is False
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units.get(child, {}).get("status") != "done"


def test_abbreviated_head_current_head_pair_fails_closed(kanban_home, tmp_path):
    live = _init_git_head(kanban_home.parent / "repo-short")
    short = live[:7]
    assert len(short) == 7
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id=?",
            (str(kanban_home.parent / "repo-short"), child),
        )
        denied = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=short, current_head=short,
        )
        assert denied.get("ok") is False
        assert "40-character" in str(denied.get("error") or "")
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units.get(child, {}).get("status") != "done"
        digest = _seed_child_proof(conn, oid, child, head=short)
        issued = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest, caller_task_id=parent,
        )
        assert issued["ok"] is False
        assert kb.get_task(conn, child).status != "done"


def test_stale_proof_after_later_failed_run_cannot_issue_or_consume_grant(kanban_home):
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        digest = _seed_child_proof(
            conn, oid, child, workspace=kanban_home.parent / "repo-stale-proof",
        )
        issued_before = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest, caller_task_id=parent,
        )
        assert issued_before["ok"] is True
        now = int(__import__("time").time())
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_runs (task_id, status, outcome, started_at, ended_at, error) "
                "VALUES (?, 'failed', 'failed', ?, ?, 'boom')",
                (child, now - 10, now),
            )
            conn.execute(
                "UPDATE tasks SET status='blocked', current_run_id=NULL, "
                "claim_lock=NULL, claim_expires=NULL WHERE id=?",
                (child,),
            )
        packet = contract.build_canonical_evidence(conn, child, objective_id=oid)
        assert packet.get("run_id")
        assert packet.get("run_outcome") == "failed"
        assert contract.persisted_proof_present(packet) is False
        issued = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=contract.canonical_evidence_hash(packet),
            caller_task_id=parent,
        )
        assert issued["ok"] is False
        replay = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest, caller_task_id=parent,
        )
        assert replay["ok"] is False
        closed = contract.reconcile_descendant(
            conn, supervisor_task_id=parent, descendant_task_id=child,
            transition="complete", evidence_hash=digest,
            caller_task_id=parent, objective_id=oid,
        )
        assert closed["ok"] is False
        assert kb.get_task(conn, child).status != "done"


def test_descendant_grant_denied_when_live_head_unreadable(kanban_home, tmp_path):
    missing = tmp_path / "no-such-child-worktree"
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        conn.execute(
            "UPDATE tasks SET workspace_path=? WHERE id=?",
            (str(missing), child),
        )
        digest = _seed_child_proof(conn, oid, child, head="a" * 40)
        issued = contract.issue_descendant_grant(
            conn, objective_id=oid, supervisor_task_id=parent,
            descendant_task_id=child, transition="complete",
            evidence_hash=digest, caller_task_id=parent,
        )
        assert issued["ok"] is False
        assert "live" in str(issued.get("error") or "").lower()
        assert kb.get_task(conn, child).status != "done"


def test_structured_pass_rejected_when_git_head_fn_is_none(kanban_home):
    head = "a" * 40
    with kb.connect() as conn:
        _parent, child, oid = _graph(conn)
        denied = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=head, current_head=head,
            git_head_fn=None,
        )
        assert denied.get("ok") is False
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units.get(child, {}).get("status") != "done"


def test_review_cap_missing_origin_fails_closed_across_session_replace(
    kanban_home, monkeypatch,
):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        parent, child, oid = _graph(conn)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "worker-chat")
        monkeypatch.setenv("HERMES_SESSION_KEY", "worker-chat")
        monkeypatch.setenv("HERMES_KANBAN_TASK", child)
        last = None
        for i in range(5):
            last = contract.record_review_verdict(
                conn, task_id=child, verdict="fail", head=f"h{i}",
                blockers=["p1"], remoko=remoko,
            )
        assert last["review_cap"] is True
        assert not last.get("request_id")
        assert last.get("ok") is False
        assert remoko.calls == []
        chats = {
            row["chat_id"]
            for row in conn.execute("SELECT chat_id FROM kanban_notify_subs").fetchall()
        }
        assert "worker-chat" not in chats
        faults = conn.execute(
            "SELECT kind, payload FROM kanban_supervisor_events "
            "WHERE kind = 'lifecycle_fault' AND task_id = ?",
            (child,),
        ).fetchall()
        assert faults
        payload = json.loads(faults[0]["payload"] or "{}")
        assert payload.get("reason") == "review_cap_missing_origin"
        obj = sup.get_objective(conn, oid)
        assert obj["origin_chat_id"] != "worker-chat"
        assert obj["remoko_request_id"] in (None, "")

    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "replacement-chat")
    monkeypatch.setenv("HERMES_SESSION_KEY", "replacement-chat")
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        again = contract.record_review_verdict(
            conn, task_id=child, verdict="fail", head="h5",
            blockers=["p1"], remoko=remoko,
        )
        assert again.get("request_id") in (None, "")
        assert remoko.calls == []
        chats = {
            row["chat_id"]
            for row in conn.execute("SELECT chat_id FROM kanban_notify_subs").fetchall()
        }
        assert "worker-chat" not in chats
        assert "replacement-chat" not in chats
        faults = conn.execute(
            "SELECT 1 FROM kanban_supervisor_events "
            "WHERE kind = 'lifecycle_fault' AND task_id = ?",
            (child,),
        ).fetchall()
        assert faults
