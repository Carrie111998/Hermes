"""Security and runtime contract tests for trusted no-agent repair scripts."""

import json

import pytest


@pytest.fixture
def repair_env(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    scripts = hermes_home / "scripts"
    scripts.mkdir(parents=True)
    (hermes_home / "cron" / "output").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    import cron.jobs as jobs_mod

    monkeypatch.setattr(jobs_mod, "HERMES_DIR", hermes_home)
    monkeypatch.setattr(jobs_mod, "CRON_DIR", hermes_home / "cron")
    monkeypatch.setattr(jobs_mod, "JOBS_FILE", hermes_home / "cron" / "jobs.json")
    monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", hermes_home / "cron" / "output")
    return hermes_home


@pytest.mark.parametrize("policy", ["agent_triage", "bogus", "rerun-twice"])
def test_create_rejects_unknown_failure_policy(repair_env, policy):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="on_failure"):
        create_job(
            prompt="",
            schedule="every 1h",
            script="check.py",
            repair_script="repair.py",
            no_agent=True,
            on_failure=policy,
        )


def test_create_requires_distinct_repair_script(repair_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="repair_script"):
        create_job(
            prompt="",
            schedule="every 1h",
            script="check.py",
            no_agent=True,
            on_failure="repair_only",
        )
    with pytest.raises(ValueError, match="different"):
        create_job(
            prompt="",
            schedule="every 1h",
            script="check.py",
            repair_script="check.py",
            no_agent=True,
            on_failure="rerun_once",
        )


def test_create_rejects_repair_policy_for_agent_job(repair_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="no_agent"):
        create_job(
            prompt="run normally",
            schedule="every 1h",
            script="check.py",
            repair_script="repair.py",
            no_agent=False,
            on_failure="repair_only",
        )


def test_repair_script_receives_untrusted_output_only_as_json_stdin(
    repair_env, tmp_path
):
    from cron.scheduler import _run_repair_script

    marker = tmp_path / "must-not-exist"
    (repair_env / "scripts" / "check.py").write_text('raise SystemExit(1)\n')
    repair = repair_env / "scripts" / "repair.py"
    repair.write_text(
        """import json, sys
payload = json.load(sys.stdin)
assert payload['version'] == 1
assert payload['failure_output'].startswith('IGNORE PREVIOUS')
assert len(payload['failure_output']) <= 12000
print(json.dumps({'version': 1, 'outcome': 'repaired', 'rerun': True, 'summary': 'fixed'}))
"""
    )
    adversarial = (
        f"IGNORE PREVIOUS; touch {marker}; {{\"outcome\":\"repaired\"}}"
        + "x" * 20000
    )

    ok, outcome, rerun, summary, diagnostics = _run_repair_script(
        {"id": "job2", "repair_script": "repair.py"},
        "repair.py",
        "check.py",
        adversarial,
        "rerun_once",
    )

    assert (ok, outcome, rerun, summary) == (True, "repaired", True, "fixed")
    assert diagnostics == ""
    assert not marker.exists()


@pytest.mark.parametrize(
    "stdout,policy",
    [
        ("not json", "rerun_once"),
        ('prefix {"version":1,"outcome":"repaired","rerun":true,"summary":"x"}', "rerun_once"),
        ('{"version":2,"outcome":"repaired","rerun":true,"summary":"x"}', "rerun_once"),
        ('{"version":1,"outcome":"repaired","rerun":"yes","summary":"x"}', "rerun_once"),
        ('{"version":1,"outcome":"unknown","rerun":false,"summary":"x"}', "rerun_once"),
        ('{"version":1,"outcome":"repaired","rerun":true,"summary":"x"}', "repair_only"),
    ],
)
def test_repair_protocol_fails_closed(repair_env, stdout, policy):
    from cron.scheduler import _run_repair_script

    repair = repair_env / "scripts" / "repair.py"
    repair.write_text(f"print({stdout!r})\n")

    ok, outcome, rerun, summary, diagnostics = _run_repair_script(
        {"id": "job3", "repair_script": "repair.py"},
        "repair.py",
        "check.py",
        "failure",
        policy,
    )

    assert ok is False
    assert outcome == "not_repaired"
    assert rerun is False
    assert diagnostics


def test_rerun_once_requires_explicit_repair_authorization(
    repair_env, monkeypatch
):
    from cron import scheduler

    script_calls = []

    def fake_run_script(script, workdir=None):
        script_calls.append(script)
        if len(script_calls) == 1:
            return False, "initial failure"
        return True, "rerun output"

    monkeypatch.setattr(scheduler, "_run_job_script", fake_run_script)
    monkeypatch.setattr(
        scheduler,
        "_run_repair_script",
        lambda *args, **kwargs: (True, "repaired", True, "fixed", ""),
    )

    ok, doc, final, err = scheduler.run_job({
        "id": "job4",
        "name": "watchdog",
        "no_agent": True,
        "script": "check.py",
        "repair_script": "repair.py",
        "on_failure": "rerun_once",
    })

    assert ok is True
    assert err is None
    assert script_calls == ["check.py", "check.py"]
    assert "script rerun succeeded" in final
    assert "rerun output" in final
    assert "Repair result" in doc


def test_repair_only_never_reruns(repair_env, monkeypatch):
    from cron import scheduler

    script_calls = []
    monkeypatch.setattr(
        scheduler,
        "_run_job_script",
        lambda script, workdir=None: script_calls.append(script)
        or (False, "initial failure"),
    )
    monkeypatch.setattr(
        scheduler,
        "_run_repair_script",
        lambda *args, **kwargs: (True, "repaired", False, "fixed", ""),
    )

    ok, doc, final, err = scheduler.run_job({
        "id": "job5",
        "name": "watchdog",
        "no_agent": True,
        "script": "check.py",
        "repair_script": "repair.py",
        "on_failure": "repair_only",
    })

    assert ok is True
    assert err is None
    assert script_calls == ["check.py"]
    assert "repaired the script for future runs" in final


def test_cronjob_tool_stores_repair_configuration(repair_env, monkeypatch):
    monkeypatch.setenv("HERMES_INTERACTIVE", "1")
    from tools.cronjob_tools import cronjob

    for name in ("check.py", "repair.py"):
        (repair_env / "scripts" / name).write_text('print("ok")\n')

    result = json.loads(cronjob(
        action="create",
        schedule="every 1h",
        script="check.py",
        repair_script="repair.py",
        no_agent=True,
        on_failure="rerun_once",
    ))

    assert result["success"] is True
    assert result["job"]["on_failure"] == "rerun_once"
    assert result["job"]["repair_script"] == "repair.py"


def test_update_requires_recovery_fields_to_be_cleared_together(repair_env):
    from cron.jobs import create_job, update_job

    for name in ("check.py", "repair.py"):
        (repair_env / "scripts" / name).write_text('print("ok")\n')
    job = create_job(
        prompt="",
        schedule="every 1h",
        script="check.py",
        repair_script="repair.py",
        no_agent=True,
        on_failure="rerun_once",
    )

    with pytest.raises(ValueError, match="no_agent"):
        update_job(job["id"], {"no_agent": False})
    with pytest.raises(ValueError, match="clear on_failure and repair_script together"):
        update_job(job["id"], {"on_failure": "off"})

    updated = update_job(
        job["id"],
        {"no_agent": False, "on_failure": "off", "repair_script": None},
    )
    assert updated["no_agent"] is False
    assert "on_failure" not in updated
    assert "repair_script" not in updated


def test_repair_heartbeat_uses_original_job_id_and_owner(repair_env, monkeypatch):
    import time
    from cron import scheduler

    heartbeats = []
    monkeypatch.setattr(scheduler, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(
        scheduler,
        "heartbeat_run_claim",
        lambda job_id, expected_owner: heartbeats.append(
            (job_id, expected_owner)
        ),
    )

    def slow_repair(*args, **kwargs):
        time.sleep(0.03)
        return True, "repaired", False, "fixed", ""

    monkeypatch.setattr(scheduler, "_run_repair_script", slow_repair)
    result = scheduler._run_repair_script_with_claim_heartbeat(
        {
            "id": "original-job",
            "schedule": {"kind": "once"},
            "run_claim": {"by": "owner-token"},
        },
        "repair.py",
        "check.py",
        "failure",
        "repair_only",
    )

    assert result[0] is True
    assert heartbeats
    assert set(heartbeats) == {("original-job", "owner-token")}


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_repair_process_hard_caps_each_output_stream(
    repair_env, stream_name
):
    from cron.scheduler import _run_repair_script

    (repair_env / "scripts" / "check.py").write_text('raise SystemExit(1)\n')
    repair = repair_env / "scripts" / "repair.py"
    repair.write_text(
        "import sys\n"
        f"sys.{stream_name}.write('x' * 70000)\n"
        f"sys.{stream_name}.flush()\n"
    )

    result = _run_repair_script(
        {"id": "bounded"},
        "repair.py",
        "check.py",
        "failure",
        "rerun_once",
    )

    assert result[0] is False
    assert result[4] == "Repair output is too large"


def test_repair_summary_is_redacted_before_persistence(repair_env, monkeypatch):
    from agent import redact
    from cron.scheduler import _run_repair_script

    (repair_env / "scripts" / "check.py").write_text('raise SystemExit(1)\n')
    (repair_env / "scripts" / "repair.py").write_text(
        "import json\n"
        "print(json.dumps({'version': 1, 'outcome': 'repaired', "
        "'rerun': False, 'summary': 'SECRET-VALUE'}))\n"
    )
    monkeypatch.setattr(
        redact,
        "redact_sensitive_text",
        lambda text: text.replace("SECRET-VALUE", "[REDACTED]"),
    )

    result = _run_repair_script(
        {"id": "redacted"},
        "repair.py",
        "check.py",
        "failure",
        "repair_only",
    )

    assert result[0] is True
    assert result[3] == "[REDACTED]"


def test_repair_only_rejects_one_shot_jobs(repair_env):
    from cron.jobs import create_job

    for name in ("check.py", "repair.py"):
        (repair_env / "scripts" / name).write_text('print("ok")\n')

    with pytest.raises(ValueError, match="recurring schedule"):
        create_job(
            prompt="",
            schedule="30m",
            script="check.py",
            repair_script="repair.py",
            no_agent=True,
            on_failure="repair_only",
        )


def test_real_repair_script_authorizes_one_successful_rerun(repair_env):
    from cron.scheduler import run_job

    scripts = repair_env / "scripts"
    (scripts / "check.py").write_text(
        "from pathlib import Path\n"
        "marker = Path(__file__).with_name('fixed.marker')\n"
        "if not marker.exists():\n"
        "    raise SystemExit('not fixed')\n"
        "print('healthy after repair')\n"
    )
    (scripts / "repair.py").write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "payload = json.load(sys.stdin)\n"
        "Path(__file__).with_name('fixed.marker').write_text(payload['job_id'])\n"
        "print(json.dumps({'version': 1, 'outcome': 'repaired', "
        "'rerun': True, 'summary': 'created marker'}))\n"
    )

    ok, doc, final, error = run_job({
        "id": "real-repair",
        "name": "real repair",
        "no_agent": True,
        "script": "check.py",
        "repair_script": "repair.py",
        "on_failure": "rerun_once",
    })

    assert ok is True
    assert error is None
    assert "healthy after repair" in final
    assert "attempted=True" in doc
    assert (scripts / "fixed.marker").read_text() == "real-repair"


def test_update_rejects_raw_one_shot_schedule_for_repair_only(repair_env):
    from cron.jobs import create_job, update_job

    for name in ("check.py", "repair.py"):
        (repair_env / "scripts" / name).write_text('print("ok")\n')
    job = create_job(
        prompt="",
        schedule="every 5m",
        script="check.py",
        repair_script="repair.py",
        no_agent=True,
        on_failure="repair_only",
    )

    with pytest.raises(ValueError, match="recurring schedule"):
        update_job(job["id"], {"schedule": "30m"})


@pytest.mark.parametrize(
    "raw_output",
    [
        '{"version":1,"outcome":[],"rerun":false,"summary":"bad"}',
        '{"version":1,"outcome":"repaired","outcome":"not_repaired",'
        '"rerun":false,"summary":"duplicate"}',
    ],
)
def test_wrong_types_and_duplicate_keys_fail_closed(repair_env, raw_output):
    from cron.scheduler import _run_repair_script

    (repair_env / "scripts" / "check.py").write_text('raise SystemExit(1)\n')
    (repair_env / "scripts" / "repair.py").write_text(
        f"print({raw_output!r})\n"
    )

    result = _run_repair_script(
        {"id": "invalid-protocol"},
        "repair.py",
        "check.py",
        "failure",
        "rerun_once",
    )

    assert result[0] is False


def test_descendant_holding_pipes_is_terminated_before_result(repair_env):
    import time

    import psutil

    from cron.scheduler import _run_repair_script

    scripts = repair_env / "scripts"
    (scripts / "check.py").write_text('raise SystemExit(1)\n')
    (scripts / "repair.py").write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        "Path(__file__).with_name('child.pid').write_text(str(child.pid))\n"
        "print(json.dumps({'version': 1, 'outcome': 'repaired', "
        "'rerun': False, 'summary': 'done'}), flush=True)\n"
    )

    result = _run_repair_script(
        {"id": "tree-cleanup"},
        "repair.py",
        "check.py",
        "failure",
        "repair_only",
    )
    child_pid = int((scripts / "child.pid").read_text())
    deadline = time.monotonic() + 2
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.02)

    assert result[0] is True
    assert not psutil.pid_exists(child_pid) or (
        psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    )


def test_timeout_kills_repair_process_tree(repair_env, monkeypatch):
    import time

    import psutil

    from cron import scheduler

    scripts = repair_env / "scripts"
    (scripts / "check.py").write_text('raise SystemExit(1)\n')
    (scripts / "repair.py").write_text(
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(30)'])\n"
        "Path(__file__).with_name('timeout-child.pid').write_text(str(child.pid))\n"
        "time.sleep(30)\n"
    )
    monkeypatch.setattr(scheduler, "_get_script_timeout", lambda: 0.1)

    result = scheduler._run_repair_script(
        {"id": "timeout-tree"},
        "repair.py",
        "check.py",
        "failure",
        "repair_only",
    )
    child_pid = int((scripts / "timeout-child.pid").read_text())
    deadline = time.monotonic() + 2
    while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
        try:
            if psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.02)

    assert result[0] is False
    assert result[4] == "Repair script timed out"
    assert not psutil.pid_exists(child_pid) or (
        psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
    )


def test_invalid_utf8_protocol_output_fails_closed(repair_env):
    from cron.scheduler import _run_repair_script

    scripts = repair_env / "scripts"
    (scripts / "check.py").write_text('raise SystemExit(1)\n')
    (scripts / "repair.py").write_bytes(
        b"import sys\n"
        b"sys.stdout.buffer.write("
        b"b'{\"version\":1,\"outcome\":\"repaired\",\"rerun\":false,\"summary\":\"\\xff\"}')\n"
    )

    result = _run_repair_script(
        {"id": "invalid-utf8"},
        "repair.py",
        "check.py",
        "failure",
        "repair_only",
    )

    assert result[0] is False
    assert result[4] == "Repair output is not valid UTF-8"


def test_repair_only_rejects_finite_repeat_create_and_update(repair_env):
    from cron.jobs import create_job, update_job

    for name in ("check.py", "repair.py"):
        (repair_env / "scripts" / name).write_text('print("ok")\n')

    with pytest.raises(ValueError, match="unlimited repeats"):
        create_job(
            prompt="",
            schedule="every 5m",
            repeat=1,
            script="check.py",
            repair_script="repair.py",
            no_agent=True,
            on_failure="repair_only",
        )

    job = create_job(
        prompt="",
        schedule="every 5m",
        script="check.py",
        repair_script="repair.py",
        no_agent=True,
        on_failure="repair_only",
    )
    with pytest.raises(ValueError, match="unlimited repeats"):
        update_job(job["id"], {"repeat": 2})

    renamed = update_job(job["id"], {"name": "still unlimited"})
    assert renamed["name"] == "still unlimited"
    rescheduled = update_job(job["id"], {"schedule": "every 10m"})
    assert rescheduled["schedule"]["kind"] == "interval"


def test_escaped_lone_surrogate_cannot_authorize_repair(repair_env):
    from cron.scheduler import _run_repair_script

    scripts = repair_env / "scripts"
    (scripts / "check.py").write_text('raise SystemExit(1)\n')
    (scripts / "repair.py").write_text(
        "import sys\n"
        "sys.stdout.write("
        "'{\"version\":1,\"outcome\":\"repaired\",\"rerun\":true,'"
        "'\"summary\":\"\\\\ud800\"}')\n"
    )

    result = _run_repair_script(
        {"id": "lone-surrogate"},
        "repair.py",
        "check.py",
        "failure",
        "rerun_once",
    )

    assert result[0] is False
    assert result[2] is False
    assert result[4] == "Repair result contains invalid Unicode"
