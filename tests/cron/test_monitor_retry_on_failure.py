"""Tests for monitor_retry_on_failure — opt-in commit-on-success semantics.

Incident (2026-08-29, ufc-watch): the monitor hash was persisted at
DETECTION time, so when the agent run then died on provider timeouts the
change was already consumed and every retry became a silent ``no_change``
tick. The opt-in field makes the hash commit only after a SUCCESSFUL agent
run, keeping the change retryable. Legacy jobs (field absent) keep
commit-at-detection semantics byte-for-byte.

Commit boundary (deliberate): the hash commits on agent-run success inside
``run_job`` — BEFORE delivery. A delivery error afterwards never un-commits
(the agent already acted on the change; replay would duplicate side
effects).
"""

from __future__ import annotations

import json
import sys

import pytest


@pytest.fixture
def hermes_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME for each test so jobs/scripts/snapshots don't leak."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "scripts").mkdir()
    (home / "cron").mkdir()

    monkeypatch.setenv("HERMES_HOME", str(home))

    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)
    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.monitor
    importlib.reload(cron.monitor)
    import cron.scheduler
    importlib.reload(cron.scheduler)

    return home


def _write_script(home, name: str, body: str) -> str:
    path = home / "scripts" / name
    path.write_text(body, encoding="utf-8")
    return name


def _install_agent_stubs(monkeypatch, observed: dict, fail: bool = False,
                         empty: bool = False):
    """Stub the agent machinery; fail=True makes every run raise (provider
    timeout shape); empty=True returns success with an EMPTY final_response
    (the #8585 soft-failure shape)."""
    import cron.scheduler as sched

    observed.setdefault("prompts", [])
    observed.setdefault("agent_runs", 0)

    class FakeAgent:
        def __init__(self, **kwargs):
            pass

        def run_conversation(self, prompt, *_a, **_kw):
            observed["agent_runs"] += 1
            observed["prompts"].append(prompt)
            if fail:
                raise RuntimeError("ReadTimeout: provider request timed out")
            return {
                "final_response": "" if empty else "agent done",
                "messages": [],
            }

        def get_activity_summary(self):
            return {"seconds_since_activity": 0.0}

    fake_mod = type(sys)("run_agent")
    fake_mod.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_mod)

    from hermes_cli import runtime_provider as _rtp
    monkeypatch.setattr(
        _rtp,
        "resolve_runtime_provider",
        lambda **_kw: {
            "provider": "test",
            "api_key": "k",
            "base_url": "http://test.local",
            "api_mode": "chat_completions",
        },
    )

    monkeypatch.setattr(sched, "_resolve_origin", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_delivery_target", lambda job: None)
    monkeypatch.setattr(sched, "_resolve_cron_enabled_toolsets", lambda job, cfg: None)
    monkeypatch.setenv("HERMES_CRON_TIMEOUT", "0")

    import dotenv
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_a, **_kw: True)


def _make_job(hermes_env, script_body: str, retry: bool):
    from cron.jobs import create_job

    _write_script(hermes_env, "mon.sh", script_body)
    return create_job(
        prompt="Summarize what changed",
        schedule="every 5m",
        monitor_script="mon.sh",
        deliver="local",
        monitor_retry_on_failure=retry,
    )


def _stored_state(job_id):
    from cron.jobs import get_job

    return get_job(job_id).get("monitor_state") or {}


# ---------------------------------------------------------------------------
# data layer: validation + persistence of the opt-in field
# ---------------------------------------------------------------------------


def test_create_retry_flag_requires_monitor_source(hermes_env):
    from cron.jobs import create_job

    with pytest.raises(ValueError, match="monitor_retry_on_failure requires"):
        create_job(
            prompt="p",
            schedule="every 5m",
            monitor_retry_on_failure=True,
        )


def test_create_stores_retry_flag_only_when_true(hermes_env):
    from cron.jobs import create_job, get_job

    _write_script(hermes_env, "mon.sh", "echo stable\n")
    job_on = create_job(
        prompt="p", schedule="every 5m", monitor_script="mon.sh",
        monitor_retry_on_failure=True,
    )
    job_off = create_job(
        prompt="p", schedule="every 5m", monitor_script="mon.sh",
    )
    assert get_job(job_on["id"]).get("monitor_retry_on_failure") is True
    # Legacy shape: key ABSENT (not False) — pre-feature records stay
    # byte-identical, no migration.
    assert "monitor_retry_on_failure" not in get_job(job_off["id"])


def test_update_door_validation_and_clear(hermes_env):
    from cron.jobs import create_job, get_job, update_job

    _write_script(hermes_env, "mon.sh", "echo stable\n")
    plain = create_job(prompt="p", schedule="every 5m")
    with pytest.raises(ValueError, match="monitor_retry_on_failure requires"):
        update_job(plain["id"], {"monitor_retry_on_failure": True})

    job = create_job(prompt="p", schedule="every 5m", monitor_script="mon.sh")
    update_job(job["id"], {"monitor_retry_on_failure": True})
    assert get_job(job["id"]).get("monitor_retry_on_failure") is True
    # Clearing returns the record to the absent-key legacy shape.
    update_job(job["id"], {"monitor_retry_on_failure": False})
    assert "monitor_retry_on_failure" not in get_job(job["id"])


def test_cronjob_tool_create_and_update_retry_flag(hermes_env):
    from cron.jobs import get_job
    from tools.cronjob_tools import cronjob

    _write_script(hermes_env, "mon.sh", "echo hi\n")
    created = json.loads(
        cronjob(
            action="create",
            prompt="React",
            schedule="every 5m",
            monitor_script="mon.sh",
            monitor_retry_on_failure=True,
            deliver="local",
        )
    )
    assert created.get("success") is True
    assert created["job"].get("monitor_retry_on_failure") is True
    assert get_job(created["job_id"]).get("monitor_retry_on_failure") is True

    updated = json.loads(
        cronjob(
            action="update",
            job_id=created["job_id"],
            monitor_retry_on_failure=False,
        )
    )
    assert updated.get("success") is True
    assert "monitor_retry_on_failure" not in get_job(created["job_id"])

    rejected = json.loads(
        cronjob(action="update", job_id=created["job_id"], monitor_script="",
                monitor_retry_on_failure=True)
    )
    assert rejected.get("success") is False


# ---------------------------------------------------------------------------
# scheduler semantics: legacy vs opt-in
# ---------------------------------------------------------------------------


def test_legacy_default_consumes_change_on_agent_failure(hermes_env, monkeypatch):
    """Explicit legacy contract: commit-at-detection. A failed agent run
    consumes the change; the next tick is no_change. This is exactly the
    pre-fix behavior non-opted jobs must keep."""
    from cron.jobs import get_job
    from cron.scheduler import SILENT_MARKER, run_job

    job = _make_job(hermes_env, "echo 'state A'\n", retry=False)
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, fail=True)

    success, _doc, _final, error = run_job(job)
    assert success is False
    assert error is not None
    assert observed["agent_runs"] == 1
    # Hash committed at detection despite the failure (legacy).
    assert _stored_state(job["id"]).get("last_output_hash")

    job = get_job(job["id"])
    success, doc, final, error = run_job(job)
    assert success is True
    assert final == SILENT_MARKER
    assert observed["agent_runs"] == 1  # consumed — no retry
    assert "no_change" in doc


def test_optin_failed_run_leaves_change_retryable(hermes_env, monkeypatch):
    """The incident fix: changed output + provider failure must NOT commit
    the hash, so the next tick re-detects the SAME change and retries."""
    from cron.jobs import get_job
    from cron.scheduler import run_job

    job = _make_job(hermes_env, "echo 'state A'\n", retry=True)
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, fail=True)

    success, _doc, _final, error = run_job(job)
    assert success is False
    assert observed["agent_runs"] == 1
    # NOTHING committed — first-run baseline never landed.
    assert _stored_state(job["id"]).get("last_output_hash") is None


def test_optin_next_tick_retries_and_success_commits(hermes_env, monkeypatch):
    from cron.jobs import get_job
    from cron.scheduler import run_job

    job = _make_job(hermes_env, "echo 'state A'\n", retry=True)
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, fail=True)

    run_job(job)  # fails, no commit
    assert observed["agent_runs"] == 1

    # Provider recovers: SAME monitor output, next tick must re-run the
    # agent (not no_change) and then commit.
    _install_agent_stubs(monkeypatch, observed, fail=False)
    job = get_job(job["id"])
    success, _doc, final, error = run_job(job)
    assert success is True
    assert error is None
    assert observed["agent_runs"] == 2  # retried the same change
    assert "Monitor Baseline" in observed["prompts"][1]  # still first commit
    committed = _stored_state(job["id"]).get("last_output_hash")
    assert committed

    # No-change suppression preserved after the successful commit.
    from cron.scheduler import SILENT_MARKER

    job = get_job(job["id"])
    success, doc, final, error = run_job(job)
    assert success is True
    assert final == SILENT_MARKER
    assert observed["agent_runs"] == 2  # unchanged — suppressed
    assert "no_change" in doc


def test_optin_change_after_commit_also_retries(hermes_env, monkeypatch):
    """A SECOND change (post-commit) is retried the same way when the agent
    fails on it."""
    from cron.jobs import get_job
    from cron.scheduler import run_job

    job = _make_job(hermes_env, "echo 'state A'\n", retry=True)
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, fail=False)
    run_job(job)  # baseline A commits
    hash_a = _stored_state(job["id"])["last_output_hash"]

    _write_script(hermes_env, "mon.sh", "echo 'state B'\n")
    _install_agent_stubs(monkeypatch, observed, fail=True)
    job = get_job(job["id"])
    success, _d, _f, error = run_job(job)
    assert success is False
    # B was detected but NOT committed — stored hash is still A's.
    assert _stored_state(job["id"])["last_output_hash"] == hash_a

    _install_agent_stubs(monkeypatch, observed, fail=False)
    job = get_job(job["id"])
    success, _d, _f, error = run_job(job)
    assert success is True
    assert observed["agent_runs"] == 3
    assert "MONITOR CHANGE DETECTED" in observed["prompts"][2]
    assert _stored_state(job["id"])["last_output_hash"] != hash_a


def test_optin_reverted_output_clears_pending(hermes_env, monkeypatch):
    """Change detected, agent fails, source REVERTS to committed output →
    no_change tick clears the stale pending commit so a later unrelated
    success cannot persist it."""
    from cron.jobs import get_job
    from cron.scheduler import SILENT_MARKER, run_job
    import cron.monitor as monitor

    job = _make_job(hermes_env, "echo 'state A'\n", retry=True)
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, fail=False)
    run_job(job)  # baseline A commits

    _write_script(hermes_env, "mon.sh", "echo 'state B'\n")
    _install_agent_stubs(monkeypatch, observed, fail=True)
    job = get_job(job["id"])
    run_job(job)  # B detected (pending), agent fails
    assert monitor._PENDING_COMMITS.get(job["id"]) is not None

    _write_script(hermes_env, "mon.sh", "echo 'state A'\n")  # revert
    job = get_job(job["id"])
    success, doc, final, error = run_job(job)
    assert success is True
    assert final == SILENT_MARKER  # back at committed output → no_change
    assert monitor._PENDING_COMMITS.get(job["id"]) is None


def test_optin_delivery_error_still_commits(hermes_env, monkeypatch):
    """Delivery-error boundary (deliberate): the hash commits on agent-run
    success BEFORE delivery, so a failed delivery never re-arms the change
    (the agent already acted on it; replay would duplicate side effects)."""
    from cron.jobs import get_job
    import cron.scheduler as scheduler

    _write_script(hermes_env, "mon.sh", "echo 'state A'\n")
    from cron.jobs import create_job

    job = create_job(
        prompt="Summarize what changed",
        schedule="every 5m",
        monitor_script="mon.sh",
        deliver="telegram",
        name="retry delivery boundary",
        monitor_retry_on_failure=True,
    )
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, fail=False)
    # Skip the delivery-credential preflight (no gateway config in the test
    # home) so the run reaches the delivery step itself.
    monkeypatch.setattr(scheduler, "_preflight_check_delivery", lambda job: None)
    monkeypatch.setattr(
        scheduler, "_deliver_result", lambda *a, **kw: "delivery boom"
    )

    assert scheduler.run_one_job(job) is True
    assert observed["agent_runs"] == 1
    # Committed despite the delivery failure.
    assert _stored_state(job["id"]).get("last_output_hash")
    reloaded = get_job(job["id"])
    assert reloaded.get("last_delivery_error") == "delivery boom"


def test_optin_empty_response_does_not_commit_and_retries(hermes_env, monkeypatch):
    """Review finding (HIGH): the agent run SUCCEEDS but returns an empty
    final_response. The caller reclassifies that run as failed (#8585), so
    the monitor commit boundary must match scheduler success semantics:
    no hash commit, the next identical tick re-detects and re-runs; a
    later GOOD response commits and the tick after that suppresses."""
    from cron.jobs import get_job
    import cron.scheduler as scheduler

    job = _make_job(hermes_env, "echo 'state A'\n", retry=True)
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, empty=True)

    # Full scheduler path (run_one_job) so the caller's empty-response
    # reclassification is exercised, not just run_job's return tuple.
    assert scheduler.run_one_job(job) is True
    assert observed["agent_runs"] == 1
    reloaded = get_job(job["id"])
    # Failed by the scheduler contract — last_status is NOT ok.
    assert reloaded.get("last_status") == "error"
    assert "empty response" in (reloaded.get("last_error") or "")
    # Hash NOT committed — the change stays retryable.
    assert _stored_state(job["id"]).get("last_output_hash") is None

    # Next IDENTICAL tick re-detects the same change and re-runs the agent.
    job = get_job(job["id"])
    assert scheduler.run_one_job(job) is True
    assert observed["agent_runs"] == 2
    assert _stored_state(job["id"]).get("last_output_hash") is None

    # Agent recovers with a real response: the commit boundary opens.
    _install_agent_stubs(monkeypatch, observed, empty=False)
    job = get_job(job["id"])
    assert scheduler.run_one_job(job) is True
    assert observed["agent_runs"] == 3
    assert _stored_state(job["id"]).get("last_output_hash")
    assert get_job(job["id"]).get("last_status") == "ok"

    # No-change suppression preserved after the successful commit.
    job = get_job(job["id"])
    assert scheduler.run_one_job(job) is True
    assert observed["agent_runs"] == 3  # unchanged — suppressed


def test_optin_survives_scheduler_restart(hermes_env, monkeypatch):
    """Crash/restart correctness: the pending slot is process-local, so a
    'restart' (module reload) between detection-failure and the next tick
    simply re-detects the change — the stored hash was never touched."""
    import importlib

    from cron.jobs import get_job
    from cron.scheduler import run_job

    job = _make_job(hermes_env, "echo 'state A'\n", retry=True)
    observed: dict = {}
    _install_agent_stubs(monkeypatch, observed, fail=True)
    run_job(job)  # fails; pending only in memory
    assert _stored_state(job["id"]).get("last_output_hash") is None

    import cron.jobs
    importlib.reload(cron.jobs)
    import cron.monitor
    importlib.reload(cron.monitor)
    import cron.scheduler
    importlib.reload(cron.scheduler)
    _install_agent_stubs(monkeypatch, observed, fail=False)

    job = cron.jobs.get_job(job["id"])
    success, _d, _f, error = cron.scheduler.run_job(job)
    assert success is True
    assert observed["agent_runs"] == 2  # change re-detected and retried
    assert cron.jobs.get_job(job["id"])["monitor_state"]["last_output_hash"]
