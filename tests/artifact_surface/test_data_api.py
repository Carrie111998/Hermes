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
