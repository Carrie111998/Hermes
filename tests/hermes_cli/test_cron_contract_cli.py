from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]


def run_cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(ROOT)}
    return subprocess.run(
        [sys.executable, "-m", "hermes_cli.main", "cron", *args],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_rejects_impossible_shapes_without_mutation(tmp_path):
    home = tmp_path / ".hermes"
    (home / "scripts").mkdir(parents=True)
    cases = (
        ("create", "every 5m", "--no-agent"),
        ("create", "every 5m", ""),
    )
    for args in cases:
        result = run_cli(home, *args)
        assert result.returncode != 0
    jobs = home / "cron" / "jobs.json"
    assert not jobs.exists() or jobs.read_text(encoding="utf-8") in {"[]", ""}


def test_cli_rejects_raw_snapshot_argument_without_mutation(tmp_path):
    home = tmp_path / ".hermes"
    (home / "cron").mkdir(parents=True)
    secret_snapshot = "secret-snapshot-never-in-argv"
    result = run_cli(home, "restore", "job-1", "--snapshot", secret_snapshot, "--json")
    assert result.returncode != 0
    assert "unrecognized arguments:" in result.stderr
    jobs = home / "cron" / "jobs.json"
    assert not jobs.exists() or jobs.read_text(encoding="utf-8") == "[]"


def test_cli_rejects_invalid_one_shot_next_run_shape_without_mutation(tmp_path):
    home = tmp_path / ".hermes"
    (home / "cron").mkdir(parents=True)
    snapshot = '{"id":"job-1","name":"x","enabled":true,"state":"scheduled","schedule":"2026-01-01T00:00:00","next_run_at":null,"repeat":1,"delivery":"local","skills":[],"no_agent":true,"script":"watch.sh","prompt":null}'
    result = run_cli(home, "restore", "job-1", "--snapshot", snapshot, "--json")
    assert result.returncode != 0
    jobs = home / "cron" / "jobs.json"
    assert not jobs.exists() or jobs.read_text(encoding="utf-8") == "[]"
