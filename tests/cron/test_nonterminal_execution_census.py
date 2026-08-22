"""Read-only, lossless census of claimed and running cron executions."""

from __future__ import annotations

import sqlite3


def _point_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return executions


def test_census_returns_every_full_nonterminal_row_without_mutation(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    first = executions.create_execution("job-000", source="builtin")
    executions.mark_execution_running(first["id"])
    for index in range(1, 505):
        executions.create_execution(f"job-{index:03d}", source="builtin")
    terminal = executions.create_execution("finished", source="builtin")
    executions.finish_execution(terminal["id"], success=True)

    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        before = conn.execute(
            "SELECT * FROM executions ORDER BY claimed_at, id"
        ).fetchall()

    rows = executions.nonterminal_execution_census()

    assert len(rows) == 505
    assert {row["id"] for row in rows} == {
        row[0] for row in before if row[6] in {"claimed", "running"}
    }
    assert [(row["claimed_at"], row["id"]) for row in rows] == sorted(
        (row["claimed_at"], row["id"]) for row in rows
    )
    durable_columns = {
        "id", "job_id", "source", "process_id", "pid", "process_started_at",
        "status", "claimed_at", "started_at", "finished_at", "error",
    }
    assert all(durable_columns < row.keys() for row in rows)
    assert all(row["owner_liveness"] == "live" for row in rows)
    assert all(
        row["owner_liveness_evidence"] == {
            "process_id": row["process_id"],
            "pid": row["pid"],
            "process_started_at": row["process_started_at"],
            "reason": "process_start_time_matches",
            "pid_exists": True,
            "observed_process_started_at": row["process_started_at"],
        }
        for row in rows
    )

    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        after = conn.execute(
            "SELECT * FROM executions ORDER BY claimed_at, id"
        ).fetchall()
    assert after == before


def _insert_foreign_execution(executions, *, suffix, pid, started_at):
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at)
               VALUES (?, ?, 'builtin', ?, ?, ?, 'claimed', ?)""",
            (
                f"execution-{suffix}",
                f"job-{suffix}",
                f"process-{suffix}",
                pid,
                started_at,
                f"2026-08-21T12:00:00.{len(suffix):06d}-04:00",
            ),
        )


def test_census_does_not_trust_inherited_process_uuid_without_pid_proof(
    monkeypatch, tmp_path
):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions._connect().close()
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            """INSERT INTO executions
               (id, job_id, source, process_id, pid, process_started_at,
                status, claimed_at)
               VALUES ('forked', 'job-forked', 'builtin', ?, 4999, 111,
                       'running', '2026-08-21T12:00:00-04:00')""",
            (executions._PROCESS_ID,),
        )
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr(executions, "_process_start_time", lambda _pid: 999)

    row = executions.nonterminal_execution_census()[0]

    assert row["owner_liveness"] == "dead"
    assert row["owner_liveness_evidence"]["reason"] == "process_start_time_mismatch"


def test_census_classifies_exact_foreign_owner_evidence(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions._connect().close()
    _insert_foreign_execution(executions, suffix="live", pid=4101, started_at=101)
    _insert_foreign_execution(executions, suffix="recycled", pid=4102, started_at=202)
    _insert_foreign_execution(executions, suffix="gone", pid=4103, started_at=303)

    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda pid: pid != 4103,
    )
    observed = {4101: 101, 4102: 999}
    monkeypatch.setattr(executions, "_process_start_time", observed.get)

    by_job = {row["job_id"]: row for row in executions.nonterminal_execution_census()}

    assert by_job["job-live"]["owner_liveness"] == "live"
    assert by_job["job-live"]["owner_liveness_evidence"] == {
        "process_id": "process-live",
        "pid": 4101,
        "process_started_at": 101,
        "reason": "process_start_time_matches",
        "pid_exists": True,
        "observed_process_started_at": 101,
    }
    assert by_job["job-recycled"]["owner_liveness"] == "dead"
    assert by_job["job-recycled"]["owner_liveness_evidence"] == {
        "process_id": "process-recycled",
        "pid": 4102,
        "process_started_at": 202,
        "reason": "process_start_time_mismatch",
        "pid_exists": True,
        "observed_process_started_at": 999,
    }
    assert by_job["job-gone"]["owner_liveness"] == "dead"
    assert by_job["job-gone"]["owner_liveness_evidence"] == {
        "process_id": "process-gone",
        "pid": 4103,
        "process_started_at": 303,
        "reason": "pid_not_found",
        "pid_exists": False,
    }


def test_census_treats_missing_identity_evidence_as_unprovable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions._connect().close()
    _insert_foreign_execution(executions, suffix="no-recorded-start", pid=4201, started_at=None)
    _insert_foreign_execution(executions, suffix="no-observed-start", pid=4202, started_at=222)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)
    monkeypatch.setattr(executions, "_process_start_time", lambda _pid: None)

    by_job = {row["job_id"]: row for row in executions.nonterminal_execution_census()}

    assert by_job["job-no-recorded-start"]["owner_liveness"] == "unprovable"
    assert by_job["job-no-recorded-start"]["owner_liveness_evidence"] == {
        "process_id": "process-no-recorded-start",
        "pid": 4201,
        "process_started_at": None,
        "reason": "recorded_process_start_time_missing",
        "pid_exists": True,
    }
    assert by_job["job-no-observed-start"]["owner_liveness"] == "unprovable"
    assert by_job["job-no-observed-start"]["owner_liveness_evidence"] == {
        "process_id": "process-no-observed-start",
        "pid": 4202,
        "process_started_at": 222,
        "reason": "observed_process_start_time_unavailable",
        "pid_exists": True,
        "observed_process_started_at": None,
    }


def test_census_treats_indeterminate_pid_probe_as_unprovable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions._connect().close()
    _insert_foreign_execution(executions, suffix="pid-indeterminate", pid=4250, started_at=111)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: None)

    row = executions.nonterminal_execution_census()[0]

    assert row["owner_liveness"] == "unprovable"
    assert row["owner_liveness_evidence"] == {
        "process_id": "process-pid-indeterminate",
        "pid": 4250,
        "process_started_at": 111,
        "reason": "pid_probe_indeterminate",
        "pid_exists": None,
    }


def test_census_treats_liveness_probe_errors_as_unprovable(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions._connect().close()
    _insert_foreign_execution(executions, suffix="pid-error", pid=4301, started_at=111)
    _insert_foreign_execution(executions, suffix="start-error", pid=4302, started_at=222)

    def probe_pid(pid):
        if pid == 4301:
            raise PermissionError("pid denied")
        return True

    def probe_start(pid):
        assert pid == 4302
        raise OSError("start raced")

    monkeypatch.setattr("gateway.status._pid_exists", probe_pid)
    monkeypatch.setattr(executions, "_process_start_time", probe_start)

    by_job = {row["job_id"]: row for row in executions.nonterminal_execution_census()}

    assert by_job["job-pid-error"]["owner_liveness"] == "unprovable"
    assert by_job["job-pid-error"]["owner_liveness_evidence"] == {
        "process_id": "process-pid-error",
        "pid": 4301,
        "process_started_at": 111,
        "reason": "pid_probe_error",
        "probe_error": "PermissionError: pid denied",
    }
    assert by_job["job-start-error"]["owner_liveness"] == "unprovable"
    assert by_job["job-start-error"]["owner_liveness_evidence"] == {
        "process_id": "process-start-error",
        "pid": 4302,
        "process_started_at": 222,
        "reason": "process_start_time_probe_error",
        "pid_exists": True,
        "probe_error": "OSError: start raced",
    }


def test_census_bounds_probe_error_evidence(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions._connect().close()
    _insert_foreign_execution(executions, suffix="long-error", pid=4350, started_at=111)
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda _pid: (_ for _ in ()).throw(RuntimeError("x" * 5000)),
    )

    evidence = executions.nonterminal_execution_census()[0]["owner_liveness_evidence"]

    assert evidence["probe_error"].startswith("RuntimeError: ")
    assert len(evidence["probe_error"]) <= 500


def test_cross_profile_census_reads_default_and_every_named_profile(
    monkeypatch, tmp_path
):
    import cron.executions as executions

    root = tmp_path / "root"
    default_ledger = root / "cron" / "executions.db"
    tracker_ledger = root / "profiles" / "tracker" / "cron" / "executions.db"
    for ledger, suffix in ((default_ledger, "default"), (tracker_ledger, "tracker")):
        monkeypatch.setattr(executions, "EXECUTIONS_FILE", ledger)
        executions.create_execution(f"job-{suffix}", source="builtin")
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", tracker_ledger)
    monkeypatch.setattr(executions, "_canonical_hermes_root", lambda: root)

    rows = executions.cross_profile_nonterminal_execution_census()

    assert {row["job_id"] for row in rows} == {"job-default", "job-tracker"}
    assert {row["execution_ledger"] for row in rows} == {
        str(default_ledger.resolve()),
        str(tracker_ledger.resolve()),
    }


def test_cross_profile_census_refuses_malformed_profile_ledger(monkeypatch, tmp_path):
    import cron.executions as executions

    root = tmp_path / "root"
    malformed = root / "profiles" / "broken" / "cron" / "executions.db"
    malformed.parent.mkdir(parents=True)
    malformed.write_bytes(b"not sqlite")
    monkeypatch.setattr(executions, "_canonical_hermes_root", lambda: root)

    import pytest

    with pytest.raises(sqlite3.DatabaseError):
        executions.cross_profile_nonterminal_execution_census()


def test_census_raises_instead_of_returning_partial_rows(monkeypatch, tmp_path):
    executions = _point_ledger(monkeypatch, tmp_path)
    executions._connect().close()
    _insert_foreign_execution(executions, suffix="good", pid=4401, started_at=111)
    _insert_foreign_execution(executions, suffix="malformed", pid=4402, started_at=222)
    with sqlite3.connect(executions.EXECUTIONS_FILE) as conn:
        conn.execute(
            "UPDATE executions SET pid=? WHERE id=?",
            ("not-an-integer", "execution-malformed"),
        )

    import pytest

    with pytest.raises(ValueError):
        executions.nonterminal_execution_census()
