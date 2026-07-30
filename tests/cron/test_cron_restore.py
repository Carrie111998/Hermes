from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("PYTHONPYCACHEPREFIX", str(tmp_path / "pyc"))
    return tmp_path


def cli(*args: str, input_text: str | None = None):
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        env=os.environ.copy(), input=input_text, capture_output=True, text=True,
    )


def create_snapshot(schedule: str = "every 5m", *, repeat: str = "0") -> dict:
    created = cli("cron", "create", schedule, "", "--name", "restore-me",
                   "--script", "restore.sh", "--no-agent", "--provider", "openai-codex",
                   "--repeat", repeat, "--json")
    assert created.returncode == 0, created.stderr
    return json.loads(created.stdout)


def read_rows() -> list[dict]:
    result = cli("cron", "list", "--all", "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_subprocess_restore_roundtrip_and_missing_error_is_redacted(temp_home):
    snapshot = create_snapshot()
    job_id = snapshot["id"]
    before = read_rows()
    assert cli("cron", "edit", job_id, "--name", "mutated", "--json").returncode == 0
    restored = cli("cron", "restore", job_id, "--snapshot-stdin", "--json",
                   input_text=json.dumps(snapshot))
    assert restored.returncode == 0, restored.stdout
    assert json.loads(restored.stdout) == snapshot
    assert read_rows() == before
    missing = cli("cron", "restore", "missing", "--snapshot-stdin", "--json",
                  input_text=json.dumps(snapshot))
    assert missing.returncode != 0
    assert "missing" not in missing.stdout
    assert "CRON_RESTORE_ID_MISMATCH" in missing.stdout


def test_versioned_restore_rejects_unknown_nested_fields_without_mutation(temp_home):
    snapshot = create_snapshot()
    before = read_rows()
    cases = [
        {**snapshot, "record": {**snapshot["record"], "unknown_dangerous_field": "x"}},
        {**snapshot, "record": {**snapshot["record"], "skills": [1]}},
        {**snapshot, "presence": snapshot["presence"] + ["not_a_field"]},
        {**snapshot, "schedule_state": {"kind": "interval", "minutes": 1,
                                          "display": "every 1m", "unexpected": True}},
    ]
    for value in cases:
        result = cli("cron", "restore", snapshot["id"], "--snapshot-stdin", "--json",
                     input_text=json.dumps(value))
        assert result.returncode != 0
        assert read_rows() == before
    oversized = json.dumps({**snapshot, "prompt": "x" * (64 * 1024)})
    result = cli("cron", "restore", snapshot["id"], "--snapshot-stdin", "--json",
                 input_text=oversized)
    assert result.returncode != 0
    assert read_rows() == before


def test_restore_preserves_repeat_progress_presence_null_and_notification_evidence(temp_home):
    snapshot = create_snapshot(repeat="3")
    record = dict(snapshot["record"])
    record["repeat"] = {"times": 3, "completed": 2}
    record["provider"] = None
    record["model"] = None
    record["workdir"] = None
    snapshot["record"] = record
    snapshot["presence"] = sorted(record)
    snapshot["repeat_state"] = record["repeat"]
    snapshot["repeat"] = 3
    snapshot["provider"] = None
    snapshot["model"] = None
    snapshot["workdir"] = None
    restored = cli("cron", "restore", snapshot["id"], "--snapshot-stdin", "--json",
                   input_text=json.dumps(snapshot))
    assert restored.returncode == 0, restored.stdout
    assert json.loads(restored.stdout) == snapshot
    # Provider notification is an in-process hook, not a production log file.
    assert not (temp_home / "cron" / "provider_notifications.jsonl").exists()


@pytest.mark.parametrize("schedule", ["2026-07-31T10:00:00-04:00", "0 7 * * *"])
def test_one_shot_and_cron_snapshots_are_versioned_and_restoreable(temp_home, schedule):
    snapshot = create_snapshot(schedule)
    assert snapshot["schema_version"] == 2
    assert snapshot["record"]["schedule"] == snapshot["schedule_state"]
    restored = cli("cron", "restore", snapshot["id"], "--snapshot-stdin", "--json",
                   input_text=json.dumps(snapshot))
    assert restored.returncode == 0, restored.stdout
    assert json.loads(restored.stdout) == snapshot


def test_direct_restore_rejects_non_json_graphs_before_mutation(temp_home):
    from cron.jobs import restore_job

    snapshot = create_snapshot()
    job_id = snapshot["id"]
    before = read_rows()
    cases = [
        ({**snapshot, "record": {**snapshot["record"], "skills": ("x",)}},
         "CRON_RESTORE_NON_SERIALIZABLE"),
        ({**snapshot, "record": {**snapshot["record"], "origin": {"value": float("nan")}}},
         "CRON_RESTORE_NON_SERIALIZABLE"),
        ({**snapshot, "record": {**snapshot["record"], "origin": {"value": 2**10000}}},
         "CRON_RESTORE_BOUNDS_EXCEEDED"),
    ]
    cycle = {}
    cycle["self"] = cycle
    cases.append(({**snapshot, "record": {**snapshot["record"], "origin": cycle}},
                  "CRON_RESTORE_CYCLE"))

    for invalid_snapshot, code in cases:
        with pytest.raises(ValueError, match=code):
            restore_job(job_id, invalid_snapshot)
        assert read_rows() == before


def test_direct_restore_rejects_canonical_utf8_payload_over_64k_before_mutation(temp_home):
    from cron.jobs import restore_job

    snapshot = create_snapshot()
    before = read_rows()
    record = dict(snapshot["record"])
    record["origin"] = {f"k{i}": "x" * 14_000 for i in range(5)}
    with pytest.raises(ValueError, match="CRON_RESTORE_BOUNDS_EXCEEDED"):
        restore_job(snapshot["id"], {**snapshot, "record": record})
    assert read_rows() == before


def test_cli_restore_maps_only_explicit_restore_codes(monkeypatch, capsys):
    from hermes_cli import cron as cron_cli

    class Stdin:
        buffer = type("Buffer", (), {"read": lambda self, limit: b"{}"})()

    monkeypatch.setattr(sys, "stdin", Stdin())
    monkeypatch.setattr("cron.jobs.restore_job", lambda *_args: (_ for _ in ()).throw(
        ValueError("CRON_RESTORE_INVENTED_SECRET")
    ))
    rc = cron_cli.cron_restore(SimpleNamespace(
        job_id="job-1", snapshot_stdin=True, json=False,
    ))
    assert rc == 1
    output = capsys.readouterr().out
    assert "CRON_RESTORE_INVALID_SNAPSHOT" in output
    assert "INVENTED_SECRET" not in output


def test_cli_restore_notifies_provider_once_only_after_success(temp_home, monkeypatch, capsys):
    from hermes_cli import cron as cron_cli

    snapshot = create_snapshot()
    calls = []
    monkeypatch.setattr("cron.scheduler._notify_provider_jobs_changed",
                        lambda: calls.append("notified"))
    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {
        "buffer": type("Buffer", (), {
            "read": lambda self, limit: json.dumps(snapshot).encode("utf-8")
        })()
    })())
    assert cron_cli.cron_restore(SimpleNamespace(
        job_id=snapshot["id"], snapshot_stdin=True, json=True,
    )) == 0
    capsys.readouterr()
    assert calls == ["notified"]

    monkeypatch.setattr(sys, "stdin", type("Stdin", (), {
        "buffer": type("Buffer", (), {
            "read": lambda self, limit: json.dumps(snapshot).encode("utf-8")
        })()
    })())
    assert cron_cli.cron_restore(SimpleNamespace(
        job_id="missing", snapshot_stdin=True, json=False,
    )) == 1
    capsys.readouterr()
    assert calls == ["notified"]
