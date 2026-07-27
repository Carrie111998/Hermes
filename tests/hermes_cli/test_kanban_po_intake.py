from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli import kanban_db as kb


def _strict_board(tmp_path, monkeypatch, board="po-intake"):
    home = tmp_path / ".hermes"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb.ensure_product_board_defaults(board)
    path = kb.board_metadata_path(board)
    metadata = json.loads(path.read_text())
    metadata["qualification"]["required"] = True
    metadata.setdefault("product_workflow", {})["handoff_v2"] = True
    path.write_text(json.dumps(metadata))
    return board


def _active_intake(conn, monkeypatch, *, now=100):
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            {"kind": "task_create", "request": {"title": "Build export"}}
        ),
        source="work-inbox",
        session_id="session-1",
        created_at=now,
    )
    run = kb.claim_qualification_intake(
        conn,
        intake_id,
        profile="productowner",
        runtime_identity={
            "provider": "claude-cli",
            "model": "claude-opus-5",
            "effort": "high",
            "surface": "work_inbox_intake",
        },
        now=now + 1,
    )
    monkeypatch.setenv("HERMES_WORK_INBOX_INTAKE", intake_id)
    monkeypatch.setenv("HERMES_WORK_INBOX_RUN_ID", str(run["id"]))
    monkeypatch.setenv("HERMES_WORK_INBOX_CLAIM_LOCK", run["claim_lock"])
    monkeypatch.setenv("HERMES_PROFILE", "productowner")
    return intake_id, run


def _proposal():
    return {
        "work": {
            "item_kind": "card",
            "work_type": "story",
            "title": "Build export",
            "outcome": "Users can export a report",
            "scope": ["CSV export"],
            "out_of_scope": ["PDF export"],
        },
        "routing": {"epic_id": None, "dependencies": []},
        "entry_assessment": {
            "reason": "placeholder",
            "skipped_phases": [],
            "evidence": [],
        },
        "handover": {
            "deliverables": ["Architecture decision"],
            "required_evidence": ["Architecture tests"],
            "done_when": ["Architecture is implementable"],
            "next_phase": "development",
            "next_role": "developer",
        },
        "rules": {
            "allowed": ["Implement CSV export"],
            "forbidden": ["Add PDF export"],
        },
        "classification": ["framework:story", "path:po", "intake:feature"],
        "stories": [],
    }


def test_new_work_launches_configured_product_owner_without_waiting(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    conn = kb.connect(tmp_path / "kanban.db")
    intake_id = kb.create_qualification_intake(
        conn,
        raw_request=json.dumps(
            {"kind": "task_create", "request": {"title": "Assess feature"}}
        ),
        source="work-inbox",
        session_id="session-1",
        created_at=100,
    )
    identity = {
        "profile": "productowner",
        "provider": "claude-cli",
        "model": "claude-opus-5",
        "effort": "high",
        "surface": "work_inbox_intake",
        "source": "work_inbox_intake",
        "version": 1,
    }
    monkeypatch.setattr(
        kb,
        "resolve_profile_runtime_identity",
        lambda profile, **kwargs: identity,
    )
    spawned = []

    def spawn(run, *, board):
        spawned.append((run, board))
        return 4242

    try:
        result = kanban_po_intake.dispatch_product_owner_intake(
            conn,
            board="strict",
            intake_id=intake_id,
            spawn_fn=spawn,
            now=110,
        )
        persisted = kb.get_qualification_intake_run(conn, result["run_id"])
    finally:
        conn.close()

    assert result["status"] == "running"
    assert result["provider"] == "claude-cli"
    assert spawned[0][1] == "strict"
    assert persisted["worker_pid"] == 4242
    assert persisted["model"] == "claude-opus-5"
    assert persisted["effort"] == "high"


def test_spawn_is_detached_intake_scoped_and_disables_provider_fallback(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    captured = {}

    class _Popen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            self.pid = 5150

    monkeypatch.setattr(kanban_po_intake.subprocess, "Popen", _Popen)
    monkeypatch.setattr(kb, "_resolve_hermes_argv", lambda: ["/opt/hermes"])
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: tmp_path / "kanban.db")
    monkeypatch.setattr(
        kb, "workspaces_root", lambda board=None: tmp_path / "workspaces"
    )
    monkeypatch.setattr(
        kb, "worker_logs_dir", lambda board=None: tmp_path / "logs"
    )
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda profile: str(tmp_path / "profiles" / profile),
    )
    run = {
        "id": 7,
        "intake_id": "qi_one",
        "claim_lock": "claim-secret",
        "profile": "productowner",
        "provider": "claude-cli",
        "model": "claude-opus-5",
        "effort": "high",
    }

    pid = kanban_po_intake._spawn_product_owner_intake(run, board="strict")

    assert pid == 5150
    assert captured["start_new_session"] is True
    assert captured["stdin"] is kanban_po_intake.subprocess.DEVNULL
    assert captured["cmd"] == [
        "/opt/hermes",
        "-p",
        "productowner",
        "--cli",
        "--accept-hooks",
        "--toolsets",
        "kanban",
        "chat",
        "-q",
        "Assess the claimed Work Inbox intake. Use work_inbox_show first, then finish with exactly one work_inbox_decide call.",
    ]
    env = captured["env"]
    assert env["HERMES_WORK_INBOX_INTAKE"] == "qi_one"
    assert env["HERMES_WORK_INBOX_RUN_ID"] == "7"
    assert env["HERMES_WORK_INBOX_CLAIM_LOCK"] == "claim-secret"
    assert env["HERMES_DISABLE_PROVIDER_FALLBACK"] == "1"
    assert env["HERMES_INFERENCE_PROVIDER"] == "claude-cli"
    assert env["HERMES_INFERENCE_MODEL"] == "claude-opus-5"
    assert env["HERMES_INFERENCE_EFFORT"] == "high"
    assert "HERMES_KANBAN_TASK" not in env


def test_requalification_keeps_auxiliary_qualifier(monkeypatch):
    from hermes_cli import kanban_po_intake
    from hermes_cli import kanban_qualifier

    record = {
        "id": "qi_requal",
        "raw_request": json.dumps(
            {"kind": "task_requalification", "task_id": "t_one"}
        ),
    }
    called = []
    monkeypatch.setattr(
        kanban_qualifier,
        "qualify_intake",
        lambda conn, *, board, intake_id: called.append(intake_id)
        or {"status": "qualified"},
    )

    result = kanban_po_intake.route_pending_intake(
        SimpleNamespace(), board="strict", intake=record
    )

    assert result["status"] == "qualified"
    assert called == ["qi_requal"]


def test_accepted_po_decision_is_signed_and_materialized_at_architecture(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch)
    conn = kb.connect(board=board)
    intake_id, run = _active_intake(conn, monkeypatch)
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="Clear product outcome",
            proposal=_proposal(),
        )
        task = kb.get_task(conn, result["task_id"])
        contract = kb.get_work_contract(conn, task.work_contract_id)["contract"]
    finally:
        conn.close()

    assert result["status"] == "qualified"
    assert task.current_step_key == "architecture"
    assert task.assignee == "architect"
    assert contract["qualification_path"] == "po"
    assert contract["issuer"] == {
        "surface": "work_inbox_intake",
        "profile": "productowner",
        "provider": "claude-cli",
        "model": "claude-opus-5",
        "effort": "high",
        "run_id": run["id"],
        "issued_at": contract["issuer"]["issued_at"],
    }
    check = kb.connect(board=board)
    try:
        assert kb.get_qualification_intake(check, intake_id)["status"] == "qualified"
        assert kb.get_qualification_intake_run(check, run["id"])["status"] == "completed"
    finally:
        check.close()


def test_clarification_stays_inert_and_two_invalid_decisions_need_attention(
    tmp_path, monkeypatch
):
    from hermes_cli import kanban_po_intake

    board = _strict_board(tmp_path, monkeypatch, "po-intake-invalid")
    conn = kb.connect(board=board)
    intake_id, run = _active_intake(conn, monkeypatch)
    try:
        first = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="try",
            proposal={"work": {}},
        )
        second = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="accepted",
            reason="try again",
            proposal={"work": {}},
        )
        assert first["status"] == "invalid"
        assert second["status"] == "attention_required"
        assert kb.get_qualification_intake(conn, intake_id)["status"] == "attention_required"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()

    conn = kb.connect(board=board)
    second_id = kb.create_qualification_intake(
        conn,
        raw_request='{"kind":"task_create","request":{"title":"Clarify"}}',
        source="work-inbox",
        session_id="session-2",
    )
    second_run = kb.claim_qualification_intake(
        conn,
        second_id,
        profile="productowner",
        runtime_identity={"provider": "claude-cli", "model": "opus", "effort": "high"},
    )
    monkeypatch.setenv("HERMES_WORK_INBOX_INTAKE", second_id)
    monkeypatch.setenv("HERMES_WORK_INBOX_RUN_ID", str(second_run["id"]))
    monkeypatch.setenv("HERMES_WORK_INBOX_CLAIM_LOCK", second_run["claim_lock"])
    try:
        result = kanban_po_intake.decide_product_owner_intake(
            conn,
            board=board,
            disposition="needs_clarification",
            reason="Customer is ambiguous",
            question="Which customer segment?",
        )
        assert result["status"] == "needs_clarification"
        assert kb.get_qualification_intake(conn, second_id)["status"] == "needs_clarification"
        assert kb.list_qualification_intake_events(conn, second_id)[0]["payload"][
            "question"
        ] == "Which customer segment?"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()
