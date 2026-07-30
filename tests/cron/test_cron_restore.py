from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


def cli(*args: str, input_text: str | None = None):
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", *args],
        env=os.environ.copy(), input=input_text, capture_output=True, text=True,
    )


def create_snapshot():
    created = cli("cron", "create", "every 1m", "", "--name", "restore-me",
                   "--script", "restore.sh", "--no-agent", "--json")
    assert created.returncode == 0, created.stderr
    return json.loads(created.stdout)


def read_rows():
    result = cli("cron", "list", "--all", "--json")
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_top_level_stdin_restore_exact_rollback_and_main_exit(temp_home):
    snapshot = create_snapshot()
    job_id = snapshot["id"]
    assert cli("cron", "edit", job_id, "--name", "mutated", "--json").returncode == 0
    restored = cli("cron", "restore", job_id, "--snapshot-stdin", "--json",
                   input_text=json.dumps(snapshot))
    assert restored.returncode == 0, restored.stderr
    assert json.loads(restored.stdout) == snapshot
    assert read_rows() == [snapshot]
    assert cli("cron", "restore", "missing", "--snapshot-stdin", "--json",
               input_text=json.dumps(snapshot)).returncode != 0


def test_restore_rejects_bad_input_without_mutation(temp_home):
    snapshot = create_snapshot()
    before = read_rows()
    cases = [
        {**snapshot, "id": "other"},
        {**snapshot, "unknown_dangerous_field": "x"},
        {**snapshot, "enabled": "yes"},
        {**snapshot, "state": "running"},
        {**snapshot, "repeat": True},
        {**snapshot, "skills": [1]},
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


def test_restore_preserves_explicit_nulls_and_notifies_provider(temp_home, monkeypatch):
    snapshot = create_snapshot()
    calls = []
    import cron.scheduler as scheduler
    import cron.jobs as jobs
    import hermes_cli.cron as cron_cli
    monkeypatch.setattr(scheduler, "_notify_provider_jobs_changed", lambda: calls.append(1))
    snapshot.update({"prompt": None, "model": None, "provider": None, "workdir": None,
                     "next_run_at": None, "repeat": None})
    result = cli("cron", "restore", snapshot["id"], "--snapshot-stdin", "--json",
                 input_text=json.dumps(snapshot))
    assert result.returncode == 0
    monkeypatch.setattr(jobs, "restore_job", lambda job_id, value: value)
    monkeypatch.setattr(cron_cli, "_canonical_job", lambda value: value)
    from argparse import Namespace
    assert cron_cli.cron_restore(Namespace(job_id=snapshot["id"], snapshot=json.dumps(snapshot),
                                            snapshot_stdin=False, json=True)) == 0
    assert calls == [1]
    assert read_rows() == [snapshot]
