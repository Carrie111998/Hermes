from __future__ import annotations

import json
from types import SimpleNamespace

from hermes_cli import kanban_db as kb


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
