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
from pathlib import Path
from unittest.mock import MagicMock, patch
import threading
import time


def _patch_pipeline(monkeypatch, *, success=True, output="out", final="final response",
                    error=None, silent_marker_in=None):
    """Patch the job pipeline primitives and record the call order."""
    calls = []

    def fake_run_job(job, *, defer_agent_teardown=None, **_kwargs):
        calls.append(("run_job", job["id"]))
        fr = final if silent_marker_in is None else silent_marker_in
        return (success, output, fr, error)

    def fake_save(jid, out):
        calls.append(("save", jid))
        return f"/tmp/{jid}.txt"

    def fake_deliver(job, content, adapters=None, loop=None, **_kwargs):
        calls.append(("deliver", job["id"]))
        return None

    def fake_mark(jid, ok, err=None, delivery_error=None, **_kwargs):
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
    monkeypatch.setattr(
        s,
        "get_due_jobs",
        lambda **_kwargs: [{"id": "j1", "name": "t"}],
    )
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


def test_claimed_run_passes_exact_owner_to_dispatch_and_output(monkeypatch):
    """Dispatch consumption and output publication share the attempt token."""
    dispatch_owners = []
    save_owners = []
    monkeypatch.setattr(
        s,
        "_start_run_claim_heartbeat",
        lambda *_a, **_kw: (threading.Event(), threading.Event(), threading.Thread()),
    )
    monkeypatch.setattr(s, "_stop_run_claim_heartbeat", lambda *_a: None)
    monkeypatch.setattr(
        s,
        "claim_dispatch",
        lambda _jid, *, expected_run_claim_owner=None: (
            dispatch_owners.append(expected_run_claim_owner) or True
        ),
    )
    monkeypatch.setattr(
        s,
        "run_job",
        lambda *_a, **_kw: (True, "output", "[SILENT]", None),
    )
    monkeypatch.setattr(
        s,
        "save_job_output",
        lambda _jid, _out, *, expected_run_claim_owner=None: (
            save_owners.append(expected_run_claim_owner) or Path("/tmp/out.md")
        ),
    )
    monkeypatch.setattr(s, "mark_job_run", lambda *_a, **_kw: True)
    monkeypatch.setattr(s, "finish_execution", lambda *_a, **_kw: None)

    assert s.run_one_job({
        "id": "owned-pipeline",
        "execution_id": "execution-owned",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt-a"},
    }) is True
    assert dispatch_owners == ["attempt-a"]
    assert save_owners == ["attempt-a"]


def test_output_rejection_is_ownership_loss_and_stale_execution_fails(monkeypatch):
    finished = []
    monkeypatch.setattr(
        s,
        "_start_run_claim_heartbeat",
        lambda *_a, **_kw: (threading.Event(), threading.Event(), threading.Thread()),
    )
    monkeypatch.setattr(s, "_stop_run_claim_heartbeat", lambda *_a: None)
    monkeypatch.setattr(s, "claim_dispatch", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        s,
        "run_job",
        lambda *_a, **_kw: (True, "stale output", "stale response", None),
    )
    monkeypatch.setattr(s, "save_job_output", lambda *_a, **_kw: None)
    monkeypatch.setattr(s, "_deliver_result", lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("stale attempt must not deliver")
    ))
    monkeypatch.setattr(s, "mark_job_run", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        s,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )

    assert s.run_one_job({
        "id": "stale-save",
        "execution_id": "execution-stale-save",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt-a"},
    }) is False
    assert finished[-1][1]["success"] is False
    assert "ownership" in finished[-1][1]["error"].lower()


def test_shutdown_output_rejection_consumes_only_current_attempt_flag(monkeypatch):
    """A shutdown-cleared token must not poison the next run of the same job."""
    job = {
        "id": "shutdown-save-loss",
        "execution_id": "execution-shutdown-save-loss",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt-a"},
    }
    scoped_key = f"{s._get_hermes_home().resolve()}\0{job['id']}"
    s._interrupted_job_ids.add(scoped_key)
    s._interrupted_run_claim_owners[scoped_key] = "attempt-a"
    monkeypatch.setattr(
        s,
        "_start_run_claim_heartbeat",
        lambda *_a, **_kw: (threading.Event(), threading.Event(), threading.Thread()),
    )
    monkeypatch.setattr(s, "_stop_run_claim_heartbeat", lambda *_a: None)
    monkeypatch.setattr(s, "claim_dispatch", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        s, "run_job", lambda *_a, **_kw: (True, "output", "response", None)
    )
    monkeypatch.setattr(s, "save_job_output", lambda *_a, **_kw: None)
    monkeypatch.setattr(s, "finish_execution", lambda *_a, **_kw: None)

    try:
        assert s.run_one_job(job) is False
        assert scoped_key not in s._interrupted_job_ids
        assert scoped_key not in s._interrupted_run_claim_owners
    finally:
        s._interrupted_job_ids.discard(scoped_key)
        s._interrupted_run_claim_owners.pop(scoped_key, None)


def test_run_one_job_rejects_lost_attempt_before_side_effect(monkeypatch):
    ran = []
    finished = []
    monkeypatch.setattr(s, "heartbeat_run_claim", lambda *_a, **_kw: False)
    monkeypatch.setattr(s, "run_job", lambda *_a, **_kw: ran.append(True))
    monkeypatch.setattr(
        s,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )

    assert s.run_one_job({
        "id": "lost-fence",
        "execution_id": "execution-lost",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "old"},
    }) is False
    assert ran == []
    assert finished[-1][0] == "execution-lost"
    assert "ownership lost" in finished[-1][1]["error"].lower()


def test_run_claim_heartbeat_retries_transient_store_error(monkeypatch):
    """A temporary jobs-store error must not make a live attempt look dead."""
    calls = []

    def flaky_heartbeat(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("temporary fsync failure")
        return True

    monkeypatch.setattr(s, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(s, "heartbeat_run_claim", flaky_heartbeat)

    heartbeat = s._start_run_claim_heartbeat(
        {
            "id": "transient-heartbeat",
            "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt"},
        },
        thread_name="test-run-claim-heartbeat",
    )
    assert heartbeat is not None
    stop, ownership_lost, _thread = heartbeat
    deadline = time.monotonic() + 2
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.005)
    stop.set()
    s._stop_run_claim_heartbeat(heartbeat)

    assert len(calls) >= 3
    assert not ownership_lost.is_set()


def test_run_claim_heartbeat_self_fences_after_persistent_store_error(monkeypatch):
    """A worker stops itself before its unverifiable token can expire."""
    calls = []

    def persistent_failure(*_args, **_kwargs):
        calls.append(True)
        if len(calls) == 1:
            return True
        raise OSError("persistent isolated store failure")

    monkeypatch.setattr(s, "_RUN_CLAIM_HEARTBEAT_SECONDS", 0.005)
    monkeypatch.setattr(s, "_run_claim_self_fence_seconds", lambda: 0.02)
    monkeypatch.setattr(s, "heartbeat_run_claim", persistent_failure)

    heartbeat = s._start_run_claim_heartbeat(
        {
            "id": "self-fenced-heartbeat",
            "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt"},
        },
        thread_name="test-run-claim-self-fence",
    )
    assert heartbeat is not None
    _stop, ownership_lost, _thread = heartbeat
    assert ownership_lost.wait(timeout=2)
    s._stop_run_claim_heartbeat(heartbeat)
    assert len(calls) >= 2


def test_run_one_job_propagates_claim_loss_signal_to_job_pipeline(monkeypatch):
    """The active job pipeline must receive the heartbeat's loss signal."""
    ownership_lost = threading.Event()
    heartbeat = (threading.Event(), ownership_lost, threading.Thread())
    seen = []

    monkeypatch.setattr(s, "_start_run_claim_heartbeat", lambda *_a, **_kw: heartbeat)
    monkeypatch.setattr(s, "_stop_run_claim_heartbeat", lambda _heartbeat: None)
    monkeypatch.setattr(
        s,
        "_run_one_job_body",
        lambda *_a, **kwargs: seen.append(kwargs.get("claim_ownership_lost")) or True,
    )

    assert s.run_one_job({
        "id": "claim-loss-signal",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt"},
    }) is True
    assert seen == [ownership_lost]


def test_delivery_fails_closed_when_claim_store_cannot_verify(monkeypatch):
    """Unverifiable ownership must suppress the external delivery side effect."""
    heartbeats = 0
    delivered = []

    def heartbeat(*_args, **_kwargs):
        nonlocal heartbeats
        heartbeats += 1
        if heartbeats == 1:
            return True
        raise OSError("claim store unavailable")

    monkeypatch.setattr(s, "heartbeat_run_claim", heartbeat)
    monkeypatch.setattr(
        s,
        "run_job",
        lambda *_a, **_kw: (True, "output", "do not deliver", None),
    )
    monkeypatch.setattr(s, "save_job_output", lambda *_a, **_kw: "/tmp/output")
    monkeypatch.setattr(
        s,
        "_deliver_result",
        lambda *_a, **_kw: delivered.append(True),
    )
    monkeypatch.setattr(s, "mark_job_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(s, "finish_execution", lambda *_a, **_kw: None)

    assert s.run_one_job({
        "id": "unverifiable-delivery",
        "execution_id": "execution-unverifiable",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt"},
    }) is False
    assert delivered == []


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

    def fake_run_job(job, *, defer_agent_teardown=None, **_kwargs):
        # This is where resolve_runtime_provider() would read a secret. Prove a
        # scope is installed and the profile's secret resolves without raising.
        scope_during_run["scope"] = ss.current_secret_scope()
        scope_during_run["base_url"] = ss.get_secret("OPENROUTER_BASE_URL")
        return (True, "out", "final", None)

    def fake_deliver(*_args, **_kwargs):
        scope_during_run["scope_during_delivery"] = ss.current_secret_scope()
        scope_during_run["delivery_base_url"] = ss.get_secret(
            "OPENROUTER_BASE_URL"
        )
        return None

    monkeypatch.setattr(s, "run_job", fake_run_job)
    monkeypatch.setattr(s, "save_job_output", lambda jid, out: f"/tmp/{jid}.txt")
    monkeypatch.setattr(s, "_deliver_result", fake_deliver)
    monkeypatch.setattr(s, "mark_job_run", lambda *a, **k: True)

    ss.set_multiplex_active(True)
    try:
        ok = s.run_one_job({"id": "j7", "name": "t"})
    finally:
        ss.set_multiplex_active(False)

    assert ok is True
    # Scope was installed during run_job and the profile secret resolved.
    assert scope_during_run["scope"] is not None
    assert scope_during_run["base_url"] == "https://openrouter.ai/api/v1"
    assert scope_during_run["scope_during_delivery"] is not None
    assert scope_during_run["delivery_base_url"] == "https://openrouter.ai/api/v1"
    # And it was torn down after run_one_job returned (no leak).
    assert ss.current_secret_scope() is None


def test_terminal_mark_cas_false_fails_execution_ledger(monkeypatch):
    finished = []
    monkeypatch.setattr(
        s,
        "_start_run_claim_heartbeat",
        lambda *_a, **_kw: (threading.Event(), threading.Event(), threading.Thread()),
    )
    monkeypatch.setattr(s, "_stop_run_claim_heartbeat", lambda *_a: None)
    monkeypatch.setattr(s, "claim_dispatch", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        s, "run_job", lambda *_a, **_kw: (True, "output", "[SILENT]", None)
    )
    monkeypatch.setattr(s, "save_job_output", lambda *_a, **_kw: Path("/tmp/out"))
    monkeypatch.setattr(s, "mark_job_run", lambda *_a, **_kw: False)
    monkeypatch.setattr(
        s,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )

    assert s.run_one_job({
        "id": "terminal-cas-loss",
        "execution_id": "execution-terminal-cas-loss",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt-a"},
    }) is False
    assert finished[-1][1]["success"] is False
    assert "terminal" in finished[-1][1]["error"].lower()


def test_shutdown_race_after_delivery_check_fails_ledger_and_attempt(monkeypatch):
    job = {
        "id": "late-shutdown",
        "execution_id": "execution-late-shutdown",
        "run_claim": {"at": "2026-01-01T00:00:00+00:00", "by": "attempt-a"},
    }
    scoped_key = f"{s._get_hermes_home().resolve()}\0{job['id']}"
    finished = []
    outcome = {}
    monkeypatch.setattr(
        s,
        "_start_run_claim_heartbeat",
        lambda *_a, **_kw: (threading.Event(), threading.Event(), threading.Thread()),
    )
    monkeypatch.setattr(s, "_stop_run_claim_heartbeat", lambda *_a: None)
    monkeypatch.setattr(s, "claim_dispatch", lambda *_a, **_kw: True)
    monkeypatch.setattr(
        s, "run_job", lambda *_a, **_kw: (True, "output", "[SILENT]", None)
    )
    monkeypatch.setattr(s, "save_job_output", lambda *_a, **_kw: Path("/tmp/out"))
    monkeypatch.setattr(s, "heartbeat_run_claim", lambda *_a, **_kw: True)
    monkeypatch.setattr(s, "_is_interrupted", lambda *_a, **_kw: False)

    def race_shutdown(_content):
        s._interrupted_job_ids.add(scoped_key)
        s._interrupted_run_claim_owners[scoped_key] = "attempt-a"
        return True

    monkeypatch.setattr(s, "_is_cron_silence_response", race_shutdown)
    monkeypatch.setattr(
        s,
        "mark_job_run",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("shutdown already wrote the terminal state")
        ),
    )
    monkeypatch.setattr(
        s,
        "finish_execution",
        lambda execution_id, **kwargs: finished.append((execution_id, kwargs)),
    )

    assert s.run_one_job(job, _attempt_outcome=outcome) is True
    assert finished[-1][1]["success"] is False
    assert outcome["success"] is False
    assert "shutdown" in outcome["error"].lower()
