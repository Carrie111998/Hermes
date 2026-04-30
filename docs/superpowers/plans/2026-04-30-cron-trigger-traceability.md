# Cron Trigger Traceability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add caller-traceability to `cron.jobs.trigger_job()` so every off-schedule cron fire (CLI `hermes cron run`, LLM tool, HTTP API) emits a typed `cron_triggered` event with caller + reason, persisted to a 30-day rolling per-job log.

**Architecture:** New `EventType.CRON_TRIGGERED` (Priority.LOW). `trigger_job(job_id, caller=None, reason=None)` calls a new `events.producers.cron_trigger_emitter.emit_cron_triggered()` helper after writing `next_run_at = NOW`, capturing `(prev_next_run_at, new_next_run_at, caller, reason)`. Anonymous calls (`caller is None`) log a warning. A new `CronTriggerLog` subscriber appends each event to `events/cron_triggers.jsonl` with weekly rotation + 30-day retention so future investigations can grep `job_id` directly without scanning the full audit log. CLI (`hermes cron run --reason …`), LLM tool (`cronjob action="run"` already accepts `reason`), and HTTP API (`POST /api/cron/jobs/{id}/trigger?reason=…`) all thread fixed caller strings.

**Tech Stack:** Python 3.11+, sqlite3 (event bus), pytest, argparse (CLI), FastAPI (HTTP), existing `events/` infrastructure.

---

## Background — postmortem reference

`profiles/sentinel/workspace/silence-investigation-2026-04-30.md` Fix 4 section. The 2026-04-30 `sentinel-vip-morning` triple-fire (14:02 / 14:34 / 14:49 UTC) had no observable cause because `trigger_job()` writes `next_run_at = NOW` without leaving any trace. By the time investigation begins, the only evidence (the prior `next_run_at`) has been overwritten.

## Architectural decisions (locked)

1. **`trigger_job` is the single emission point** — not each caller. Callers thread a `caller` string in; `trigger_job` reads `prev_next_run_at` BEFORE the update, calls `update_job`, then emits one `cron_triggered` event with both old and new timestamps.
2. **Emission goes through a thin producer helper** (`events/producers/cron_trigger_emitter.py`) wrapped in try/except so a bus failure never breaks `trigger_job`. Matches the `events/producers/cron_emitter.py` pattern.
3. **`caller=None` is allowed but warns** at WARN level via `logger.warning(...)`. Tests assert the warning fires. This forces every future caller to be explicit without breaking the existing `tools.cronjob_tools.cronjob` API.
4. **Rolling log = JSONL not JSON-dict** — same shape as `audit.jsonl` for consistency. Per-job query is a `grep` away. Stored at `events/cron_triggers.jsonl` (cross-profile, canonical root).
5. **`cron_triggered` is `Priority.LOW`** — informational, batched in the audit layer, NOT routed to Telegram or WhatsApp by default.

## File Structure

| Path | Action | Responsibility |
|------|--------|----------------|
| `events/schema.py` | Modify | Add `EventType.CRON_TRIGGERED` |
| `events/paths.py` | Modify | Add `cron_trigger_log_path()` |
| `events/producers/cron_trigger_emitter.py` | **Create** | Thin helper that builds the payload + calls `bus.emit()` defensively |
| `events/subscribers/cron_trigger_log.py` | **Create** | New subscriber appending JSONL with weekly rotation + 30-day retention |
| `events/gateway_integration.py` | Modify | Register `CronTriggerLog` subscriber in `startup()` |
| `cron/jobs.py` | Modify | `trigger_job(job_id, caller=None, reason=None)` signature; emit after `update_job` |
| `tools/cronjob_tools.py` | Modify | `cronjob` action `run` reads `caller` kwarg (defaults `"llm:cronjob_tool"`); already has `reason` |
| `hermes_cli/main.py` | Modify | `cron run --reason` flag at `cron_run.add_argument(...)` |
| `hermes_cli/cron.py` | Modify | `cron_command "run"` reads `args.reason`; `_job_action` threads it through `_cron_api`; pass `caller="hermes_cli:cron_run"` |
| `hermes_cli/web_server.py` | Modify | `POST /api/cron/jobs/{id}/trigger?reason=…` reads query string; passes `caller="http_api:web_server"` |
| `tests/cron/test_jobs.py` | Modify | New `TestTriggerJob` class — emission, caller/reason, anonymous warning, prev/new timestamps |
| `tests/events/producers/test_cron_trigger_emitter.py` | **Create** | Helper unit tests |
| `tests/events/subscribers/test_cron_trigger_log.py` | **Create** | Subscriber unit tests (JSONL shape, rotation, retention) |
| `tests/hermes_cli/test_cron.py` | Modify | Extend `test_pause_resume_run` to verify CLI passes caller + reason |
| `tests/tools/test_cronjob_tools.py` | Modify | New test asserting LLM tool defaults `caller="llm:cronjob_tool"` |
| `tests/integration/test_cron_trigger_traceability.py` | **Create** | End-to-end: CLI run → bus event → JSONL line |

---

## Task 1: Add `EventType.CRON_TRIGGERED` + path helper

**Files:**
- Modify: `events/schema.py:43-47` (insert in cron lifecycle block)
- Modify: `events/paths.py:97` (append new helper)
- Test: `tests/events/test_schema.py` (existing) — no test edit needed; `test_event_type_from_string_roundtrip` will cover the new member if it exists; otherwise we add one in Task 2

- [ ] **Step 1: Write the failing test** — confirm `EventType.from_string("cron_triggered")` returns the enum member. Add to `tests/events/test_schema.py` (create file if missing):

```python
from events.schema import EventType, Priority


def test_cron_triggered_event_type_exists():
    assert EventType.from_string("cron_triggered") is EventType.CRON_TRIGGERED


def test_cron_triggered_default_priority_is_low():
    assert EventType.CRON_TRIGGERED.default_priority is Priority.LOW
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/events/test_schema.py::test_cron_triggered_event_type_exists -xvs
```

Expected: `AttributeError: type object 'EventType' has no attribute 'CRON_TRIGGERED'` (or similar enum lookup miss).

- [ ] **Step 3: Add the EventType member**

In `events/schema.py`, locate the cron-lifecycle block (lines 42-47):

```python
    # Cron lifecycle
    CRON_STARTED = ("cron_started", Priority.LOW)
    CRON_COMPLETED = ("cron_completed", Priority.NORMAL)
    CRON_FAILED = ("cron_failed", Priority.HIGH)
    CRON_FAILED_CONSECUTIVE = ("cron_failed_consecutive", Priority.CRITICAL)
    CRON_STALE = ("cron_stale", Priority.HIGH)
```

Insert one line **after `CRON_STARTED`** (chronologically — triggered fires before started):

```python
    # Cron lifecycle
    CRON_STARTED = ("cron_started", Priority.LOW)
    # Off-schedule trigger record — emitted by trigger_job() in cron/jobs.py
    # whenever a caller sets next_run_at = NOW (CLI `hermes cron run`, LLM
    # cronjob tool action="run", HTTP API trigger endpoint). Carries caller
    # + reason + previous/new next_run_at so off-schedule fires can be
    # attributed in postmortems. LOW priority => audit-logger captures it
    # but Telegram/WhatsApp routing leaves it out by default.
    CRON_TRIGGERED = ("cron_triggered", Priority.LOW)
    CRON_COMPLETED = ("cron_completed", Priority.NORMAL)
    CRON_FAILED = ("cron_failed", Priority.HIGH)
    CRON_FAILED_CONSECUTIVE = ("cron_failed_consecutive", Priority.CRITICAL)
    CRON_STALE = ("cron_stale", Priority.HIGH)
```

- [ ] **Step 4: Add the path helper**

Append to `events/paths.py`:

```python
def cron_trigger_log_path() -> Path:
    """Per-job rolling log of off-schedule cron fires (cron_triggered events).

    Maintained by the CronTriggerLog subscriber. JSONL format, weekly
    rotation into events/audit/, 30-day retention. Operators grep this
    by job_id during postmortems instead of scanning audit.jsonl in full.
    """
    return events_dir() / "cron_triggers.jsonl"
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/events/test_schema.py -xvs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/.hermes/agent-src && git add events/schema.py events/paths.py tests/events/test_schema.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(events): add CRON_TRIGGERED event type + cron_trigger_log_path helper"
```

---

## Task 2: Build `CronTriggerEmitter` helper

**Files:**
- Create: `events/producers/cron_trigger_emitter.py`
- Create: `tests/events/producers/test_cron_trigger_emitter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/events/producers/test_cron_trigger_emitter.py`:

```python
"""Tests for events.producers.cron_trigger_emitter."""

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.cron_trigger_emitter import emit_cron_triggered


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def test_emit_basic(bus):
    event_id = emit_cron_triggered(
        bus,
        job_id="abc123",
        job_name="sentinel-vip-morning",
        caller="hermes_cli:cron_run",
        reason="investigation 2026-04-30",
        previous_next_run_at="2026-05-01T09:00:00+00:00",
        new_next_run_at="2026-04-30T14:34:00+00:00",
    )
    assert event_id  # non-empty string

    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1
    e = events[0]
    assert e.source == "sentinel-vip-morning"
    assert e.priority is Priority.LOW
    assert e.job_id == "abc123"
    assert e.payload["caller"] == "hermes_cli:cron_run"
    assert e.payload["reason"] == "investigation 2026-04-30"
    assert e.payload["job_name"] == "sentinel-vip-morning"
    assert e.payload["previous_next_run_at"] == "2026-05-01T09:00:00+00:00"
    assert e.payload["new_next_run_at"] == "2026-04-30T14:34:00+00:00"


def test_emit_anonymous_caller_omits_field(bus):
    emit_cron_triggered(
        bus,
        job_id="abc123",
        job_name="job",
        caller=None,
        reason=None,
        previous_next_run_at=None,
        new_next_run_at="2026-04-30T14:34:00+00:00",
    )
    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1
    assert events[0].payload["caller"] is None
    assert events[0].payload["reason"] is None
    assert events[0].payload["previous_next_run_at"] is None


def test_emit_swallows_bus_failure(bus, monkeypatch, caplog):
    """A broken bus must NOT propagate — trigger_job must keep working."""
    def boom(*args, **kwargs):
        raise RuntimeError("bus is dead")

    monkeypatch.setattr(bus, "emit", boom)

    # Must not raise
    result = emit_cron_triggered(
        bus,
        job_id="abc123",
        job_name="job",
        caller="hermes_cli:cron_run",
        reason=None,
        previous_next_run_at=None,
        new_next_run_at="2026-04-30T14:34:00+00:00",
    )
    assert result is None
    assert "cron_trigger_emitter" in caplog.text or "emit failed" in caplog.text.lower()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/events/producers/test_cron_trigger_emitter.py -xvs
```

Expected: `ModuleNotFoundError: No module named 'events.producers.cron_trigger_emitter'`.

- [ ] **Step 3: Write the helper**

Create `events/producers/cron_trigger_emitter.py`:

```python
"""Cron trigger emitter — writes one cron_triggered event per off-schedule fire.

Called by cron.jobs.trigger_job() after the job's next_run_at has been
written to NOW. Defensive: any bus failure is logged and swallowed so
that an unhealthy event bus never breaks the trigger path itself.
"""

import logging
from typing import Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)


def emit_cron_triggered(
    bus: EventBus,
    *,
    job_id: str,
    job_name: str,
    caller: Optional[str],
    reason: Optional[str],
    previous_next_run_at: Optional[str],
    new_next_run_at: str,
) -> Optional[str]:
    """Emit one CRON_TRIGGERED event capturing the caller + state transition.

    Returns the event_id on success, None on failure (logged but swallowed).
    """
    try:
        return bus.emit(
            event_type=EventType.CRON_TRIGGERED,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "caller": caller,
                "reason": reason,
                "previous_next_run_at": previous_next_run_at,
                "new_next_run_at": new_next_run_at,
            },
            job_id=job_id,
        )
    except Exception:
        logger.exception(
            "cron_trigger_emitter: emit failed for job_id=%s caller=%s",
            job_id, caller,
        )
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/events/producers/test_cron_trigger_emitter.py -xvs
```

Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/agent-src && git add events/producers/cron_trigger_emitter.py tests/events/producers/test_cron_trigger_emitter.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(events): add cron_trigger_emitter helper for trigger_job audit"
```

---

## Task 3: Modify `trigger_job()` to accept caller + reason and emit

**Files:**
- Modify: `cron/jobs.py:571-585` (`trigger_job` definition)
- Modify: `tests/cron/test_jobs.py` (append `TestTriggerJob` class at end of file)

- [ ] **Step 1: Write the failing test**

Append to `tests/cron/test_jobs.py`:

```python
# =========================================================================
# trigger_job — caller traceability
# =========================================================================

class TestTriggerJob:
    def test_basic_signature_with_caller_and_reason(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import create_job, trigger_job
        from events.bus import EventBus
        from events.schema import EventType

        # Redirect the emitter's bus to a temp DB
        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        result = trigger_job(
            job["id"],
            caller="hermes_cli:cron_run",
            reason="investigation 2026-04-30",
        )

        assert result is not None
        assert result["state"] == "scheduled"

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        e = events[0]
        assert e.payload["caller"] == "hermes_cli:cron_run"
        assert e.payload["reason"] == "investigation 2026-04-30"
        assert e.payload["job_id"] == job["id"]
        assert e.payload["job_name"] == job["name"]
        assert e.payload["previous_next_run_at"] == job["next_run_at"]
        assert e.payload["new_next_run_at"] == result["next_run_at"]

    def test_anonymous_caller_logs_warning(self, tmp_cron_dir, monkeypatch, caplog):
        import logging
        from cron.jobs import create_job, trigger_job
        from events.bus import EventBus

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        with caplog.at_level(logging.WARNING, logger="cron.jobs"):
            trigger_job(job["id"])  # no caller

        assert any(
            "anonymous" in rec.message.lower() or "caller=None" in rec.message
            for rec in caplog.records
        ), f"Expected anonymous-caller warning; got: {[r.message for r in caplog.records]}"

    def test_returns_none_for_unknown_job(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import trigger_job
        from events.bus import EventBus

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        assert trigger_job("nonexistent", caller="test") is None

        from events.schema import EventType
        assert bus.query(event_type=EventType.CRON_TRIGGERED) == []

    def test_emit_failure_does_not_break_trigger(self, tmp_cron_dir, monkeypatch):
        """Bus failure must not propagate — trigger_job must still update state."""
        from cron.jobs import create_job, trigger_job, get_job

        def broken_bus():
            raise RuntimeError("bus broken")

        monkeypatch.setattr("cron.jobs._get_event_bus", broken_bus)

        job = create_job(prompt="x", schedule="every 1h")
        result = trigger_job(job["id"], caller="test")

        # The state mutation must still have happened
        assert result is not None
        assert result["state"] == "scheduled"
        assert get_job(job["id"])["state"] == "scheduled"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/cron/test_jobs.py::TestTriggerJob -xvs
```

Expected: FAIL — `trigger_job() got an unexpected keyword argument 'caller'`.

- [ ] **Step 3: Modify `trigger_job` and add `_get_event_bus`**

Replace `cron/jobs.py:571-585` with:

```python
def _get_event_bus():
    """Lazy-construct an EventBus instance for emit-side use.

    Kept as a module function so tests can monkeypatch the entire bus,
    and so an import failure (e.g. during early bootstrap) doesn't crash
    cron.jobs at module load.
    """
    from events.bus import EventBus
    return EventBus()


def trigger_job(
    job_id: str,
    caller: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Schedule a job to run on the next scheduler tick.

    Sets ``next_run_at = NOW`` and emits a ``cron_triggered`` event capturing
    the caller (e.g. ``"hermes_cli:cron_run"``, ``"llm:cronjob_tool"``,
    ``"http_api:web_server"``) and an optional reason string. ``caller=None``
    is allowed for backward compatibility but logs a WARNING — every internal
    caller should pass an explicit caller string.
    """
    job = get_job(job_id)
    if not job:
        return None

    if caller is None:
        logger.warning(
            "trigger_job called anonymously (caller=None) for job_id=%s "
            "name=%s — postmortem attribution will be impossible. "
            "Pass an explicit caller string.",
            job_id, job.get("name"),
        )

    previous_next_run_at = job.get("next_run_at")

    updated = update_job(
        job_id,
        {
            "enabled": True,
            "state": "scheduled",
            "paused_at": None,
            "paused_reason": None,
            "next_run_at": _hermes_now().isoformat(),
        },
    )

    if updated is not None:
        try:
            from events.producers.cron_trigger_emitter import emit_cron_triggered
            bus = _get_event_bus()
            emit_cron_triggered(
                bus,
                job_id=job_id,
                job_name=updated.get("name") or job.get("name") or job_id,
                caller=caller,
                reason=reason,
                previous_next_run_at=previous_next_run_at,
                new_next_run_at=updated["next_run_at"],
            )
        except Exception:
            # Defensive: any bus/import failure must not break trigger_job.
            # The state mutation has already been persisted; the audit gap
            # is a known degradation, not a correctness regression.
            logger.exception(
                "trigger_job: cron_triggered emit failed for job_id=%s",
                job_id,
            )

    return updated
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/cron/test_jobs.py -xvs
```

Expected: All `TestTriggerJob` PASS, no regressions in other test classes.

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/agent-src && git add cron/jobs.py tests/cron/test_jobs.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(cron): trigger_job emits cron_triggered event with caller + reason"
```

---

## Task 4: Update `tools.cronjob_tools.cronjob` to thread caller for `run` action

**Files:**
- Modify: `tools/cronjob_tools.py:223-241` (signature) and `:335-337` (run branch)
- Modify: `tests/tools/test_cronjob_tools.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/tools/test_cronjob_tools.py`:

```python
class TestCronjobRunCallerTraceability:
    def test_run_action_defaults_caller_to_llm(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import create_job
        from tools.cronjob_tools import cronjob
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        result_json = cronjob(action="run", job_id=job["id"])
        import json
        result = json.loads(result_json)
        assert result["success"] is True

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        assert events[0].payload["caller"] == "llm:cronjob_tool"

    def test_run_action_accepts_explicit_caller(self, tmp_cron_dir, monkeypatch):
        from cron.jobs import create_job
        from tools.cronjob_tools import cronjob
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="x", schedule="every 1h")
        cronjob(action="run", job_id=job["id"],
                caller="hermes_cli:cron_run", reason="manual investigation")

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert events[0].payload["caller"] == "hermes_cli:cron_run"
        assert events[0].payload["reason"] == "manual investigation"
```

You will also need this fixture at the top of `tests/tools/test_cronjob_tools.py` if it isn't already present (check the existing file first — if `tmp_cron_dir` already exists, do NOT redefine it):

```python
@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/tools/test_cronjob_tools.py::TestCronjobRunCallerTraceability -xvs
```

Expected: FAIL — `cronjob() got an unexpected keyword argument 'caller'`.

- [ ] **Step 3: Modify `cronjob` signature and run branch**

In `tools/cronjob_tools.py`, change the `cronjob` signature (around line 223). Add `caller: Optional[str] = None` after `reason`:

```python
def cronjob(
    action: str,
    job_id: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule: Optional[str] = None,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    include_disabled: bool = False,
    skill: Optional[str] = None,
    skills: Optional[List[str]] = None,
    model: Optional[str] = None,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    reason: Optional[str] = None,
    caller: Optional[str] = None,
    script: Optional[str] = None,
    enabled_toolsets: Optional[List[str]] = None,
    task_id: str = None,
) -> str:
```

Then update the `run` branch at line 335:

```python
        if normalized in {"run", "run_now", "trigger"}:
            effective_caller = caller or "llm:cronjob_tool"
            updated = trigger_job(job_id, caller=effective_caller, reason=reason)
            return json.dumps({"success": True, "job": _format_job(updated)}, indent=2)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/tools/test_cronjob_tools.py -xvs
```

Expected: All PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
cd ~/.hermes/agent-src && git add tools/cronjob_tools.py tests/tools/test_cronjob_tools.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(tools): cronjob action=run threads caller + reason to trigger_job"
```

---

## Task 5: CLI — `hermes cron run --reason` flag + `caller="hermes_cli:cron_run"`

**Files:**
- Modify: `hermes_cli/main.py:7357-7361` (add `--reason` to `cron_run` parser)
- Modify: `hermes_cli/cron.py:35-38` (`_cron_api`), `:239-250` (`_job_action`), `:282-283` (`run` dispatch)
- Modify: `tests/hermes_cli/test_cron.py` (extend `test_pause_resume_run`)

- [ ] **Step 1: Write the failing test**

Replace `test_pause_resume_run` in `tests/hermes_cli/test_cron.py` with the extended version (keep the file's existing imports + `tmp_cron_dir` fixture, just edit the one method body):

```python
    def test_pause_resume_run(self, tmp_cron_dir, capsys, monkeypatch):
        from events.bus import EventBus
        from events.schema import EventType

        bus = EventBus(db_path=tmp_cron_dir / "events.db")
        monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        paused = get_job(job["id"])
        assert paused["state"] == "paused"

        cron_command(Namespace(cron_command="resume", job_id=job["id"]))
        resumed = get_job(job["id"])
        assert resumed["state"] == "scheduled"

        cron_command(
            Namespace(
                cron_command="run",
                job_id=job["id"],
                reason="investigation 2026-04-30",
            )
        )
        triggered = get_job(job["id"])
        assert triggered["state"] == "scheduled"

        events = bus.query(event_type=EventType.CRON_TRIGGERED)
        assert len(events) == 1
        assert events[0].payload["caller"] == "hermes_cli:cron_run"
        assert events[0].payload["reason"] == "investigation 2026-04-30"

        out = capsys.readouterr().out
        assert "Paused job" in out
        assert "Resumed job" in out
        assert "Triggered job" in out
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/hermes_cli/test_cron.py::TestCronCommandLifecycle::test_pause_resume_run -xvs
```

Expected: FAIL — either `Namespace` missing `reason` attr or no `cron_triggered` event emitted.

- [ ] **Step 3a: Add `--reason` flag to `cron run` parser**

In `hermes_cli/main.py:7357-7361`, replace:

```python
    cron_run = cron_subparsers.add_parser(
        "run", help="Run a job on the next scheduler tick"
    )
    cron_run.add_argument("job_id", help="Job ID to trigger")
    _add_accept_hooks_flag(cron_run)
```

With:

```python
    cron_run = cron_subparsers.add_parser(
        "run", help="Run a job on the next scheduler tick"
    )
    cron_run.add_argument("job_id", help="Job ID to trigger")
    cron_run.add_argument(
        "--reason",
        default=None,
        help="Free-form reason string captured in the cron_triggered audit event "
             "(e.g. \"investigation 2026-04-30\"). Helps attribute off-schedule "
             "fires in postmortems.",
    )
    _add_accept_hooks_flag(cron_run)
```

- [ ] **Step 3b: Thread reason + caller through `cron.py`**

In `hermes_cli/cron.py`, modify `_cron_api` (line 35-38) — no signature change needed since it's `**kwargs`-based. Then modify `_job_action` (line 239) to accept reason and pass `caller`:

```python
def _job_action(action: str, job_id: str, success_verb: str, *,
                reason: Optional[str] = None,
                caller: Optional[str] = None) -> int:
    kwargs = {"action": action, "job_id": job_id}
    if reason is not None:
        kwargs["reason"] = reason
    if caller is not None:
        kwargs["caller"] = caller
    result = _cron_api(**kwargs)
    if not result.get("success"):
        print(color(f"Failed to {action} job: {result.get('error', 'unknown error')}", Colors.RED))
        return 1
    job = result.get("job") or result.get("removed_job") or {}
    print(color(f"{success_verb} job: {job.get('name', job_id)} ({job_id})", Colors.GREEN))
    if action in {"resume", "run"} and result.get("job", {}).get("next_run_at"):
        print(f"  Next run: {result['job']['next_run_at']}")
    if action == "run":
        print("  It will run on the next scheduler tick.")
    return 0
```

Add `from typing import Optional` to imports at the top of `hermes_cli/cron.py` if not present.

Then update the `run` dispatch in `cron_command` (line 282-283):

```python
    if subcmd == "run":
        return _job_action(
            "run",
            args.job_id,
            "Triggered",
            reason=getattr(args, "reason", None),
            caller="hermes_cli:cron_run",
        )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/hermes_cli/test_cron.py tests/tools/test_cronjob_tools.py tests/cron/test_jobs.py -xvs
```

Expected: All PASS.

- [ ] **Step 5: Sanity-check the CLI parses the new flag**

```bash
cd ~/.hermes/agent-src && python -m hermes_cli cron run --help 2>&1 | grep reason
```

Expected: `--reason REASON` line appears.

- [ ] **Step 6: Commit**

```bash
cd ~/.hermes/agent-src && git add hermes_cli/main.py hermes_cli/cron.py tests/hermes_cli/test_cron.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(hermes-cli): cron run --reason flag + caller=hermes_cli:cron_run"
```

---

## Task 6: HTTP API — pass `caller="http_api:web_server"` + optional `?reason=…`

**Files:**
- Modify: `hermes_cli/web_server.py:2079-2085` (trigger endpoint)
- Test: skip dedicated HTTP-layer test (no existing test pattern for this endpoint); rely on Task 8 integration test for end-to-end coverage

- [ ] **Step 1: Modify the endpoint**

Replace `hermes_cli/web_server.py:2079-2085`:

```python
@app.post("/api/cron/jobs/{job_id}/trigger")
async def trigger_cron_job(job_id: str, reason: Optional[str] = None):
    from cron.jobs import trigger_job
    job = trigger_job(job_id, caller="http_api:web_server", reason=reason)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
```

If `Optional` isn't already imported in `web_server.py` (check imports at top), add `from typing import Optional` to the existing typing import block.

- [ ] **Step 2: Smoke-check the import doesn't break**

```bash
cd ~/.hermes/agent-src && python -c "from hermes_cli.web_server import app; print('OK')"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
cd ~/.hermes/agent-src && git add hermes_cli/web_server.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(web-server): /api/cron/jobs/{id}/trigger threads caller=http_api + ?reason="
```

---

## Task 7: `CronTriggerLog` subscriber — per-job rolling JSONL with rotation

**Files:**
- Create: `events/subscribers/cron_trigger_log.py`
- Create: `tests/events/subscribers/test_cron_trigger_log.py`
- Modify: `events/gateway_integration.py` (register subscriber in `startup()`)

- [ ] **Step 1: Write the failing test**

Create `tests/events/subscribers/test_cron_trigger_log.py`:

```python
"""Tests for events.subscribers.cron_trigger_log."""

import json
import time
from datetime import datetime, timedelta

import pytest

from events.bus import EventBus
from events.schema import EventType
from events.subscribers.cron_trigger_log import CronTriggerLog


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


@pytest.fixture
def log_path(tmp_path):
    return tmp_path / "events" / "cron_triggers.jsonl"


def test_writes_jsonl_line_per_event(bus, log_path):
    sub = CronTriggerLog(bus, log_path=log_path)

    bus.emit(
        event_type=EventType.CRON_TRIGGERED,
        source="sentinel-vip-morning",
        payload={
            "job_id": "abc123",
            "job_name": "sentinel-vip-morning",
            "caller": "hermes_cli:cron_run",
            "reason": "investigation",
            "previous_next_run_at": "2026-05-01T09:00:00+00:00",
            "new_next_run_at": "2026-04-30T14:34:00+00:00",
        },
        job_id="abc123",
    )
    sub.poll()

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event_type"] == "cron_triggered"
    assert rec["payload"]["caller"] == "hermes_cli:cron_run"
    assert rec["job_id"] == "abc123"


def test_ignores_other_event_types(bus, log_path):
    sub = CronTriggerLog(bus, log_path=log_path)

    bus.emit(
        event_type=EventType.CRON_STARTED,
        source="x",
        payload={"job_id": "x", "job_name": "x", "schedule": "0 0 * * *"},
        job_id="x",
    )
    sub.poll()

    # File may or may not exist but must contain zero CRON_TRIGGERED records
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["event_type"] != "cron_started"


def test_rotation_creates_dated_archive_after_age_threshold(bus, log_path, monkeypatch):
    """When the active file is older than ROTATION_INTERVAL, rotate it into an archive dir."""
    sub = CronTriggerLog(bus, log_path=log_path)

    # Write one event so the file exists with content
    bus.emit(
        event_type=EventType.CRON_TRIGGERED,
        source="x",
        payload={"job_id": "x", "job_name": "x", "caller": "test",
                 "reason": None, "previous_next_run_at": None,
                 "new_next_run_at": "2026-04-30T14:34:00+00:00"},
        job_id="x",
    )
    sub.poll()
    assert log_path.exists()

    # Backdate mtime to 8 days ago
    eight_days_ago = time.time() - 8 * 86400
    import os
    os.utime(log_path, (eight_days_ago, eight_days_ago))

    # Force rotation check to bypass once-per-hour gate
    sub._last_rotation_check = 0

    # Trigger another event so poll() runs the rotation check
    bus.emit(
        event_type=EventType.CRON_TRIGGERED,
        source="y",
        payload={"job_id": "y", "job_name": "y", "caller": "test",
                 "reason": None, "previous_next_run_at": None,
                 "new_next_run_at": "2026-04-30T14:34:00+00:00"},
        job_id="y",
    )
    sub.poll()

    archive_dir = log_path.parent / "audit"
    assert archive_dir.exists()
    archives = list(archive_dir.glob("cron_triggers-*.jsonl"))
    assert len(archives) >= 1


def test_subscriber_id_and_event_type_filter(bus, log_path):
    sub = CronTriggerLog(bus, log_path=log_path)
    assert sub.subscriber_id == "cron-trigger-log"
    assert sub.event_types == [EventType.CRON_TRIGGERED]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/events/subscribers/test_cron_trigger_log.py -xvs
```

Expected: `ModuleNotFoundError: No module named 'events.subscribers.cron_trigger_log'`.

- [ ] **Step 3: Create the subscriber**

Create `events/subscribers/cron_trigger_log.py`:

```python
"""CronTriggerLog subscriber — per-job rolling JSONL of cron_triggered events.

Mirrors the AuditLogger pattern but consumes ONLY cron_triggered events,
giving operators a focused, easy-to-grep artifact for postmortem
attribution of off-schedule cron fires.

Storage: events/cron_triggers.jsonl (canonical root, cross-profile)
Rotation: weekly into events/audit/cron_triggers-YYYY-MM-DD.jsonl
Retention: 30 days
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from events.bus import EventBus
from events.schema import Event, EventType
from events.subscribers.base import BaseSubscriber

logger = logging.getLogger(__name__)

ROTATION_INTERVAL = 604800  # 7 days
RETENTION_DAYS = 30


class CronTriggerLog(BaseSubscriber):
    subscriber_id = "cron-trigger-log"
    poll_interval_seconds = 5
    event_types: List[EventType] = [EventType.CRON_TRIGGERED]

    def __init__(self, bus: EventBus, log_path: Optional[Path] = None):
        super().__init__(bus)
        if log_path is None:
            from events.paths import cron_trigger_log_path
            log_path = cron_trigger_log_path()
        self.log_path = Path(log_path)
        self._archive_dir = self.log_path.parent / "audit"
        self._last_rotation_check: float = 0

    def handle(self, event: Event) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(event.to_dict(), ensure_ascii=False)
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")

        now = time.monotonic()
        if now - self._last_rotation_check > 3600:
            self._rotate_if_needed()
            self._cleanup_old_archives()
            self._last_rotation_check = now

    def _rotate_if_needed(self) -> None:
        if not self.log_path.exists():
            return
        try:
            stat = self.log_path.stat()
            age = time.time() - stat.st_mtime
            if age < ROTATION_INTERVAL:
                return
            if stat.st_size == 0:
                return

            self._archive_dir.mkdir(parents=True, exist_ok=True)
            date_str = datetime.now().strftime("%Y-%m-%d")
            dest = self._archive_dir / f"cron_triggers-{date_str}.jsonl"

            counter = 1
            while dest.exists():
                dest = self._archive_dir / f"cron_triggers-{date_str}-{counter}.jsonl"
                counter += 1

            self.log_path.rename(dest)
            logger.info("CronTriggerLog: rotated to %s", dest.name)
        except Exception:
            logger.exception("CronTriggerLog: rotation failed")

    def _cleanup_old_archives(self) -> None:
        if not self._archive_dir.exists():
            return
        try:
            cutoff = time.time() - (RETENTION_DAYS * 86400)
            for f in self._archive_dir.glob("cron_triggers-*.jsonl"):
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    logger.info("CronTriggerLog: purged old archive %s", f.name)
        except Exception:
            logger.exception("CronTriggerLog: archive cleanup failed")
```

- [ ] **Step 4: Register the subscriber in `gateway_integration.py`**

In `events/gateway_integration.py`, near line 27 (the audit_logger import), add:

```python
from events.subscribers.cron_trigger_log import CronTriggerLog
```

Then in `startup()` near line 82 where subscribers are registered, add immediately after `_registry.register(AuditLogger(_bus))`:

```python
    _registry.register(CronTriggerLog(_bus))
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/events/subscribers/test_cron_trigger_log.py -xvs
```

Expected: 4 PASS.

- [ ] **Step 6: Commit**

```bash
cd ~/.hermes/agent-src && git add events/subscribers/cron_trigger_log.py tests/events/subscribers/test_cron_trigger_log.py events/gateway_integration.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(events): CronTriggerLog subscriber for per-job rolling tail"
```

---

## Task 8: End-to-end integration test (CLI → bus → JSONL log)

**Files:**
- Create: `tests/integration/test_cron_trigger_traceability.py`

If `tests/integration/` doesn't exist yet, create it (empty `__init__.py` if Python pkg discovery requires it — check existing `tests/` layout first).

- [ ] **Step 1: Write the integration test**

```python
"""End-to-end: hermes cron run --reason flows through to JSONL log."""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from cron.jobs import create_job
from events.bus import EventBus
from events.schema import EventType
from events.subscribers.cron_trigger_log import CronTriggerLog
from hermes_cli.cron import cron_command


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def test_cli_run_reaches_jsonl_log(tmp_cron_dir, monkeypatch, capsys):
    bus = EventBus(db_path=tmp_cron_dir / "events.db")
    log_path: Path = tmp_cron_dir / "events" / "cron_triggers.jsonl"
    monkeypatch.setattr("cron.jobs._get_event_bus", lambda: bus)

    sub = CronTriggerLog(bus, log_path=log_path)

    job = create_job(prompt="x", schedule="every 1h")
    cron_command(
        Namespace(
            cron_command="run",
            job_id=job["id"],
            reason="integration test",
        )
    )

    # Bus has one event
    events = bus.query(event_type=EventType.CRON_TRIGGERED)
    assert len(events) == 1

    # Subscriber writes one JSONL line
    sub.poll()
    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["event_type"] == "cron_triggered"
    assert rec["payload"]["caller"] == "hermes_cli:cron_run"
    assert rec["payload"]["reason"] == "integration test"
    assert rec["payload"]["job_id"] == job["id"]
    assert rec["payload"]["new_next_run_at"]
```

- [ ] **Step 2: Run the integration test**

```bash
cd ~/.hermes/agent-src && python -m pytest tests/integration/test_cron_trigger_traceability.py -xvs
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
cd ~/.hermes/agent-src && git add tests/integration/test_cron_trigger_traceability.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "test(integration): cron run --reason → bus → cron_triggers.jsonl"
```

---

## Task 9: Full-suite regression check + branch hand-off

- [ ] **Step 1: Run the full test suite for everything we touched**

```bash
cd ~/.hermes/agent-src && python -m pytest \
  tests/cron/ \
  tests/events/ \
  tests/hermes_cli/test_cron.py \
  tests/tools/test_cronjob_tools.py \
  tests/integration/test_cron_trigger_traceability.py \
  -x 2>&1 | tail -30
```

Expected: ZERO failures. If any unrelated test fails (e.g. the pre-existing modified-but-uncommitted `whatsapp_escalator` files leaked in), STOP — investigate the worktree isolation; do NOT mark this task complete.

- [ ] **Step 2: Verify the branch is clean and self-contained**

```bash
cd ~/.hermes/agent-src && git log --oneline main..HEAD
```

Expected: 7-8 commits, one per Task 1-8.

- [ ] **Step 3: Verify no unrelated files are staged or modified**

```bash
cd ~/.hermes/agent-src && git status
```

Expected: clean working tree.

- [ ] **Step 4: Print the branch ready for hand-off**

```bash
cd ~/.hermes/agent-src && echo "Branch: $(git branch --show-current)" && \
  echo "Tip:    $(git rev-parse HEAD)" && \
  echo "Diff:   $(git diff --stat main..HEAD | tail -1)"
```

Report the branch name + tip SHA + line-count summary back to Diego.

---

## Self-review checklist

**Spec coverage:**
- [x] Item 1 (caller arg + warning) → Task 3
- [x] Item 2 (CRON_TRIGGERED event + payload fields) → Task 1 (enum), Task 2 (helper), Task 3 (call site)
- [x] Item 3 (all callers updated) → Task 4 (LLM tool), Task 5 (CLI), Task 6 (HTTP API)
- [x] Item 4 (rolling per-job tail) → Task 7
- [x] Item 5 (test coverage) → every task ships its own test; Task 8 wraps it end-to-end

**Constraints honored:**
- [x] No change to cron schedules or `next_run_at = NOW` semantics — `trigger_job` still does exactly the same state mutation; emission is added AFTER
- [x] No change to `HERMES_CRON_HARD_TIMEOUT` or env vars
- [x] All work on agent-src — no parent `.hermes` repo edits
- [x] Author override `--author="Diego <diegodearagao@gmail.com>"` on every commit
- [x] No push or merge — branch is the deliverable

**Type consistency:**
- `caller` is `Optional[str]` everywhere
- `reason` is `Optional[str]` everywhere
- `previous_next_run_at` / `new_next_run_at` are ISO8601 strings (matching existing `next_run_at` shape)
- `_get_event_bus` is the single seam tests monkeypatch (consistent in Tasks 3, 4, 5, 8)
