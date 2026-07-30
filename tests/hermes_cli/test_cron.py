"""Tests for hermes_cli.cron command handling."""

from argparse import Namespace
from types import SimpleNamespace
import json

import pytest

from cron.jobs import create_job, get_job, list_jobs
from hermes_cli import cron as cron_cli
from hermes_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestCronCommandLifecycle:

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
                clear_skills=False,
                add_skills=None,
                remove_skills=None,
                script=None,
                workdir=None,
                no_agent=None,
            )
        )
        updated = get_job(job["id"])
        assert updated["skills"] == ["maps", "blogwatcher"]
        assert updated["name"] == "Edited Job"
        assert updated["prompt"] == "Revised prompt"
        assert updated["schedule_display"] == "every 120m"

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
                clear_skills=True,
                add_skills=None,
                remove_skills=None,
                script=None,
                workdir=None,
                no_agent=None,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["skills"] == []
        assert cleared["skill"] is None

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
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["skills"] == ["blogwatcher", "maps"]
        assert jobs[0]["name"] == "Skill combo"



class TestGatewayNotRunningWarning:
    """`cron create` / `cron list` must warn when the gateway (and thus the
    cron ticker) isn't running, since jobs only fire inside the gateway.
    Regression guard for #51038 — the most common cron 'jobs never fired'
    report was simply a gateway that was never started.
    """


    def test_list_warns_when_gateway_absent(self, tmp_cron_dir, capsys, monkeypatch):
        create_job(prompt="Daily report", schedule="0 11 * * *")
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="list", all=True))
        out = capsys.readouterr().out
        assert "Gateway is not running" in out


class TestExternalCronProviderStatus:
    """With an external cron provider (e.g. Chronos), jobs fire via a
    NAS-mediated webhook, NOT the in-process ticker. The ticker-heartbeat /
    gateway-process heuristics are meaningless there, so neither
    `cron status` nor the create/list warning must claim the gateway being
    absent means jobs won't fire — that was a false-negative on every healthy
    Chronos instance (the heartbeat is intentionally never written).
    """

    def test_status_reports_provider_not_ticker_for_chronos(
        self, tmp_cron_dir, capsys, monkeypatch
    ):
        create_job(prompt="Ping", schedule="every 2m")
        monkeypatch.setattr(
            "hermes_cli.cron._active_cron_provider_name", lambda: "chronos"
        )
        # Even with NO gateway process and NO ticker heartbeat, Chronos status
        # must NOT report a stall / "not firing".
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(Namespace(cron_command="status"))
        out = capsys.readouterr().out
        assert "chronos" in out
        assert "managed scheduler" in out
        assert "not firing" not in out.lower()
        assert "STALLED" not in out
        assert "Gateway is not running" not in out
        # Still surfaces the active-job summary.
        assert "active job(s)" in out


    def test_create_silent_for_chronos_even_without_gateway(
        self, tmp_cron_dir, capsys, monkeypatch
    ):
        # The create-time "gateway not running" nag is a ticker-only concern;
        # an external provider doesn't depend on a live in-process ticker.
        monkeypatch.setattr(
            "hermes_cli.cron._active_cron_provider_name", lambda: "chronos"
        )
        monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 2m",
                prompt="Ping",
                name="Ping",
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                script=None,
                workdir=None,
                no_agent=False,
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out
        assert "Gateway is not running" not in out


def test_cron_list_warns_when_gateway_not_running(monkeypatch, capsys):
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [
            {
                "id": "job-1",
                "name": "Nightly docs",
                "schedule_display": "every day",
                "state": "scheduled",
                "enabled": True,
                "next_run_at": "2026-06-01T00:00:00Z",
                "deliver": ["local"],
            }
        ],
    )
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")

    cron_cli.cron_list()

    out = capsys.readouterr().out
    assert "Gateway is not running" in out
    assert "Nightly docs" in out


def test_cron_tick_invokes_scheduler_tick_with_verbose(monkeypatch):
    calls = []
    monkeypatch.setattr("cron.scheduler.tick", lambda verbose=False: calls.append(verbose))

    cron_cli.cron_tick()

    assert calls == [True]


def test_cron_create_failure_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_cron_api", lambda **kwargs: {"success": False, "error": "boom"})

    args = SimpleNamespace(
        schedule="every day",
        prompt="refresh docs",
        name=None,
        deliver=None,
        repeat=None,
        skill=None,
        skills=None,
        script=None,
        workdir=None,
        no_agent=False,
    )

    rc = cron_cli.cron_create(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "Failed to create job: boom" in out


def test_json_list_exposes_canonical_persisted_fields(tmp_cron_dir, capsys):
    job = create_job(
        prompt="",
        schedule="every 1m",
        name="Recovery",
        repeat=0,
        deliver="telegram:-1004416879179:4",
        script="kanban_blocked_recovery_controller.py",
        skills=[],
        no_agent=True,
    )
    cron_command(SimpleNamespace(cron_command="list", all=True, json=True))
    value = json.loads(capsys.readouterr().out)
    assert value == [{
        "delivery": "telegram:-1004416879179:4", "deliver": "telegram:-1004416879179:4",
        "enabled": True, "id": job["id"], "model": None, "name": "Recovery",
        "no_agent": True, "next_run_at": job["next_run_at"], "platform": "telegram",
        "prompt": "", "provider": None, "recipient": "-1004416879179", "repeat": None,
        "run_at": None, "schedule": "every 1m", "script": "kanban_blocked_recovery_controller.py",
        "skills": [], "state": "scheduled", "thread": "4", "workdir": None,
    }]


def test_json_create_and_edit_emit_canonical_job(tmp_cron_dir, capsys):
    args = SimpleNamespace(
        cron_command="create", schedule="every 1m", prompt="", name="Recovery",
        deliver="telegram:-1004416879179:4", repeat=0, skill=None, skills=None,
        script="recovery.py", workdir=None, no_agent=True, model=None,
        model_provider=None, json=True,
    )
    assert cron_cli.cron_create(args) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["id"] and created["repeat"] is None and created["no_agent"] is True
    edit = SimpleNamespace(
        cron_command="edit", job_id=created["id"], schedule=None, prompt=None, name="Recovery",
        deliver="telegram:-1004416879179:4", repeat=0, skill=None, skills=None,
        clear_skills=True, add_skills=None, remove_skills=None, script="recovery.py",
        workdir="", no_agent=True, model="", model_provider="", json=True,
    )
    assert cron_cli.cron_edit(edit) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["id"] == created["id"] and updated["repeat"] is None
    assert updated["delivery"] == "telegram:-1004416879179:4"
    assert updated["thread"] == "4" and updated["no_agent"] is True
    assert updated["prompt"] == "" and updated["skills"] == []
    assert updated["model"] is None and updated["provider"] is None and updated["workdir"] is None


def test_json_mutations_fail_closed_when_canonical_readback_is_missing(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_cron_api", lambda **kwargs: {
        "success": True, "job_id": "job-1", "name": "x", "schedule": "every 1m",
        "next_run_at": None, "job": {"job_id": "job-1", "name": "x", "schedule": "every 1m"},
    })
    monkeypatch.setattr("cron.jobs.get_job", lambda job_id: None)
    args = SimpleNamespace(schedule="every 1m", prompt="", name="x", deliver=None,
                           repeat=None, skill=None, skills=None, script="x.py", workdir=None,
                           no_agent=True, json=True)
    assert cron_cli.cron_create(args) == 1
    assert "canonical readback missing" in capsys.readouterr().err


def test_json_delivery_normalizes_legacy_and_multi_target_shapes():
    single = cron_cli._canonical_job({"id": "1", "deliver": ["telegram:chat:7"]})
    assert single["delivery"] == "telegram:chat:7"
    assert single["platform"] == "telegram" and single["thread"] == "7"
    multi = cron_cli._canonical_job({"id": "2", "deliver": "telegram:chat:7, discord:room"})
    assert multi["delivery"] == ["telegram:chat:7", "discord:room"]
    assert multi["platform"] is None and multi["recipient"] is None and multi["thread"] is None


def test_edit_by_name_uses_canonical_id_for_mutation_and_readback(tmp_cron_dir, capsys):
    job = create_job(prompt="old", schedule="every 1m", name="Recovery")
    args = SimpleNamespace(cron_command="edit", job_id="Recovery", schedule=None, prompt="new",
                           name=None, deliver=None, repeat=None, skill=None, skills=None,
                           clear_skills=False, add_skills=None, remove_skills=None, script=None,
                           workdir=None, no_agent=None, model=None, model_provider=None, json=True)
    assert cron_cli.cron_edit(args) == 0
    value = json.loads(capsys.readouterr().out)
    assert value["id"] == job["id"] and value["prompt"] == "new"


def test_top_level_cmd_cron_propagates_handler_return_code(monkeypatch):
    from hermes_cli import main
    monkeypatch.setattr("hermes_cli.cron.cron_command", lambda args: 7)
    assert main.cmd_cron(SimpleNamespace(cron_command="list")) == 7
