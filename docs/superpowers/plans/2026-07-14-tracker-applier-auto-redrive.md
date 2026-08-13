# Tracker-Applier Auto-Re-Drive + Partial-Backlog Alert — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Auto-re-drive backoff-eligible tracker `partial/` intents back through the applier (shipped DISABLED behind a hard-gated feature flag), and always-on alert when the `partial/` queue exceeds a threshold.

**Architecture:** A pure `IntentApplier.redrive_partials()` method (filename `.rdN` marker + exponential backoff, run on the single-writer applier thread) + a subscriber-level feature flag + a read-only edge-triggered `PartialBacklogMonitor` producer (sibling of `ResourcePressureMonitor`) emitting a new `TRACKER_PARTIAL_BACKLOG` event to `jobflow_decisions`.

**Tech Stack:** Python 3.11, stdlib only (`os`, `re`, `time`, `json`, `pathlib`, `dataclasses`); pytest; the Hermes event bus (`events/`).

Spec: `docs/superpowers/specs/2026-07-14-tracker-applier-auto-redrive-design.md`.

## Global Constraints

- **Repo:** all code + tests + this plan live in `~/.hermes/agent-src` (its own local-only git repo). **Author = Diego.** **NEVER push.** Commit only when Diego approves; gitleaks pre-commit runs (PS 5.1: `git commit -F msgfile`).
- **HARD GATE:** the re-drive feature flag `TRACKER_APPLIER_REDRIVE_ENABLED` **defaults `"0"`** and must ship DISABLED until jobflow-api :4100 is restarted with commit `8d7b5f5`'s dist LIVE (verify grep==1 AND running :4100 PID start-time > dist mtime). The **alert (Task 1 + Task 3 + its wiring) is NOT gated.**
- **Single-writer invariant:** `IntentApplier` is single-threaded. `redrive_partials()` MUST be driven only from the dedicated `_applier_poll_loop` thread — never the shared `_subscriber_poll_loop`. The read-only alert `check()` goes in the shared loop.
- **Do NOT** auto-restart the gateway or :4100. Report PID + time; Diego restarts.
- **Test with synthetic files under `tmp_path`**, never the live `~/.hermes/mailbox/tracker/partial/`.
- **Test command (run from `~/.hermes/agent-src`):**
  `PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/ tests/events/ -q`

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `events/schema.py` | new `EventType.TRACKER_PARTIAL_BACKLOG` | 1 |
| `events/subscribers/telegram_notifier.py` | `TOPIC_ROUTING` entry → `jobflow_decisions` | 1 |
| `tests/events/test_schema_contract.py` | enum contract test | 1 |
| `tests/events/test_telegram_routing.py` | routing test | 1 |
| `intent_applier/applier.py` | `redrive_partials()`, marker helpers, `_move_to_partial()` | 2 |
| `tests/intent_applier/test_applier.py` | marker parse + backoff + re-drive tests | 2 |
| `events/producers/partial_backlog_monitor.py` | NEW `PartialBacklogMonitor` producer | 3 |
| `tests/events/producers/test_partial_backlog_monitor.py` | NEW monitor tests | 3 |
| `events/subscribers/tracker_intent_applier.py` | feature flag + `redrive_partials()` wrapper + `tracker_partial_dir()` | 4 |
| `tests/events/subscribers/test_tracker_intent_applier.py` | NEW flag/delegation tests | 4 |
| `events/gateway_integration.py` | construct monitor + getter + redrive hook (applier thread) + alert hook (shared loop) | 5 |
| `tests/events/test_gateway_integration.py` | wiring tests | 5 |

---

## Task 1: `TRACKER_PARTIAL_BACKLOG` event type + routing

**Files:**
- Modify: `events/schema.py` (after the `RESOURCE_PRESSURE` entry, before `def __init__`)
- Modify: `events/subscribers/telegram_notifier.py` (in `TOPIC_ROUTING`, after `'job_high_score'`)
- Test: `tests/events/test_schema_contract.py`, `tests/events/test_telegram_routing.py`

**Interfaces:**
- Produces: `EventType.TRACKER_PARTIAL_BACKLOG` with `type_string == "tracker_partial_backlog"`, `default_priority == Priority.HIGH`; `TOPIC_ROUTING["tracker_partial_backlog"] == "jobflow_decisions"`.

- [ ] **Step 1: Write the failing contract + routing tests**

Append to `tests/events/test_schema_contract.py`:

```python
class TestTrackerPartialBacklogEnumEntry:
    """TRACKER_PARTIAL_BACKLOG is the tracker partial/ pileup early-warning
    (2026-07-14; the 07-13 storm's 13 partials sat ~a day unnoticed). Emitted by
    events.producers.partial_backlog_monitor.PartialBacklogMonitor and routed to
    jobflow_decisions (the human-action lane). Must remain a first-class EventType."""

    def test_enum_entry_exists(self):
        assert hasattr(EventType, "TRACKER_PARTIAL_BACKLOG")

    def test_type_string_is_stable(self):
        assert EventType.TRACKER_PARTIAL_BACKLOG.type_string == "tracker_partial_backlog"

    def test_default_priority_is_high(self):
        assert EventType.TRACKER_PARTIAL_BACKLOG.default_priority == Priority.HIGH

    def test_resolvable_from_string(self):
        resolved = EventType.from_string("tracker_partial_backlog")
        assert resolved is EventType.TRACKER_PARTIAL_BACKLOG
```

Append to `tests/events/test_telegram_routing.py`:

```python
def test_tracker_partial_backlog_routes_to_jobflow_decisions():
    # The partial-backlog alert (2026-07-14) is a human-action signal: an
    # operator must re-drive or investigate a growing partial/ queue — same lane
    # as approvals/apply-packets.
    from events.subscribers.telegram_notifier import TOPIC_ROUTING
    assert TOPIC_ROUTING["tracker_partial_backlog"] == "jobflow_decisions"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/test_schema_contract.py::TestTrackerPartialBacklogEnumEntry tests/events/test_telegram_routing.py::test_tracker_partial_backlog_routes_to_jobflow_decisions -q`
Expected: FAIL (`AttributeError: TRACKER_PARTIAL_BACKLOG` / `KeyError: 'tracker_partial_backlog'`).

- [ ] **Step 3: Add the EventType enum entry**

In `events/schema.py`, immediately after the `RESOURCE_PRESSURE = ("resource_pressure", Priority.HIGH)` line (currently line 307) and before `def __init__`, insert:

```python

    # Tracker-intent-applier partial-backlog early-warning — added 2026-07-14.
    # On 2026-07-13 thirteen APPROVAL_INTENT partials piled up in
    # ~/.hermes/mailbox/tracker/partial/ and sat ~a day unnoticed. A partial is
    # an intent whose pipeline.json write succeeded but whose Postgres mirror
    # (:4100 step 4) did not; the idempotency key is unburned so it stays
    # re-drivable. Emitted by events.producers.partial_backlog_monitor.
    # PartialBacklogMonitor, which read-only counts partial/ on the shared
    # subscriber poll loop and fires on the rising edge of count > threshold
    # (default 3). HIGH so it survives significant_only / digest_only verbosity;
    # routed to jobflow_decisions (the human-action lane). Payload:
    #   count (int)                — partial *_INTENT_*.json files right now
    #   threshold (int)            — the alert threshold that was crossed
    #   oldest_age_seconds (float) — age of the oldest partial (entered-partial mtime)
    #   capped_count (int)         — number of job IDs in sample_job_ids
    #   sample_job_ids (list[str]) — up to SAMPLE_CAP job IDs for triage
    TRACKER_PARTIAL_BACKLOG = ("tracker_partial_backlog", Priority.HIGH)
```

- [ ] **Step 4: Add the TOPIC_ROUTING entry**

In `events/subscribers/telegram_notifier.py`, in the `TOPIC_ROUTING` dict immediately after the `'job_high_score': 'jobflow_decisions',` line (currently line 58), insert:

```python
    # Tracker partial-backlog alert (2026-07-14) — operator must re-drive or
    # investigate a growing partial/ queue; same human-action lane as approvals.
    'tracker_partial_backlog': 'jobflow_decisions',
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/test_schema_contract.py::TestTrackerPartialBacklogEnumEntry tests/events/test_telegram_routing.py::test_tracker_partial_backlog_routes_to_jobflow_decisions -q`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
git -C ~/.hermes/agent-src add events/schema.py events/subscribers/telegram_notifier.py tests/events/test_schema_contract.py tests/events/test_telegram_routing.py
git -C ~/.hermes/agent-src commit -F <msgfile>
```
Message: `feat(events): add TRACKER_PARTIAL_BACKLOG event type + jobflow_decisions routing`

---

## Task 2: `IntentApplier.redrive_partials()` + marker helpers + `_move_to_partial()`

**Files:**
- Modify: `intent_applier/applier.py`
- Test: `tests/intent_applier/test_applier.py`

**Interfaces:**
- Produces (used by Task 4): `IntentApplier.redrive_partials() -> dict[str, str]` (filename → `"redriven"|"waiting"|"capped"`); constructor kwargs `redrive_base_backoff: float = 120.0`, `redrive_multiplier: float = 2.0`, `redrive_max_backoff: float = 1800.0`, `max_redrive_attempts: int = 5`.
- Internal: `_parse_redrive_attempt(path: Path) -> int`, `_bump_redrive_marker(name: str, new_n: int) -> str`, `_move_to_partial(src: Path) -> Path`.

- [ ] **Step 1: Write the failing marker-parse + backoff + re-drive tests**

In `tests/intent_applier/test_applier.py`, add `import os` and `import time` at the top (alongside `import json`), and append:

```python
def _write_partial(partial_dir: Path, name: str, payload: dict, age_seconds: float) -> Path:
    """Write a synthetic partial intent whose mtime is ``age_seconds`` in the past."""
    partial_dir.mkdir(parents=True, exist_ok=True)
    p = partial_dir / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    past = time.time() - age_seconds
    os.utime(p, (past, past))
    return p


class TestRedriveMarkerParsing:
    def test_no_marker_is_attempt_zero(self, applier):
        a, _j, _m = applier
        assert a._parse_redrive_attempt(Path("20260713T1_APPROVAL_INTENT_main.json")) == 0

    def test_rd1_marker_parses(self, applier):
        a, _j, _m = applier
        assert a._parse_redrive_attempt(Path("x_INTENT_main.rd1.json")) == 1

    def test_rd12_marker_parses(self, applier):
        a, _j, _m = applier
        assert a._parse_redrive_attempt(Path("x_INTENT_main.rd12.json")) == 12

    def test_bump_from_zero_appends_rd1(self, applier):
        a, _j, _m = applier
        assert a._bump_redrive_marker("x_INTENT_main.json", 1) == "x_INTENT_main.rd1.json"

    def test_bump_replaces_existing_marker(self, applier):
        a, _j, _m = applier
        assert a._bump_redrive_marker("x_INTENT_main.rd1.json", 2) == "x_INTENT_main.rd2.json"


class TestRedrivePartials:
    def test_eligible_partial_moves_to_inbox_with_bumped_marker(self, mailbox, applier):
        a, _j, _m = applier
        # N=0 -> backoff 120s; age 200s -> eligible.
        _write_partial(mailbox["partial"], "20260713T1_APPROVAL_INTENT_main.json",
                       VALID_INTENT_PAYLOAD, age_seconds=200)
        result = a.redrive_partials()
        assert result == {"20260713T1_APPROVAL_INTENT_main.json": "redriven"}
        assert not (mailbox["partial"] / "20260713T1_APPROVAL_INTENT_main.json").exists()
        assert (mailbox["inbox"] / "20260713T1_APPROVAL_INTENT_main.rd1.json").exists()

    def test_not_yet_eligible_partial_stays(self, mailbox, applier):
        a, _j, _m = applier
        # N=0 -> backoff 120s; age 30s -> too young.
        _write_partial(mailbox["partial"], "20260713T2_APPROVAL_INTENT_main.json",
                       VALID_INTENT_PAYLOAD, age_seconds=30)
        result = a.redrive_partials()
        assert result == {"20260713T2_APPROVAL_INTENT_main.json": "waiting"}
        assert (mailbox["partial"] / "20260713T2_APPROVAL_INTENT_main.json").exists()
        assert list(mailbox["inbox"].glob("*")) == []

    def test_backoff_grows_with_attempt(self, mailbox, applier):
        a, _j, _m = applier
        # N=1 -> backoff 240s; age 200s -> still waiting (proves 2^N spacing).
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd1.json",
                       VALID_INTENT_PAYLOAD, age_seconds=200)
        assert a.redrive_partials() == {"x_APPROVAL_INTENT_main.rd1.json": "waiting"}

    def test_capped_partial_is_left_in_place(self, mailbox, applier):
        a, _j, _m = applier
        # N=5 == max_redrive_attempts -> capped, even if ancient.
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=99999)
        result = a.redrive_partials()
        assert result == {"x_APPROVAL_INTENT_main.rd5.json": "capped"}
        assert (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert list(mailbox["inbox"].glob("*")) == []

    def test_move_to_partial_resets_mtime(self, mailbox, applier):
        a, jobops, _m = applier
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("boom")
        # Write intent with an OLD mtime; landing in partial must reset it to now,
        # else the per-attempt backoff would measure from intent creation time.
        f = write_intent(mailbox["inbox"], "20260713T3_APPROVAL_INTENT_main.json",
                         VALID_INTENT_PAYLOAD)
        old = time.time() - 5000
        os.utime(f, (old, old))
        assert a.apply_one(f) == "partial"
        landed = mailbox["partial"] / "20260713T3_APPROVAL_INTENT_main.json"
        assert landed.exists()
        assert time.time() - landed.stat().st_mtime < 60  # reset to ~now

    def test_redriven_intent_reapplies_end_to_end(self, mailbox, applier):
        a, jobops, _m = applier
        # 1) transient failure -> partial (key unburned, mtime reset to now).
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("read timeout")
        f = write_intent(mailbox["inbox"], "20260713T4_APPROVAL_INTENT_main.json",
                         VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "partial"
        key = VALID_INTENT_PAYLOAD["idempotency_key"]
        assert not a.idempotency.is_applied(key)
        # 2) age the partial past its 120s backoff, then re-drive.
        landed = mailbox["partial"] / "20260713T4_APPROVAL_INTENT_main.json"
        past = time.time() - 200
        os.utime(landed, (past, past))
        assert a.redrive_partials() == {"20260713T4_APPROVAL_INTENT_main.json": "redriven"}
        # 3) :4100 recovers; the re-driven file re-applies on the next scan.
        jobops.post_legacy_stage.side_effect = None
        jobops.post_legacy_stage.return_value = {"success": True}
        outcomes = a.scan_inbox()
        assert outcomes == {"20260713T4_APPROVAL_INTENT_main.rd1.json": "applied"}
        assert a.idempotency.is_applied(key)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/test_applier.py::TestRedriveMarkerParsing tests/intent_applier/test_applier.py::TestRedrivePartials -q`
Expected: FAIL (`AttributeError: 'IntentApplier' object has no attribute '_parse_redrive_attempt'`).

- [ ] **Step 3: Add imports + module-level marker regex**

In `intent_applier/applier.py`, extend the stdlib imports (currently `import json` / `import logging` / `import traceback`) to also include `os`, `re`, `time` (final block, alphabetized):

```python
import json
import logging
import os
import re
import time
import traceback
```

After the `PROTECTED_STAGES = {...}` line (currently line 54), add:

```python

# Re-drive attempt marker: original intent stems never end in ``.rd<digits>``
# (they end in the poster tag, e.g. ``_main`` or a job8 hex), so a trailing
# ``.rdN`` on the stem is unambiguously the applier's own re-drive counter.
_REDRIVE_MARKER_RE = re.compile(r"\.rd(\d+)$")
```

- [ ] **Step 4: Add the constructor params**

In `IntentApplier.__init__`, add these four keyword params at the end of the signature (after `resume_full: Optional[Callable[[str, dict], object]] = None,`):

```python
        redrive_base_backoff: float = 120.0,
        redrive_multiplier: float = 2.0,
        redrive_max_backoff: float = 1800.0,
        max_redrive_attempts: int = 5,
```

and store them in the body, immediately after `self.resume_full = resume_full`:

```python
        self.redrive_base_backoff = redrive_base_backoff
        self.redrive_multiplier = redrive_multiplier
        self.redrive_max_backoff = redrive_max_backoff
        self.max_redrive_attempts = max_redrive_attempts
```

- [ ] **Step 5: Route both partial branches through `_move_to_partial`**

In `apply_one`, both partial branches currently end with the identical line
`            self._move_to(intent_path, self.partial_dir)`. Replace **both** occurrences
(the `except CircuitBreakerOpen:` branch and the `except JobOpsClientTransientError as exc:` branch) with:

```python
            self._move_to_partial(intent_path)
```

- [ ] **Step 6: Add the helper + re-drive methods**

In `intent_applier/applier.py`, replace the existing `_move_to` method (currently the last method, lines 316-320) with `_move_to` **unchanged plus** the new methods below:

```python
    def _move_to(self, src: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        src.replace(dest)
        return dest

    def _move_to_partial(self, src: Path) -> Path:
        """Move an intent into partial/ and stamp its mtime to NOW.

        rename/replace PRESERVE mtime, so without this the file's mtime would be
        the intent's *creation* time and the per-attempt exponential backoff in
        redrive_partials() would never space retries. Stamping on landing makes
        partial mtime mean "when it last entered partial/".
        """
        dest = self._move_to(src, self.partial_dir)
        os.utime(dest, None)
        return dest

    def _parse_redrive_attempt(self, path: Path) -> int:
        """Parse the re-drive attempt count N from a ``.rdN`` filename marker.

        ``Path.stem`` has already stripped ``.json``. No marker => attempt 0.
        """
        m = _REDRIVE_MARKER_RE.search(path.stem)
        return int(m.group(1)) if m else 0

    def _bump_redrive_marker(self, name: str, new_n: int) -> str:
        """Return ``name`` (a ``*.json`` filename) with any ``.rdN`` marker
        replaced by ``.rd{new_n}``."""
        stem = name[:-5] if name.endswith(".json") else name
        stem = _REDRIVE_MARKER_RE.sub("", stem)
        return f"{stem}.rd{new_n}.json"

    def redrive_partials(self) -> dict[str, str]:
        """Move backoff-eligible partials back to inbox/ for reprocessing.

        Pure filesystem logic; ALWAYS acts (the feature flag lives at the
        subscriber layer). MUST be called on the single-writer applier thread —
        it shares _move_to/glob semantics with scan_inbox and is not race-free
        against a concurrent scan.

        For each ``*_INTENT_*.json`` in partial/:
          * attempt N = ``.rdN`` marker (absent => 0);
          * if N >= max_redrive_attempts => leave in place ("capped") for the
            PartialBacklogMonitor alert (partials are not dead: step-3 succeeded,
            key unburned, still manually re-drivable);
          * else eligible iff ``now - mtime >= min(base * mult**N, max_backoff)``,
            where mtime is the "entered-partial" clock set by _move_to_partial;
          * eligible => rename to ``.rd{N+1}`` and move to inbox/; the next 1s
            scan_inbox re-runs steps 3/3b/4 (key unburned => it re-applies).

        Returns {original_filename: "redriven" | "waiting" | "capped"}.
        """
        results: dict[str, str] = {}
        now = time.time()
        for path in sorted(self.partial_dir.glob("*_INTENT_*.json")):
            n = self._parse_redrive_attempt(path)
            if n >= self.max_redrive_attempts:
                results[path.name] = "capped"
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                # File vanished mid-sweep (raced by another mover) — skip.
                continue
            backoff = min(
                self.redrive_base_backoff * (self.redrive_multiplier ** n),
                self.redrive_max_backoff,
            )
            if age < backoff:
                results[path.name] = "waiting"
                continue
            new_name = self._bump_redrive_marker(path.name, n + 1)
            self.inbox_dir.mkdir(parents=True, exist_ok=True)
            try:
                path.replace(self.inbox_dir / new_name)
            except OSError:
                logger.exception(
                    "intent-applier: failed to re-drive partial %s", path.name
                )
                continue
            results[path.name] = "redriven"
            logger.info(
                "intent-applier: re-driving partial %s -> inbox/%s (attempt %d)",
                path.name, new_name, n + 1,
            )
        return results
```

- [ ] **Step 7: Run the new tests, then the whole applier suite**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/test_applier.py -q`
Expected: PASS (all existing + 11 new tests). The existing `TestPartialDoesNotBurnIdempotencyKey` and `TestApplierFailures` still pass (the `_move_to_partial` swap is behavior-preserving except for the mtime stamp).

- [ ] **Step 8: Commit**

```bash
git -C ~/.hermes/agent-src add intent_applier/applier.py tests/intent_applier/test_applier.py
git -C ~/.hermes/agent-src commit -F <msgfile>
```
Message: `feat(applier): redrive_partials() with .rdN marker + backoff; utime on partial landing`

---

## Task 3: `PartialBacklogMonitor` producer

**Files:**
- Create: `events/producers/partial_backlog_monitor.py`
- Test: `tests/events/producers/test_partial_backlog_monitor.py`

**Interfaces:**
- Consumes: `EventType.TRACKER_PARTIAL_BACKLOG` (Task 1); `events.bus.EventBus`.
- Produces (used by Task 5): `PartialBacklogMonitor(bus, *, partial_dir=None, sampler=None, clock=None, alert_threshold=3, re_alert_cooldown_seconds=900.0)`; `.check() -> Optional[str]`; `.evaluate(sample, now) -> Optional[str]`; `PartialBacklogSample(count, oldest_age_seconds, sample_job_ids)`; `sample_partial_backlog(partial_dir, now, *, sample_cap=10) -> PartialBacklogSample`.

- [ ] **Step 1: Write the failing monitor tests**

Create `tests/events/producers/test_partial_backlog_monitor.py`:

```python
"""Tests for events.producers.partial_backlog_monitor — PartialBacklogMonitor.

Mirrors tests/events/producers/test_resource_monitor.py: the sampler + clock are
injected so the edge/cooldown core is tested deterministically — no real mailbox,
no sleeps. Added 2026-07-14 after the 07-13 partial pileup sat ~a day un-alerted.
"""
import json
import os
import time

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.partial_backlog_monitor import (
    PartialBacklogMonitor,
    PartialBacklogSample,
    sample_partial_backlog,
)


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def make_sample(count=5, oldest_age_seconds=300.0, sample_job_ids=None):
    return PartialBacklogSample(
        count=count,
        oldest_age_seconds=oldest_age_seconds,
        sample_job_ids=sample_job_ids or [f"job-{i}" for i in range(min(count, 3))],
    )


def _backlog_events(bus):
    return bus.query(event_type=EventType.TRACKER_PARTIAL_BACKLOG)


class TestNoFalsePositive:
    def test_below_threshold_emits_nothing(self, bus):
        m = PartialBacklogMonitor(bus)
        assert m.evaluate(make_sample(count=2), now=0.0) is None
        assert _backlog_events(bus) == []

    def test_at_threshold_does_not_emit(self, bus):
        # Strictly greater-than: exactly 3 is not yet a backlog.
        m = PartialBacklogMonitor(bus)
        assert m.evaluate(make_sample(count=3), now=0.0) is None
        assert _backlog_events(bus) == []


class TestRisingEdge:
    def test_above_threshold_emits(self, bus):
        m = PartialBacklogMonitor(bus)
        assert m.evaluate(make_sample(count=4), now=0.0)
        assert len(_backlog_events(bus)) == 1

    def test_emitted_event_is_high_priority(self, bus):
        m = PartialBacklogMonitor(bus)
        m.evaluate(make_sample(count=9), now=0.0)
        assert _backlog_events(bus)[0].priority is Priority.HIGH

    def test_source_is_applier(self, bus):
        m = PartialBacklogMonitor(bus)
        m.evaluate(make_sample(count=9), now=0.0)
        assert _backlog_events(bus)[0].source == "tracker-intent-applier"


class TestEdgeTriggerAndCooldown:
    def test_sustained_backlog_emits_once_within_cooldown(self, bus):
        m = PartialBacklogMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert m.evaluate(make_sample(count=6), now=0.0)
        assert m.evaluate(make_sample(count=6), now=60.0) is None
        assert len(_backlog_events(bus)) == 1

    def test_sustained_backlog_re_emits_after_cooldown(self, bus):
        m = PartialBacklogMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert m.evaluate(make_sample(count=6), now=0.0)
        assert m.evaluate(make_sample(count=6), now=901.0)
        assert len(_backlog_events(bus)) == 2

    def test_falling_edge_re_arms(self, bus):
        m = PartialBacklogMonitor(bus, re_alert_cooldown_seconds=900.0)
        assert m.evaluate(make_sample(count=6), now=0.0)
        # Backlog drains to <= threshold: falling edge resets the episode.
        assert m.evaluate(make_sample(count=1), now=60.0) is None
        # New rise fires immediately, NOT gated by the prior cooldown.
        assert m.evaluate(make_sample(count=6), now=120.0)
        assert len(_backlog_events(bus)) == 2


class TestPayload:
    def test_payload_shape(self, bus):
        m = PartialBacklogMonitor(bus)
        m.evaluate(
            make_sample(count=7, oldest_age_seconds=1234.5,
                        sample_job_ids=["a", "b", "c"]),
            now=0.0,
        )
        p = _backlog_events(bus)[0].payload
        assert p["count"] == 7
        assert p["threshold"] == 3
        assert p["oldest_age_seconds"] == pytest.approx(1234.5, abs=0.1)
        assert p["capped_count"] == 3
        assert p["sample_job_ids"] == ["a", "b", "c"]


class TestCheckIntegration:
    def test_check_uses_injected_sampler_and_emits(self, bus):
        m = PartialBacklogMonitor(bus, sampler=lambda: make_sample(count=9))
        assert m.check()
        assert len(_backlog_events(bus)) == 1

    def test_check_noop_when_sampler_returns_none(self, bus):
        m = PartialBacklogMonitor(bus, sampler=lambda: None)
        assert m.check() is None
        assert _backlog_events(bus) == []

    def test_check_swallows_sampler_exceptions(self, bus):
        def boom():
            raise OSError("stat failed")
        m = PartialBacklogMonitor(bus, sampler=boom)
        assert m.check() is None
        assert _backlog_events(bus) == []


class TestRealSampler:
    def test_missing_dir_is_empty_sample(self, tmp_path):
        s = sample_partial_backlog(tmp_path / "nope", now=1000.0)
        assert s.count == 0
        assert s.sample_job_ids == []

    def test_counts_and_samples_job_ids(self, tmp_path):
        partial = tmp_path / "partial"
        partial.mkdir()
        for i in range(4):
            p = partial / f"20260713T10000{i}_APPROVAL_INTENT_main.json"
            p.write_text(json.dumps({"job_id": f"job-{i}"}), encoding="utf-8")
            past = time.time() - (100 + i)
            os.utime(p, (past, past))
        # A non-intent file must be ignored (shared mailbox).
        (partial / "note.json").write_text("{}", encoding="utf-8")
        s = sample_partial_backlog(partial, now=time.time())
        assert s.count == 4
        assert set(s.sample_job_ids) == {"job-0", "job-1", "job-2", "job-3"}
        assert s.oldest_age_seconds >= 100

    def test_sample_cap_bounds_job_ids(self, tmp_path):
        partial = tmp_path / "partial"
        partial.mkdir()
        for i in range(15):
            (partial / f"20260713T1000{i:02d}_APPROVAL_INTENT_main.json").write_text(
                json.dumps({"job_id": f"job-{i}"}), encoding="utf-8")
        s = sample_partial_backlog(partial, now=time.time(), sample_cap=10)
        assert s.count == 15
        assert len(s.sample_job_ids) == 10
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/producers/test_partial_backlog_monitor.py -q`
Expected: FAIL (`ModuleNotFoundError: events.producers.partial_backlog_monitor`).

- [ ] **Step 3: Create the producer module**

Create `events/producers/partial_backlog_monitor.py`:

```python
"""PartialBacklogMonitor — emits TRACKER_PARTIAL_BACKLOG when partial/ grows.

Sibling of ResourcePressureMonitor: where that watches host commit/pagefile/disk,
this watches the tracker-intent-applier's partial/ queue
(~/.hermes/mailbox/tracker/partial/). A partial is an intent whose pipeline.json
write (step 3) succeeded but whose Postgres mirror (step 4, :4100) did not; the
idempotency key is unburned so it stays re-drivable. On 2026-07-13 thirteen such
partials piled up and sat ~a day UNNOTICED. This monitor closes that gap.

ALWAYS ON — independent of the auto-re-drive feature flag. Monitoring must not be
gated on the fix being live; the alert is exactly what tells an operator to act
when auto-re-drive is off or has hit the attempt cap.

Emission policy (identical to ResourcePressureMonitor): edge-triggered with a
re-arm cooldown — fire once on the rising edge of "count > threshold", stay quiet
while the backlog persists, re-ping every re_alert_cooldown_seconds if sustained,
and re-arm on the falling edge (count drops to <= threshold).

Read-only: counts files and best-effort reads job_id for a triage sample. Never
mutates the mailbox, so it is safe off the single-writer applier thread — it is
called from the SHARED subscriber poll loop, next to the resource monitor.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from events.bus import EventBus
from events.schema import EventType

logger = logging.getLogger(__name__)

DEFAULT_ALERT_THRESHOLD = 3               # fire when partial count strictly exceeds this
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 900.0  # re-ping a sustained backlog every 15 min
SAMPLE_CAP = 10                           # cap sample_job_ids so the payload stays small

_INTENT_GLOB = "*_INTENT_*.json"


@dataclass(frozen=True)
class PartialBacklogSample:
    """A point-in-time reading of the tracker partial/ queue."""

    count: int
    oldest_age_seconds: float
    sample_job_ids: List[str] = field(default_factory=list)


def _read_job_id(path: Path) -> str:
    """Best-effort top-level ``job_id`` from an intent file; else the filename stem."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        jid = data.get("job_id")
        if isinstance(jid, str) and jid:
            return jid
    except Exception:
        pass
    return path.stem


def sample_partial_backlog(
    partial_dir: Path, now: float, *, sample_cap: int = SAMPLE_CAP,
) -> PartialBacklogSample:
    """Count partial *_INTENT_*.json files and sample up to ``sample_cap`` job IDs.

    Missing dir => empty sample (count 0). Best-effort: an unreadable/corrupt file
    still counts, and its job-id sample falls back to the filename stem.
    """
    if not partial_dir.exists():
        return PartialBacklogSample(count=0, oldest_age_seconds=0.0, sample_job_ids=[])
    paths = sorted(partial_dir.glob(_INTENT_GLOB))
    oldest_age = 0.0
    sample_ids: List[str] = []
    for p in paths:
        try:
            age = now - p.stat().st_mtime
            if age > oldest_age:
                oldest_age = age
        except OSError:
            continue
        if len(sample_ids) < sample_cap:
            sample_ids.append(_read_job_id(p))
    return PartialBacklogSample(
        count=len(paths), oldest_age_seconds=oldest_age, sample_job_ids=sample_ids,
    )


class PartialBacklogMonitor:
    """Counts partial/ and emits TRACKER_PARTIAL_BACKLOG on the rising edge.

    Call check() every ~60s from the shared subscriber poll loop. The sampler and
    clock are injectable so the edge/cooldown core is fully testable without a
    real mailbox or sleeps.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        partial_dir: Optional[Path] = None,
        sampler: Optional[Callable[[], Optional[PartialBacklogSample]]] = None,
        clock: Optional[Callable[[], float]] = None,
        alert_threshold: int = DEFAULT_ALERT_THRESHOLD,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
    ):
        self.bus = bus
        self.partial_dir = Path(partial_dir) if partial_dir is not None else None
        self._sampler = sampler or self._default_sampler
        self._clock = clock or time.monotonic
        self.alert_threshold = alert_threshold
        self.re_alert_cooldown_seconds = re_alert_cooldown_seconds
        # Edge-trigger state.
        self._in_backlog: bool = False
        self._last_emit: Optional[float] = None

    def _default_sampler(self) -> Optional[PartialBacklogSample]:
        if self.partial_dir is None:
            return None
        # Wall-clock for file ages; the monotonic clock() drives edge/cooldown.
        return sample_partial_backlog(self.partial_dir, time.time())

    def check(self) -> Optional[str]:
        """Sample, evaluate, emit on a rising edge. Returns event_id or None.

        Swallows sampler failures: a filesystem read blowing up must never crash
        the gateway poll loop.
        """
        try:
            sample = self._sampler()
        except Exception:
            logger.exception("PartialBacklogMonitor: sampler raised")
            return None
        if sample is None:
            return None
        return self.evaluate(sample, self._clock())

    def evaluate(self, sample: PartialBacklogSample, now: float) -> Optional[str]:
        """Evaluate one sample at monotonic ``now``; emit on rising edge.

        Pure given (sample, now) + internal edge state — the testable core.
        """
        if sample.count <= self.alert_threshold:
            # Backlog clear (or never present): re-arm so the next rise fires now.
            self._in_backlog = False
            return None

        rising_edge = not self._in_backlog
        cooldown_elapsed = (
            self._last_emit is None
            or (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        self._in_backlog = True
        if not (rising_edge or cooldown_elapsed):
            return None

        self._last_emit = now
        return self._emit(sample)

    def _emit(self, sample: PartialBacklogSample) -> str:
        payload = {
            "count": sample.count,
            "threshold": self.alert_threshold,
            "oldest_age_seconds": round(sample.oldest_age_seconds, 1),
            "capped_count": len(sample.sample_job_ids),
            "sample_job_ids": list(sample.sample_job_ids),
        }
        logger.warning(
            "Tracker partial backlog: %d partial intents (> %d) — oldest %.0fs; sample=%s",
            sample.count, self.alert_threshold, sample.oldest_age_seconds,
            payload["sample_job_ids"],
        )
        return self.bus.emit(
            event_type=EventType.TRACKER_PARTIAL_BACKLOG,
            source="tracker-intent-applier",
            payload=payload,
            tags=["tracker", "partial", "backlog"],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/producers/test_partial_backlog_monitor.py -q`
Expected: PASS (16 tests).

- [ ] **Step 5: Commit**

```bash
git -C ~/.hermes/agent-src add events/producers/partial_backlog_monitor.py tests/events/producers/test_partial_backlog_monitor.py
git -C ~/.hermes/agent-src commit -F <msgfile>
```
Message: `feat(events): PartialBacklogMonitor producer — edge-triggered partial/ alert`

---

## Task 4: Subscriber feature flag + `redrive_partials()` wrapper + `tracker_partial_dir()`

**Files:**
- Modify: `events/subscribers/tracker_intent_applier.py`
- Test: `tests/events/subscribers/test_tracker_intent_applier.py`

**Interfaces:**
- Consumes: `IntentApplier.redrive_partials()` + backoff kwargs (Task 2).
- Produces (used by Task 5): `TrackerIntentApplierSubscriber.redrive_partials() -> int`; module fn `tracker_partial_dir() -> Path`; `_redrive_enabled_from_env() -> bool`.

- [ ] **Step 1: Write the failing flag/delegation tests**

Create `tests/events/subscribers/test_tracker_intent_applier.py`:

```python
"""Tests for the tracker-intent-applier subscriber's re-drive feature flag.

The flag (TRACKER_APPLIER_REDRIVE_ENABLED, default OFF) is the HARD GATE: auto-
re-drive must stay disabled until jobflow-api :4100 runs commit 8d7b5f5's dist
(idempotent no-op guard live). IntentApplier.redrive_partials() is pure/always-
acts; the subscriber wrapper is where the flag lives.
"""
from unittest.mock import MagicMock

import pytest

from events.bus import EventBus
from events.subscribers.tracker_intent_applier import (
    TrackerIntentApplierSubscriber,
    _redrive_enabled_from_env,
    tracker_partial_dir,
)


@pytest.fixture
def subscriber(tmp_path):
    bus = EventBus(db_path=tmp_path / "events" / "event_bus.db")
    return TrackerIntentApplierSubscriber(bus)


class TestRedriveFlagParsing:
    def test_default_is_disabled(self, monkeypatch):
        monkeypatch.delenv("TRACKER_APPLIER_REDRIVE_ENABLED", raising=False)
        assert _redrive_enabled_from_env() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy_values_enable(self, monkeypatch, val):
        monkeypatch.setenv("TRACKER_APPLIER_REDRIVE_ENABLED", val)
        assert _redrive_enabled_from_env() is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "", "off", "garbage"])
    def test_other_values_stay_disabled(self, monkeypatch, val):
        monkeypatch.setenv("TRACKER_APPLIER_REDRIVE_ENABLED", val)
        assert _redrive_enabled_from_env() is False


class TestRedriveDelegation:
    def test_flag_off_is_noop(self, subscriber):
        subscriber._redrive_enabled = False
        subscriber._applier = MagicMock()
        assert subscriber.redrive_partials() == 0
        subscriber._applier.redrive_partials.assert_not_called()

    def test_flag_on_calls_applier(self, subscriber):
        subscriber._redrive_enabled = True
        subscriber._applier = MagicMock()
        subscriber._applier.redrive_partials.return_value = {
            "a_INTENT_main.json": "redriven",
            "b_INTENT_main.json": "waiting",
        }
        assert subscriber.redrive_partials() == 1
        subscriber._applier.redrive_partials.assert_called_once()

    def test_flag_on_but_applier_not_built_is_noop(self, subscriber):
        subscriber._redrive_enabled = True
        subscriber._applier = None
        assert subscriber.redrive_partials() == 0


class TestTrackerPartialDir:
    def test_ends_in_partial(self):
        assert tracker_partial_dir().name == "partial"
        assert tracker_partial_dir().parent.name == "tracker"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/subscribers/test_tracker_intent_applier.py -q`
Expected: FAIL (`ImportError: cannot import name '_redrive_enabled_from_env'`).

- [ ] **Step 3: Add module helpers + `tracker_partial_dir`**

In `events/subscribers/tracker_intent_applier.py`, after the `logger = logging.getLogger(__name__)` line, add:

```python

_TRUTHY = {"1", "true", "yes", "on"}


def _redrive_enabled_from_env() -> bool:
    """The HARD-GATE feature flag. Default OFF until :4100 runs 8d7b5f5's dist.

    Auto-re-drive must stay disabled until jobflow-api :4100 is restarted with
    the idempotent no-op guard + lock/statement timeouts LIVE, or re-driving an
    already-applied intent could double-write / re-fire notifications.
    """
    return os.environ.get(
        "TRACKER_APPLIER_REDRIVE_ENABLED", "0"
    ).strip().lower() in _TRUTHY


def _redrive_config_from_env() -> dict:
    """Backoff/attempt tuning for IntentApplier.redrive_partials(), env-overridable."""
    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return default

    def _i(name: str, default: int) -> int:
        try:
            return int(os.environ[name])
        except (KeyError, ValueError):
            return default

    return {
        "redrive_base_backoff": _f("TRACKER_APPLIER_REDRIVE_BASE_SECONDS", 120.0),
        "redrive_multiplier": _f("TRACKER_APPLIER_REDRIVE_MULTIPLIER", 2.0),
        "redrive_max_backoff": _f("TRACKER_APPLIER_REDRIVE_MAX_BACKOFF_SECONDS", 1800.0),
        "max_redrive_attempts": _i("TRACKER_APPLIER_REDRIVE_MAX_ATTEMPTS", 5),
    }


def tracker_partial_dir() -> Path:
    """The tracker mailbox partial/ dir — read-only counted by PartialBacklogMonitor."""
    return _tracker_mailbox(_hermes_root())["partial"]
```

(`_tracker_mailbox`, `_hermes_root`, `os`, and `Path` are already imported in this module.)

- [ ] **Step 4: Read the flag in `__init__` and pass config in `startup()`**

In `TrackerIntentApplierSubscriber.__init__`, after `self._applier: IntentApplier | None = None`, add:

```python
        self._redrive_enabled = _redrive_enabled_from_env()
        self._redrive_config = _redrive_config_from_env()
```

In `startup()`, change the `IntentApplier(...)` construction to spread the config and log the flag:

```python
        self._applier = IntentApplier(
            inbox_dir=self._mailbox["inbox"],
            processed_dir=self._mailbox["processed"],
            partial_dir=self._mailbox["partial"],
            dead_letter_dir=self._mailbox["dead_letter"],
            pipeline_manager=PipelineManager(),
            jobops_client=JobOpsClient(base_url=self._jobops_url),
            idempotency=idempotency,
            resume_full=_resume_full,
            **self._redrive_config,
        )
        logger.info(
            "tracker-intent-applier: ready (inbox=%s, jobops=%s, redrive_enabled=%s)",
            self._mailbox["inbox"],
            self._jobops_url,
            self._redrive_enabled,
        )
```

- [ ] **Step 5: Add the `redrive_partials()` wrapper**

Add this method to `TrackerIntentApplierSubscriber` (e.g. after `poll()`):

```python
    def redrive_partials(self) -> int:
        """Flag-gated wrapper: re-drive eligible partials iff the feature is enabled.

        The HARD GATE. Returns the number of partials re-driven this sweep (0 when
        disabled or nothing eligible). IntentApplier.redrive_partials() itself is
        pure/always-acts; THIS method is the feature flag — it must stay OFF until
        :4100 runs 8d7b5f5's dist (idempotent no-op guard live).
        """
        if not self._redrive_enabled or self._applier is None:
            return 0
        results = self._applier.redrive_partials()
        redriven = sum(1 for v in results.values() if v == "redriven")
        if redriven:
            logger.info("tracker-intent-applier: re-drove %d partial(s)", redriven)
        return redriven
```

- [ ] **Step 6: Run the new tests, then the full applier + subscriber suites**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/subscribers/test_tracker_intent_applier.py tests/intent_applier/ -q`
Expected: PASS (11 new + all existing).

- [ ] **Step 7: Commit**

```bash
git -C ~/.hermes/agent-src add events/subscribers/tracker_intent_applier.py tests/events/subscribers/test_tracker_intent_applier.py
git -C ~/.hermes/agent-src commit -F <msgfile>
```
Message: `feat(applier): TRACKER_APPLIER_REDRIVE_ENABLED flag + redrive delegation + tracker_partial_dir`

---

## Task 5: Gateway wiring (monitor construction + getter + redrive hook + alert hook)

**Files:**
- Modify: `events/gateway_integration.py`
- Test: `tests/events/test_gateway_integration.py`

**Interfaces:**
- Consumes: `PartialBacklogMonitor` (Task 3), `TrackerIntentApplierSubscriber.redrive_partials()` + `tracker_partial_dir()` (Task 4).
- Produces: `gi.get_partial_backlog_monitor() -> Optional[PartialBacklogMonitor]`; module globals `_partial_backlog_monitor`, constants `REDRIVE_INTERVAL_SECONDS = 60`, `PARTIAL_BACKLOG_CHECK_INTERVAL_SECONDS = 60`.

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/events/test_gateway_integration.py` (the `PartialBacklogMonitor` import goes at the top with the other `events.producers` import):

```python
from events.producers.partial_backlog_monitor import PartialBacklogMonitor  # noqa: E402


class TestPartialBacklogWiring:
    """The PartialBacklogMonitor (2026-07-14 partial-pileup remediation) must be
    constructed at startup and sampled by the SHARED subscriber poll loop; the
    auto-re-drive must be driven from the DEDICATED applier thread (single-writer
    invariant), gated at 60s. Verified via source introspection, like the
    ResourcePressureMonitor wiring above (real startup() is heavy/timing-sensitive).
    """

    def test_getter_returns_module_global(self):
        assert gi.get_partial_backlog_monitor() is gi._partial_backlog_monitor

    def test_startup_constructs_partial_backlog_monitor(self):
        src = inspect.getsource(gi.startup)
        assert "PartialBacklogMonitor(" in src

    def test_shared_loop_checks_partial_backlog(self):
        src = inspect.getsource(gi._subscriber_poll_loop)
        assert "_partial_backlog_monitor.check()" in src

    def test_applier_loop_redrives_on_its_own_thread(self):
        # Re-drive MUST be on the single-writer applier thread, gated to 60s.
        src = inspect.getsource(gi._applier_poll_loop)
        assert "redrive_partials()" in src
        assert "REDRIVE_INTERVAL_SECONDS" in src

    def test_shared_loop_does_not_redrive(self):
        # Guard the single-writer invariant: the shared loop must NOT re-drive
        # (that would race scan_inbox on the applier thread).
        src = inspect.getsource(gi._subscriber_poll_loop)
        assert "redrive_partials()" not in src
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/test_gateway_integration.py::TestPartialBacklogWiring -q`
Expected: FAIL (`AttributeError: module 'events.gateway_integration' has no attribute 'get_partial_backlog_monitor'`).

- [ ] **Step 3: Add imports + constants**

In `events/gateway_integration.py`, after `from events.producers.resource_monitor import ResourcePressureMonitor` (line 25), add:

```python
from events.producers.partial_backlog_monitor import PartialBacklogMonitor
```

Change the tracker-applier import (line 37) to also bring in the partial-dir helper:

```python
from events.subscribers.tracker_intent_applier import (
    TrackerIntentApplierSubscriber,
    tracker_partial_dir,
)
```

After `APPLIER_POLL_INTERVAL_SECONDS = 1` (line 78), add:

```python
# Auto-re-drive eligible partials at most once/min, on the dedicated applier
# thread (single-writer). Flag-gated at the subscriber (default off — the :4100
# hard gate). See docs/superpowers/specs/2026-07-14-tracker-applier-auto-redrive-design.md.
REDRIVE_INTERVAL_SECONDS = 60
# Read-only partial/ backlog count on the shared subscriber loop, once/min.
PARTIAL_BACKLOG_CHECK_INTERVAL_SECONDS = 60
```

- [ ] **Step 4: Add the module global + startup construction + getter**

After `_resource_monitor: Optional[ResourcePressureMonitor] = None` (line 83), add:

```python
_partial_backlog_monitor: Optional[PartialBacklogMonitor] = None
```

In `startup()`, add `_partial_backlog_monitor` to the `global` declaration (line 108), and after
`_resource_monitor = ResourcePressureMonitor(_bus)` (line 119) add:

```python
    _partial_backlog_monitor = PartialBacklogMonitor(
        _bus, partial_dir=tracker_partial_dir(),
    )
```

After the `get_resource_monitor()` function (line 316-318), add:

```python


def get_partial_backlog_monitor() -> Optional[PartialBacklogMonitor]:
    """Get the tracker partial-backlog monitor (counts mailbox/tracker/partial/)."""
    return _partial_backlog_monitor
```

- [ ] **Step 5: Add the shared-loop alert check**

In `_subscriber_poll_loop`, add a timer var next to `last_resource_check` (after line 535):

```python
    last_partial_backlog_check: float = time.monotonic()
```

Immediately after the resource-pressure `if _resource_monitor and now - last_resource_check >= 60:` block (after line 728, before the mailbox scan block), add:

```python
            # Tracker partial-backlog check every 60s — counts
            # mailbox/tracker/partial/ and emits TRACKER_PARTIAL_BACKLOG on the
            # rising edge of count > threshold (2026-07-14; the 07-13 pileup sat
            # ~a day unnoticed). Read-only, so it runs here in the shared loop
            # rather than the latency-sensitive applier thread.
            if _partial_backlog_monitor and now - last_partial_backlog_check >= PARTIAL_BACKLOG_CHECK_INTERVAL_SECONDS:
                try:
                    _partial_backlog_monitor.check()
                except Exception:
                    logger.exception("Partial backlog check failed")
                last_partial_backlog_check = now
```

- [ ] **Step 6: Add the applier-thread redrive hook**

In `_applier_poll_loop`, after the `interval = ...` block and before `while not _stop_event.is_set():` (after line 851), add:

```python
    # Skip the boot tick (reconnect-storm window); first re-drive one interval in.
    last_redrive = time.monotonic()
```

Inside the loop, replace the current body:

```python
    while not _stop_event.is_set():
        try:
            if _applier_subscriber is not None:
                _applier_subscriber.poll()
        except Exception:
            logger.exception("tracker-intent-applier dedicated poll failed")
        _stop_event.wait(timeout=interval)
```

with:

```python
    while not _stop_event.is_set():
        try:
            if _applier_subscriber is not None:
                _applier_subscriber.poll()
        except Exception:
            logger.exception("tracker-intent-applier dedicated poll failed")
        # Auto-re-drive eligible partials at most once/min, on THIS single-writer
        # thread (never the shared loop) so it can't race scan_inbox's
        # is_applied/mark_applied/_move_to. Flag-gated inside the subscriber
        # (TRACKER_APPLIER_REDRIVE_ENABLED, default off — the :4100 hard gate).
        now = time.monotonic()
        if _applier_subscriber is not None and now - last_redrive >= REDRIVE_INTERVAL_SECONDS:
            try:
                _applier_subscriber.redrive_partials()
            except Exception:
                logger.exception("tracker-intent-applier redrive failed")
            last_redrive = now
        _stop_event.wait(timeout=interval)
```

- [ ] **Step 7: Run the wiring tests, then the full gateway suite**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/events/test_gateway_integration.py -q`
Expected: PASS (5 new + all existing; the real-startup tests still pass because `PartialBacklogMonitor` construction is side-effect-free — it only stores the path, never globs at construction).

- [ ] **Step 8: Full regression + commit**

Run: `cd ~/.hermes/agent-src && PYTHONPATH=$(pwd) python -m pytest tests/intent_applier/ tests/events/ -q`
Expected: PASS (whole suite green).

```bash
git -C ~/.hermes/agent-src add events/gateway_integration.py tests/events/test_gateway_integration.py
git -C ~/.hermes/agent-src commit -F <msgfile>
```
Message: `feat(gateway): wire PartialBacklogMonitor (shared loop) + applier-thread partial re-drive`

---

## Post-implementation: HARD-GATE re-check + report (do NOT enable)

After all tasks pass, **re-verify the :4100 hard gate** (do not restart anything):

```bash
grep -c "current_business_state === mappedState.businessState" \
  ~/.hermes/services/jobflow-platform/services/jobflow-api/dist/modules/jobs/repository.js
# and the running process start-time vs dist mtime:
```
```powershell
Get-CimInstance Win32_Process -Filter "Name='node.exe'" |
  ? { $_.CommandLine -like '*jobflow-api*' } | select ProcessId,CreationDate
```

Report:
1. What shipped + test results (counts).
2. Whether the :4100 hard gate is met — grep==1 **AND** running-PID start-time **>** dist mtime.
   As of 2026-07-14 it is **NOT** (running PID 30172 started 07-13 17:09, dist recompiled 07-14
   09:06) — so `TRACKER_APPLIER_REDRIVE_ENABLED` stays `"0"`; the re-drive path is dormant.
3. A **gateway restart is required** to load the new applier code (editable install) AND to make
   the always-on backlog **alert** live. Enabling re-drive additionally needs Diego to (a) restart
   :4100 onto `8d7b5f5`'s dist, (b) set `TRACKER_APPLIER_REDRIVE_ENABLED=1` in `profiles/main/.env`,
   (c) restart the gateway. Do **NOT** perform these — report and let Diego decide.

## Self-Review notes (author)

- **Spec coverage:** Component 1 → Task 2; Component 2 → Task 4; Component 3 → Tasks 1+3; wiring →
  Task 5. All acceptance-criteria bullets map to a test.
- **Type consistency:** `IntentApplier.redrive_partials() -> dict[str,str]`; subscriber
  `redrive_partials() -> int` (counts `"redriven"`); `PartialBacklogMonitor.evaluate/check ->
  Optional[str]`; `PartialBacklogSample(count, oldest_age_seconds, sample_job_ids)`. Names are
  consistent across tasks.
- **Scope trim (documented):** the alert `alert_threshold`/`re_alert_cooldown_seconds` ship as
  constructor params at the approved defaults (3 / 900); they are **not** env-read in `startup()`
  (keeping the critical startup path crash-free), unlike the re-drive backoff tuning which is
  env-read off the hot path in the subscriber. The `TRACKER_APPLIER_REDRIVE_ENABLED` flag and the
  backoff env overrides are the only env surface.
