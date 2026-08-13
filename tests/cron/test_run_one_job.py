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


def _patch_outer_failure_pipeline(monkeypatch, *, delivery_result=None):
    """Install the minimal real run_one_job shell around an escaped failure."""
    from agent import secret_scope as ss

    delivered = []
    marked = []
    finished = []

    monkeypatch.setattr(s, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(s, "mark_execution_running", lambda _execution_id: None)
    monkeypatch.setattr(ss, "build_profile_secret_scope", lambda _home: None)
    monkeypatch.setattr(ss, "set_secret_scope", lambda _scope: None)
    monkeypatch.setattr(ss, "reset_secret_scope", lambda _token: None)
    monkeypatch.setattr(
        s,
        "run_job",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("outer boom")),
    )
    monkeypatch.setattr(
        s,
        "_deliver_result",
        lambda job, content, **_kwargs: delivered.append((job["id"], content))
        or delivery_result,
    )
    monkeypatch.setattr(
        s,
        "mark_job_run",
        lambda *args, **kwargs: marked.append((args, kwargs)),
    )
    monkeypatch.setattr(
        s,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )
    return delivered, marked, finished


def test_run_one_job_delivers_exception_escaping_normal_completion(monkeypatch):
    """An escaped run_job exception must alert the configured destination."""
    delivered, marked, finished = _patch_outer_failure_pipeline(monkeypatch)
    job = {
        "id": "outer-failure",
        "name": "Outer failure",
        "deliver": "telegram:123",
        "execution_id": "exec-outer-failure",
    }

    assert s.run_one_job(job) is False

    assert delivered == [
        ("outer-failure", "⚠️ Cron 'Outer failure' failed: outer boom")
    ]
    assert marked == [
        (("outer-failure", False, "outer boom"), {"delivery_error": None})
    ]
    assert finished == [
        (
            "exec-outer-failure",
            {
                "success": False,
                "error": "outer boom",
                "delivery_outcome": "delivered",
            },
        )
    ]


def test_run_one_job_records_outer_failure_delivery_error(monkeypatch):
    """A failed outer-failure alert is visible in both job and ledger state."""
    delivered, marked, finished = _patch_outer_failure_pipeline(
        monkeypatch, delivery_result="telegram offline",
    )
    job = {
        "id": "outer-delivery-failure",
        "name": "Outer delivery failure",
        "deliver": "telegram:123",
        "execution_id": "exec-outer-delivery-failure",
    }

    assert s.run_one_job(job) is False

    assert len(delivered) == 1
    assert marked == [
        (
            ("outer-delivery-failure", False, "outer boom"),
            {"delivery_error": "telegram offline"},
        )
    ]
    assert finished[0][1]["delivery_outcome"] == "failed"


def test_run_one_job_does_not_redeliver_after_post_delivery_exception(monkeypatch):
    """Bookkeeping failure after delivery must not send the result twice."""
    from agent import secret_scope as ss

    delivered = []
    finished = []
    monkeypatch.setattr(s, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(s, "mark_execution_running", lambda _execution_id: None)
    monkeypatch.setattr(ss, "build_profile_secret_scope", lambda _home: None)
    monkeypatch.setattr(ss, "set_secret_scope", lambda _scope: None)
    monkeypatch.setattr(ss, "reset_secret_scope", lambda _token: None)
    monkeypatch.setattr(
        s, "run_job",
        lambda *_args, **_kwargs: (True, "output", "finished", None),
    )
    monkeypatch.setattr(s, "save_job_output", lambda *_args: "/tmp/output.md")
    monkeypatch.setattr(
        s, "_deliver_result",
        lambda job, content, **_kwargs: delivered.append((job["id"], content)),
    )
    monkeypatch.setattr(
        s,
        "mark_job_run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("mark failed")),
    )
    monkeypatch.setattr(
        s,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )
    job = {
        "id": "post-delivery-failure",
        "name": "Post delivery failure",
        "deliver": "telegram:123",
        "execution_id": "exec-post-delivery-failure",
    }

    assert s.run_one_job(job) is False

    assert delivered == [("post-delivery-failure", "finished")]
    assert finished[0][1]["delivery_outcome"] == "delivered"


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

