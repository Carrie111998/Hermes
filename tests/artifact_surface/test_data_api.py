import json
import sqlite3
from artifact_surface import data_api


def test_read_events(tmp_path):
    db = tmp_path / "event_bus.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE events (event_id TEXT, event_type TEXT, source TEXT, "
                 "priority TEXT, created_at TEXT, payload TEXT, rowid_alias INTEGER)")
    conn.execute("INSERT INTO events VALUES ('e1','job_scored','matcher','normal',"
                 "'2026-06-05T10:00:00+00:00', ?, 1)", (json.dumps({"job_id": "J1"}),))
    conn.commit(); conn.close()
    rows = data_api.read_events(db_path=db, limit=10)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "job_scored"
    assert rows[0]["payload"]["job_id"] == "J1"


def test_read_events_missing_db_returns_empty(tmp_path):
    assert data_api.read_events(db_path=tmp_path / "nope.db") == []


def test_read_cron(tmp_path):
    jobs = tmp_path / "jobs.json"
    jobs.write_text(json.dumps({"jobs": [
        {"id": "a", "name": "postgres-sync", "schedule_display": "*/15 * * * *",
         "enabled": True, "state": "scheduled", "next_run_at": "2026-06-05T16:45:00-04:00",
         "last_run_at": "2026-06-05T16:30:00-04:00", "last_status": "ok",
         "last_error": None, "consecutive_errors": 0},
    ], "updated_at": "x"}), encoding="utf-8")
    rows = data_api.read_cron(jobs_path=jobs)
    assert rows[0]["name"] == "postgres-sync"
    assert rows[0]["next_run_at"] == "2026-06-05T16:45:00-04:00"
    assert "prompt" not in rows[0]


def test_read_jobflow(tmp_path):
    sub = tmp_path / "019b"
    sub.mkdir()
    (sub / "status.json").write_text(json.dumps({
        "job_id": "019b", "platform": "workday", "status": "dry_run_failed",
        "submitted": False, "success": False, "requiresHuman": True,
    }), encoding="utf-8")
    result = data_api.read_jobflow(submissions_dir=tmp_path)
    assert result["counts_by_status"]["dry_run_failed"] == 1
    assert result["submissions"][0]["platform"] == "workday"


def test_read_financier(tmp_path):
    snaps = tmp_path / "snapshots"; snaps.mkdir()
    runs = tmp_path / "runs"; runs.mkdir()
    (snaps / "latest.json").write_text(json.dumps({
        "generated_at": "2026-06-05T20:41:00+00:00", "period": "pm",
        "quotes": [{"symbol": "SPY", "price": 737.41, "change_pct": -2.59}],
    }), encoding="utf-8")
    (runs / "20260605_pm.txt").write_text("PM digest text", encoding="utf-8")
    result = data_api.read_financier(workspace_dir=tmp_path)
    assert result["snapshot"]["period"] == "pm"
    assert result["latest_digest"] == "PM digest text"


def test_read_financier_missing_returns_empty(tmp_path):
    result = data_api.read_financier(workspace_dir=tmp_path)
    assert result["snapshot"] == {}
    assert result["latest_digest"] == ""


def _write_devflow_ledger(path, *, include_leases=True, include_artifacts=True):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE requests (request_id TEXT, state TEXT, source_agent TEXT)")
    conn.executemany(
        "INSERT INTO requests VALUES (?,?,?)",
        [("dwr_triaged", "TRIAGED", "critic"), ("dwr_building", "BUILDING", "operator")],
    )
    if include_leases:
        conn.execute(
            "CREATE TABLE leases (request_id TEXT, lease_id TEXT, holder TEXT, acquired_at TEXT, "
            "expires_at TEXT, heartbeat_at TEXT, worktree_path TEXT, branch TEXT)"
        )
        conn.executemany(
            "INSERT INTO leases VALUES (?,?,?,?,?,?,?,?)",
            [
                ("dwr_building", "lse_active", "ddp.executor", "2026-08-08T10:00:00+00:00",
                 "2026-08-08T11:00:00+00:00", "2026-08-08T10:20:00+00:00", "/tmp/a", "ddp-a"),
                ("dwr_expired", "lse_expired", "ddp.executor", "2026-08-08T08:00:00+00:00",
                 "2026-08-08T09:00:00+00:00", "2026-08-08T08:20:00+00:00", "/tmp/b", "ddp-b"),
            ],
        )
    if include_artifacts:
        conn.execute(
            "CREATE TABLE artifacts (id INTEGER PRIMARY KEY, request_id TEXT, kind TEXT, ref TEXT, created_at TEXT)"
        )
        conn.execute(
            "INSERT INTO artifacts VALUES (1,?,?,?,?)",
            ("dwr_triaged", "autonomy_gate", "shadow:merge:low:shadow_mode:abc123", "2026-08-08T10:30:00+00:00"),
        )
    conn.commit()
    conn.close()


def test_read_devflow_returns_summary_from_ledger(tmp_path):
    db = tmp_path / "delegation_ledger.db"
    _write_devflow_ledger(db)

    result = data_api.read_devflow(
        ledger_path=db,
        now="2026-08-08T10:30:00+00:00",
    )

    assert result["ledger_total"] == 2
    assert result["by_state"] == {"BUILDING": 1, "TRIAGED": 1}
    assert result["by_source"] == {"critic": 1, "operator": 1}
    assert result["awaiting_approval_count"] == 1
    assert result["autonomy_decisions_recent"] == [{
        "request_id": "dwr_triaged", "mode": "shadow", "action": "merge",
        "tier": "low", "reason": "shadow_mode", "created_at": "2026-08-08T10:30:00+00:00",
    }]


def test_read_devflow_returns_bounded_filtered_request_detail(tmp_path):
    db = tmp_path / "delegation_ledger.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE requests (request_id TEXT, state TEXT, source_agent TEXT, source_kind TEXT, "
        "target_repo TEXT, target_subsystem TEXT, kind TEXT, severity TEXT, terminal_reason TEXT, "
        "created_at TEXT, updated_at TEXT, envelope_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE leases (request_id TEXT, lease_id TEXT, holder TEXT, acquired_at TEXT, "
        "expires_at TEXT, heartbeat_at TEXT, worktree_path TEXT, branch TEXT)"
    )
    conn.execute(
        "CREATE TABLE artifacts (id INTEGER PRIMARY KEY, request_id TEXT, kind TEXT, ref TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE transitions (id INTEGER PRIMARY KEY, request_id TEXT, from_state TEXT, to_state TEXT, "
        "actor TEXT, policy_version TEXT, evidence_ref TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE evidence_log (id INTEGER PRIMARY KEY, request_id TEXT, evidence_json TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE human_decisions (id INTEGER PRIMARY KEY, request_id TEXT, actor TEXT, decision TEXT, "
        "evidence_ref TEXT, confirmation_token TEXT, created_at TEXT)"
    )
    safe = json.dumps({"title": "Safe title", "acceptance_criteria": ["prove it"]})
    unsafe = json.dumps({"title": "<script>alert(1)</script>", "acceptance_criteria": ["<b>raw</b>"]})
    conn.executemany(
        "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("dwr_03", "TRIAGED", "critic", "critic", "repo", "api", "bug", "high", None,
             "2026-08-08T10:03:00+00:00", "2026-08-08T10:03:00+00:00", safe),
            ("dwr_02", "BUILDING", "operator", "explicit", "repo", "ui", "task", "low", None,
             "2026-08-08T10:02:00+00:00", "2026-08-08T10:02:00+00:00", unsafe),
            ("dwr_01", "FAILED", "critic", "critic", "repo", "api", "bug", "high", "FAILED",
             "2026-08-08T10:01:00+00:00", "2026-08-08T10:01:00+00:00", safe),
        ],
    )
    conn.execute(
        "INSERT INTO leases VALUES (?,?,?,?,?,?,?,?)",
        ("dwr_02", "lse_02", "ddp.executor", "2026-08-08T10:02:00+00:00",
         "2026-08-08T11:00:00+00:00", "2026-08-08T10:20:00+00:00", "/tmp/dwr_02", "ddp-02"),
    )
    conn.execute(
        "INSERT INTO artifacts VALUES (1,?,?,?,?)",
        ("dwr_03", "plan", "https://example.test/plan", "2026-08-08T10:04:00+00:00"),
    )
    conn.execute(
        "INSERT INTO transitions VALUES (1,?,?,?,?,?,?,?)",
        ("dwr_03", "REQUESTED", "TRIAGED", "ddp.triage", "policy-v1", "mailbox:one", "2026-08-08T10:04:00+00:00"),
    )
    conn.execute(
        "INSERT INTO evidence_log VALUES (1,?,?,?)",
        ("dwr_03", json.dumps({"reason": "operator review"}), "2026-08-08T10:04:00+00:00"),
    )
    conn.execute(
        "INSERT INTO human_decisions VALUES (1,?,?,?,?,?,?)",
        ("dwr_03", "telegram:admin-1", "approve", "operator reviewed", "secret-token", "2026-08-08T10:05:00+00:00"),
    )
    conn.commit()
    conn.close()

    result = data_api.read_devflow(
        ledger_path=db, now="2026-08-08T10:30:00+00:00", state="TRIAGED", limit=1,
    )
    detail = data_api.read_devflow(
        ledger_path=db, now="2026-08-08T10:30:00+00:00", request_id="dwr_03",
    )

    assert result["request_page"] == {"limit": 1, "next_cursor": "dwr_03", "has_more": False}
    assert result["requests"] == [{
        "request_id": "dwr_03", "state": "TRIAGED", "source_agent": "critic",
        "source_kind": "critic", "target_repo": "repo", "target_subsystem": "api",
        "kind": "bug", "severity": "high", "terminal_reason": None,
        "created_at": "2026-08-08T10:03:00+00:00", "updated_at": "2026-08-08T10:03:00+00:00",
        "title": "Safe title", "acceptance_criteria": ["prove it"], "lease": None,
        "latest_artifact": {"kind": "plan", "ref": "https://example.test/plan", "created_at": "2026-08-08T10:04:00+00:00"},
    }]
    assert result["approval_queue"] == result["requests"]
    assert detail["request_detail"]["request_id"] == "dwr_03"
    assert detail["request_detail"]["transitions"] == [{
        "from_state": "REQUESTED", "to_state": "TRIAGED", "actor": "ddp.triage",
        "policy_version": "policy-v1", "evidence_ref": "mailbox:one", "created_at": "2026-08-08T10:04:00+00:00",
    }]
    assert detail["request_detail"]["evidence"] == [{"created_at": "2026-08-08T10:04:00+00:00", "summary": {"reason": "operator review"}}]
    assert detail["request_detail"]["human_decisions"] == [{
        "actor": "telegram:admin-1", "decision": "approve",
        "evidence_ref": "operator reviewed", "created_at": "2026-08-08T10:05:00+00:00",
    }]
    assert "secret-token" not in json.dumps(detail["request_detail"])
    assert result["side_state_counts"] == {"FAILED": 1}
    assert result["ledger_freshness"] == {
        "latest_request_updated_at": "2026-08-08T10:03:00+00:00",
        "latest_transition_at": "2026-08-08T10:04:00+00:00",
        "last_successful_read_at": "2026-08-08T10:30:00+00:00",
    }


def test_read_devflow_rejects_invalid_request_query_parameters(tmp_path):
    db = tmp_path / "delegation_ledger.db"
    _write_devflow_ledger(db)

    result = data_api.read_devflow(ledger_path=db, state="not a state", limit=999, cursor="invalid")

    assert result["requests"] == []
    assert result["request_page"] == {"limit": 100, "next_cursor": None, "has_more": False}
    assert any("state" in error for error in result["read_errors"])
    assert any("cursor" in error for error in result["read_errors"])


def test_read_devflow_empty_ledger_returns_zeros(tmp_path):
    result = data_api.read_devflow(ledger_path=tmp_path / "missing.db")

    assert result["ledger_total"] == 0
    assert result["by_state"] == {}
    assert result["active_leases"] == []
    assert result["expired_leases"] == []
    assert result["read_errors"] == []


def test_read_devflow_surfaces_active_and_expired_leases(tmp_path):
    db = tmp_path / "delegation_ledger.db"
    _write_devflow_ledger(db)

    result = data_api.read_devflow(
        ledger_path=db,
        now="2026-08-08T10:30:00+00:00",
    )

    assert [lease["lease_id"] for lease in result["active_leases"]] == ["lse_active"]
    assert [lease["lease_id"] for lease in result["expired_leases"]] == ["lse_expired"]


def test_read_devflow_surfaces_live_gateway_policy(tmp_path):
    allowlist = tmp_path / "allowlist.json"
    allowlist.write_text(json.dumps({"targets": {
        "hermes": {"live_gateway_imports": True},
        "fixture": {"live_gateway_imports": False},
    }}), encoding="utf-8")

    result = data_api.read_devflow(
        ledger_path=tmp_path / "missing.db",
        allowlist_path=allowlist,
        sentinel_path=tmp_path / ".autonomy_enabled",
    )

    assert result["live_gateway_imports"] == {"hermes": True, "fixture": False}
    assert result["autonomy_sentinel_note"] == "no (shadow-first)"


def test_read_devflow_tolerates_stage_one_schema(tmp_path):
    db = tmp_path / "delegation_ledger.db"
    _write_devflow_ledger(db, include_leases=False, include_artifacts=False)

    result = data_api.read_devflow(ledger_path=db)

    assert result["ledger_total"] == 2
    assert result["active_leases"] == []
    assert result["expired_leases"] == []
    assert any("leases" in error for error in result["read_errors"])


def _write_boot_jsonl(boot_dir, boot_id="20260722-143803"):
    lines = [
        {"ev": "boot-start", "at": "2026-07-22T18:39:41Z", "bootId": boot_id},
        {"ev": "phase", "at": "2026-07-22T18:39:48Z", "phase": "Infrastructure"},
        {"ev": "step", "at": "2026-07-22T18:39:48Z", "name": "Docker Desktop daemon",
         "tier": "critical", "state": "running", "durationMs": 0, "detail": ""},
        {"ev": "step", "at": "2026-07-22T18:39:49Z", "name": "Docker Desktop daemon",
         "tier": "critical", "state": "start-error", "durationMs": 0,
         "detail": "Parameter set cannot be resolved."},
        {"ev": "step", "at": "2026-07-22T18:39:49Z", "name": "PostgreSQL :5432",
         "tier": "critical", "state": "running", "durationMs": 0, "detail": ""},
        {"ev": "step", "at": "2026-07-22T18:39:52Z", "name": "PostgreSQL :5432",
         "tier": "critical", "state": "started", "durationMs": 2180, "detail": ""},
        {"ev": "boot-end", "at": "2026-07-22T18:45:32Z", "state": "failed"},
    ]
    text = "\n".join(json.dumps(x) for x in lines) + "\n"
    # laptop-start's Add-Content writes a BOM on the first line; the reader must cope
    (boot_dir / f"boot-{boot_id}.jsonl").write_text(text, encoding="utf-8-sig")


def test_read_boot_summarizes_jsonl(tmp_path):
    boot_dir = tmp_path / "boot"; boot_dir.mkdir()
    _write_boot_jsonl(boot_dir)
    result = data_api.read_boot(boot_dir=boot_dir, progress_path=tmp_path / "nope.json")
    assert result["current"] == {}
    assert len(result["boots"]) == 1
    b = result["boots"][0]
    assert b["bootId"] == "20260722-143803"
    assert b["state"] == "failed"
    assert b["startedAt"] == "2026-07-22T18:39:41Z"
    assert b["finishedAt"] == "2026-07-22T18:45:32Z"
    assert b["durationSecs"] == 351
    assert b["counts"] == {"total": 2, "done": 1, "failed": 1, "skipped": 0}
    assert b["anomalyCount"] == 0
    steps = {s["name"]: s for s in b["steps"]}
    docker = steps["Docker Desktop daemon"]
    assert docker["state"] == "start-error"
    assert docker["detail"] == "Parameter set cannot be resolved."
    assert docker["phase"] == "Infrastructure"
    assert docker["offsetMs"] == 7000
    pg = steps["PostgreSQL :5432"]
    assert pg["state"] == "started"
    assert pg["durationMs"] == 2180
    assert pg["offsetMs"] == 8000


def test_read_boot_merges_final_snapshot(tmp_path):
    boot_dir = tmp_path / "boot"; boot_dir.mkdir()
    _write_boot_jsonl(boot_dir)
    (boot_dir / "boot-20260722-143803.final.json").write_text(json.dumps({
        "bootId": "20260722-143803", "state": "failed",
        "anomalies": [{"severity": "error", "kind": "task-329-kill",
                       "at": "2026-07-22T18:56:18Z", "count": 1, "detail": "killed"}],
        "sweep": {"lastRunAt": "2026-07-22T19:07:20Z", "runs": 6},
    }), encoding="utf-8")
    result = data_api.read_boot(boot_dir=boot_dir, progress_path=tmp_path / "nope.json")
    b = result["boots"][0]
    assert b["anomalyCount"] == 1
    assert b["anomalies"][0]["kind"] == "task-329-kill"
    assert b["sweep"]["runs"] == 6


def test_read_boot_current_boot_gets_progress_anomalies(tmp_path):
    boot_dir = tmp_path / "boot"; boot_dir.mkdir()
    _write_boot_jsonl(boot_dir)
    progress = tmp_path / "boot-progress.json"
    progress.write_text(json.dumps({
        "bootId": "20260722-143803", "state": "failed",
        "anomalies": [{"severity": "warn", "kind": "app-popup", "count": 6}],
    }), encoding="utf-8")
    result = data_api.read_boot(boot_dir=boot_dir, progress_path=progress)
    assert result["current"]["bootId"] == "20260722-143803"
    assert result["boots"][0]["anomalyCount"] == 1


def test_read_boot_missing_dir_returns_empty(tmp_path):
    result = data_api.read_boot(boot_dir=tmp_path / "nope",
                                progress_path=tmp_path / "nope.json")
    assert result == {"current": {}, "boots": []}


def test_read_boot_newest_first_and_limit(tmp_path):
    boot_dir = tmp_path / "boot"; boot_dir.mkdir()
    _write_boot_jsonl(boot_dir, boot_id="20260720-202506")
    _write_boot_jsonl(boot_dir, boot_id="20260722-143803")
    result = data_api.read_boot(boot_dir=boot_dir, progress_path=tmp_path / "n.json")
    ids = [b["bootId"] for b in result["boots"]]
    assert ids == ["20260722-143803", "20260720-202506"]
    limited = data_api.read_boot(boot_dir=boot_dir, progress_path=tmp_path / "n.json",
                                 limit=1)
    assert [b["bootId"] for b in limited["boots"]] == ["20260722-143803"]
