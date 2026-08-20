"""LS-2776: active_pr classifier, objective ledger, starvation, Remoko."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_supervision_contract as contract
from hermes_cli import kanban_supervisor as sup


PR_URL = "Opened https://github.com/example/repo/pull/42 for review."


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
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


def _seed_prior_run(conn, task_id: str, *, outcome: str = "reclaimed") -> None:
    _force_ready(conn, task_id)
    claimed = kb.claim_task(conn, task_id)
    assert claimed is not None
    now = int(time.time())
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE task_runs SET status=?, outcome=?, ended_at=? "
            "WHERE task_id=? AND ended_at IS NULL",
            (outcome, outcome, now, task_id),
        )
        conn.execute(
            "UPDATE tasks SET status='ready', claim_lock=NULL, "
            "claim_expires=NULL, worker_pid=NULL, current_run_id=NULL "
            "WHERE id=?",
            (task_id,),
        )


class FakeRemoko(sup.RemokoClient):
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.requests: dict[str, dict] = {}

    def request(self, payload: dict) -> dict:
        rid = f"rk-{len(self.calls) + 1}"
        rec = {
            "request_id": rid,
            "id": rid,
            "external_id": payload.get("external_id"),
            "status": "pending",
            "answer": None,
            "choices": list(payload.get("choices") or []),
        }
        self.calls.append(payload)
        self.requests[rid] = rec
        return {"request_id": rid, "id": rid}

    def get_request(self, request_id: str) -> dict:
        rec = self.requests.get(str(request_id))
        if rec is None:
            return {}
        return dict(rec)

    def find_request(self, external_id: str) -> dict:
        for rec in self.requests.values():
            if rec.get("external_id") == external_id:
                return dict(rec)
        return {}

    def seed(self, *, request_id: str, external_id: str, status: str = "pending") -> str:
        rec = {
            "request_id": request_id,
            "id": request_id,
            "external_id": external_id,
            "status": status,
            "answer": None,
            "choices": [],
        }
        self.requests[request_id] = rec
        return request_id

    def answer(self, request_id: str, answer: str) -> None:
        rec = self.requests[str(request_id)]
        rec["status"] = "answered"
        rec["answer"] = answer


class FailOnceRemoko(FakeRemoko):
    """Jude's repro: first send fails, later ticks must still retry."""

    def __init__(self) -> None:
        super().__init__()
        self.failures_left = 1

    def request(self, payload: dict) -> dict:
        if self.failures_left > 0:
            self.failures_left -= 1
            raise RuntimeError("inbox down")
        return super().request(payload)


def test_never_run_ready_child_with_pr_comment_is_dispatchable(
    kanban_home, monkeypatch,
):
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="publish PR", assignee="cole")
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        assert kb.check_respawn_guard(conn, tid) is None
        res = kb.dispatch_once(conn, dry_run=True)
        assert tid in [s[0] for s in res.spawned]
        assert tid not in dict(res.respawn_guarded)


def test_body_only_pr_url_is_not_scanned(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="body pr",
            assignee="cole",
            body=f"See {PR_URL}",
        )
        _seed_prior_run(conn, tid)
        assert kb.check_respawn_guard(conn, tid) is None


def test_after_run_and_pr_comment_duplicate_respawn_stays_guarded(kanban_home, monkeypatch):
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already ran", assignee="cole")
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        assert kb.check_respawn_guard(conn, tid) == "active_pr"
        res = kb.dispatch_once(conn, dry_run=True)
        assert tid not in [s[0] for s in res.spawned]
        assert dict(res.respawn_guarded).get(tid) == "active_pr"


def test_review_lane_bypass_preserved(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="review", assignee="jude")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        assert kb.request_review(
            conn, tid, summary="ready", expected_run_id=claimed.current_run_id,
        )
        assert kb.check_respawn_guard(conn, tid, lane="review") is None


def test_exemption_comment_token_allows_respawn(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="exempt", assignee="cole")
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        kb.add_comment(conn, tid, author="operator", body="respawn-ok")
        assert kb.check_respawn_guard(conn, tid) is None


def test_exemption_update_existing_pr_token(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="update pr", assignee="cole")
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        kb.add_comment(
            conn, tid, author="operator",
            body="guard-exemption: update-existing-pr",
        )
        assert kb.check_respawn_guard(conn, tid) is None


def test_metadata_exemption_allows_respawn(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="meta exempt", assignee="cole")
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        sup.set_respawn_exemption(conn, tid, "operator_requeue")
        assert kb.check_respawn_guard(conn, tid) is None


def test_explicit_requeue_after_pr_producing_run_is_dispatchable(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="requeue me", assignee="cole")
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        assert kb.check_respawn_guard(conn, tid) == "active_pr"
        later = int(time.time()) + 5
        with kb.write_txn(conn):
            conn.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) "
                "VALUES (?, 'promoted', ?, ?)",
                (tid, '{"status":"ready"}', later),
            )
        assert kb.check_respawn_guard(conn, tid) is None


def test_recent_success_still_guards_after_completed_run(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="just finished", assignee="cole")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.complete_task(
            conn, tid, summary="done",
            expected_run_id=claimed.current_run_id,
        )
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.check_respawn_guard(conn, tid) == "recent_success"


def test_kanban_child_upserts_objective_unit(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "origin-session")
    monkeypatch.setenv("HERMES_SESSION_KEY", "origin-session")
    monkeypatch.setenv("HERMES_PROFILE", "default")
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        kb.add_notify_sub(
            conn, task_id=root, platform="webui",
            chat_id="origin-session", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "origin-session"},
        )
        monkeypatch.setenv("HERMES_KANBAN_TASK", root)
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
        )
        obj = sup.get_objective_for_root(conn, root)
        assert obj is not None
        assert obj["origin_platform"] == "webui"
        units = sup.list_units(conn, obj["id"])
        assert any(u["kind"] == "kanban" and u["ref"] == child for u in units)
        assert not sup.objective_is_complete(conn, obj["id"])


def test_delegate_child_finishing_after_parent_return_still_reconciles(
    kanban_home, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_rootfake")
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        monkeypatch.setenv("HERMES_KANBAN_TASK", root)
        oid = sup.ensure_objective(conn, root)
        monkeypatch.setenv("HERMES_OBJECTIVE_ID", oid)
    # Parent "returns" — new connection, same board.
    sup.note_delegate_spawn(subagent_id="sa-0-deadbeef", owner_profile="cole")
    with kb.connect() as conn:
        units = sup.list_units(conn, oid)
        assert any(u["ref"] == "sa-0-deadbeef" and u["status"] == "running" for u in units)
        assert not sup.objective_is_complete(conn, oid)
    sup.note_delegate_stop(
        subagent_id="sa-0-deadbeef", status="completed", summary="ok",
    )
    with kb.connect() as conn:
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units["sa-0-deadbeef"]["status"] == "awaiting_verification"
        assert not sup.objective_is_complete(conn, oid)
        from hermes_cli.kanban_supervision_contract import (
            canonical_evidence_hash,
            process_exit_evidence,
            verify_process_exit,
        )

        proof = json.loads(units["sa-0-deadbeef"]["proof"])
        digest = canonical_evidence_hash(process_exit_evidence(proof))
        verified = verify_process_exit(
            conn, kind="delegate_task", ref="sa-0-deadbeef", evidence_hash=digest,
        )
        assert verified["ok"] is True
        assert verified["status"] == "done"
        kb.complete_task(conn, root, summary="parent done")
        status = sup.reconcile_objective(conn, oid)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units["sa-0-deadbeef"]["status"] == "done"
        # Root kanban unit may not exist; objective completes only when
        # every ledger row is terminal.
        if all(u["status"] in {"done", "failed"} for u in units.values()):
            assert status == "done"


def test_bot_chat_completing_after_delegator_return_still_reconciles(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        monkeypatch.setenv("HERMES_KANBAN_TASK", root)
        oid = sup.ensure_objective(conn, root)
        monkeypatch.setenv("HERMES_OBJECTIVE_ID", oid)
    monkeypatch.setenv("HERMES_PROFILE", "jude")
    sup.note_bot_chat_handoff(session_id="20260819_bot", title="Bot Chat")
    # Delegator process gone. Exit is not completion.
    sup.note_bot_chat_complete(session_id="20260819_bot", owner_profile="jude")
    with kb.connect() as conn:
        units = sup.list_units(conn, oid)
        bot = [u for u in units if u["kind"] == "bot_chat"]
        assert bot and bot[0]["status"] == "awaiting_verification"
        assert not sup.objective_is_complete(conn, oid)


def test_starvation_emits_once_and_survives_reload(kanban_home, monkeypatch):
    import hermes_cli.profiles as profmod

    monkeypatch.setattr(profmod, "profile_exists", lambda name: True)
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="stuck", assignee="cole")
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        emitted = []
        for _ in range(5):
            ev = sup.record_respawn_guard(conn, tid, "active_pr")
            if ev:
                emitted.append(ev)
        assert len(emitted) == 1
        assert emitted[0]["count"] >= 3
        assert emitted[0]["reason"] == "active_pr"
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("starvation") == 1
        row = conn.execute(
            "SELECT consecutive_count, last_reason FROM kanban_respawn_guard_state "
            "WHERE task_id=?",
            (tid,),
        ).fetchone()
        assert row["consecutive_count"] >= 5
        assert row["last_reason"] == "active_pr"

    # Gateway restart: new connection, count must persist; no second event.
    with kb.connect() as conn:
        ev = sup.record_respawn_guard(conn, tid, "active_pr")
        assert ev is None
        kinds = [e.kind for e in kb.list_events(conn, tid)]
        assert kinds.count("starvation") == 1


def test_starvation_wakes_supervisor_and_notifies_origin(kanban_home, monkeypatch):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        kb.add_notify_sub(
            conn, task_id=root, platform="webui",
            chat_id="origin-live", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "origin-live"},
        )
        child = kb.create_task(
            conn, title="child", assignee="cole",
        )
        kb.complete_task(conn, root, summary="root launched")
        with kb.write_txn(conn):
            conn.execute(
                "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
                (root, child),
            )
        _seed_prior_run(conn, child)
        kb.add_comment(conn, child, author="worker", body=PR_URL)
        for _ in range(3):
            sup.record_respawn_guard(conn, child, "active_pr")
        result = sup.supervise_once(conn, remoko=remoko)
        assert result.starvation
        action = result.starvation[0]["action"]
        assert action in {"owner_blocker", "requeued_exemption", "auto_repaired"}
        kinds = [e.kind for e in kb.list_events(conn, child)]
        assert "starvation" in kinds
        subs = kb.list_notify_subs(conn, child)
        chats = {s["chat_id"] for s in subs}
        assert "origin-live" in chats or any(
            (s.get("delivery_metadata") or {}).get("session_key") == "origin-live"
            for s in subs
        )


def test_typed_owner_blocker_one_remoko_revalidate_resume_complete(
    kanban_home, monkeypatch,
):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="needs owner", assignee="cole", parents=[root],
        )
        kb.complete_task(conn, root, summary="root launched")
        _seed_prior_run(conn, child)
        kb.add_comment(conn, child, author="worker", body=PR_URL)
        oid = sup.ensure_objective(conn, root)
        for _ in range(3):
            sup.record_respawn_guard(conn, child, "active_pr")
        result = sup.supervise_once(conn, remoko=remoko)
        assert remoko.calls, result
        assert len(remoko.calls) == 1
        ext = remoko.calls[0]["external_id"]
        assert ext.startswith(f"obj-{oid}-")
        obj = sup.get_objective(conn, oid)
        assert obj["status"] == "blocked_owner"
        assert obj["remoko_request_id"]
        # Duplicate tick must not send a second Remoko card.
        sup.supervise_once(conn, remoko=remoko)
        assert len(remoko.calls) == 1
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        remoko.answer(obj["remoko_request_id"], "Update existing PR")
        reported: list[dict] = []

        def _report(**kwargs):
            reported.append(kwargs)

        ok = sup.resume_after_owner_answer(
            conn,
            objective_id=oid,
            task_id=child,
            answer="Update existing PR",
            expected_external_id=ext,
            report_execution=_report,
            remoko=remoko,
        )
        assert ok is True
        assert reported and reported[0]["status"] == "accepted"
        assert not sup.revalidate_owner_answer(
            conn,
            objective_id=oid,
            answer="Update existing PR",
            expected_external_id="obj-other-key",
            remoko=remoko,
        )
        kb.complete_task(conn, child, summary="updated existing PR")
        kb.complete_task(conn, root, summary="root done")
        status = sup.reconcile_objective(conn, oid)
        assert status == "done"
        assert sup.objective_is_complete(conn, oid)


def test_no_false_completion_while_child_nonterminal(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
        )
        oid = sup.ensure_objective(conn, root)
        kb.complete_task(conn, root, summary="parent returned")
        assert not sup.objective_is_complete(conn, oid)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units[child]["status"] != "done"


def test_stale_review_invalidated_when_head_moves(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir",
            workspace_path="/tmp/fake-repo",
        )
        oid = sup.ensure_objective(conn, root)
        kb.complete_task(conn, child, summary="jude-verdict: pass")
        kb.add_comment(conn, child, author="jude", body="jude-verdict: pass")
        sup.note_kanban_terminal(
            conn, child, status="done",
            proof={"type": "jude_verdict", "verdict": "pass", "head": "aaa"},
        )
        conn.execute(
            "UPDATE kanban_objective_units SET terminal_predicate='jude_verdict_pass', "
            "status='done', proof=? WHERE kind='kanban' AND ref=?",
            (json.dumps({"type": "jude_verdict", "verdict": "pass", "head": "aaa"}), child),
        )
        heads = {"/tmp/fake-repo": "bbb"}
        invalidated = sup.invalidate_stale_reviews(
            conn, git_head_fn=lambda path: heads.get(path),
        )
        assert child in invalidated
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units[child]["status"] == "pending"
        assert units[child]["next_gate"] == "re-review"
        assert not sup.objective_is_complete(conn, oid)


def test_duplicate_event_idempotency(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="idemp", assignee="cole")
        first = sup._record_supervisor_event(
            conn, event_key="starvation:t:active_pr:1",
            kind="starvation", task_id=tid, payload={"n": 1},
        )
        second = sup._record_supervisor_event(
            conn, event_key="starvation:t:active_pr:1",
            kind="starvation", task_id=tid, payload={"n": 2},
        )
        assert first is True
        assert second is False


def test_exact_origin_delivery_prefers_parent_origin(kanban_home, monkeypatch):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        kb.add_notify_sub(
            conn, task_id=root, platform="webui",
            chat_id="73c58f750cba", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "73c58f750cba"},
        )
        oid = sup.ensure_objective(
            conn, root,
            origin=sup.SessionOrigin(
                platform="webui",
                chat_id="73c58f750cba",
                session_key="73c58f750cba",
                profile="default",
            ),
        )
        monkeypatch.setenv("HERMES_KANBAN_TASK", root)
        monkeypatch.setenv("HERMES_OBJECTIVE_ID", oid)
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "7779276c4c10")
        monkeypatch.setenv("HERMES_SESSION_KEY", "7779276c4c10")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
        )
        origin = sup.resolve_notify_origin(conn, child)
        assert origin is not None
        assert origin.notify_chat_id() == "73c58f750cba"
        assert origin.notify_chat_id() != "7779276c4c10"
        chats = {s["chat_id"] for s in kb.list_notify_subs(conn, child)}
        assert "73c58f750cba" in chats
        assert "7779276c4c10" not in chats
        md = kb.list_notify_subs(conn, child)[0].get("delivery_metadata") or {}
        assert md.get("session_key") == "73c58f750cba"


def test_claim_clears_guard_streak(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="clear me", assignee="cole")
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        sup.record_respawn_guard(conn, tid, "active_pr")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        row = conn.execute(
            "SELECT consecutive_count, last_reason FROM kanban_respawn_guard_state "
            "WHERE task_id=?",
            (tid,),
        ).fetchone()
        assert row is None or int(row["consecutive_count"] or 0) == 0


def test_update_existing_pr_intent_requeues(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="update existing PR on the branch", assignee="cole",
        )
        _seed_prior_run(conn, tid)
        kb.add_comment(conn, tid, author="worker", body=PR_URL)
        for _ in range(3):
            sup.record_respawn_guard(conn, tid, "active_pr")
        action = sup.handle_starvation(conn, tid, "active_pr")
        assert action["action"] == "requeued_exemption"
        assert kb.check_respawn_guard(conn, tid) is None


def test_supervisor_schema_present_on_fresh_and_reopen(kanban_home):
    with kb.connect() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for name in (
            "kanban_objectives",
            "kanban_objective_units",
            "kanban_respawn_guard_state",
            "kanban_supervisor_events",
        ):
            assert name in tables
    kb._INITIALIZED_PATHS.clear()
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='kanban_objectives'"
        ).fetchone()
        assert row is not None


def test_delegate_and_bot_chat_ledger_without_kanban_env(kanban_home, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_OBJECTIVE_ID", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    oid = sup.note_delegate_spawn(subagent_id="sa-adhoc", owner_profile="cole")
    assert oid
    bot = sup.note_bot_chat_handoff(session_id="20260819_adhoc", title="Bot Chat")
    assert bot
    with kb.connect() as conn:
        units = sup.list_units(conn, oid)
        kinds = {u["kind"] for u in units}
        assert "delegate_task" in kinds
        assert "bot_chat" in kinds


def test_remoko_failure_does_not_fabricate_request_id(kanban_home):
    class Boom(sup.RemokoClient):
        def request(self, payload: dict) -> dict:
            raise RuntimeError("inbox down")

    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, root)
        rid = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=root,
            decision_key="active_pr_starvation",
            purpose="stuck", remoko=Boom(),
        )
        assert rid is None
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert not obj.get("remoko_request_id")
        assert obj["status"] != "blocked_owner"
        retry = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=root,
            decision_key="active_pr_starvation",
            purpose="stuck", remoko=remoko,
        )
        assert retry
        assert len(remoko.calls) == 1
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert obj["remoko_request_id"] == retry


def _seed_reserving_owner_blocker(conn, oid: str, task_id: str, decision_key: str = "active_pr_starvation"):
    external_id = f"obj-{oid}-{decision_key}"
    event_key = f"remoko:{oid}:{decision_key}"
    assert sup._record_supervisor_event(
        conn,
        event_key=event_key,
        kind="owner_blocker",
        task_id=task_id,
        objective_id=oid,
        payload={
            "external_id": external_id,
            "decision_key": decision_key,
            "status": "reserving",
            "choices": ["Update existing PR", "Open a new PR", "Leave it parked", "Wait"],
        },
    )
    return external_id, event_key


def test_reserving_event_with_live_request_is_recovered(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, root)
        ext, event_key = _seed_reserving_owner_blocker(conn, oid, root)
        live_id = remoko.seed(request_id="rk-orphan", external_id=ext)
        rid = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=root,
            decision_key="active_pr_starvation",
            purpose="stuck",
            choices=["Update existing PR", "Open a new PR", "Leave it parked", "Wait"],
            remoko=remoko,
        )
        assert rid == live_id
        assert remoko.calls == []
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert obj["remoko_request_id"] == live_id
        assert obj["remoko_external_id"] == ext
        assert obj["status"] == "blocked_owner"
        event = sup._supervisor_event(conn, event_key)
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["status"] == "sent"
        assert payload["request_id"] == live_id
        again = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=root,
            decision_key="active_pr_starvation",
            purpose="stuck", remoko=remoko,
        )
        assert again == live_id
        assert remoko.calls == []


def test_reserving_without_live_request_does_not_send_duplicate(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, root)
        ext, event_key = _seed_reserving_owner_blocker(conn, oid, root)
        rid = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=root,
            decision_key="active_pr_starvation",
            purpose="stuck", remoko=remoko,
        )
        assert rid is None
        assert remoko.calls == []
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert not obj.get("remoko_request_id")
        assert obj["status"] != "blocked_owner"
        event = sup._supervisor_event(conn, event_key)
        assert event is not None
        payload = json.loads(event["payload"])
        assert payload["status"] == "reserving"
        assert payload["external_id"] == ext


def test_supervise_once_retries_owner_blocker_after_fail_once_send(kanban_home):
    remoko = FailOnceRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="needs owner", assignee="cole", parents=[root],
        )
        kb.complete_task(conn, root, summary="root launched")
        _seed_prior_run(conn, child)
        kb.add_comment(conn, child, author="worker", body=PR_URL)
        oid = sup.ensure_objective(conn, root)
        for _ in range(3):
            sup.record_respawn_guard(conn, child, "active_pr")

        first = sup.supervise_once(conn, remoko=remoko)
        assert first.starvation
        assert first.starvation[0]["action"] == "owner_blocker"
        assert first.starvation[0].get("request_id") is None
        assert remoko.calls == []
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert not obj.get("remoko_request_id")
        assert obj["status"] != "blocked_owner"
        reservation = sup._supervisor_event(
            conn, f"remoko:{oid}:active_pr_starvation",
        )
        assert reservation is None
        handled_rows = conn.execute(
            "SELECT event_key, payload FROM kanban_supervisor_events "
            "WHERE kind = 'starvation_handled' AND task_id = ?",
            (child,),
        ).fetchall()
        for row in handled_rows:
            payload = json.loads(row["payload"] or "{}")
            assert sup._starvation_handling_is_terminal(payload) is False

        second = sup.supervise_once(conn, remoko=remoko)
        assert len(remoko.calls) == 1
        assert second.remoko_requests
        rid = second.remoko_requests[-1]
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert obj["remoko_request_id"] == rid
        assert obj["status"] == "blocked_owner"

        third = sup.supervise_once(conn, remoko=remoko)
        assert len(remoko.calls) == 1
        assert remoko.calls[0]["external_id"] == f"obj-{oid}-active_pr_starvation"
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert obj["remoko_request_id"] == rid
        assert not third.starvation or all(
            action.get("request_id") in {None, rid} for action in third.starvation
        )


def test_supervise_once_retries_poisoned_handled_starvation_without_request(
    kanban_home,
):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="needs owner", assignee="cole", parents=[root],
        )
        kb.complete_task(conn, root, summary="root launched")
        _seed_prior_run(conn, child)
        kb.add_comment(conn, child, author="worker", body=PR_URL)
        oid = sup.ensure_objective(conn, root)
        starvation_key = ""
        for _ in range(3):
            ev = sup.record_respawn_guard(conn, child, "active_pr")
            if ev:
                starvation_key = f"starvation:{child}:active_pr:{ev['first_guard_at']}"
        assert starvation_key
        assert sup._supervisor_event(conn, starvation_key) is not None
        assert sup._record_supervisor_event(
            conn,
            event_key=f"handled:{starvation_key}",
            kind="starvation_handled",
            task_id=child,
            payload={"action": "owner_blocker", "request_id": None},
        )

        result = sup.supervise_once(conn, remoko=remoko)
        assert len(remoko.calls) == 1
        assert result.remoko_requests
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert obj["remoko_request_id"] == result.remoko_requests[-1]

        again = sup.supervise_once(conn, remoko=remoko)
        assert len(remoko.calls) == 1
        assert not again.starvation or all(
            action.get("request_id") == obj["remoko_request_id"]
            for action in again.starvation
        )


def test_supervise_once_recovers_reserving_after_handled_starvation(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(conn, title="child", assignee="cole", parents=[root])
        oid = sup.ensure_objective(conn, root)
        ext, _event_key = _seed_reserving_owner_blocker(conn, oid, child)
        live_id = remoko.seed(request_id="rk-tick-orphan", external_id=ext)
        assert sup._record_supervisor_event(
            conn,
            event_key="handled:starvation:already",
            kind="starvation_handled",
            task_id=child,
            payload={"action": "owner_blocker", "request_id": None},
        )
        result = sup.supervise_once(conn, remoko=remoko)
        assert live_id in result.remoko_requests
        assert remoko.calls == []
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert obj["remoko_request_id"] == live_id
        assert obj["status"] == "blocked_owner"


def test_resume_requires_persisted_request_and_known_choice(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, root)
        assert not sup.resume_after_owner_answer(
            conn, objective_id=oid, task_id=root,
            answer="Update existing PR",
            expected_external_id=f"obj-{oid}-active_pr_starvation",
        )
        rid = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=root,
            decision_key="active_pr_starvation",
            purpose="stuck",
            choices=["Update existing PR", "Open a new PR", "Leave it parked", "Wait"],
            remoko=remoko,
        )
        assert rid
        ext = remoko.calls[0]["external_id"]
        assert not sup.revalidate_owner_answer(
            conn, objective_id=oid, answer="not a choice",
            expected_external_id=ext, remoko=remoko,
        )
        assert not sup.resume_after_owner_answer(
            conn, objective_id=oid, task_id=root,
            answer="Wait", expected_external_id=ext, remoko=remoko,
        )
        remoko.answer(rid, "Update existing PR")
        assert sup.resume_after_owner_answer(
            conn, objective_id=oid, task_id=root,
            answer="Update existing PR", expected_external_id=ext, remoko=remoko,
        )


def test_resume_requires_live_remoko_owner_answer(kanban_home):
    remoko = FakeRemoko()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, root)
        rid = sup.request_owner_blocker(
            conn, objective_id=oid, task_id=root,
            decision_key="active_pr_starvation",
            purpose="stuck",
            choices=["Update existing PR", "Open a new PR", "Leave it parked", "Wait"],
            remoko=remoko,
        )
        ext = remoko.calls[0]["external_id"]
        assert remoko.get_request(rid)["status"] == "pending"
        assert not sup.revalidate_owner_answer(
            conn, objective_id=oid, answer="Update existing PR",
            expected_external_id=ext, remoko=remoko,
        )
        assert not sup.resume_after_owner_answer(
            conn, objective_id=oid, task_id=root,
            answer="Update existing PR", expected_external_id=ext, remoko=remoko,
        )
        remoko.answer(rid, "Update existing PR")
        assert sup.revalidate_owner_answer(
            conn, objective_id=oid, answer="Update existing PR",
            expected_external_id=ext, remoko=remoko,
        )
        assert sup.resume_after_owner_answer(
            conn, objective_id=oid, task_id=root,
            answer="Update existing PR", expected_external_id=ext, remoko=remoko,
        )


def test_concurrent_ticks_create_one_remoko_request(kanban_home):
    import threading

    remoko = FakeRemoko()
    results: list[str | None] = []
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, root)

    def worker():
        with kb.connect() as conn:
            results.append(
                sup.request_owner_blocker(
                    conn, objective_id=oid, task_id=root,
                    decision_key="active_pr_starvation",
                    purpose="stuck", remoko=remoko,
                )
            )

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    sent = [rid for rid in results if rid]
    assert len(remoko.calls) == 1
    assert len(set(sent)) == 1
    with kb.connect() as conn:
        obj = sup.get_objective(conn, oid)
        assert obj is not None
        assert obj["remoko_request_id"] == sent[0]


def test_failed_unit_does_not_complete_objective(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        oid = sup.ensure_objective(conn, root)
        sup.upsert_unit(
            conn, objective_id=oid, kind="delegate_task",
            ref="sa-fail", status="failed",
            terminal_predicate="child_completed",
            proof={"child_status": "failed"},
        )
        assert not sup.objective_is_complete(conn, oid)
        status = sup.reconcile_objective(conn, oid)
        assert status != "done"


def test_task_done_without_proof_is_not_complete(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
        )
        oid = sup.ensure_objective(conn, root)
        conn.execute(
            "UPDATE kanban_objective_units SET status='done', proof=NULL "
            "WHERE kind='kanban' AND ref=?",
            (child,),
        )
        kb.complete_task(conn, child, summary="done")
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        # complete_task writes proof; wipe it again to isolate the predicate.
        conn.execute(
            "UPDATE kanban_objective_units SET proof=NULL "
            "WHERE kind='kanban' AND ref=?",
            (child,),
        )
        assert not sup._unit_satisfies_predicate(conn, {
            **units[child], "status": "done", "proof": None,
            "terminal_predicate": "task_done_with_proof",
        })


def test_ensure_objective_does_not_bind_worker_webui_session(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "7779276c4c10")
    monkeypatch.setenv("HERMES_SESSION_KEY", "7779276c4c10")
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        monkeypatch.setenv("HERMES_KANBAN_TASK", root)
        child = kb.create_task(conn, title="child", assignee="cole", parents=[root])
        oid = sup.note_kanban_child(conn, child, parents=[root])
        assert oid
        obj = sup.get_objective(conn, oid)
        assert obj["origin_chat_id"] != "7779276c4c10"
        assert obj["origin_session_key"] != "7779276c4c10"
        chats = {s["chat_id"] for s in kb.list_notify_subs(conn, child)}
        assert "7779276c4c10" not in chats


def test_mark_units_by_ref_stays_inside_owning_objective(kanban_home, monkeypatch):
    sid = "20260819_botchat"
    with kb.connect() as conn:
        a = kb.create_task(conn, title="obj-a", assignee="default")
        b = kb.create_task(conn, title="obj-b", assignee="default")
        oid_a = sup.ensure_objective(conn, a)
        oid_b = sup.ensure_objective(conn, b)
        sup.upsert_unit(conn, objective_id=oid_a, kind="bot_chat", ref=sid, status="running")
        sup.upsert_unit(conn, objective_id=oid_b, kind="bot_chat", ref=sid, status="running")
        monkeypatch.setenv("HERMES_OBJECTIVE_ID", oid_a)
        sup._mark_units_by_ref(
            conn, kind="bot_chat", ref=sid, status="awaiting_verification",
            proof={"terminal": "process_exit"},
        )
        units_a = {u["ref"]: u for u in sup.list_units(conn, oid_a)}
        units_b = {u["ref"]: u for u in sup.list_units(conn, oid_b)}
        assert units_a[sid]["status"] == "awaiting_verification"
        assert units_b[sid]["status"] == "running"


def test_stale_untrusted_jude_comment_does_not_write_current_head(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    import subprocess
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init"], cwd=repo, check=True, capture_output=True)
    live = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True).stdout.strip()
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir", workspace_path=str(repo),
        )
        oid = sup.ensure_objective(conn, root)
        sup.upsert_unit(conn, objective_id=oid, kind="kanban", ref=child, status="pending")
        kb.add_comment(conn, child, author="worker", body="jude-verdict: pass\nreviewed_head=deadbeef")
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
        assert proof.get("verdict") != "pass"
        assert proof.get("head") != live
        kb.add_comment(
            conn, child, author="jude",
            body=f"jude-verdict: pass\nreviewed_head={live}",
        )
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
        assert proof.get("verdict") != "pass"
        recorded = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=live,
            current_head=live, git_head_fn=lambda _p: live,
        )
        assert recorded.get("ok") is True
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"])
        assert proof["verdict"] == "pass"
        assert proof["head"] == live
        assert proof["verified"] is True


def _init_git_head(repo: Path) -> str:
    import subprocess
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "--allow-empty", "-m", "init"],
        cwd=repo, check=True, capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def test_abbreviated_reviewed_head_fails_closed_even_if_prefix_matches(kanban_home, tmp_path):
    repo = tmp_path / "repo"
    live = _init_git_head(repo)
    assert len(live) == 40
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir", workspace_path=str(repo),
        )
        oid = sup.ensure_objective(conn, root)
        sup.upsert_unit(conn, objective_id=oid, kind="kanban", ref=child, status="pending")
        for short in (live[:7], live[:12]):
            kb.add_comment(
                conn, child, author="jude",
                body=f"jude-verdict: pass\nreviewed_head={short}",
            )
            sup._maybe_record_jude_proof(conn, child)
            units = {u["ref"]: u for u in sup.list_units(conn, oid)}
            proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
            assert proof.get("verdict") != "pass"
            assert proof.get("head") != live
        kb.add_comment(
            conn, child, author="jude",
            body=f"jude-verdict: pass\nreviewed_head={live}",
        )
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
        assert proof.get("verdict") != "pass"
        recorded = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=live,
            current_head=live, git_head_fn=lambda _p: live,
        )
        assert recorded.get("ok") is True
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"])
        assert proof["verdict"] == "pass"
        assert proof["head"] == live
        assert proof["verified"] is True
        assert sup._unit_satisfies_predicate(conn, {
            **units[child],
            "status": "done",
            "terminal_predicate": "jude_verdict_pass",
            "proof": units[child]["proof"],
        })


def test_prefixing_reviewed_head_fails_closed_when_live_is_short(kanban_home, monkeypatch):
    short_live = "deadbee"
    long_reviewed = short_live + ("c" * (40 - len(short_live)))
    monkeypatch.setattr(sup, "git_head", lambda _path: short_live)
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir", workspace_path="/tmp/fake-repo",
        )
        oid = sup.ensure_objective(conn, root)
        sup.upsert_unit(conn, objective_id=oid, kind="kanban", ref=child, status="pending")
        kb.add_comment(
            conn, child, author="jude",
            body=f"jude-verdict: pass\nreviewed_head={long_reviewed}",
        )
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
        assert proof.get("verdict") != "pass"
        assert proof.get("head") != long_reviewed


def test_existing_jude_pass_invalidated_when_current_head_missing(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir",
            workspace_path="/tmp/fake-repo",
        )
        oid = sup.ensure_objective(conn, root)
        conn.execute(
            "UPDATE kanban_objective_units SET terminal_predicate='jude_verdict_pass', "
            "status='done', proof=? WHERE kind='kanban' AND ref=?",
            (json.dumps({"type": "jude_verdict", "verdict": "pass", "head": "aaa", "verified": True}), child),
        )
        invalidated = sup.invalidate_stale_reviews(
            conn, git_head_fn=lambda _path: None,
        )
        assert child in invalidated
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert units[child]["status"] == "pending"
        assert units[child]["next_gate"] == "re-review"
        assert not sup.objective_is_complete(conn, oid)


def test_jude_pass_predicate_fails_closed_without_current_head(kanban_home, monkeypatch):
    recorded = "a" * 40
    monkeypatch.setattr(sup, "git_head", lambda _path: None)
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir",
            workspace_path="/tmp/missing-repo",
        )
        oid = sup.ensure_objective(conn, root)
        proof = {"type": "jude_verdict", "verdict": "pass", "head": recorded, "verified": True}
        conn.execute(
            "UPDATE kanban_objective_units SET terminal_predicate='jude_verdict_pass', "
            "status='done', proof=? WHERE kind='kanban' AND ref=?",
            (json.dumps(proof), child),
        )
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        assert not sup._unit_satisfies_predicate(conn, {
            **units[child],
            "status": "done",
            "terminal_predicate": "jude_verdict_pass",
            "proof": json.dumps(proof),
        })
        assert not sup.objective_is_complete(conn, oid)


def test_jude_proof_mutation_stays_inside_owning_objective(kanban_home, tmp_path):
    live = _init_git_head(tmp_path / "repo-jude-scope")
    with kb.connect() as conn:
        root_a = kb.create_task(conn, title="obj-a", assignee="default")
        root_b = kb.create_task(conn, title="obj-b", assignee="default")
        child = kb.create_task(
            conn, title="shared-ref", assignee="cole", parents=[root_a],
            workspace_kind="dir", workspace_path=str(tmp_path / "repo-jude-scope"),
        )
        oid_a = sup.ensure_objective(conn, root_a)
        oid_b = sup.ensure_objective(conn, root_b)
        sup.upsert_unit(conn, objective_id=oid_a, kind="kanban", ref=child, status="pending")
        sup.upsert_unit(conn, objective_id=oid_b, kind="kanban", ref=child, status="pending")
        kb.add_comment(
            conn, child, author="jude",
            body=f"jude-verdict: pass\nreviewed_head={live}",
        )
        sup._maybe_record_jude_proof(conn, child)
        units_a = {u["ref"]: u for u in sup.list_units(conn, oid_a)}
        units_b = {u["ref"]: u for u in sup.list_units(conn, oid_b)}
        proof_a = json.loads(units_a[child]["proof"] or "{}") if units_a[child].get("proof") else {}
        proof_b = json.loads(units_b[child]["proof"] or "{}") if units_b[child].get("proof") else {}
        assert proof_a.get("verdict") != "pass"
        assert proof_b.get("verdict") != "pass"
        recorded = contract.record_review_verdict(
            conn, task_id=child, verdict="pass", head=live,
            current_head=live, git_head_fn=lambda _p: live,
        )
        assert recorded.get("ok") is True
        sup._maybe_record_jude_proof(conn, child)
        units_a = {u["ref"]: u for u in sup.list_units(conn, oid_a)}
        units_b = {u["ref"]: u for u in sup.list_units(conn, oid_b)}
        proof_a = json.loads(units_a[child]["proof"] or "{}")
        proof_b = json.loads(units_b[child]["proof"] or "{}") if units_b[child].get("proof") else {}
        assert proof_a.get("verdict") == "pass"
        assert proof_a.get("head") == live
        assert proof_a.get("verified") is True
        assert proof_b.get("verdict") != "pass"
        assert proof_b.get("head") != live


def test_root_stable_under_reversed_parent_insertion(kanban_home):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        left = kb.create_task(conn, title="left", assignee="cole", parents=[root])
        right = kb.create_task(conn, title="right", assignee="cole", parents=[root])
        child_ab = kb.create_task(
            conn, title="fan-ab", assignee="cole", parents=[left, right],
        )
        child_ba = kb.create_task(
            conn, title="fan-ba", assignee="cole", parents=[right, left],
        )
        assert sup.canonical_root_task_id(conn, child_ab) == root
        assert sup.canonical_root_task_id(conn, child_ba) == root
        assert sup._root_task_id(conn, child_ab) == root
        assert sup._root_task_id(conn, child_ba) == root
        oid_ab = sup.note_kanban_child(conn, child_ab, parents=[left, right])
        oid_ba = sup.note_kanban_child(conn, child_ba, parents=[right, left])
        assert oid_ab and oid_ab == oid_ba
        obj = sup.get_objective_for_root(conn, root)
        assert obj and obj["id"] == oid_ab


def test_two_independent_roots_sharing_child_are_rejected(kanban_home):
    with kb.connect() as conn:
        first = kb.create_task(conn, title="obj-a", assignee="default")
        second = kb.create_task(conn, title="obj-b", assignee="default")
        oid_a = sup.ensure_objective(conn, first)
        oid_b = sup.ensure_objective(conn, second)
        child = kb.create_task(
            conn, title="shared", assignee="cole", parents=[first, second],
        )
        assert sup.canonical_root_task_id(conn, child) is None
        assert sup._root_task_id(conn, child) == child
        assert sup.note_kanban_child(conn, child, parents=[first, second]) is None
        assert sup.note_kanban_child(conn, child, parents=[second, first]) is None
        refs_a = {u["ref"] for u in sup.list_units(conn, oid_a)}
        refs_b = {u["ref"] for u in sup.list_units(conn, oid_b)}
        assert child not in refs_a
        assert child not in refs_b


def test_diamond_fan_in_wakes_canonical_origin_regardless_of_parent_order(
    kanban_home, monkeypatch,
):
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        kb.add_notify_sub(
            conn, task_id=root, platform="webui",
            chat_id="origin-live", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "origin-live"},
        )
        sup.ensure_objective(
            conn, root,
            origin=sup.SessionOrigin(
                platform="webui", chat_id="origin-live",
                session_key="origin-live", profile="default",
            ),
        )
        left = kb.create_task(conn, title="left", assignee="cole", parents=[root])
        right = kb.create_task(conn, title="right", assignee="cole", parents=[root])
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "7779276c4c10")
        monkeypatch.setenv("HERMES_SESSION_KEY", "7779276c4c10")
        child_ab = kb.create_task(
            conn, title="ab", assignee="cole", parents=[left, right],
        )
        child_ba = kb.create_task(
            conn, title="ba", assignee="cole", parents=[right, left],
        )
        for child in (child_ab, child_ba):
            origin = sup.resolve_notify_origin(conn, child)
            assert origin is not None
            assert origin.notify_chat_id() == "origin-live"
            chats = {s["chat_id"] for s in kb.list_notify_subs(conn, child)}
            assert "origin-live" in chats
            assert "7779276c4c10" not in chats


def test_cross_objective_fan_in_does_not_wake_foreign_origin(kanban_home, monkeypatch):
    with kb.connect() as conn:
        first = kb.create_task(conn, title="obj-a", assignee="default")
        second = kb.create_task(conn, title="obj-b", assignee="default")
        kb.add_notify_sub(
            conn, task_id=first, platform="webui",
            chat_id="origin-a", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "origin-a"},
        )
        kb.add_notify_sub(
            conn, task_id=second, platform="webui",
            chat_id="origin-b", delivery_mode="notify+wake",
            delivery_metadata={"session_key": "origin-b"},
        )
        sup.ensure_objective(
            conn, first,
            origin=sup.SessionOrigin(
                platform="webui", chat_id="origin-a", session_key="origin-a",
            ),
        )
        sup.ensure_objective(
            conn, second,
            origin=sup.SessionOrigin(
                platform="webui", chat_id="origin-b", session_key="origin-b",
            ),
        )
        monkeypatch.setenv("HERMES_SESSION_PLATFORM", "webui")
        monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "7779276c4c10")
        monkeypatch.setenv("HERMES_SESSION_KEY", "7779276c4c10")
        child = kb.create_task(
            conn, title="shared", assignee="cole", parents=[first, second],
        )
        origin = sup.resolve_notify_origin(conn, child)
        assert origin is None or origin.notify_chat_id() not in {"origin-a", "origin-b"}
        chats = {s["chat_id"] for s in kb.list_notify_subs(conn, child)}
        assert "origin-a" not in chats
        assert "origin-b" not in chats
        assert "7779276c4c10" not in chats


def test_unattached_cross_fan_in_cannot_issue_descendant_grant(kanban_home, tmp_path):
    workspace = tmp_path / "repo-unattached-fan"
    live = _init_git_head(workspace)
    with kb.connect() as conn:
        first = kb.create_task(conn, title="sup-a", assignee="default")
        second = kb.create_task(conn, title="sup-b", assignee="default")
        child = kb.create_task(
            conn, title="shared", assignee="cole",
            parents=[first, second],
            workspace_kind="dir", workspace_path=str(workspace),
        )
        oid_a = sup.ensure_objective(conn, first)
        packet = contract.build_canonical_evidence(
            conn, child, objective_id=oid_a, supervisor_task_id=first,
        )
        issued = contract.issue_descendant_grant(
            conn, objective_id=oid_a, supervisor_task_id=first,
            descendant_task_id=child, transition="complete",
            evidence_hash=contract.canonical_evidence_hash(packet),
            caller_task_id=first,
        )
        assert issued["ok"] is False
        assert kb.get_task(conn, child).status != "done"
        assert live


def test_comment_authors_cannot_mint_jude_proof(kanban_home, tmp_path):
    live = _init_git_head(tmp_path / "repo-comment-authors")
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo-comment-authors"),
        )
        oid = sup.ensure_objective(conn, root)
        sup.upsert_unit(conn, objective_id=oid, kind="kanban", ref=child, status="pending")
        for author in ("worker", "cole", "self", "turing", "default"):
            kb.add_comment(
                conn, child, author=author,
                body=f"jude-verdict: pass\nreviewed_head={live}",
            )
            sup._maybe_record_jude_proof(conn, child)
            units = {u["ref"]: u for u in sup.list_units(conn, oid)}
            proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
            assert proof.get("verdict") != "pass"
            assert proof.get("verified") is not True


def test_stale_and_foreign_review_receipts_do_not_mint_proof(kanban_home, tmp_path):
    live = _init_git_head(tmp_path / "repo-stale-receipt")
    stale = "a" * 40
    assert stale != live
    with kb.connect() as conn:
        root = kb.create_task(conn, title="root", assignee="default")
        other = kb.create_task(conn, title="other", assignee="cole", parents=[root])
        child = kb.create_task(
            conn, title="child", assignee="cole", parents=[root],
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo-stale-receipt"),
        )
        oid = sup.ensure_objective(conn, root)
        sup.upsert_unit(conn, objective_id=oid, kind="kanban", ref=child, status="pending")
        sup._record_supervisor_event(
            conn, event_key="review_verdict:stale",
            kind="review_verdict", task_id=child,
            payload={
                "verdict": "pass", "head": stale, "current_head": stale,
                "blockers": [], "stale": False, "verified": True,
            },
        )
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
        assert proof.get("verdict") != "pass"
        sup._record_supervisor_event(
            conn, event_key="review_verdict:foreign",
            kind="review_verdict", task_id=other,
            payload={
                "verdict": "pass", "head": live, "current_head": live,
                "blockers": [], "stale": False, "verified": True,
            },
        )
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
        assert proof.get("verdict") != "pass"
        sup._record_supervisor_event(
            conn, event_key="review_verdict:unverified",
            kind="review_verdict", task_id=child,
            payload={
                "verdict": "pass", "head": live, "current_head": live,
                "blockers": [], "stale": False,
            },
        )
        sup._maybe_record_jude_proof(conn, child)
        units = {u["ref"]: u for u in sup.list_units(conn, oid)}
        proof = json.loads(units[child]["proof"] or "{}") if units[child].get("proof") else {}
        assert proof.get("verified") is not True
        assert proof.get("verdict") != "pass"
