# JobFlow Reconciler Enabled-Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the JobFlow reconciler's "resolve to exactly one ENABLED cron job, fail closed on zero-or-multiple" guard out of an LLM prompt and into code, so triggering a disabled worker can no longer silently re-enable it.

**Architecture:** A new non-enabling trigger primitive (`cron.jobs.request_run`) refuses any job whose `enabled` is false and writes only `next_run_at`. The dispatcher's existing resolver moves into `jobflow_dispatch/activate.py` so the reconciler wrapper and the event dispatcher share one implementation. A new `activate_pending()` orchestrates resolve → dedupe → activate, and `render_report()` owns the stdout/wake-gate contract. The cron pre-run script becomes a thin caller, and the agent is demoted from actuator to diagnostician.

**Tech Stack:** Python 3.11, pytest, existing `cron/`, `jobflow_dispatch/`, `events/` packages. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-10-jobflow-reconcile-enabled-guard-design.md`

## Global Constraints

- **The cron schedule `30 0,6,12,18 * * *` MUST NOT change.** The 30-minute offset makes the reconciler trail the 6-hourly worker; aligning them recreates the cadence trap. See agent memory `jobflow-reconciler-offset-from-tailor-cycle`.
- **Never hand-edit `cron/jobs.json`.** Job changes go through `hermes cron create` / `hermes cron edit`. Hand-edits can leave records with no `id`.
- **Fail-closed on zero-or-multiple matches is preserved.** Activating the wrong worker is worse than not activating one, because the next reconcile catches the miss.
- **`trigger_job` behavior is NOT changed.** Its three production callers (`hermes_cli/cron.py:495`, `gateway/platforms/api_server.py:4704`, `hermes_cli/web_server.py:11945`) are operator-initiated and must keep reviving. Task 1 pins this with a test.
- **The activation ledger stays read-only on this path.** The reconciler claims nothing; it remains the safety net, not a second claimant.
- **Run tests from PowerShell**, from the worktree root, as `python -m pytest ...`. See agent memory `gateway-suite-windows-green`.
- **`docs/superpowers/*` is gitignored** (`.gitignore:121`) but 22 specs and 24 plans are tracked. Committing a file under it requires `git add -f`, matching the established convention.
- **SEQUENCING — the wrapper imports the SHARED CHECKOUT, not this worktree.** `jobflow_reconcile.py` does `sys.path.insert(0, Path.home() / ".hermes" / "agent-src")`. Tasks 1–4 must be merged into `~/.hermes/agent-src` before Task 5's wrapper can import `jobflow_dispatch.activate` at runtime. Tasks 5 and 6 are live-system changes in a different git repo (`~/.hermes`) and need explicit go-ahead before execution.

---

### Task 1: `cron.jobs.request_run()` — the non-enabling trigger

**Files:**
- Modify: `cron/jobs.py` (insert after `trigger_job`, which ends at line 1756)
- Modify: `cron/__init__.py:26` (import list) and `cron/__init__.py:39` (`__all__`)
- Test: `tests/cron/test_jobs.py` (new class at end of file)

**Interfaces:**
- Consumes: `resolve_job_ref`, `update_job`, `_hermes_now`, `emit_cron_triggered_safe`, `logger` — all already in `cron/jobs.py`.
- Produces: `cron.jobs.request_run(job_id: str, *, caller: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]`. Returns the updated job dict on success; `None` when the job is unknown OR not enabled. Raises `ValueError` on an empty caller and `AmbiguousJobReference` on an ambiguous name. Task 3 calls this.

- [ ] **Step 1: Write the failing tests**

Append to `tests/cron/test_jobs.py`:

```python
# =========================================================================
# request_run — the non-enabling trigger
#
# Spec: docs/superpowers/specs/2026-08-10-jobflow-reconcile-enabled-guard-design.md
# Why it exists: trigger_job sets enabled=True, so the JobFlow reconciler
# triggering a mis-resolved job would silently revive a worker an operator
# disabled. request_run refuses instead.
# =========================================================================

class TestRequestRun:
    def test_schedules_an_enabled_job_without_touching_lifecycle_fields(
        self, tmp_cron_dir, monkeypatch
    ):
        """It advances next_run_at and writes NOTHING else."""
        from cron.jobs import create_job, get_job, request_run
        from events.bus import EventBus

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        result = request_run(
            job["id"], caller="cron:jobflow-reconcile", reason="reconcile"
        )

        assert result is not None
        assert result["next_run_at"] != job["next_run_at"]
        assert result["enabled"] is True
        assert result["state"] == job["state"]
        assert result["paused_at"] == job["paused_at"]

        stored = get_job(job["id"])
        assert stored["next_run_at"] == result["next_run_at"]

    def test_disabled_job_is_not_revived_and_the_store_is_byte_identical(
        self, tmp_cron_dir, monkeypatch
    ):
        """THE load-bearing regression. A refused request must not write."""
        from cron.jobs import JOBS_FILE, create_job, pause_job, request_run
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        pause_job(job["id"])
        before = JOBS_FILE.read_bytes()

        assert request_run(job["id"], caller="test", reason="r") is None

        assert JOBS_FILE.read_bytes() == before
        assert bus.query(event_type=EventType.CRON_TRIGGERED) == []

    def test_refuses_a_job_disabled_without_being_paused(self, tmp_cron_dir, monkeypatch):
        """`enabled` is the gate, not `state`.

        pause_job sets both, but a job disabled directly through update_job has
        enabled=False with state="scheduled". Gating on state would activate it.
        """
        from cron.jobs import create_job, request_run, update_job
        from events.bus import EventBus

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        disabled = update_job(job["id"], {"enabled": False})
        assert disabled["state"] == "scheduled"

        assert request_run(job["id"], caller="test") is None

    def test_trigger_job_still_revives_a_disabled_job(self, tmp_cron_dir, monkeypatch):
        """Deliberate contrast, pinned on purpose.

        The operator paths (CLI `cron run`, api_server, web_server) rely on
        trigger_job reviving. A refactor that converges the two functions must
        fail HERE rather than silently removing that behavior.
        """
        from cron.jobs import create_job, pause_job, trigger_job
        from events.bus import EventBus

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        pause_job(job["id"])
        result = trigger_job(job["id"], caller="test")

        assert result is not None
        assert result["enabled"] is True
        assert result["state"] == "scheduled"

    def test_requires_a_non_empty_caller(self, tmp_cron_dir):
        """A new API with no back-compat debt takes the stricter contract."""
        from cron.jobs import create_job, request_run

        job = create_job(prompt="x", schedule="every 1h")
        with pytest.raises(ValueError, match="caller"):
            request_run(job["id"], caller="")
        with pytest.raises(ValueError, match="caller"):
            request_run(job["id"], caller="   ")

    def test_returns_none_for_unknown_job(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import request_run
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        assert request_run("nonexistent", caller="test") is None
        assert bus.query(event_type=EventType.CRON_TRIGGERED) == []

    def test_emits_cron_triggered_with_caller_and_reason(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import create_job, request_run
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        result = request_run(
            job["id"], caller="cron:jobflow-reconcile", reason="reconcile"
        )

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        e = events[0]
        assert e.payload["caller"] == "cron:jobflow-reconcile"
        assert e.payload["reason"] == "reconcile"
        assert e.payload["job_id"] == job["id"]
        assert e.payload["previous_next_run_at"] == job["next_run_at"]
        assert e.payload["new_next_run_at"] == result["next_run_at"]

    def test_emit_failure_does_not_break_the_write(self, tmp_cron_dir, monkeypatch):
        """An unhealthy bus must never cost the activation."""
        from cron.jobs import create_job, get_job, request_run

        def broken_bus():
            raise RuntimeError("bus broken")

        monkeypatch.setattr("cron.jobs._get_event_bus", broken_bus)

        job = create_job(prompt="x", schedule="every 1h")
        result = request_run(job["id"], caller="test")

        assert result is not None
        assert get_job(job["id"])["next_run_at"] == result["next_run_at"]

    def test_ambiguous_name_raises(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import AmbiguousJobReference, create_job, request_run
        from events.bus import EventBus

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        create_job(prompt="a", schedule="every 1h", name="dup")
        create_job(prompt="b", schedule="every 1h", name="dup")
        with pytest.raises(AmbiguousJobReference):
            request_run("dup", caller="test")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/cron/test_jobs.py::TestRequestRun -v`
Expected: FAIL — `ImportError: cannot import name 'request_run' from 'cron.jobs'` on every test except `test_trigger_job_still_revives_a_disabled_job`, which should already PASS (it describes existing behavior).

- [ ] **Step 3: Implement `request_run`**

In `cron/jobs.py`, insert immediately after `trigger_job` ends (after line 1756, before `def remove_job`):

```python
def request_run(
    job_id: str,
    *,
    caller: str,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Schedule an ALREADY-ENABLED job for the next tick. Never enables.

    The non-enabling counterpart to ``trigger_job``, for callers that are
    activating work on a worker's behalf rather than expressing an operator's
    "run this now" intent. ``trigger_job`` sets ``enabled: True``, so an
    automated caller that mis-resolves a job silently revives a worker an
    operator deliberately disabled — the hazard ``cron.wake_channel`` avoids
    in-process, and which this function makes available across processes.

    Returns ``None`` — writing nothing at all — when the job is unknown or not
    enabled. Fails closed on purpose: not activating is recoverable, reviving a
    disabled worker is not.

    Writes EXACTLY ``next_run_at``. The due scan gates on ``enabled`` and never
    reads ``state`` (see ``get_due_and_skipped_jobs``), and ``pause_job`` always
    sets ``enabled: False`` alongside ``state: "paused"`` — so the single
    ``enabled`` check above already covers paused jobs, and no lifecycle field
    needs touching. Keeping the write to one field is what makes "this cannot
    change operator-visible state" assertable.

    ``caller`` is required, unlike ``trigger_job``'s warn-and-continue
    back-compat allowance: this is a new API, and an unattributable automated
    activation is impossible to reconstruct in a postmortem.
    """
    if not isinstance(caller, str) or not caller.strip():
        raise ValueError("caller must be a non-empty string")

    job = resolve_job_ref(job_id)
    if not job:
        return None

    if not job.get("enabled"):
        logger.info(
            "request_run refused job_id=%s name=%s: not enabled — not reviving "
            "(caller=%s reason=%s)",
            job["id"], job.get("name"), caller, reason,
        )
        return None

    previous_next_run_at = job.get("next_run_at")

    updated = update_job(job["id"], {"next_run_at": _hermes_now().isoformat()})

    if updated is not None:
        emit_cron_triggered_safe(
            job_id=job["id"],
            job_name=updated.get("name") or job.get("name") or job["id"],
            caller=caller,
            reason=reason,
            previous_next_run_at=previous_next_run_at,
            new_next_run_at=updated["next_run_at"],
        )

    return updated
```

- [ ] **Step 4: Export it from the package**

In `cron/__init__.py`, add `request_run,` to the import block from `.jobs` (beside `trigger_job` on line 26) and add `"request_run",` to `__all__` (beside `"trigger_job"` on line 39).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/cron/test_jobs.py::TestRequestRun -v`
Expected: PASS, 9 passed.

- [ ] **Step 6: Run the full cron suite for regressions**

Run: `python -m pytest tests/cron/ -q`
Expected: PASS with no new failures. Compare against the pre-change baseline if anything fails — see agent memory `tests-tools-windows-baseline`.

- [ ] **Step 7: Commit**

```bash
git add cron/jobs.py cron/__init__.py tests/cron/test_jobs.py && git commit -m "feat(cron): add request_run, a non-enabling trigger that refuses disabled jobs"
```

---

### Task 2: Relocate the resolver into `jobflow_dispatch`

**Files:**
- Create: `jobflow_dispatch/activate.py`
- Modify: `events/subscribers/jobflow_dispatcher.py:60-86` (delete the function, import it instead)
- Test: `tests/jobflow_dispatch/test_activate.py`

**Interfaces:**
- Consumes: `activity_policy.registry.ActivityRegistry`, `cron.jobs.load_jobs` (imported lazily inside the function so importing this module stays cheap).
- Produces: `jobflow_dispatch.activate.resolve_job_id_for_activity(activity_id: str) -> Optional[str]`. Re-exported unchanged as `events.subscribers.jobflow_dispatcher.resolve_job_id_for_activity`. Task 3 calls it.

- [ ] **Step 1: Write the failing tests**

Create `tests/jobflow_dispatch/test_activate.py`:

```python
"""Resolving an activity to the one enabled cron job that serves it.

Fail-closed on zero OR multiple is the whole point: activating the wrong
worker is worse than not activating one, because the next reconcile catches
the miss. Tests use REAL activity IDs from activity_policy/policies.yaml so a
rename of an alias breaks here rather than in production.
"""

from __future__ import annotations

import pytest

from jobflow_dispatch.activate import resolve_job_id_for_activity


def _job(name, job_id, enabled=True):
    return {"id": job_id, "name": name, "enabled": enabled}


class TestResolveJobIdForActivity:
    def test_resolves_a_single_enabled_job(self, monkeypatch):
        monkeypatch.setattr(
            "cron.jobs.load_jobs",
            lambda: [_job("jobflow-tailor", "b95c7eba034a")],
        )
        assert resolve_job_id_for_activity("jobflow.tailor.generate") == "b95c7eba034a"

    def test_refuses_when_the_only_match_is_disabled(self, monkeypatch):
        """The hazard this whole change exists to close."""
        monkeypatch.setattr(
            "cron.jobs.load_jobs",
            lambda: [_job("jobflow-tailor", "b95c7eba034a", enabled=False)],
        )
        assert resolve_job_id_for_activity("jobflow.tailor.generate") is None

    def test_refuses_when_no_job_matches(self, monkeypatch):
        monkeypatch.setattr("cron.jobs.load_jobs", lambda: [])
        assert resolve_job_id_for_activity("jobflow.tailor.generate") is None

    def test_refuses_when_two_enabled_jobs_match(self, monkeypatch):
        """Refuse to guess rather than pick the first."""
        monkeypatch.setattr(
            "cron.jobs.load_jobs",
            lambda: [
                _job("jobflow-tailor", "aaaaaaaaaaaa"),
                _job("jobflow-tailor", "bbbbbbbbbbbb"),
            ],
        )
        assert resolve_job_id_for_activity("jobflow.tailor.generate") is None

    def test_unknown_activity_returns_none(self, monkeypatch):
        monkeypatch.setattr("cron.jobs.load_jobs", lambda: [])
        assert resolve_job_id_for_activity("no.such.activity") is None


def test_dispatcher_still_exposes_the_resolver():
    """The subscriber's import must survive the move — it is its default arg."""
    from events.subscribers import jobflow_dispatcher

    assert (
        jobflow_dispatcher.resolve_job_id_for_activity is resolve_job_id_for_activity
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/jobflow_dispatch/test_activate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'jobflow_dispatch.activate'`.

- [ ] **Step 3: Create the module**

Create `jobflow_dispatch/activate.py`:

```python
"""Turning a scanned activation into a woken worker, in code.

The reconciler used to hand its scan output to an LLM and instruct it, in
prose, to resolve each activity to exactly one ENABLED cron job and trigger it
with ``hermes cron run``. That command re-enables whatever it triggers, so the
only thing standing between a mis-resolving agent and a revived worker was a
sentence in a prompt. This module is that sentence, as code.

``resolve_job_id_for_activity`` lives here rather than in the event subscriber
because it now has two consumers — the dispatcher and the reconciler — and the
dispatcher's own docstring already requires that routing, the claim ledger, and
the availability predicate each have exactly one implementation. Resolution
belongs in that set.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def resolve_job_id_for_activity(activity_id: str) -> Optional[str]:
    """Map a policy activity ID to exactly one enabled cron job ID.

    Fails closed on zero or multiple matches: activating the wrong worker is
    worse than not activating one, because the reconciler will catch the miss.
    """
    from activity_policy.registry import ActivityRegistry
    from cron.jobs import load_jobs

    registry = ActivityRegistry.load_default()
    policy = registry.policies.get(activity_id)
    if policy is None or not policy.aliases:
        logger.warning("dispatch: no policy/alias for activity %s", activity_id)
        return None

    names = {alias for alias in policy.aliases}
    matches = [
        job for job in load_jobs()
        if job.get("name") in names and job.get("enabled")
    ]
    if len(matches) != 1:
        logger.warning(
            "dispatch: activity %s resolved %d enabled jobs — refusing to guess",
            activity_id, len(matches),
        )
        return None
    return matches[0].get("id")
```

- [ ] **Step 4: Delete the original and import it instead**

In `events/subscribers/jobflow_dispatcher.py`, delete the whole `def resolve_job_id_for_activity(...)` block (lines 60–86) and add this import beside the existing `jobflow_dispatch` imports near line 42:

```python
from jobflow_dispatch.activate import resolve_job_id_for_activity
```

Leave `__init__`'s default argument (`resolve_job_id: Callable[[str], Optional[str]] = resolve_job_id_for_activity`) exactly as it is — it now binds the imported name.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/jobflow_dispatch/test_activate.py tests/events/subscribers/test_jobflow_dispatcher.py -v`
Expected: PASS. The dispatcher's own suite must be unchanged — this task moves code, it does not alter behavior.

- [ ] **Step 6: Confirm the new module ships in the wheel**

Run: `python -m pytest tests/test_packaging_metadata.py -q`
Expected: PASS. `jobflow_dispatch` is already declared, so a new module inside it needs no pyproject change — this run proves that rather than assuming it.

- [ ] **Step 7: Commit**

```bash
git add jobflow_dispatch/activate.py events/subscribers/jobflow_dispatcher.py tests/jobflow_dispatch/test_activate.py && git commit -m "refactor(jobflow): move the activity resolver into jobflow_dispatch"
```

---

### Task 3: `activate_pending()` — resolve, dedupe, activate

**Files:**
- Modify: `jobflow_dispatch/activate.py`
- Test: `tests/jobflow_dispatch/test_activate.py`

**Interfaces:**
- Consumes: `resolve_job_id_for_activity` (Task 2), `cron.jobs.request_run` (Task 1), `jobflow_dispatch.contracts.Activation`.
- Produces:
  - `ActivationReport` — frozen dataclass with fields `activations: int`, `activities: int`, `activated: tuple[str, ...]` (job IDs), `unresolved: tuple[str, ...]`, `refused: tuple[str, ...]`, `errors: tuple[str, ...]` (all activity IDs), plus property `needs_agent: bool`.
  - `activate_pending(activations, *, resolve=..., request_run=None, caller=CALLER, reason=REASON) -> ActivationReport`
  - Module constants `CALLER = "cron:jobflow-reconcile"` and `REASON = "reconcile"`.

  Task 4 consumes `ActivationReport`. Task 5 calls `activate_pending`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/jobflow_dispatch/test_activate.py`:

```python
from jobflow_dispatch.activate import ActivationReport, activate_pending
from jobflow_dispatch.contracts import Activation


def _act(activity_id, key="tailor/inbox/m1.json"):
    return Activation(
        activity_id=activity_id,
        profile="main",
        message_key=key,
        correlation_id=None,
        reason="reconcile",
    )


class _Recorder:
    """Stand-in for cron.jobs.request_run that records how it was called."""

    def __init__(self, refuse=(), raise_for=()):
        self.calls = []
        self._refuse = set(refuse)
        self._raise_for = set(raise_for)

    def __call__(self, job_id, *, caller, reason=None):
        self.calls.append((job_id, caller, reason))
        if job_id in self._raise_for:
            raise RuntimeError("boom")
        if job_id in self._refuse:
            return None
        return {"id": job_id, "next_run_at": "2026-08-11T00:30:00-04:00"}


class TestActivatePending:
    def test_activates_each_resolved_job_once(self):
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=lambda a: {"a.one": "job-1", "a.two": "job-2"}[a],
            request_run=runner,
        )

        assert [c[0] for c in runner.calls] == ["job-1", "job-2"]
        assert report.activated == ("job-1", "job-2")
        assert report.activations == 2
        assert report.activities == 2
        assert report.needs_agent is False

    def test_many_activations_for_one_job_wake_it_once(self):
        """Trigger each distinct job at most once per run, however many
        activations map to it."""
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one", f"k{i}") for i in range(5)],
            resolve=lambda a: "job-1",
            request_run=runner,
        )

        assert len(runner.calls) == 1
        assert report.activations == 5
        assert report.activities == 1
        assert report.activated == ("job-1",)

    def test_two_activities_sharing_one_job_wake_it_once(self):
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=lambda a: "job-1",
            request_run=runner,
        )

        assert len(runner.calls) == 1
        assert report.activated == ("job-1",)
        assert report.needs_agent is False

    def test_unresolved_activity_is_counted_and_never_activated(self):
        runner = _Recorder()
        report = activate_pending(
            [_act("a.one")],
            resolve=lambda a: None,
            request_run=runner,
        )

        assert runner.calls == []
        assert report.unresolved == ("a.one",)
        assert report.activated == ()
        assert report.needs_agent is True

    def test_job_disabled_between_scan_and_activation_is_refused(self):
        """The TOCTOU case: resolution succeeded, request_run said no."""
        runner = _Recorder(refuse={"job-1"})
        report = activate_pending(
            [_act("a.one")],
            resolve=lambda a: "job-1",
            request_run=runner,
        )

        assert report.refused == ("a.one",)
        assert report.activated == ()
        assert report.needs_agent is True

    def test_one_failure_does_not_prevent_the_others(self):
        runner = _Recorder(raise_for={"job-1"})
        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=lambda a: {"a.one": "job-1", "a.two": "job-2"}[a],
            request_run=runner,
        )

        assert report.errors == ("a.one",)
        assert report.activated == ("job-2",)
        assert report.needs_agent is True

    def test_a_raising_resolver_is_isolated_too(self):
        runner = _Recorder()

        def resolve(activity_id):
            if activity_id == "a.one":
                raise RuntimeError("registry broken")
            return "job-2"

        report = activate_pending(
            [_act("a.one", "k1"), _act("a.two", "k2")],
            resolve=resolve,
            request_run=runner,
        )

        assert report.errors == ("a.one",)
        assert report.activated == ("job-2",)

    def test_attribution_is_stable(self):
        """Activations must be reconstructable from the audit log."""
        runner = _Recorder()
        activate_pending(
            [_act("a.one")], resolve=lambda a: "job-1", request_run=runner
        )

        assert runner.calls == [("job-1", "cron:jobflow-reconcile", "reconcile")]

    def test_empty_input_is_a_clean_silent_report(self):
        report = activate_pending([], resolve=lambda a: "job-1", request_run=_Recorder())
        assert report == ActivationReport(0, 0, (), (), (), ())
        assert report.needs_agent is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/jobflow_dispatch/test_activate.py -v`
Expected: FAIL — `ImportError: cannot import name 'ActivationReport' from 'jobflow_dispatch.activate'`.

- [ ] **Step 3: Implement the orchestrator**

Append to `jobflow_dispatch/activate.py` (and add `from dataclasses import dataclass` plus `from typing import Callable, Optional, Sequence` to the imports at the top):

```python
#: Attribution for every activation this module performs. Reaches the
#: cron_triggered event and therefore ~/.hermes/events/audit.jsonl, which is
#: the ONLY durable record of a reconcile activation: a wakeAgent:false run has
#: its stdout replaced by the scheduler's silent_doc, and _run_job_script
#: discards stderr entirely on exit 0.
CALLER = "cron:jobflow-reconcile"
REASON = "reconcile"


@dataclass(frozen=True)
class ActivationReport:
    """What one reconcile pass did, in counts the wake gate can read.

    The three failure buckets are kept apart because they mean different
    things to whoever reads the report: ``unresolved`` is a broken
    activity-to-job mapping, ``refused`` is a job that was disabled between the
    scan and the activation, and ``errors`` is a fault in this code or the cron
    store. Collapsing them into one number would hide which.
    """

    activations: int
    activities: int
    activated: tuple[str, ...]
    unresolved: tuple[str, ...]
    refused: tuple[str, ...]
    errors: tuple[str, ...]

    @property
    def needs_agent(self) -> bool:
        """True when something needs a human-legible diagnosis.

        A pass that activated ten workers cleanly needs no agent; a pass that
        could not resolve one activity does.
        """
        return bool(self.unresolved or self.refused or self.errors)


def activate_pending(
    activations: Sequence["Activation"],
    *,
    resolve: Callable[[str], Optional[str]] = resolve_job_id_for_activity,
    request_run: Optional[Callable[..., Optional[dict]]] = None,
    caller: str = CALLER,
    reason: str = REASON,
) -> ActivationReport:
    """Resolve each pending activity to an enabled job and schedule it.

    Fail-closed twice over: ``resolve`` refuses anything that does not map to
    exactly one enabled job, and ``request_run`` independently refuses a job
    that is not enabled at the moment of the write. Neither can revive a
    disabled worker.

    Every activity is isolated — a resolver that raises, or a cron store that
    fails one write, costs that one activity and not the rest.
    """
    if request_run is None:
        from cron.jobs import request_run as _default_request_run

        request_run = _default_request_run

    # Dedupe activities while preserving scan order. There are only four routed
    # activities in ROUTES, so this list is bounded by construction and needs no
    # display cap.
    activity_ids: list[str] = []
    seen: set[str] = set()
    for activation in activations:
        activity_id = activation.activity_id
        if activity_id not in seen:
            seen.add(activity_id)
            activity_ids.append(activity_id)

    activated: list[str] = []
    unresolved: list[str] = []
    refused: list[str] = []
    errors: list[str] = []
    woken: set[str] = set()

    for activity_id in activity_ids:
        try:
            job_id = resolve(activity_id)
        except Exception:
            logger.exception("activate: resolving %s failed", activity_id)
            errors.append(activity_id)
            continue

        if not job_id:
            unresolved.append(activity_id)
            continue

        if job_id in woken:
            continue  # one wake per job per run, however many activities map to it

        try:
            result = request_run(job_id, caller=caller, reason=reason)
        except Exception:
            logger.exception(
                "activate: request_run failed for %s (%s)", job_id, activity_id
            )
            errors.append(activity_id)
            continue

        if result is None:
            # Enabled at scan time, not enabled now — or gone. Fail closed and
            # let the next reconcile catch it.
            logger.warning(
                "activate: %s (%s) refused — not enabled at activation time",
                job_id, activity_id,
            )
            refused.append(activity_id)
            continue

        woken.add(job_id)
        activated.append(job_id)

    return ActivationReport(
        activations=len(activations),
        activities=len(activity_ids),
        activated=tuple(activated),
        unresolved=tuple(unresolved),
        refused=tuple(refused),
        errors=tuple(errors),
    )
```

Add this import beside the others at the top of the file, and change the signature's
annotation from `Sequence["Activation"]` to the unquoted `Sequence[Activation]`. The
import is genuinely used by that annotation, so no `noqa` is warranted regardless of lint
configuration — Pyflakes counts a name used in an annotation as used even under
`from __future__ import annotations`.

```python
from jobflow_dispatch.contracts import Activation
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/jobflow_dispatch/test_activate.py -v`
Expected: PASS, 15 passed.

- [ ] **Step 5: Commit**

```bash
git add jobflow_dispatch/activate.py tests/jobflow_dispatch/test_activate.py && git commit -m "feat(jobflow): activate_pending resolves and wakes without ever enabling"
```

---

### Task 4: `render_report()` — the stdout and wake-gate contract

**Files:**
- Modify: `jobflow_dispatch/activate.py`
- Test: `tests/jobflow_dispatch/test_activate.py`

**Interfaces:**
- Consumes: `ActivationReport` (Task 3).
- Produces: `render_report(report: ActivationReport) -> str` — a newline-joined block whose LAST line is the JSON wake gate. Task 5 prints its return value verbatim.

- [ ] **Step 1: Write the failing tests**

Append to `tests/jobflow_dispatch/test_activate.py`:

```python
import json

from jobflow_dispatch.activate import render_report


def _last_line(text):
    return [ln for ln in text.splitlines() if ln.strip()][-1]


class TestRenderReport:
    def test_clean_pass_gates_the_agent_off(self):
        """Activating workers is not, by itself, a reason to spend a session."""
        report = ActivationReport(3, 2, ("job-1", "job-2"), (), (), ())
        out = render_report(report)

        assert json.loads(_last_line(out)) == {"wakeAgent": False}
        assert "activated=2" in out

    def test_unresolved_gates_the_agent_on(self):
        report = ActivationReport(1, 1, (), ("a.one",), (), ())
        assert json.loads(_last_line(render_report(report))) == {"wakeAgent": True}

    def test_refused_gates_the_agent_on(self):
        report = ActivationReport(1, 1, (), (), ("a.one",), ())
        assert json.loads(_last_line(render_report(report))) == {"wakeAgent": True}

    def test_errors_gate_the_agent_on(self):
        report = ActivationReport(1, 1, (), (), (), ("a.one",))
        assert json.loads(_last_line(render_report(report))) == {"wakeAgent": True}

    def test_the_gate_is_always_the_last_non_empty_line(self):
        """The cron script slot reads exactly this line. If a detail line ever
        lands after it, the gate silently stops working."""
        report = ActivationReport(4, 3, ("job-1",), ("a.two",), ("a.three",), ("a.four",))
        out = render_report(report)

        assert json.loads(_last_line(out)) == {"wakeAgent": True}
        assert out.splitlines()[-1] == _last_line(out)

    def test_failing_activities_are_named_so_the_agent_can_diagnose(self):
        report = ActivationReport(2, 2, (), ("a.one",), ("a.two",), ())
        out = render_report(report)

        assert "a.one" in out
        assert "a.two" in out

    def test_no_message_bodies_or_paths_leak(self):
        """Only activity IDs, job IDs and counts reach stdout."""
        report = ActivationReport(1, 1, ("job-1",), (), (), ())
        out = render_report(report)

        assert "inbox" not in out
        assert ".json" not in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/jobflow_dispatch/test_activate.py::TestRenderReport -v`
Expected: FAIL — `ImportError: cannot import name 'render_report'`.

- [ ] **Step 3: Implement the renderer**

Append to `jobflow_dispatch/activate.py` (add `import json` to the top-level imports):

```python
def render_report(report: ActivationReport) -> str:
    """Format one pass for the cron script slot's stdout.

    The LAST non-empty line MUST be the wake gate — the scheduler parses only
    that line and treats anything unparseable as "wake the agent". Keeping the
    gate here, rather than in the wrapper script, is what lets a unit test hold
    that invariant.

    Only activity IDs, job IDs and counts appear. Message bodies and mailbox
    paths never reach stdout: for an agent-path job this text is injected into
    the prompt verbatim.
    """
    lines = [
        "jobflow-reconcile: "
        f"activations={report.activations} "
        f"activities={report.activities} "
        f"activated={len(report.activated)} "
        f"unresolved={len(report.unresolved)} "
        f"refused={len(report.refused)} "
        f"errors={len(report.errors)}"
    ]
    for job_id in report.activated:
        lines.append(f"- activated: {job_id}")
    for activity_id in report.unresolved:
        lines.append(
            f"- UNRESOLVED: {activity_id} did not map to exactly one enabled job"
        )
    for activity_id in report.refused:
        lines.append(
            f"- REFUSED: {activity_id} resolved to a job that was not enabled "
            "at activation time"
        )
    for activity_id in report.errors:
        lines.append(f"- ERROR: {activity_id} raised during activation")

    # MUST be last.
    lines.append(json.dumps({"wakeAgent": report.needs_agent}))
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/jobflow_dispatch/test_activate.py -v`
Expected: PASS, 22 passed.

- [ ] **Step 5: Run the whole affected suite and the linter**

Run: `python -m pytest tests/jobflow_dispatch/ tests/events/ tests/cron/ -q`
Expected: PASS with no new failures.

Run: `python -m ruff check --no-cache jobflow_dispatch/ cron/jobs.py events/subscribers/jobflow_dispatcher.py tests/jobflow_dispatch/`
Expected: `All checks passed!`

- [ ] **Step 6: Commit**

```bash
git add jobflow_dispatch/activate.py tests/jobflow_dispatch/test_activate.py && git commit -m "feat(jobflow): render_report owns the reconcile stdout and wake-gate contract"
```

---

### Task 5: Thin the reconciler wrapper

> **Live-system task, separate repo.** The file is tracked in the `~/.hermes` git repo, not `agent-src`. It imports `jobflow_dispatch` from `~/.hermes/agent-src` — the SHARED checkout — so Tasks 1–4 must be merged there before this runs correctly. Confirm with Diego before executing.

**Files:**
- Modify: `~/.hermes/profiles/main/scripts/jobflow_reconcile.py`

**Interfaces:**
- Consumes: `jobflow_dispatch.reconcile.scan_actionable`, `jobflow_dispatch.store.ActivationStore`, `jobflow_dispatch.store.default_ledger_path`, `jobflow_dispatch.activate.activate_pending`, `jobflow_dispatch.activate.render_report`.
- Produces: stdout per the wake-gate contract. Nothing imports this file.

- [ ] **Step 1: Verify the shared checkout has the new code**

Run: `python -c "import sys; sys.path.insert(0, r'C:\Users\diego\.hermes\agent-src'); from jobflow_dispatch.activate import activate_pending, render_report; print('ok')"`
Expected: `ok`. If this fails with `ModuleNotFoundError`, Tasks 1–4 have not landed in the shared checkout — STOP and merge first.

- [ ] **Step 2: Rewrite the module docstring and `main()`**

In `~/.hermes/profiles/main/scripts/jobflow_reconcile.py`, replace the docstring's output-contract paragraph and the `MAX_LISTED` constant and `main()` body. The final file's changed parts:

```python
"""Cron wrapper: deterministic recovery for missed JobFlow activations.

Events are the primary activation path; this is the safety net that catches
work stranded by a dropped event, a dispatcher restart, or a message written
while the subscriber was down.

This script ACTIVATES. It resolves each stranded activity to exactly one
ENABLED cron job and schedules that job through the non-enabling
``cron.jobs.request_run``, which refuses a disabled job outright. That guard
used to live in the cron prompt, where a mis-resolving agent running
``hermes cron run`` would have silently re-enabled a worker an operator
disabled. No agent sits between the check and the action any more.

Output contract — the cron ``script:`` slot delivers stdout verbatim and reads
its last non-empty line as the wake gate:

* nothing pending  -> exactly ``{"wakeAgent": false}`` and nothing else, so an
  idle reconcile costs no message, no session, and no model call
* all activated    -> a summary line, then ``{"wakeAgent": false}`` — a clean
  pass is silent even when it woke workers
* anything unresolved, refused, or errored -> the summary, the named
  activities, then ``{"wakeAgent": true}`` so an agent can diagnose the broken
  activity-to-job mapping
* scan failure     -> ``{"wakeAgent": true, "errors": N}``

That last case matters: a reconciler that crashed must never be indistinguish-
able from one that found nothing. Silence is reserved for "verified idle".

Message bodies never reach stdout. Only activity IDs, job IDs and counts do.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path.home() / ".hermes" / "agent-src"))

from jobflow_dispatch.activate import activate_pending, render_report  # noqa: E402
from jobflow_dispatch.reconcile import scan_actionable  # noqa: E402
from jobflow_dispatch.store import ActivationStore, default_ledger_path  # noqa: E402

HERMES = Path.home() / ".hermes"
MAILBOX_ROOT = HERMES / "mailbox"
#: Must be the SAME ledger the dispatcher subscriber claims into; if these
#: diverge the reconciler re-dispatches work the subscriber already took.
LEDGER_PATH = default_ledger_path()


def main() -> int:
    try:
        store = ActivationStore(LEDGER_PATH)
        pending = scan_actionable(MAILBOX_ROOT, store, now=time.time())
    except Exception as exc:
        # Sanitized: a path or packet fragment in the message would be
        # delivered verbatim.
        print(f"[jobflow-reconcile] scan failed: {type(exc).__name__}", file=sys.stderr)
        print(json.dumps({"wakeAgent": True, "errors": 1}))
        return 0

    if not pending:
        print("[jobflow-reconcile] nothing pending", file=sys.stderr)
        print(json.dumps({"wakeAgent": False}))
        return 0

    try:
        report = activate_pending(pending)
    except Exception as exc:
        # activate_pending isolates per-activity faults itself, so reaching
        # here means the whole pass failed — report it rather than going quiet.
        print(
            f"[jobflow-reconcile] activation failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        print(json.dumps({"wakeAgent": True, "errors": 1}))
        return 0

    print(render_report(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note the `MAX_LISTED` constant is deleted: `render_report` lists deduped ACTIVITIES, and `ROUTES` defines only four, so the output is bounded by construction rather than by a cap.

- [ ] **Step 3: Dry-run against the live mailbox**

Run: `python C:\Users\diego\.hermes\profiles\main\scripts\jobflow_reconcile.py`

Expected, if nothing is pending: exactly one stdout line, `{"wakeAgent": false}`, with `[jobflow-reconcile] nothing pending` on stderr.

Expected, if work IS pending: a `jobflow-reconcile: activations=N ...` line, zero or more `- activated: <job_id>` lines, then the gate. **This run really does schedule jobs** — check `hermes cron list` afterwards and confirm every job named in the output was already enabled.

- [ ] **Step 4: Measure the added import cost once**

The wrapper now imports `cron.jobs`, which parses a ~130 KB `jobs.json`. The spec accepts
this (four runs a day) but says to measure rather than assume.

Run:
```bash
python -c "import sys,time; sys.path.insert(0, r'C:\Users\diego\.hermes\agent-src'); t=time.perf_counter(); import cron.jobs; print(f'{time.perf_counter()-t:.2f}s')"
```
Expected: under ~2s. If it exceeds the job's script timeout budget, report the number
rather than working around it — that would be a finding about `cron.jobs` import cost, not
about this change.

- [ ] **Step 5: Verify the audit trail landed**

Run: `python -c "import sys; sys.path.insert(0, r'C:\Users\diego\.hermes\agent-src'); from events.bus import EventBus; from events.schema import EventType; print([e.payload for e in EventBus().query(event_type=EventType.CRON_TRIGGERED, limit=5)])"`
Expected: if step 3 activated anything, a `cron_triggered` payload with `caller="cron:jobflow-reconcile"` and `reason="reconcile"`. If step 3 activated nothing, an empty or unrelated list — not a failure.

- [ ] **Step 6: Commit (in the `~/.hermes` repo)**

```bash
cd ~/.hermes && git add profiles/main/scripts/jobflow_reconcile.py && git commit -m "feat(jobflow): reconciler activates in code instead of via the agent"
```

---

### Task 6: Demote the agent to diagnostician

> **Live cron change.** Uses `hermes cron edit` — never a hand-edit of `jobs.json`. Confirm with Diego before executing. Task 5 must be live first, or the prompt will describe behavior the script does not yet have.

**Files:**
- Modify: cron job `64711e6d8334` (`jobflow-reconcile`), `prompt` field only

**Interfaces:**
- Consumes: the stdout format produced by `render_report` (Task 4).
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Capture the current prompt for rollback**

Run:
```bash
python -c "import json,pathlib; jobs=json.loads((pathlib.Path.home()/'.hermes/profiles/main/cron/jobs.json').read_text(encoding='utf-8')); print(next(j['prompt'] for j in jobs if j['id']=='64711e6d8334'))" > "$HOME/AppData/Local/Temp/claude/jobflow-reconcile-prompt.bak.txt"
```
Expected: the file contains the current prompt beginning `Local-only JobFlow reconcile follow-up on Hermes.`

- [ ] **Step 2: Write the new prompt to a scratch file**

Shell quoting mangles multi-line prompts, so pass it via a file. Write this to `C:\Users\diego\AppData\Local\Temp\claude\jobflow-reconcile-prompt.txt`:

```
Local-only JobFlow reconcile follow-up on Hermes. This is maintenance, never user-facing.

The jobflow_reconcile.py pre-run script has just executed (see Script Output above). It scanned the mailbox for actionable messages that no worker holds, and it ALREADY ACTIVATED every activity it could resolve. Activation is done in code. You are here only because something did not resolve cleanly.

You have NO activation duties. Specifically:
- Do NOT run `hermes cron run` for any job, for any reason.
- Do NOT trigger, enable, resume, or otherwise start any cron job.
Triggering re-enables a disabled job, which is the exact failure this design removed. If you believe a job should be woken, say so in your report and stop.

For each line in the Script Output beginning UNRESOLVED, REFUSED, or ERROR, diagnose it and report:
- UNRESOLVED <activity>: the activity did not map to exactly one ENABLED cron job. Look up its aliases in activity_policy/policies.yaml and compare against `hermes cron list --all`. Say whether zero jobs matched or more than one, name the candidates, and say whether any match is disabled deliberately (check paused_reason) or by accident.
- REFUSED <activity>: it resolved, but the job was not enabled when activation ran. Name the job and its paused_reason.
- ERROR <activity>: activation raised. Report what the Script Output says and nothing more.

Do NOT read, edit, move, or delete any mailbox message - the worker owns those.
Do NOT modify cron/jobs.json.
Do NOT change the HERMES_JOBFLOW_EVENT_DISPATCH setting.

Report one line per failing activity, prefixed with its activity ID, plus a final summary line:
  jobflow-reconcile: unresolved=<N> refused=<N> errors=<N>

If the Script Output's last non-empty line is {"wakeAgent": false}, there is nothing to diagnose - report nothing and end.
```

- [ ] **Step 3: Apply it**

```bash
hermes cron edit 64711e6d8334 --prompt "$(cat "$HOME/AppData/Local/Temp/claude/jobflow-reconcile-prompt.txt")"
```
Expected: `Updated` (or the CLI's success line) with no error.

- [ ] **Step 4: Verify ONLY the prompt changed**

Run:
```bash
python -c "import json,pathlib; j=next(x for x in json.loads((pathlib.Path.home()/'.hermes/profiles/main/cron/jobs.json').read_text(encoding='utf-8')) if x['id']=='64711e6d8334'); print(j['schedule']['expr'], '|', j['enabled'], '|', j['no_agent'], '|', j['script'], '|', j['deliver'], '|', j['next_run_at'])"
```
Expected exactly: `30 0,6,12,18 * * * | True | False | jobflow_reconcile.py | local | 2026-08-11T00:30:00-04:00`

**If the schedule expression is anything other than `30 0,6,12,18 * * *`, STOP and restore it** — that offset is load-bearing.

- [ ] **Step 5: Confirm the job list is otherwise intact**

Run: `hermes cron list --all`
Expected: `jobflow-reconcile` shown `[active]`, and the same total job count as before the edit (68 as of 2026-08-10).

- [ ] **Step 6: Record the change**

No commit — `jobs.json` is gitignored runtime state. Note the applied prompt and the rollback file path in the session's MemPalace drawer instead.

---

## Verification

After all six tasks:

- [ ] `python -m pytest tests/cron/ tests/jobflow_dispatch/ tests/events/ -q` — no new failures against baseline
- [ ] `python -m ruff check --no-cache jobflow_dispatch/ cron/ events/subscribers/ tests/jobflow_dispatch/` — clean
- [ ] `hermes cron list --all` shows `jobflow-reconcile` active on `30 0,6,12,18 * * *`
- [ ] The first live run at or after `2026-08-11T00:30` completes with `last_status=ok` (`hermes cron runs 64711e6d8334`)
- [ ] Grep the gateway log for `request_run refused` — any hit is a genuine disabled-job save and worth reading
- [ ] Check `~/.hermes/events/audit.jsonl` for the same `job_id` carrying `caller="cron:jobflow-reconcile"` across consecutive reconcile windows — that recurrence is the signature of the invisible wake loop documented in the spec's Accepted risks (a permanently stuck mailbox message that resolves and activates cleanly every pass, so the gate never opens)
