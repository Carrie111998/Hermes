from __future__ import annotations

import concurrent.futures
import json
import sqlite3
from pathlib import Path

from hermes_cli import agents_os


def test_legacy_task_foreign_keys_are_repaired_without_data_loss(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    paths = agents_os.resolve_paths(None)
    paths.root.mkdir(parents=True)
    with sqlite3.connect(paths.db) as conn:
        conn.executescript("""
            CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                workflow TEXT, priority INTEGER NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, notes TEXT NOT NULL, route TEXT, approval_required INTEGER NOT NULL);
            INSERT INTO tasks VALUES ('task-legacy','Legacy','pending',NULL,3,'now','now','',NULL,0);
            CREATE TABLE approvals (id TEXT PRIMARY KEY, title TEXT NOT NULL, status TEXT NOT NULL,
                risk TEXT NOT NULL, task_id TEXT, payload TEXT NOT NULL, created_at TEXT NOT NULL,
                resolved_at TEXT, FOREIGN KEY(task_id) REFERENCES "tasks_legacy_v1"(id));
            CREATE TABLE artifacts (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                path TEXT NOT NULL, task_id TEXT, workflow TEXT, created_at TEXT NOT NULL, run_id TEXT,
                FOREIGN KEY(task_id) REFERENCES "tasks_legacy_v1"(id));
            CREATE TABLE events (id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, event_type TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES "tasks_legacy_v1"(id));
            CREATE TABLE runs (id TEXT PRIMARY KEY, task_id TEXT, workflow TEXT NOT NULL, status TEXT NOT NULL,
                input TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT,
                FOREIGN KEY(task_id) REFERENCES "tasks_legacy_v1"(id));
            CREATE TABLE reviews (id TEXT PRIMARY KEY, task_id TEXT, run_id TEXT, status TEXT NOT NULL,
                kind TEXT NOT NULL, reviewer TEXT NOT NULL, notes TEXT NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(task_id) REFERENCES "tasks_legacy_v1"(id));
            INSERT INTO approvals VALUES ('approval-legacy','Legacy','pending','normal','task-legacy','','now',NULL);
            INSERT INTO artifacts VALUES ('artifact-legacy','test','Legacy','/tmp/legacy','task-legacy',NULL,'now',NULL);
            INSERT INTO events VALUES ('event-legacy','task-legacy',NULL,'legacy','{}','now');
            INSERT INTO runs VALUES ('run-legacy','task-legacy','test','created','','now',NULL);
            INSERT INTO reviews VALUES ('review-legacy','task-legacy','run-legacy','pending','general','','','now');
        """)

    with agents_os.connect(paths) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        for table in ("approvals", "events", "runs", "reviews"):
            assert {row[2] for row in conn.execute(f"PRAGMA foreign_key_list({table})")} == {"tasks"}
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_key_list(artifacts)").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1

    result = agents_os.AgentsOSService(paths).automation_intake_payload({
        "schema_version": "automation-intake.v0",
        "source": "legacy-repair-test",
        "event_id": "one",
        "goal": "Verify repaired foreign keys accept a safe local intake",
        "callback": {"type": "local_file"},
    })
    assert result["status"] == "created"


def test_approval_cli_resolution_updates_linked_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "run", "external-action-draft", "approval payload", "--task-id", "task-cli-approval"]) == 0
    created = _json_out(capsys)

    assert agents_os.main(["--vault-root", str(vault), "approval", "set", created["approval_id"], "approved", "--notes", "operator approved"]) == 0
    capsys.readouterr()
    with agents_os.connect(agents_os.resolve_paths(None)) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id='task-cli-approval'").fetchone()
        event = conn.execute("SELECT * FROM events WHERE task_id='task-cli-approval' AND event_type='approval_resolved'").fetchone()
    assert task["status"] == "ready"
    assert task["approval_required"] == 0
    assert event is not None


def test_dashboard_counts_all_tasks_beyond_preview_limit(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()
    for index in range(25):
        assert agents_os.main(["--vault-root", str(vault), "task", "add", f"Task {index}", "--id", f"task-{index}"]) == 0
        capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "dashboard", "--json"]) == 0
    dashboard = _json_out(capsys)
    assert dashboard["queue_summary"]["open_tasks"] == 25
    assert len(dashboard["tasks"]) == 20


def test_automation_concurrent_dedup_and_external_gate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()
    service = agents_os.AgentsOSService(agents_os.resolve_paths(None))
    payload = {
        "schema_version": "automation-intake.v0",
        "source": "pytest-concurrency",
        "event_id": "same-event",
        "goal": "Create exactly one concurrent automation intake",
        "callback": {"type": "local_file"},
    }
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: service.automation_intake_payload(payload), range(6)))
    assert sum(item["status"] == "created" for item in results) == 1
    assert sum(item["status"] == "deduped" for item in results) == 5
    external = service.automation_intake_payload({
        **payload,
        "source": "pytest-webhook",
        "event_id": "external-event",
        "goal": "Prepare a webhook callback approval draft",
        "callback": {"type": "webhook", "approval_required": True},
    })
    assert external["status"] == "created"
    assert external["approval_required"] is True


def _json_out(capsys):
    return json.loads(capsys.readouterr().out)


def test_agents_os_complete_runtime_sprints(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()

    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()

    assert agents_os.main(["--vault-root", str(vault), "doctor", "--json"]) == 0
    doctor = _json_out(capsys)
    assert doctor["checks"]["schema_version"] == "6"
    assert doctor["checks"]["orphan_records"] == 0
    assert doctor["checks"]["policy_home_isolated"] is True

    assert agents_os.main(["--vault-root", str(vault), "snapshot", "create", "baseline", "--json"]) == 0
    snap = _json_out(capsys)
    assert snap["snapshot_id"].startswith("snapshot-")
    assert Path(snap["export_path"]).exists()

    assert agents_os.main(["--vault-root", str(vault), "agent", "add", "local-agent", "--name", "Local Agent", "--capabilities", "code,research,qa", "--json"]) == 0
    agent = _json_out(capsys)
    assert agent["id"] == "local-agent"
    assert "code" in agent["capabilities"]

    assert agents_os.main(["--vault-root", str(vault), "agent", "list", "--json"]) == 0
    agents = _json_out(capsys)
    assert agents[0]["status"] == "available"

    wf_path = tmp_path / "workflow.json"
    wf_path.write_text(json.dumps({
        "id": "local-proof",
        "kind": "implementation",
        "requires_approval": False,
        "template": "Local proof workflow",
        "route": "local:direct",
        "capabilities": ["code"],
        "allowed_paths": [str(tmp_path)],
        "blocked_paths": [],
    }), encoding="utf-8")
    assert agents_os.main(["--vault-root", str(vault), "workflow", "validate", str(wf_path), "--json"]) == 0
    wf_valid = _json_out(capsys)
    assert wf_valid["valid"] is True
    assert agents_os.main(["--vault-root", str(vault), "workflow", "import", str(wf_path), "--json"]) == 0
    wf_import = _json_out(capsys)
    assert wf_import["workflow_id"] == "local-proof"

    assert agents_os.main(["--vault-root", str(vault), "run", "code-task", "implement local proof", "--title", "Proof", "--task-id", "task-proof"]) == 0
    run_result = _json_out(capsys)
    assert run_result["task_id"] == "task-proof"

    assert agents_os.main(["--vault-root", str(vault), "route", "task-proof", "--json"]) == 0
    route = _json_out(capsys)
    assert route["execution_allowed"] is True
    assert route["assigned_agent"] == "local-agent"

    assert agents_os.main(["--vault-root", str(vault), "execute", "task-proof", "--json"]) == 2
    executed = _json_out(capsys)
    assert executed == {"task_id": "task-proof", "status": "blocked", "reason": "runtime_required"}
    assert agents_os.main(["--vault-root", str(vault), "task", "set", "task-proof", "in_progress"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "task", "set", "task-proof", "review"]) == 0
    capsys.readouterr()

    assert agents_os.main(["--vault-root", str(vault), "review", "request", "task-proof", "--kind", "spec", "--json"]) == 0
    review = _json_out(capsys)
    assert review["review_id"].startswith("review-")
    assert agents_os.main(["--vault-root", str(vault), "review", "set", review["review_id"], "approved", "--notes", "ok", "--json"]) == 0
    review_done = _json_out(capsys)
    assert review_done["status"] == "approved"

    assert agents_os.main(["--vault-root", str(vault), "task", "set", "task-proof", "completed"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "task", "add", "Blocked task", "--id", "task-blocked", "--workflow", "code-task"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "task", "set", "task-blocked", "blocked"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "run", "external-action-draft", "approval payload", "--task-id", "task-approval", "--title", "Approval draft"]) == 0
    capsys.readouterr()

    assert agents_os.main(["--vault-root", str(vault), "dashboard", "--json"]) == 0
    dashboard = _json_out(capsys)
    assert dashboard["health"]["ok"] is True
    assert dashboard["queue_summary"] == {
        "open_tasks": 1,
        "blocked_tasks": 1,
        "review_tasks": 0,
        "completed_tasks": 1,
        "pending_approvals": 1,
        "failed_executions": 0,
        "stale_drafts": 2,
        "action_required": 2,
    }
    assert dashboard["tasks"][0]["id"] == "task-blocked"
    assert dashboard["agents"][0]["id"] == "local-agent"
    assert dashboard["reviews"][0]["status"] == "approved"
    assert dashboard["snapshots"][0]["label"] == "baseline"
    run_kinds = {(run["task_id"], run["status"]): run["kind"] for run in dashboard["runs"]}
    assert run_kinds[("task-proof", "created")] == "draft"
    assert run_kinds[("task-approval", "created")] == "draft"
    dashboard_text = Path(dashboard["dashboard_path"]).read_text(encoding="utf-8")
    assert "## Queue summary" in dashboard_text
    assert "action_required: 2" in dashboard_text
    assert "pending_approvals: 1" in dashboard_text
    assert "stale_drafts: 2" in dashboard_text
    assert "kind=draft" in dashboard_text
    assert "kind=draft" in dashboard_text
    assert "kind=draft" in dashboard_text
    assert "## Agent registry" in dashboard_text
    assert "## Review gateovi" in dashboard_text
    assert "## Snapshoti" in dashboard_text

    assert agents_os.main(["--vault-root", str(vault), "maintenance", "--json"]) == 0
    maintenance = _json_out(capsys)
    assert maintenance["status"] == "ok"
    assert Path(maintenance["report_path"]).exists()

    service = agents_os.AgentsOSService(agents_os.resolve_paths(None))
    status = service.status_payload()
    assert status["status"] == "ok"
    assert status["schema_version"] == "6"

    assert agents_os.main(["--vault-root", str(vault), "service", "status", "--json"]) == 0
    service_cli = _json_out(capsys)
    assert service_cli["status"] == "ok"
    assert service_cli["schema_version"] == "6"

    assert agents_os.main(["--vault-root", str(vault), "docs", "--json"]) == 0
    docs = _json_out(capsys)
    assert Path(docs["docs_path"]).exists()
    assert set(docs["docs"].keys()) == {"runtime", "command_reference", "recovery_runbook", "safety_policy"}
    text = Path(docs["docs_path"]).read_text(encoding="utf-8")
    assert "Agents OS" in text
    assert "schema_version: 6" in text
    for path in docs["docs"].values():
        assert Path(path).exists()


def test_agents_os_close_requires_evidence_or_approved_review(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()

    assert agents_os.main(["--vault-root", str(vault), "run", "code-task", "close proof", "--task-id", "task-close"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "route", "task-close", "--json"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "execute", "task-close", "--json"]) == 2
    blocked = _json_out(capsys)
    assert blocked["reason"] == "runtime_required"
    with agents_os.connect(agents_os.resolve_paths(None)) as conn:
        conn.execute("UPDATE tasks SET status='review',updated_at=? WHERE id='task-close'", (agents_os.utc_now(),))
        conn.commit()

    assert agents_os.main(["--vault-root", str(vault), "close", "task-close", "--json"]) == 2
    rejected = _json_out(capsys)
    assert rejected["status"] == "error"
    assert rejected["reason"] == "evidence_or_approved_review_required"

    assert agents_os.main(["--vault-root", str(vault), "review", "request", "task-close", "--kind", "qa", "--json"]) == 0
    review = _json_out(capsys)
    assert agents_os.main(["--vault-root", str(vault), "review", "set", review["review_id"], "approved", "--notes", "ok", "--json"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "close", "task-close", "--review-id", review["review_id"], "--json"]) == 0
    closed = _json_out(capsys)
    assert closed["status"] == "completed"
    assert closed["review_id"] == review["review_id"]

    assert agents_os.main(["--vault-root", str(vault), "dashboard", "--json"]) == 0
    dashboard = _json_out(capsys)
    assert dashboard["recent_completions"][0]["task_id"] == "task-close"
    assert dashboard["recent_completions"][0]["review_id"] == review["review_id"]

    assert agents_os.main(["--vault-root", str(vault), "run", "code-task", "evidence proof", "--task-id", "task-evidence"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "close", "task-evidence", "--evidence", "local proof text", "--json"]) == 0
    evidence_closed = _json_out(capsys)
    assert evidence_closed["status"] == "completed"
    assert evidence_closed["evidence"] == "local proof text"



def test_agents_os_agent_crud_and_routing_policy(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()

    wf_path = tmp_path / "code-workflow.json"
    wf_path.write_text(json.dumps({
        "id": "needs-code",
        "kind": "implementation",
        "requires_approval": False,
        "template": "Needs code capability",
        "route": "local:direct",
        "capabilities": ["code"],
        "allowed_paths": [str(tmp_path)],
        "blocked_paths": [],
    }), encoding="utf-8")
    assert agents_os.main(["--vault-root", str(vault), "workflow", "import", str(wf_path), "--json"]) == 0
    capsys.readouterr()

    assert agents_os.main(["--vault-root", str(vault), "agent", "add", "researcher", "--capabilities", "research", "--json"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "task", "add", "Needs code", "--id", "task-needs-code", "--workflow", "needs-code"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "route", "task-needs-code", "--json"]) == 0
    no_agent_route = _json_out(capsys)
    assert no_agent_route["assigned_agent"] is None
    assert no_agent_route["execution_allowed"] is False
    assert no_agent_route["new_status"] == "blocked"

    assert agents_os.main(["--vault-root", str(vault), "agent", "add", "coder", "--capabilities", "code", "--status", "disabled", "--json"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "agent", "set", "coder", "--status", "available", "--json"]) == 0
    updated = _json_out(capsys)
    assert updated["status"] == "available"
    assert agents_os.main(["--vault-root", str(vault), "agent", "show", "coder", "--json"]) == 0
    shown = _json_out(capsys)
    assert shown["id"] == "coder"
    assert shown["capabilities"] == ["code"]

    assert agents_os.main(["--vault-root", str(vault), "route", "task-needs-code", "--json"]) == 0
    assigned_route = _json_out(capsys)
    assert assigned_route["assigned_agent"] == "coder"
    assert assigned_route["execution_allowed"] is True

    assert agents_os.main(["--vault-root", str(vault), "agent", "remove", "coder", "--json"]) == 0
    removed = _json_out(capsys)
    assert removed["removed"] is True
    assert agents_os.main(["--vault-root", str(vault), "agent", "show", "coder", "--json"]) == 2
    missing = _json_out(capsys)
    assert missing["reason"] == "agent_not_found"



def test_agents_os_workflow_schema_v1_show_persists_contract(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()

    wf_path = tmp_path / "schema-v1.json"
    contract = {
        "id": "schema-v1-proof",
        "kind": "implementation",
        "requires_approval": False,
        "template": "Schema v1 proof",
        "route": "local:direct",
        "capabilities": ["code"],
        "allowed_paths": [str(tmp_path)],
        "blocked_paths": [],
        "approval_risks": ["runtime-config-change"],
        "precheck": ["doctor"],
        "execute": ["local-only"],
        "verify": ["pytest"],
        "review": ["qa"],
        "close": ["evidence-required"],
    }
    wf_path.write_text(json.dumps(contract), encoding="utf-8")
    assert agents_os.main(["--vault-root", str(vault), "workflow", "validate", str(wf_path), "--json"]) == 0
    validated = _json_out(capsys)
    assert validated["valid"] is True
    assert agents_os.main(["--vault-root", str(vault), "workflow", "import", str(wf_path), "--json"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "workflow", "show", "schema-v1-proof", "--json"]) == 0
    shown = _json_out(capsys)
    assert shown["id"] == "schema-v1-proof"
    assert shown["approval_risks"] == ["runtime-config-change"]
    assert shown["precheck"] == ["doctor"]
    assert shown["execute"] == ["local-only"]
    assert shown["verify"] == ["pytest"]
    assert shown["review"] == ["qa"]
    assert shown["close"] == ["evidence-required"]



def test_agents_os_policy_blocks_credential_paths_and_bad_home(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()

    wf_path = tmp_path / "unsafe-workflow.json"
    wf_path.write_text(json.dumps({
        "id": "unsafe",
        "kind": "implementation",
        "requires_approval": False,
        "template": "Unsafe workflow",
        "route": "local:direct",
        "capabilities": ["code"],
        "allowed_paths": [str(tmp_path / ".env")],
        "blocked_paths": [],
    }), encoding="utf-8")
    assert agents_os.main(["--vault-root", str(vault), "workflow", "validate", str(wf_path), "--json"]) == 2
    invalid = _json_out(capsys)
    assert invalid["valid"] is False
    assert "credential_path:allowed_paths" in invalid["errors"]

    monkeypatch.setenv("AGENTS_OS_HOME", str(tmp_path / "external-runtime" / "agents_os"))
    assert agents_os.main(["--vault-root", str(vault), "doctor", "--json"]) == 1
    doctor = _json_out(capsys)
    assert doctor["checks"]["policy_home_isolated"] is False
    monkeypatch.delenv("AGENTS_OS_HOME", raising=False)



def test_agents_os_mirror_validate_detects_missing_dashboard(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "dashboard", "--json"]) == 0
    dashboard = _json_out(capsys)
    Path(dashboard["dashboard_path"]).unlink()

    assert agents_os.main(["--vault-root", str(vault), "mirror", "validate", "--json"]) == 1
    invalid = _json_out(capsys)
    assert invalid["status"] == "attention"
    assert "missing_dashboard" in invalid["issues"]

    assert agents_os.main(["--vault-root", str(vault), "mirror", "rebuild", "--json"]) == 0
    rebuilt = _json_out(capsys)
    assert Path(rebuilt["dashboard_path"]).exists()
    assert agents_os.main(["--vault-root", str(vault), "mirror", "validate", "--json"]) == 0
    valid = _json_out(capsys)
    assert valid["status"] == "ok"



def test_agents_os_execute_dry_run_does_not_mutate_task_to_review(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "agent", "add", "local-agent", "--capabilities", "code", "--json"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "run", "code-task", "dry run", "--task-id", "task-dry"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "route", "task-dry", "--json"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "execute", "task-dry", "--dry-run", "--json"]) == 0
    dry = _json_out(capsys)
    assert dry["status"] == "dry_run"
    assert agents_os.main(["--vault-root", str(vault), "task", "list", "--status", "ready", "--json"]) == 0
    ready = _json_out(capsys)
    assert ready[0]["id"] == "task-dry"



def test_agents_os_service_adapter_exposes_core_payloads(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()
    service = agents_os.AgentsOSService(agents_os.resolve_paths(None))
    assert service.status_payload()["status"] == "ok"
    assert service.doctor_payload()["ok"] is True
    assert service.dashboard_payload()["health"]["ok"] is True
    assert service.maintenance_payload()["status"] == "ok"



def test_agents_os_execute_blocks_approval_gated_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    vault = tmp_path / "vault"
    vault.mkdir()
    assert agents_os.main(["--vault-root", str(vault), "init", "--no-vault"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "run", "external-action-draft", "publish", "--task-id", "task-public"]) == 0
    capsys.readouterr()
    assert agents_os.main(["--vault-root", str(vault), "execute", "task-public", "--json"]) == 2
    blocked = _json_out(capsys)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "approval_required"
