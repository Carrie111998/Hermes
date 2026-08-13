"""Characterization + unit tests for the `run_one_job` shared helper (Phase 4A).

`tick`'s per-job body (`_process_job`) is the execute → save → deliver → mark
sequence that fires ONE due job. Phase 4A extracts it into a module-level
`run_one_job(job, *, adapters=None, loop=None, verbose=False)` so the external
Chronos provider's `fire_due` can reuse the IDENTICAL body — no duplicated
correctness.

The first test characterizes the sequence as driven through `tick()` (proving
the extraction didn't change `tick`'s behavior); the rest unit-test the
extracted helper directly.
"""
from unittest.mock import Mock

import pytest

import cron.scheduler as s


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job, *, defer_agent_teardown=None, **kw):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None, **_kw):
        calls.append(("mark", jid, ok))

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", fake_save)
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", fake_mark)
    return calls


def test_tick_process_job_sequence(monkeypatch):
    """Characterization: a single due job driven through tick() runs the
    sequence run_job → save → deliver → mark, in that order."""
    calls = _patch_pipeline(monkeypatch)
    monkeypatch.setattr(s, "get_due_jobs", lambda: [{"id": "j1", "name": "t"}])
    monkeypatch.setattr(s, "advance_next_runs", lambda ids: 1)

    s.tick(verbose=False, sync=True)

    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j1", True)


def test_run_one_job_success_sequence(monkeypatch):
    """The extracted helper runs the same execute→save→deliver→mark sequence
    for a successful job."""
    calls = _patch_pipeline(monkeypatch)

    ok = s.run_one_job({"id": "j2", "name": "t"})

    assert ok is True
    assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]
    assert calls[-1] == ("mark", "j2", True)


def test_run_one_job_installs_secret_scope_under_multiplex(monkeypatch, tmp_path):
    """Regression: under profile isolation (multiplex active), run_one_job must
    execute run_job inside a profile secret scope so credential reads
    (resolve_runtime_provider -> get_secret) don't fail-close with
    UnscopedSecretError, and must tear the scope down afterward.

    Behavior contract: a scope is present during run_job and absent after,
    regardless of the concrete secret values.
    """
    from agent import secret_scope as ss

    # Point cron's home resolution at a profile whose .env carries a secret.
    (tmp_path / ".env").write_text("OPENROUTER_BASE_URL=https://openrouter.ai/api/v1\n")
    monkeypatch.setattr(s, "_get_hermes_home", lambda: tmp_path)

    scope_during_run = {}

    def fake_run_job(job, *, defer_agent_teardown=None, **kw):
        # This is where resolve_runtime_provider() would read a secret. Prove a
        # scope is installed and the profile's secret resolves without raising.
        scope_during_run["scope"] = ss.current_secret_scope()
        scope_during_run["base_url"] = ss.get_secret("OPENROUTER_BASE_URL")
        return (True, "out", "final", None)

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", lambda *a, **k: None)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: None)

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job({"id": "j7", "name": "t"})
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # Scope was installed during run_job and the profile secret resolved.
    assert scope_during_run["scope"] is not None
    assert scope_during_run["base_url"] == "https://openrouter.ai/api/v1"
    # And it was torn down after run_one_job returned (no leak).
    assert ss.current_secret_scope() is None


@pytest.mark.parametrize(
    ("diagnostic", "deliver_target"),
    [
        ("HTTP 429: provider overloaded; rate limit exhausted", "discord:123"),
        ("HTTP 429: provider overloaded; rate limit exhausted", "local"),
        ("provider request timed out after 60 seconds", "discord:123"),
        ("provider request timed out after 60 seconds", "local"),
        ("unexpected provider exception", "discord:123"),
        ("unexpected provider exception", "local"),
    ],
)
def test_local_policy_keeps_llm_failure_matrix_local(
    monkeypatch, caplog, diagnostic, deliver_target
):
    deliver = Mock(return_value=None)
    saved = []
    marked = []
    monkeypatch.setattr(
        s, "load_config", lambda: {"cron": {"model_failure_delivery": "local"}}
    )
    monkeypatch.setattr(
        s,
        "run_job",
        lambda job, *, defer_agent_teardown=None, **kw: (
            False,
            f"full model diagnostic:\n{diagnostic}",
            "",
            diagnostic,
        ),
    )
    monkeypatch.setattr(
        s, "save_job_output", lambda job_id, output: saved.append(output) or "/tmp/out.md"
    )
    monkeypatch.setattr(s, "_deliver_result", deliver)
    monkeypatch.setattr(
        s,
        "mark_job_run",
        lambda job_id, success, error=None, delivery_error=None: marked.append(
            (success, error, delivery_error)
        ),
    )

    assert s.run_one_job(
        {
            "id": "failed-model",
            "name": "provider check",
            "deliver": deliver_target,
        }
    )

    assert saved == [f"full model diagnostic:\n{diagnostic}"]
    assert marked == [(False, diagnostic, None)]
    deliver.assert_not_called()
    assert not [record for record in caplog.records if record.levelname == "WARNING"]


@pytest.mark.parametrize("policy", [None, "notify", "unexpected-value"])
def test_notify_default_and_unknown_policy_deliver_compact_failure(monkeypatch, policy):
    delivered = []
    config = {"cron": {}}
    if policy is not None:
        config["cron"]["model_failure_delivery"] = policy
    monkeypatch.setattr(s, "load_config", lambda: config)
    monkeypatch.setattr(
        s,
        "run_job",
        lambda job, *, defer_agent_teardown=None, **kw: (
            False,
            "full provider payload",
            "",
            "HTTP 429: huge provider rate limit payload",
        ),
    )
    monkeypatch.setattr(s, "save_job_output", lambda *args: "/tmp/out.md")
    monkeypatch.setattr(
        s,
        "_deliver_result",
        lambda job, content, **kwargs: delivered.append(content),
    )
    monkeypatch.setattr(s, "mark_job_run", lambda *args, **kwargs: None)

    s.run_one_job({"id": "notify-failure", "name": "digest", "deliver": "telegram"})

    assert delivered == [
        "⚠️ Cron 'digest' failed: provider rate limit. "
        "Fallback chain was exhausted or unavailable. "
        "Full details saved in cron output."
    ]


@pytest.mark.parametrize("policy", ["notify", "local"])
def test_successful_llm_response_delivers_under_either_policy(monkeypatch, policy):
    delivered = []
    monkeypatch.setattr(
        s, "load_config", lambda: {"cron": {"model_failure_delivery": policy}}
    )
    monkeypatch.setattr(
        s,
        "run_job",
        lambda job, *, defer_agent_teardown=None, **kw: (
            True,
            "full output",
            "daily report",
            None,
        ),
    )
    monkeypatch.setattr(s, "save_job_output", lambda *args: "/tmp/out.md")
    monkeypatch.setattr(
        s,
        "_deliver_result",
        lambda job, content, **kwargs: delivered.append(content),
    )
    monkeypatch.setattr(s, "mark_job_run", lambda *args, **kwargs: None)

    s.run_one_job({"id": "success", "deliver": "telegram"})

    assert delivered == ["daily report"]


def test_local_policy_keeps_silent_monitor_suppression(monkeypatch):
    calls = _patch_pipeline(monkeypatch, silent_marker_in="[SILENT] No changes detected")
    monkeypatch.setattr(
        s, "load_config", lambda: {"cron": {"model_failure_delivery": "local"}}
    )

    s.run_one_job({"id": "quiet-monitor", "deliver": "telegram"})

    assert "deliver" not in [call[0] for call in calls]


def test_successful_model_delivery_failure_remains_separate(monkeypatch):
    marked = []
    monkeypatch.setattr(
        s, "load_config", lambda: {"cron": {"model_failure_delivery": "local"}}
    )
    monkeypatch.setattr(
        s,
        "run_job",
        lambda job, *, defer_agent_teardown=None, **kw: (
            True,
            "full output",
            "daily report",
            None,
        ),
    )
    monkeypatch.setattr(s, "save_job_output", lambda *args: "/tmp/out.md")
    monkeypatch.setattr(s, "_deliver_result", lambda *args, **kwargs: "network down")
    monkeypatch.setattr(
        s,
        "mark_job_run",
        lambda job_id, success, error=None, delivery_error=None: marked.append(
            (success, error, delivery_error)
        ),
    )

    s.run_one_job({"id": "delivery-failure", "deliver": "telegram"})

    assert marked == [(True, None, "network down")]


def test_local_policy_does_not_suppress_no_agent_watchdog_failure(monkeypatch):
    delivered = []
    monkeypatch.setattr(
        s, "load_config", lambda: {"cron": {"model_failure_delivery": "local"}}
    )
    monkeypatch.setattr(
        s,
        "run_job",
        lambda job, *, defer_agent_teardown=None, **kw: (
            False,
            "watchdog diagnostic",
            "⚠ Cron watchdog 'disk' script failed",
            "exit code 3",
        ),
    )
    monkeypatch.setattr(s, "save_job_output", lambda *args: "/tmp/out.md")
    monkeypatch.setattr(
        s,
        "_deliver_result",
        lambda job, content, **kwargs: delivered.append(content),
    )
    monkeypatch.setattr(s, "mark_job_run", lambda *args, **kwargs: None)

    s.run_one_job({"id": "watchdog", "no_agent": True, "deliver": "telegram"})

    assert delivered == ["⚠️ Cron 'watchdog' failed: exit code 3"]


def test_failed_llm_diagnostic_is_force_redacted_in_output_and_last_error(
    monkeypatch, tmp_path
):
    from agent import redact
    from cron import jobs

    cron_dir = tmp_path / "cron"
    monkeypatch.setattr(jobs, "CRON_DIR", cron_dir)
    monkeypatch.setattr(jobs, "JOBS_FILE", cron_dir / "jobs.json")
    monkeypatch.setattr(jobs, "OUTPUT_DIR", cron_dir / "output")
    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)
    job = jobs.create_job(
        prompt="run report",
        schedule="every 1h",
        deliver="discord:123",
        name="redaction-check",
    )
    secret = "sk-proj-abcdefghijklmnopqrstuvwx"
    diagnostic = f"provider rejected OPENAI_API_KEY={secret}\nretry metadata preserved"
    monkeypatch.setattr(
        s, "load_config", lambda: {"cron": {"model_failure_delivery": "local"}}
    )
    monkeypatch.setattr(
        s,
        "run_job",
        lambda current_job, *, defer_agent_teardown=None, **kw: (
            False,
            f"# Failed run\n\n{diagnostic}",
            "",
            diagnostic,
        ),
    )
    deliver = Mock(return_value=None)
    monkeypatch.setattr(s, "_deliver_result", deliver)

    assert s.run_one_job(job)

    persisted_job = jobs.get_job(job["id"])
    output_files = list((jobs.OUTPUT_DIR / job["id"]).glob("*.md"))
    assert len(output_files) == 1
    persisted_output = output_files[0].read_text()
    assert secret not in persisted_output
    assert secret not in persisted_job["last_error"]
    assert "retry metadata preserved" in persisted_output
    assert "retry metadata preserved" in persisted_job["last_error"]
    assert "OPENAI_API_KEY=***" in persisted_output
    assert "OPENAI_API_KEY=***" in persisted_job["last_error"]
    assert persisted_job["last_status"] == "error"
    assert persisted_job["last_delivery_error"] is None
    deliver.assert_not_called()
