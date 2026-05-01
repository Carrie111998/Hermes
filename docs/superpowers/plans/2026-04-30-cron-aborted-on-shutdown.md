# Cron Aborted on Gateway Shutdown — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit `EventType.CRON_ABORTED` for every cron whose `cron_started` was emitted but whose terminal event (`cron_completed` / `cron_failed`) never fired before the gateway shut down. Closes the dangling-event audit gap from the 2026-04-30 sentinel-vip-morning incident (canonical event_id `4edcb4b1-aa07-4dbb-b799-8af167d4f92e`).

**Architecture:** Reuse the existing `_in_flight: Dict[str, _InFlightRecord]` registry in `cron/scheduler.py` (already designed for this per the in-tree comment at lines 158-167). Augment the record with `started_at` ISO timestamp. Add a public `flush_inflight_aborts(reason)` helper that drains the registry and emits one `CRON_ABORTED` per still-tracked cron. Wire the helper into `events/gateway_integration.py:shutdown()` ahead of the bus close. Force a synchronous subscriber drain via `_registry.poll_all()` so the AuditLogger writes the abort events to `audit.jsonl` in the same shutdown cycle.

**Tech Stack:** Python 3.11, `concurrent.futures`, `threading.Lock`, pytest, pytest-xdist (uses the `_tick_lock_isolated` fixture pattern for any test that touches `tick()`).

---

## File Structure

| Path | Responsibility |
|------|----------------|
| `events/schema.py` | Add `EventType.CRON_ABORTED` (HIGH priority). |
| `events/producers/cron_emitter.py` | Add `CronEventEmitter.on_job_aborted(...)`. Does NOT call `FailureClusterDetector.record(...)` — abort is gateway-fault, not agent-fault. |
| `cron/scheduler.py` | Augment `_InFlightRecord` with optional `started_at: str`. Populate in `_try_register_in_flight`. Add public `flush_inflight_aborts(reason: str) -> int` that drains the registry and emits `cron_aborted` per entry. |
| `events/formatting.py` | Add `EventType.CRON_ABORTED` -> icon entry in `EVENT_TYPE_EMOJI`. |
| `events/subscribers/telegram_notifier.py` | Add `'cron_aborted': 'watchdog_alerts'` to `TOPIC_ROUTING`. |
| `events/gateway_integration.py` | Call `flush_inflight_aborts("gateway_shutdown")` then `_registry.poll_all()` in `shutdown()` before `_stop_event.set()`. |
| `tests/cron/test_scheduler.py` | New `TestFlushInflightAborts` class. Update `TestInFlightRegistryShape::test_record_has_documented_fields` for the new field. |
| `tests/events/test_cron_emitter.py` | New `TestOnJobAborted` class (payload shape + cluster-detector NOT invoked). |
| `tests/events/test_gateway_integration.py` | New `TestShutdownEmitsCronAborted` class — full integration through the live shutdown path. |

The existing coverage tests `tests/events/test_formatting.py::test_event_icons_cover_all_types` and `tests/events/test_telegram_notifier.py::TestTopicRouting::test_all_event_types_have_routing` will start failing the moment `EventType.CRON_ABORTED` is added without the icon + routing entries — that fail-then-fix cycle is part of Task 1 below.

---

## Payload Schema

```python
EventType.CRON_ABORTED payload:
    job_id:                  str          # e.g. "092f4ed7657c"
    job_name:                str          # e.g. "sentinel-vip-morning"
    cron_started_event_id:   Optional[str] # for audit-log pair correlation
    started_at:              str          # ISO8601 UTC, when registered
    aborted_at:              str          # ISO8601 UTC, when flush ran
    elapsed_seconds:         float        # rounded 1dp; max(0.0, monotonic now - start)
    reason:                  str          # "gateway_shutdown" | "wallclock_timeout"
```

Reasons are categorical and will grow over time. The two starting values match the gateway-shutdown wiring (this plan) and a future wallclock-path wiring (out of scope; the `wallclock_timeout` reason is exercised in tests via direct helper invocation).

---

## Task 1: Add CRON_ABORTED EventType + icon + topic routing

**Files:**
- Modify: `events/schema.py` — add `CRON_ABORTED = ("cron_aborted", Priority.HIGH)`
- Modify: `events/formatting.py` — add `EventType.CRON_ABORTED: "🛑"` to `EVENT_TYPE_EMOJI`
- Modify: `events/subscribers/telegram_notifier.py` — add `'cron_aborted': 'watchdog_alerts'` to `TOPIC_ROUTING`
- Test: `tests/events/test_schema.py` (existing covers EventType.from_string roundtrip via existing tests; we add one explicit assertion)

- [ ] **Step 1: Run the existing coverage tests to confirm they still pass**

Run: `python -m pytest tests/events/test_formatting.py::test_event_icons_cover_all_types tests/events/test_telegram_notifier.py::TestTopicRouting::test_all_event_types_have_routing -v`

Expected: `2 passed`

This baseline confirms the coverage tests are the contract that will fail when we add CRON_ABORTED without the matching icon + routing entries.

- [ ] **Step 2: Add the failing schema test**

Append to `tests/events/test_schema.py`:

```python
def test_cron_aborted_event_type_exists():
    """Guard #1 (2026-04-30): CRON_ABORTED is emitted on gateway shutdown
    so dangling cron_started events get a paired terminal event in
    audit.jsonl."""
    assert EventType.CRON_ABORTED.type_string == "cron_aborted"
    # HIGH priority so it surfaces in operator alerts (matches CRON_FAILED
    # rather than CRON_SKIPPED_DUPLICATE -- aborting on gateway shutdown
    # is a real signal that work was lost).
    assert EventType.CRON_ABORTED.default_priority == Priority.HIGH
    # from_string roundtrip
    assert EventType.from_string("cron_aborted") is EventType.CRON_ABORTED
```

- [ ] **Step 3: Run the new test to verify it fails**

Run: `python -m pytest tests/events/test_schema.py::test_cron_aborted_event_type_exists -v`

Expected: FAIL with `AttributeError: type object 'EventType' has no attribute 'CRON_ABORTED'`

- [ ] **Step 4: Run the coverage tests to verify they will fail too once the enum exists**

(Skip — they currently pass because the enum doesn't exist. They will fail in Step 6 below as a side effect of adding the enum without the icon/routing entries. We re-run them in Step 7.)

- [ ] **Step 5: Add the EventType enum member**

Edit `events/schema.py` — insert immediately after the `CRON_SKIPPED_DUPLICATE = ...` block (around line 74). Use this docstring + line:

```python
    # Cron aborted on gateway shutdown -- Guard #1, added 2026-04-30 to
    # close the audit gap surfaced by the sentinel-vip-morning incident
    # (canonical event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e). When the
    # gateway shuts down with a cron future still in flight, emit one
    # cron_aborted per still-tracked job so audit.jsonl never accumulates
    # dangling cron_started rows. Subscribers should treat as terminal --
    # CronStaleMonitor must clear _open_jobs / _alerted on receipt, same
    # as cron_completed / cron_failed.  Payload:
    #   job_id, job_name, cron_started_event_id, started_at, aborted_at,
    #   elapsed_seconds, reason
    # where reason is one of:
    #   gateway_shutdown    (gateway is going down with futures in flight)
    #   wallclock_timeout   (HERMES_CRON_HARD_TIMEOUT enforcement)
    # HIGH priority so the abort surfaces in operator alerts rather than
    # being batched into low-priority telemetry.
    CRON_ABORTED = ("cron_aborted", Priority.HIGH)
```

- [ ] **Step 6: Run the schema test + the coverage tests — schema passes, coverage fails**

Run: `python -m pytest tests/events/test_schema.py::test_cron_aborted_event_type_exists tests/events/test_formatting.py::test_event_icons_cover_all_types tests/events/test_telegram_notifier.py::TestTopicRouting::test_all_event_types_have_routing -v`

Expected:
- `test_cron_aborted_event_type_exists` PASSES
- `test_event_icons_cover_all_types` FAILS with `AssertionError: missing icon for cron_aborted`
- `test_all_event_types_have_routing` FAILS with `AssertionError: EventType cron_aborted missing from TOPIC_ROUTING`

- [ ] **Step 7: Add the icon entry to formatting.py**

Edit `events/formatting.py`. Add after the `EventType.CRON_SKIPPED_DUPLICATE: "⏭️",` line (around line 31):

```python
    # Cron aborted on gateway shutdown (Guard #1, 2026-04-30) — distinct
    # from CRON_FAILED (agent-fault) and CRON_SKIPPED_DUPLICATE (concurrency
    # guard reject). Stop-sign signals "scheduler interrupted this fire."
    EventType.CRON_ABORTED:             "🛑",
```

- [ ] **Step 8: Add the topic routing entry to telegram_notifier.py**

Edit `events/subscribers/telegram_notifier.py`. Find the existing `'cron_skipped_duplicate': 'watchdog_alerts',` line in `TOPIC_ROUTING`. Add immediately after it:

```python
    'cron_aborted': 'watchdog_alerts',
```

- [ ] **Step 9: Run all three tests together — all three pass**

Run: `python -m pytest tests/events/test_schema.py::test_cron_aborted_event_type_exists tests/events/test_formatting.py::test_event_icons_cover_all_types tests/events/test_telegram_notifier.py::TestTopicRouting::test_all_event_types_have_routing -v`

Expected: `3 passed`

- [ ] **Step 10: Commit**

```bash
cd ~/.hermes/.claude/worktrees/agent-src-cron-aborted-shutdown
git add events/schema.py events/formatting.py events/subscribers/telegram_notifier.py tests/events/test_schema.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(events): add EventType.CRON_ABORTED + icon + telegram routing

Guard #1 (2026-04-30 cron-aborted-on-shutdown): closes the audit gap
surfaced by the sentinel-vip-morning incident (canonical event_id
4edcb4b1-aa07-4dbb-b799-8af167d4f92e). HIGH priority + watchdog_alerts
topic so a gateway-shutdown abort surfaces alongside cron_failed."
```

---

## Task 2: Augment `_InFlightRecord` with `started_at` and add `flush_inflight_aborts`

**Files:**
- Modify: `cron/scheduler.py` — add `started_at: Optional[str] = None` to `_InFlightRecord`. Populate it in `_try_register_in_flight`. Add public `flush_inflight_aborts(reason: str) -> int`.
- Test: `tests/cron/test_scheduler.py`

- [ ] **Step 1: Write failing test for `_InFlightRecord` carrying `started_at`**

Append a new test to the existing `TestInFlightRegistryShape` class in `tests/cron/test_scheduler.py` (around line 2865):

```python
    def test_record_carries_started_at_iso(self):
        """Guard #1 contract: registry exposes the wall-clock ISO start
        time so flush_inflight_aborts can populate cron_aborted's
        started_at field for the audit-log pair."""
        from cron.scheduler import _InFlightRecord
        rec = _InFlightRecord(
            start_monotonic=42.0,
            job_name="any",
            cron_started_event_id=None,
            started_at="2026-04-30T14:49:52+00:00",
        )
        assert rec.started_at == "2026-04-30T14:49:52+00:00"

    def test_register_populates_started_at(self):
        """_try_register_in_flight stamps started_at with current UTC ISO."""
        from cron import scheduler as sch
        sch._in_flight.clear()
        try:
            assert sch._try_register_in_flight("job-xyz", "any") is None
            rec = sch._in_flight["job-xyz"]
            assert rec.started_at is not None
            # Smoke check: ISO-parseable, has tz info
            from datetime import datetime
            parsed = datetime.fromisoformat(rec.started_at)
            assert parsed.tzinfo is not None
        finally:
            sch._in_flight.clear()
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python -m pytest tests/cron/test_scheduler.py::TestInFlightRegistryShape::test_record_carries_started_at_iso tests/cron/test_scheduler.py::TestInFlightRegistryShape::test_register_populates_started_at -v`

Expected: FAIL — `_InFlightRecord` got an unexpected keyword argument `started_at` (or `assert rec.started_at is not None` fails because the field doesn't exist).

- [ ] **Step 3: Augment `_InFlightRecord`**

Edit `cron/scheduler.py`. Update the dataclass at ~line 174:

```python
@dataclass
class _InFlightRecord:
    start_monotonic: float
    job_name: str
    cron_started_event_id: Optional[str] = None
    # ISO8601 UTC timestamp captured at registration. Used by
    # flush_inflight_aborts (Guard #1) to populate cron_aborted.started_at
    # so audit.jsonl pairs can be correlated by wall-clock time, not just
    # by event_id.
    started_at: Optional[str] = None
```

- [ ] **Step 4: Update `_try_register_in_flight` to populate `started_at`**

Edit `cron/scheduler.py`. Update `_try_register_in_flight` at ~line 205-221:

```python
def _try_register_in_flight(job_id: str, job_name: str) -> Optional[_InFlightRecord]:
    """Attempt to register a job as in-flight.

    Returns ``None`` if the slot was acquired (caller may proceed).
    Returns the existing ``_InFlightRecord`` if a duplicate fire is
    already running (caller must reject and emit cron_skipped_duplicate).
    """
    with _in_flight_lock:
        prior = _in_flight.get(job_id)
        if prior is not None:
            return prior
        _in_flight[job_id] = _InFlightRecord(
            start_monotonic=time.monotonic(),
            job_name=job_name,
            cron_started_event_id=None,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        return None
```

If `datetime` and `timezone` are not already imported at the top of `cron/scheduler.py`, add `from datetime import datetime, timezone`. (Verify by inspecting the top of the file before editing.)

- [ ] **Step 5: Run the registry tests to verify they pass**

Run: `python -m pytest tests/cron/test_scheduler.py::TestInFlightRegistryShape -v`

Expected: All `TestInFlightRegistryShape` tests pass (including the existing `test_record_has_documented_fields`, which still works because the new field has a default).

- [ ] **Step 6: Run the duplicate-fire-guard tests to confirm no regression**

Run: `python -m pytest tests/cron/test_scheduler.py::TestDuplicateFireGuard -v`

Expected: All pass (the existing tests don't assert on `started_at` so they're unaffected).

- [ ] **Step 7: Write failing test for `flush_inflight_aborts` happy path**

Append a new test class to `tests/cron/test_scheduler.py`:

```python
class TestFlushInflightAborts:
    """Guard #1 (2026-04-30): drain the in-flight registry on gateway
    shutdown and emit one cron_aborted per still-tracked job."""

    @pytest.fixture(autouse=True)
    def _reset_in_flight(self):
        from cron import scheduler as sch
        sch._in_flight.clear()
        yield
        sch._in_flight.clear()

    def test_emits_one_cron_aborted_per_inflight_entry(self):
        from cron import scheduler as sch
        from cron.scheduler import _InFlightRecord

        emitter = MagicMock()
        emitter.on_job_aborted.return_value = "abort-evt-id"

        sch._in_flight["job-a"] = _InFlightRecord(
            start_monotonic=time.monotonic() - 5.0,
            job_name="sentinel-vip-morning",
            cron_started_event_id="started-a",
            started_at="2026-04-30T14:49:52+00:00",
        )
        sch._in_flight["job-b"] = _InFlightRecord(
            start_monotonic=time.monotonic() - 2.0,
            job_name="jobflow-scout",
            cron_started_event_id="started-b",
            started_at="2026-04-30T14:50:30+00:00",
        )

        with patch("cron.scheduler._get_event_emitter", return_value=emitter):
            count = sch.flush_inflight_aborts("gateway_shutdown")

        assert count == 2
        assert emitter.on_job_aborted.call_count == 2
        # Registry is drained so subsequent fires are not blocked.
        assert sch._in_flight == {}

        # Verify the payload of each call carries the expected fields.
        calls = {c.kwargs["job_id"]: c.kwargs for c in emitter.on_job_aborted.call_args_list}
        assert calls["job-a"]["job_name"] == "sentinel-vip-morning"
        assert calls["job-a"]["cron_started_event_id"] == "started-a"
        assert calls["job-a"]["started_at"] == "2026-04-30T14:49:52+00:00"
        assert calls["job-a"]["reason"] == "gateway_shutdown"
        assert calls["job-a"]["elapsed_seconds"] >= 0.0
        # aborted_at is now-ish ISO; smoke check it parses.
        from datetime import datetime
        datetime.fromisoformat(calls["job-a"]["aborted_at"])

    def test_supports_wallclock_timeout_reason(self):
        """The helper accepts the wallclock_timeout reason value verbatim
        so a future wallclock-path wiring can call it without re-shaping."""
        from cron import scheduler as sch
        from cron.scheduler import _InFlightRecord

        emitter = MagicMock()
        sch._in_flight["job-x"] = _InFlightRecord(
            start_monotonic=time.monotonic() - 1800.0,
            job_name="hung-job",
            cron_started_event_id="started-x",
            started_at="2026-04-30T13:00:00+00:00",
        )

        with patch("cron.scheduler._get_event_emitter", return_value=emitter):
            count = sch.flush_inflight_aborts("wallclock_timeout")

        assert count == 1
        emitter.on_job_aborted.assert_called_once()
        kwargs = emitter.on_job_aborted.call_args.kwargs
        assert kwargs["reason"] == "wallclock_timeout"
        assert kwargs["elapsed_seconds"] >= 1800.0

    def test_no_emitter_clears_registry_returns_zero(self):
        """When the bus is unavailable (degraded gateway), still clear
        the registry so a re-init doesn't see stale entries — but emit
        nothing. Defensive parity with the rest of the cron path."""
        from cron import scheduler as sch
        from cron.scheduler import _InFlightRecord

        sch._in_flight["job-y"] = _InFlightRecord(
            start_monotonic=time.monotonic(),
            job_name="any",
            cron_started_event_id=None,
            started_at="2026-04-30T15:00:00+00:00",
        )

        with patch("cron.scheduler._get_event_emitter", return_value=None):
            count = sch.flush_inflight_aborts("gateway_shutdown")

        assert count == 0
        assert sch._in_flight == {}

    def test_emitter_failure_does_not_break_drain(self):
        """A broken emitter must never wedge the shutdown path. Failed
        emits are logged and the drain continues; the registry is still
        cleared."""
        from cron import scheduler as sch
        from cron.scheduler import _InFlightRecord

        emitter = MagicMock()
        emitter.on_job_aborted.side_effect = RuntimeError("bus closed")

        sch._in_flight["job-z"] = _InFlightRecord(
            start_monotonic=time.monotonic(),
            job_name="any",
            cron_started_event_id=None,
            started_at="2026-04-30T15:00:00+00:00",
        )

        with patch("cron.scheduler._get_event_emitter", return_value=emitter):
            # Must NOT raise.
            count = sch.flush_inflight_aborts("gateway_shutdown")

        # Count is 0 because no emit succeeded; registry is still drained.
        assert count == 0
        assert sch._in_flight == {}
```

- [ ] **Step 8: Run the new tests to verify they fail**

Run: `python -m pytest tests/cron/test_scheduler.py::TestFlushInflightAborts -v`

Expected: All four tests FAIL — `cron.scheduler` has no attribute `flush_inflight_aborts`.

- [ ] **Step 9: Implement `flush_inflight_aborts`**

Add to `cron/scheduler.py` immediately after `_release_in_flight` (around line 242):

```python
def flush_inflight_aborts(reason: str) -> int:
    """Drain the in-flight cron registry, emitting cron_aborted per entry.

    Used at gateway shutdown (Guard #1, 2026-04-30) to ensure every
    cron_started has a paired terminal event in audit.jsonl. The
    canonical incident this closes is the sentinel-vip-morning triple-
    fire (event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e), where the
    gateway died mid-LLM and the third fire's cron_started never paired
    with cron_completed or cron_failed.

    Returns the count of cron_aborted events successfully emitted.

    Defensive guarantees (mirrors the cron path's existing patterns):
      * The registry is always cleared, even if the bus is unavailable
        or every emit fails — a fresh gateway must not see stale entries.
      * Emit exceptions are logged and swallowed per-entry so one broken
        emit cannot wedge the shutdown path.

    ``reason`` is one of:
      * ``"gateway_shutdown"`` -- gateway is shutting down with futures in flight
      * ``"wallclock_timeout"`` -- HERMES_CRON_HARD_TIMEOUT enforcement
    """
    aborted_at = datetime.now(timezone.utc).isoformat()

    # Snapshot under the lock, then release so emits don't block other
    # cron paths (notably the wallclock-poll loop's _release_in_flight).
    with _in_flight_lock:
        snapshot = list(_in_flight.items())
        _in_flight.clear()

    emitter = _get_event_emitter()
    if emitter is None:
        return 0

    count = 0
    now_mono = time.monotonic()
    for job_id, rec in snapshot:
        elapsed = max(0.0, now_mono - rec.start_monotonic)
        try:
            emitter.on_job_aborted(
                job_id=job_id,
                job_name=rec.job_name,
                cron_started_event_id=rec.cron_started_event_id,
                started_at=rec.started_at or aborted_at,
                aborted_at=aborted_at,
                elapsed_seconds=round(elapsed, 1),
                reason=reason,
            )
            count += 1
        except Exception:
            logger.exception(
                "flush_inflight_aborts: failed to emit cron_aborted for %s",
                job_id,
            )
    return count
```

- [ ] **Step 10: Run the new tests to verify they pass**

Run: `python -m pytest tests/cron/test_scheduler.py::TestFlushInflightAborts -v`

Expected: `4 passed`

- [ ] **Step 11: Run the full scheduler test suite to confirm no regressions**

Run: `python -m pytest tests/cron/test_scheduler.py -q`

Expected: All scheduler tests pass.

- [ ] **Step 12: Commit**

```bash
cd ~/.hermes/.claude/worktrees/agent-src-cron-aborted-shutdown
git add cron/scheduler.py tests/cron/test_scheduler.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(cron): flush_inflight_aborts drains _in_flight on shutdown

Guard #1 implementation step 1/2: adds the public drain helper that
walks _in_flight, emits cron_aborted per still-tracked entry, and
clears the registry. Augments _InFlightRecord with started_at ISO so
the cron_aborted payload carries both correlation (cron_started_event_id)
and wall-clock (started_at + aborted_at) for audit-log pairing.

Wiring into events.gateway_integration.shutdown() comes in the next
commit (Task 4); this commit lands the helper + tests so the wiring
can be diff-isolated."
```

---

## Task 3: Add `CronEventEmitter.on_job_aborted`

**Files:**
- Modify: `events/producers/cron_emitter.py`
- Test: `tests/events/test_cron_emitter.py`

- [ ] **Step 1: Write failing test for `on_job_aborted`**

Append to `tests/events/test_cron_emitter.py`:

```python
class TestOnJobAborted:
    """CronEventEmitter.on_job_aborted (Guard #1, 2026-04-30) — emits
    CRON_ABORTED. Deliberately does NOT feed FailureClusterDetector:
    abort is gateway-fault, not agent-fault, so it must not trip
    same-source cluster alerts."""

    def test_emits_cron_aborted_with_full_payload(self):
        from events.bus import EventBus
        from events.producers.cron_emitter import CronEventEmitter
        from events.schema import EventType

        bus = MagicMock(spec=EventBus)
        bus.emit.return_value = "evt-abort-1"
        emitter = CronEventEmitter(bus)

        result = emitter.on_job_aborted(
            job_id="092f4ed7657c",
            job_name="sentinel-vip-morning",
            cron_started_event_id="4edcb4b1-aa07-4dbb-b799-8af167d4f92e",
            started_at="2026-04-30T14:49:52+00:00",
            aborted_at="2026-04-30T14:56:43+00:00",
            elapsed_seconds=411.0,
            reason="gateway_shutdown",
        )

        assert result == "evt-abort-1"
        bus.emit.assert_called_once()
        kwargs = bus.emit.call_args.kwargs
        assert kwargs["event_type"] == EventType.CRON_ABORTED
        assert kwargs["source"] == "sentinel-vip-morning"
        assert kwargs["payload"] == {
            "job_id": "092f4ed7657c",
            "job_name": "sentinel-vip-morning",
            "cron_started_event_id": "4edcb4b1-aa07-4dbb-b799-8af167d4f92e",
            "started_at": "2026-04-30T14:49:52+00:00",
            "aborted_at": "2026-04-30T14:56:43+00:00",
            "elapsed_seconds": 411.0,
            "reason": "gateway_shutdown",
        }

    def test_does_not_record_failure_cluster(self):
        """Cron_aborted is gateway-fault. If on_job_aborted fed the
        cluster detector, a single gateway restart could trip a spurious
        agent_failure_cluster for whichever agent was in flight."""
        from events.bus import EventBus
        from events.producers.cron_emitter import CronEventEmitter

        bus = MagicMock(spec=EventBus)
        bus.emit.return_value = "evt-abort-2"
        emitter = CronEventEmitter(bus)
        emitter._cluster_detector = MagicMock()  # spy

        emitter.on_job_aborted(
            job_id="any",
            job_name="any-cron",
            cron_started_event_id=None,
            started_at="2026-04-30T15:00:00+00:00",
            aborted_at="2026-04-30T15:00:01+00:00",
            elapsed_seconds=1.0,
            reason="gateway_shutdown",
        )

        emitter._cluster_detector.record.assert_not_called()
```

(Top-of-file: ensure `from unittest.mock import MagicMock` import is present. The existing test file already imports it for other tests; verify before adding.)

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python -m pytest tests/events/test_cron_emitter.py::TestOnJobAborted -v`

Expected: FAIL — `CronEventEmitter` has no attribute `on_job_aborted`.

- [ ] **Step 3: Implement `on_job_aborted`**

Add to `events/producers/cron_emitter.py` immediately after `on_job_skipped_duplicate` (around line 124):

```python
    def on_job_aborted(
        self,
        job_id: str,
        job_name: str,
        cron_started_event_id: Optional[str],
        started_at: str,
        aborted_at: str,
        elapsed_seconds: float,
        reason: str,
    ) -> str:
        """Emit cron_aborted when a cron is interrupted before terminal completion.

        Guard #1 (2026-04-30) — closes the audit gap where a gateway
        shutdown with in-flight cron futures left dangling cron_started
        rows in audit.jsonl with no paired terminal event.

        Reasons:
          * ``"gateway_shutdown"`` -- gateway is shutting down with futures in flight
          * ``"wallclock_timeout"`` -- HERMES_CRON_HARD_TIMEOUT enforcement

        Deliberately does NOT call FailureClusterDetector.record(...).
        Cron_aborted is gateway-fault, not agent-fault, so it must not
        trip same-source cluster alerts.
        """
        return self.bus.emit(
            event_type=EventType.CRON_ABORTED,
            source=job_name,
            payload={
                "job_id": job_id,
                "job_name": job_name,
                "cron_started_event_id": cron_started_event_id,
                "started_at": started_at,
                "aborted_at": aborted_at,
                "elapsed_seconds": elapsed_seconds,
                "reason": reason,
            },
        )
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python -m pytest tests/events/test_cron_emitter.py::TestOnJobAborted -v`

Expected: `2 passed`

- [ ] **Step 5: Run the full cron_emitter test file to confirm no regressions**

Run: `python -m pytest tests/events/test_cron_emitter.py -q`

Expected: All cron_emitter tests pass.

- [ ] **Step 6: Commit**

```bash
cd ~/.hermes/.claude/worktrees/agent-src-cron-aborted-shutdown
git add events/producers/cron_emitter.py tests/events/test_cron_emitter.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(events): CronEventEmitter.on_job_aborted

Guard #1 wiring helper: emits CRON_ABORTED with the canonical payload
shape used by flush_inflight_aborts. Skips FailureClusterDetector by
design -- abort is gateway-fault, not agent-fault, so a single restart
must not trip a spurious agent_failure_cluster alert for whichever
agent was in flight."
```

---

## Task 4: Wire `flush_inflight_aborts` into `gateway_integration.shutdown()`

**Files:**
- Modify: `events/gateway_integration.py` — call `flush_inflight_aborts("gateway_shutdown")` then `_registry.poll_all()` BEFORE `_stop_event.set()`.
- Test: `tests/events/test_gateway_integration.py`

- [ ] **Step 1: Inspect existing `tests/events/test_gateway_integration.py` for test conventions**

Run: `python -m pytest tests/events/test_gateway_integration.py --collect-only -q | head -30`

Expected: lists existing test classes and naming conventions.

- [ ] **Step 2: Write failing integration test for shutdown emitting cron_aborted**

Append to `tests/events/test_gateway_integration.py` (or create a new test class). Use the existing fixtures (look for `bus`, `tmp_path`, etc. used by sibling tests):

```python
class TestShutdownEmitsCronAborted:
    """Guard #1 (2026-04-30): shutdown() must drain the in-flight cron
    registry into cron_aborted events before closing the bus, so
    audit.jsonl never accumulates dangling cron_started rows after a
    gateway restart."""

    def test_shutdown_emits_cron_aborted_for_each_inflight_entry(self, tmp_path, monkeypatch):
        """Full-cycle integration: register a cron in _in_flight, run
        startup() + shutdown(), assert cron_aborted appears in the bus's
        DB with the expected payload."""
        from cron import scheduler as sch
        from cron.scheduler import _InFlightRecord
        from events import gateway_integration as gi
        from events.bus import EventBus
        from events.schema import EventType

        # Point HERMES_HOME at a temp dir so bus + cursors land in tmp_path,
        # avoiding interference with the live ~/.hermes installation.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))

        sch._in_flight.clear()
        try:
            sch._in_flight["job-int"] = _InFlightRecord(
                start_monotonic=time.monotonic() - 60.0,
                job_name="integration-cron",
                cron_started_event_id="started-int",
                started_at="2026-04-30T14:00:00+00:00",
            )

            # Boot the bus + subscribers, then shut down. The shutdown path
            # must drain _in_flight into cron_aborted events.
            gi.startup()
            try:
                # Sanity: registry has the entry going in.
                assert "job-int" in sch._in_flight
            finally:
                gi.shutdown()

            # Registry is drained.
            assert "job-int" not in sch._in_flight

            # Re-open the bus DB to inspect what was persisted.
            bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")
            try:
                events = bus.read_since(0, limit=100)
            finally:
                bus.close()

            aborts = [e for e in events if e.event_type == EventType.CRON_ABORTED]
            assert len(aborts) == 1, f"expected 1 cron_aborted, got {len(aborts)}: {events!r}"
            payload = aborts[0].payload
            assert payload["job_id"] == "job-int"
            assert payload["job_name"] == "integration-cron"
            assert payload["cron_started_event_id"] == "started-int"
            assert payload["reason"] == "gateway_shutdown"
            assert payload["started_at"] == "2026-04-30T14:00:00+00:00"
            assert payload["elapsed_seconds"] >= 60.0
        finally:
            sch._in_flight.clear()

    def test_shutdown_with_empty_inflight_emits_nothing(self, tmp_path, monkeypatch):
        """No registered crons => no cron_aborted events emitted."""
        from cron import scheduler as sch
        from events import gateway_integration as gi
        from events.bus import EventBus
        from events.schema import EventType

        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        sch._in_flight.clear()

        gi.startup()
        gi.shutdown()

        bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")
        try:
            events = bus.read_since(0, limit=100)
        finally:
            bus.close()
        aborts = [e for e in events if e.event_type == EventType.CRON_ABORTED]
        assert aborts == []
```

If `EventBus.read_since` is the wrong method name for the bus introspection in this repo, swap to whatever the existing test files use to read events from the bus DB. Check `tests/events/test_bus.py` for conventions.

If the existing test file already monkeypatches HERMES_HOME via a session-scoped fixture, reuse it instead of inline monkeypatch.

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `python -m pytest tests/events/test_gateway_integration.py::TestShutdownEmitsCronAborted -v`

Expected: FAIL — the shutdown path does not yet call `flush_inflight_aborts`, so no cron_aborted appears.

- [ ] **Step 4: Wire `flush_inflight_aborts` into `shutdown()`**

Edit `events/gateway_integration.py`. Update the `shutdown()` function (currently at ~line 143):

```python
def shutdown() -> None:
    """Stop polling and clean up.

    Guard #1 (2026-04-30): drain the in-flight cron registry into
    cron_aborted events BEFORE stopping the poll loop and closing the
    bus, so audit.jsonl pairs every cron_started with a terminal event
    even when the gateway dies mid-run.
    """
    global _subscriber_thread, _bus

    # Drain in-flight crons FIRST while the bus is still open. We then
    # synchronously poll subscribers once so AuditLogger writes the abort
    # events to audit.jsonl in the same shutdown cycle (rather than
    # waiting for the next gateway start to drain them).
    try:
        from cron.scheduler import flush_inflight_aborts
        emitted = flush_inflight_aborts("gateway_shutdown")
        if emitted and _registry is not None:
            try:
                _registry.poll_all()
            except Exception:
                logger.exception(
                    "EventBus shutdown: poll_all after flush_inflight_aborts failed",
                )
    except Exception:
        # Never let a flush failure break the rest of shutdown.
        logger.exception("EventBus shutdown: flush_inflight_aborts failed")

    _stop_event.set()
    if _subscriber_thread:
        _subscriber_thread.join(timeout=5)
        _subscriber_thread = None
    if _registry:
        _registry.shutdown_all()
    if _bus:
        _bus.close()
        _bus = None
    logger.info("EventBus: shutdown complete")
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `python -m pytest tests/events/test_gateway_integration.py::TestShutdownEmitsCronAborted -v`

Expected: `2 passed`

- [ ] **Step 6: Run the full gateway_integration test file to confirm no regressions**

Run: `python -m pytest tests/events/test_gateway_integration.py -q`

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
cd ~/.hermes/.claude/worktrees/agent-src-cron-aborted-shutdown
git add events/gateway_integration.py tests/events/test_gateway_integration.py
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(gateway): emit cron_aborted on shutdown for in-flight crons

Guard #1 final wiring: gateway_integration.shutdown() now calls
flush_inflight_aborts(\"gateway_shutdown\") + _registry.poll_all() BEFORE
_stop_event.set() so any cron whose cron_started fired but whose
terminal event never arrived gets paired with a cron_aborted in the
same shutdown cycle.

Closes the canonical 2026-04-30 sentinel-vip-morning audit gap
(event_id 4edcb4b1-aa07-4dbb-b799-8af167d4f92e). All flush + poll
calls are wrapped in try/except so a degraded bus or broken
subscriber cannot wedge the rest of shutdown."
```

---

## Task 5: CronStaleMonitor — clear `_open_jobs` on `CRON_ABORTED`

**Why:** `CronStaleMonitor` tracks every `cron_started` and emits `cron_stale` if no `cron_completed` / `cron_failed` arrives within the threshold. If we emit `cron_aborted` and the monitor doesn't recognize it as terminal, the next gateway after restart could re-load state and emit a spurious `cron_stale` for a job that actually aborted cleanly.

**Files:**
- Modify: `events/subscribers/cron_stale_monitor.py`
- Test: `tests/events/subscribers/test_cron_stale_monitor.py` (or `tests/events/test_cron_stale_monitor.py` — pick whichever already exists; check via glob).

- [ ] **Step 1: Locate the existing CronStaleMonitor test file**

Run: `python -m pytest --collect-only -q tests/ | grep -i cron_stale | head -5`

Find the existing test file path. Use it for the new tests.

- [ ] **Step 2: Write failing test for cron_aborted clearing `_open_jobs`**

Append to the existing test file:

```python
class TestCronAbortedClearsOpenJobs:
    """Guard #1 (2026-04-30): CRON_ABORTED is a terminal event for the
    stale-monitor's purposes — it must clear _open_jobs and _alerted
    so a future fire of the same job_id can be tracked again."""

    def test_handle_cron_aborted_clears_open_jobs(self, tmp_path):
        from datetime import datetime, timezone
        from events.bus import EventBus
        from events.schema import Event, EventType
        from events.subscribers.cron_stale_monitor import CronStaleMonitor

        bus = MagicMock(spec=EventBus)
        monitor = CronStaleMonitor(bus)

        started = Event.create(
            EventType.CRON_STARTED, "any",
            payload={"job_id": "j1", "job_name": "any"},
        )
        monitor.handle(started)
        assert "j1" in monitor._open_jobs

        aborted = Event.create(
            EventType.CRON_ABORTED, "any",
            payload={
                "job_id": "j1",
                "job_name": "any",
                "cron_started_event_id": started.event_id,
                "started_at": started.timestamp,
                "aborted_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": 1.0,
                "reason": "gateway_shutdown",
            },
        )
        monitor.handle(aborted)

        assert "j1" not in monitor._open_jobs
        assert "j1" not in monitor._alerted

    def test_cron_aborted_in_event_types_filter(self):
        """The subscriber's event_types list must include CRON_ABORTED so
        the bus delivers it (otherwise the handle() branch is dead code)."""
        from events.schema import EventType
        from events.subscribers.cron_stale_monitor import CronStaleMonitor

        assert EventType.CRON_ABORTED in CronStaleMonitor.event_types
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `python -m pytest <test-file>::TestCronAbortedClearsOpenJobs -v`

Expected: FAIL — `CRON_ABORTED` is not in `event_types`, and `handle()` only treats `CRON_COMPLETED`/`CRON_FAILED` as terminal.

- [ ] **Step 4: Update CronStaleMonitor**

Edit `events/subscribers/cron_stale_monitor.py`. Two changes:

1. Add `EventType.CRON_ABORTED` to `event_types`:

```python
    event_types: Optional[List[EventType]] = [
        EventType.CRON_STARTED,
        EventType.CRON_COMPLETED,
        EventType.CRON_FAILED,
        # Guard #1 (2026-04-30): cron_aborted on gateway shutdown is a
        # terminal event for the stale-monitor's purposes -- clears
        # _open_jobs / _alerted just like cron_completed / cron_failed.
        EventType.CRON_ABORTED,
    ]
```

2. Extend the terminal branch in `handle()`:

```python
        elif event.event_type in (
            EventType.CRON_COMPLETED,
            EventType.CRON_FAILED,
            EventType.CRON_ABORTED,
        ):
            self._open_jobs.pop(job_id, None)
            self._alerted.discard(job_id)
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `python -m pytest <test-file>::TestCronAbortedClearsOpenJobs -v`

Expected: `2 passed`

- [ ] **Step 6: Run the full CronStaleMonitor test file to confirm no regressions**

Run: `python -m pytest <test-file> -q`

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
cd ~/.hermes/.claude/worktrees/agent-src-cron-aborted-shutdown
git add events/subscribers/cron_stale_monitor.py <test-file>
git commit --author="Diego <diegodearagao@gmail.com>" -m "feat(cron-stale-monitor): treat cron_aborted as terminal

Guard #1 closure: CronStaleMonitor must clear _open_jobs and _alerted
on cron_aborted, otherwise a job that aborted cleanly during gateway
shutdown could trip a spurious cron_stale alert when the monitor
re-loads state on next startup. Adds CRON_ABORTED to event_types so
the bus actually delivers it (dead-code prevention)."
```

---

## Task 6: Final verification

- [ ] **Step 1: Run the full agent-src test suite**

Run: `python -m pytest -q tests/ 2>&1 | tail -30`

Expected: All tests pass. (If pre-existing failures exist on `main`, they will still be pre-existing failures — verify they match by checking out `main` in a sibling worktree first if uncertain.)

- [ ] **Step 2: Confirm the targeted change set**

Run: `git diff --stat main..HEAD`

Expected: ~7 files changed (schema, formatting, telegram_notifier, scheduler, cron_emitter, cron_stale_monitor, gateway_integration) + ~3 test files.

- [ ] **Step 3: Confirm commits are authored as Diego**

Run: `git log main..HEAD --pretty=format:'%an %ae | %s'`

Expected: every commit shows `Diego diegodearagao@gmail.com` as author (NOT `Codex`).

- [ ] **Step 4: Hand back to Diego**

Do NOT push or merge. Report:
- Branch name: `feature/cron-aborted-shutdown`
- Worktree: `~/.hermes/.claude/worktrees/agent-src-cron-aborted-shutdown`
- Tests passing locally
- Stale `feature/cron-aborted-on-shutdown` branch left untouched (Diego decides whether to delete)

---

## Self-Review Checklist

**Spec coverage:**
- ✅ Add `EventType.CRON_ABORTED` (HIGH priority) → Task 1
- ✅ Wire emission into `cron/scheduler.py` (`flush_inflight_aborts`) → Task 2
- ✅ Payload: job_id, job_name, started_at, aborted_at, reason → Task 2 (also includes cron_started_event_id + elapsed_seconds for correlation)
- ✅ Icon entry in `events/formatting.py` → Task 1
- ✅ TOPIC_ROUTING entry in `events/subscribers/telegram_notifier.py` → Task 1
- ✅ Tests cover gateway_shutdown reason → Task 4
- ✅ Tests cover wallclock_timeout reason → Task 2 step 7 (`test_supports_wallclock_timeout_reason`)
- ✅ Defensive emission pattern (try/except so a broken bus never breaks a cron path) → Task 2 step 9 + Task 4 step 4
- ✅ TDD discipline (failing test → implementation → passing test → commit per task) → every task

**Out-of-scope (not implemented per the user's prompt):**
- Wiring `flush_inflight_aborts("wallclock_timeout")` into the actual wallclock-timeout path in `_process_job`. The current behavior (TimeoutError → cron_failed via on_job_completed) preserves audit pairing. Future work can add cron_aborted alongside cron_failed if operators want abort-vs-failure discrimination.
- Persisting `_in_flight` to disk for SIGKILL/OOM/power-loss recovery. Out of scope; documented in the stale design doc as a future enhancement.

**Type consistency:**
- `_InFlightRecord.started_at: Optional[str]` matches the type used in `flush_inflight_aborts`, `on_job_aborted` payload, and the test assertions.
- `reason: str` is a free-form string in the API but tests cover the two starting values.

**Placeholder scan:**
- No TBD / TODO / "implement later" markers.
- All code blocks are complete.
- All file paths absolute or repo-relative.
- All commit messages drafted.
