"""Script-slot .env per-run reload (2026-07-11).

_run_job_script must refresh the process env via load_hermes_dotenv before
spawning the script subprocess, so a rotated secret in the profile .env is
picked up on the next cron tick without a gateway restart (the agent-job
path has done this since #33465; the script slot did not — bit for real
when HERMES_GITHUB_TOKEN was rotated and devflow-pr-build-poll kept using
the launch-inherited dead token).
"""
import sys
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_run_job_script_reloads_dotenv_before_subprocess(tmp_path, monkeypatch):
    from cron import scheduler

    scripts_dir = Path(scheduler._get_hermes_home()) / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script = scripts_dir / "envprobe.py"
    script.write_text("print('hi')\n", encoding="utf-8")

    calls = []
    with patch("hermes_cli.env_loader.load_hermes_dotenv",
               side_effect=lambda **kw: calls.append(kw) or []), \
         patch("hermes_cli.env_loader.reset_secret_source_cache",
               side_effect=lambda: calls.append("reset")):
        ok, output = scheduler._run_job_script("envprobe.py")

    assert ok, output
    assert "reset" in calls, "secret-source cache must be reset before reload"
    assert any(isinstance(c, dict) for c in calls), \
        "load_hermes_dotenv must run before the script subprocess"
