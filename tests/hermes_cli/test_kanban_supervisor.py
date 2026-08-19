"""LS-2776: active_pr classifier, objective ledger, starvation, Remoko."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
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

    def answer(self, request_id: str, answer: str) -> None:
        rec = self.requests[str(request_id)]
        rec["status"] = "answered"
        rec["answer"] = answer


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
    # Delegator process gone.
    sup.note_bot_chat_complete(session_id="20260819_bot", owner_profile="jude")
    with kb.connect() as conn:
        units = sup.list_units(conn, oid)
        bot = [u for u in units if u["kind"] == "bot_chat"]
        assert bot and bot[0]["status"] == "done"


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
