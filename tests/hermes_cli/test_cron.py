"""Tests for hermes_cli.cron command handling."""

from argparse import Namespace

import pytest

from cron.jobs import create_job, get_job, list_jobs
from hermes_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestCronCommandLifecycle:
    def test_pause_resume_run(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        paused = get_job(job["id"])
        assert paused["state"] == "paused"

        cron_command(Namespace(cron_command="resume", job_id=job["id"]))
        resumed = get_job(job["id"])
        assert resumed["state"] == "scheduled"

        cron_command(Namespace(cron_command="run", job_id=job["id"]))
        triggered = get_job(job["id"])
        assert triggered["state"] == "scheduled"

        out = capsys.readouterr().out
        assert "Paused job" in out
        assert "Resumed job" in out
        assert "Triggered job" in out

    def test_edit_can_replace_and_clear_skills(self, tmp_cron_dir, capsys):
        job = create_job(
            prompt="Combine skill outputs",
            schedule="every 1h",
            skill="blogwatcher",
        )

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule="every 2h",
                prompt="Revised prompt",
                name="Edited Job",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["maps", "blogwatcher"],
                profile="default",
                clear_skills=False,
            )
        )
        updated = get_job(job["id"])
        assert updated["skills"] == ["maps", "blogwatcher"]
        assert updated["name"] == "Edited Job"
        assert updated["prompt"] == "Revised prompt"
        assert updated["schedule_display"] == "every 120m"
        assert updated["profile"] == "default"

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                profile="",
                clear_skills=True,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["skills"] == []
        assert cleared["skill"] is None
        assert cleared["profile"] is None

        out = capsys.readouterr().out
        assert "Updated job" in out

    def test_create_with_multiple_skills(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="Use both skills",
                name="Skill combo",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["blogwatcher", "maps"],
                profile="default",
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["skills"] == ["blogwatcher", "maps"]
        assert jobs[0]["name"] == "Skill combo"
        assert jobs[0]["profile"] == "default"


class TestGatewayLifecycleDetection:
    """_contains_gateway_lifecycle_command must catch every invocation form
    that stops/restarts the gateway executing the cron job (observed as the
    2026-07-04 frozen-scheduler incident), without flagging ordinary prompts."""

    def test_hermes_cli_forms(self):
        from hermes_cli.cron import _contains_gateway_lifecycle_command as hit

        assert hit("run hermes gateway restart if unhealthy")
        assert hit("hermes gateway stop")
        assert hit("hermes gateway install")
        assert hit("HERMES GATEWAY RESTART")

    def test_module_and_script_forms(self):
        from hermes_cli.cron import _contains_gateway_lifecycle_command as hit

        assert hit("python -m hermes_cli.main gateway restart")
        assert hit("venv/bin/python hermes_cli/main.py gateway stop")
        assert hit(r"python hermes_cli\main.py gateway restart")  # windows path

    def test_service_manager_and_kill_forms(self):
        from hermes_cli.cron import _contains_gateway_lifecycle_command as hit

        assert hit("launchctl kickstart -k gui/501/ai.hermes.gateway")
        assert hit("launchctl bootout gui/501/ai.hermes.gateway")
        assert hit("systemctl --user restart hermes-gateway.service")
        assert hit("pkill -f 'hermes.*gateway run'")

    def test_benign_prompts_pass(self):
        from hermes_cli.cron import _contains_gateway_lifecycle_command as hit

        assert not hit("")
        assert not hit("check hermes gateway status and report")
        assert not hit("restart nginx if it is down")
        assert not hit("summarize the gateway logs")
        assert not hit("launchctl list | grep something-else")

    def test_create_warns_on_lifecycle_prompt(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="If unhealthy, run hermes gateway restart",
                name="Bad watchdog",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
            )
        )
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "lifecycle" in out
        assert "Created job" in out  # warning is non-blocking
