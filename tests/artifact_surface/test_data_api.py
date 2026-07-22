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
