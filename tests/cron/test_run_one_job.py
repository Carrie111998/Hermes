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


class TestRunOneJobRevalidatesPersistedJob:
    """Regression for #82650: a scheduler snapshot must fail closed at
    dispatch admission if the persisted job was deleted or paused after the
    snapshot was taken. The stale dict must NOT reach the executor, the
    execution-running transition, completion accounting, or delivery.

    These exercise the real store (temp HERMES_HOME via the session conftest)
    against real create/trigger/due/pause/remove operations, then drive the
    shared ``run_one_job`` body with the pipeline primitives patched so the
    test can observe whether the agent path was reached.
    """

    def _due_snapshot(self, job_id):
        return next(j for j in s.get_due_jobs() if j["id"] == job_id)

    def test_deleted_job_snapshot_is_skipped(self, monkeypatch):
        """Snapshot enumerated, then the row removed → job must not execute."""
        from cron.jobs import create_job, trigger_job, remove_job

        job = create_job(
            name="stale delete repro",
            schedule="every 60m",
            prompt="must not execute after authoritative state changes",
        )
        assert trigger_job(job["id"]) is not None
        snapshot = self._due_snapshot(job["id"])
        assert remove_job(job["id"]) is True

        calls = _patch_pipeline(monkeypatch)
        result = s.run_one_job(snapshot)

        assert result is True
        assert calls == [], "deleted job reached the executor/delivery path"

    def test_paused_job_snapshot_is_skipped(self, monkeypatch):
        """Snapshot enumerated, then the row paused → job must not execute."""
        from cron.jobs import create_job, trigger_job, pause_job

        job = create_job(
            name="stale pause repro",
            schedule="every 60m",
            prompt="must not execute after authoritative state changes",
        )
        assert trigger_job(job["id"]) is not None
        snapshot = self._due_snapshot(job["id"])
        assert pause_job(job["id"]) is not None

        calls = _patch_pipeline(monkeypatch)
        result = s.run_one_job(snapshot)

        assert result is True
        assert calls == [], "paused job reached the executor/delivery path"

    def test_valid_job_snapshot_still_executes(self, monkeypatch):
        """A still-valid snapshot keeps the full execute→save→deliver→mark run."""
        from cron.jobs import create_job, trigger_job

        job = create_job(
            name="valid repro",
            schedule="every 60m",
            prompt="must execute normally",
        )
        assert trigger_job(job["id"]) is not None
        snapshot = self._due_snapshot(job["id"])

        calls = _patch_pipeline(monkeypatch)
        result = s.run_one_job(snapshot)

        assert result is True
        assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]

    def test_handed_in_dict_missing_row_keeps_compat(self, monkeypatch):
        """Direct/manual handed-in dicts (no store row, no snapshot marker)
        retain the historical missing-row compatibility: they still execute."""
        calls = _patch_pipeline(monkeypatch)

        ok = s.run_one_job({"id": "handed-in-no-row", "name": "t"})

        assert ok is True
        assert [c[0] for c in calls] == ["run_job", "save", "deliver", "mark"]


