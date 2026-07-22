# CODE_DRIFT Event Producer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Emit an edge-triggered CODE_DRIFT event when the shared detached checkout at `~/.hermes/agent-src` drifts from the landed `main` ref, so drift reaches the event bus and Telegram (spec: `docs/superpowers/specs/2026-07-21-code-drift-event-producer-design.md`).

**Architecture:** A new `CodeDriftMonitor` producer (sibling of `ResourcePressureMonitor`) polls read-only git every 15 min from the gateway subscriber loop. Edge policy: emit on rising edge / shape change / 6h re-ping; emit `status:"resolved"` on the falling edge only if the episode alerted. Episode state persists to `~/.hermes/notifications/code_drift_state.json` (wall clock) so the resolved ping survives the FF-then-restart remediation path. Routing: WARN → `ALERTS` topic, with a payload hook demoting `resolved` to INFO.

**Tech Stack:** Python 3.11, sqlite EventBus (`events/bus.py`), pytest. Run tests from PowerShell in the worktree root.

## Global Constraints

- All git access is READ-ONLY: `rev-parse`, `merge-base --is-ancestor`, `rev-list --count`, `log --format`, `status --porcelain`. Never fetch/merge/checkout.
- Every git subprocess is bounded: `timeout=15`, `capture_output=True, text=True`; `OSError`/`TimeoutExpired` degrade to "no sample", never raise out of the poll loop.
- Tests are hermetic per the events-subsystem invariants: `EventBus(db_path=tmp_path / "events" / "event_bus.db")`, injected sampler + clock, `tmp_path` state files, no sleeps, no live `~/.hermes` I/O. (pytest conftest points HERMES_HOME at a tempdir, so `events.paths` is already tempdir-scoped under test.)
- Wall clock (`time.time()`) for episode timestamps — they are persisted across restarts (monotonic resets per process).
- New `EventType` members MUST land in the same commit as their `EVENT_TYPE_EMOJI` and `TOPIC_ROUTING` entries — the pairing tests (`test_event_icons_cover_all_types`, `test_every_event_type_has_policy_entry`) fail otherwise.
- When editing `EVENT_TYPE_EMOJI`, eyeball the dict for duplicate `EventType.X` keys — ruff cannot catch attribute-access dup keys (memory: events-emoji-dict-dup-key-lint-gap).
- Commit after every task, from this worktree (`claude/agitated-jemison-43afe5`). PS 5.1: use a temp file + `git commit -F <file>` for multi-line messages.

## File Structure

- Create: `events/producers/code_drift_monitor.py` — sampler (`DriftSample`, `sample_code_drift`) + edge core (`CodeDriftMonitor`).
- Create: `tests/events/producers/test_code_drift_monitor.py` — all producer tests.
- Modify: `events/schema.py` — `CODE_DRIFT` member (after `TRACKER_PARTIAL_BACKLOG`, before `__init__`).
- Modify: `events/routing_policy.py` — `_POLICY` entry + resolved→INFO hook in `classify()`.
- Modify: `events/formatting.py` — emoji, WhatsApp title, `code_drift_body()`, green resolved dot in `header_dot()`.
- Modify: `events/subscribers/telegram_notifier.py` — body branch delegating to `code_drift_body`.
- Modify: `events/paths.py` — `code_drift_state_path()`.
- Modify: `events/gateway_integration.py` — construct/get/poll the monitor.
- Modify (tests): `tests/events/test_routing_policy.py`, `tests/events/test_formatting.py`, `tests/events/test_gateway_integration.py`.

---

### Task 1: EventType + routing + emoji + WhatsApp title

**Files:**
- Modify: `events/schema.py` (member goes right after the `TRACKER_PARTIAL_BACKLOG` block, ~line 324, before `def __init__`)
- Modify: `events/routing_policy.py` (`_POLICY` dict "system health" section ~line 160; hook chain in `classify()` after the `GATEWAY_HEALTH` branch ~line 317)
- Modify: `events/formatting.py` (`EVENT_TYPE_EMOJI` tail ~line 133; `WHATSAPP_TITLE_BY_EVENT` ~line 253)
- Test: `tests/events/test_routing_policy.py`

**Interfaces:**
- Produces: `EventType.CODE_DRIFT` (type_string `"code_drift"`, default `Priority.HIGH`) — every later task imports this. Routing contract: drifting → `Attention.WARN` on `ALERTS`; payload `{"status": "resolved"}` → `Attention.INFO`, `wa="none"`.

- [ ] **Step 1: Write the failing routing-hook test**

Append to `tests/events/test_routing_policy.py` (imports for `classify`/`Attention`/`EventType`/`Event` already exist at the top of that file — reuse the file's existing event-construction helper style; look at `test_probe_transition_recovery_is_batched_trace` ~line 137 for the local pattern of building an `Event` and asserting on `classify(...)`):

```python
class TestCodeDriftRouting:
    def _event(self, payload):
        # Match the construction style used by the file's other hook tests.
        from events.schema import Event, Priority
        return Event(
            event_id="ev-cd", event_type=EventType.CODE_DRIFT,
            timestamp="2026-07-21T12:00:00Z", source="system",
            priority=Priority.HIGH, payload=payload,
        )

    def test_drifting_is_warn_on_alerts(self):
        route = classify(self._event({"status": "drifting", "state": "behind"}))
        assert route.attention is Attention.WARN
        assert route.topic_key == "watchdog_alerts"

    def test_resolved_is_demoted_to_info(self):
        route = classify(self._event({"status": "resolved"}))
        assert route.attention is Attention.INFO
        assert route.wa == "none"
```

NOTE: before writing, open `tests/events/test_routing_policy.py` and check how existing tests construct an `Event` and what the `ALERTS` topic-key constant/string is (`watchdog_alerts` per `events/routing_policy.py` — confirm the literal via the `ALERTS` constant near the top of `events/routing_policy.py` and use whatever existing tests assert against). Adjust the two assertions to the file's idiom (`route.topic_key == ALERTS` if the test file imports the constant).

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `python -m pytest tests/events/test_routing_policy.py -k CodeDrift -v`
Expected: FAIL / error with `AttributeError: CODE_DRIFT` (member doesn't exist yet).

- [ ] **Step 3: Add the EventType member**

In `events/schema.py`, after the `TRACKER_PARTIAL_BACKLOG = (...)` line and before `def __init__`:

```python
    # Agent-src code-drift alert — added 2026-07-21. The gateway's editable
    # install imports the WORKING TREE of ~/.hermes/agent-src, which is
    # deliberately kept on a detached HEAD so worktree agents can land
    # commits onto the `main` ref via `git branch -f`. A commit landed on
    # main therefore does NOT run until the checkout is fast-forwarded and
    # the gateway restarted — on 2026-07-20/21 three restart cycles ran
    # stale code while every session believed the fix was live. Emitted by
    # events.producers.code_drift_monitor.CodeDriftMonitor (read-only git
    # probe every 15 min on the subscriber poll loop; rising edge / shape
    # change / 6h re-ping, plus a falling-edge status="resolved" event).
    # HIGH so it survives significant_only / digest_only verbosity. Payload:
    #   status (str)            — "drifting" | "resolved"
    #   state (str)             — "behind" | "ahead" | "diverged" (drifting only)
    #   head / main (str)       — short SHAs
    #   behind_count / ahead_count (int)
    #   dirty (bool)            — uncommitted changes in the checkout
    #   missed_subjects (list[str]) — up to 5 "<sha> <subject>" lines (behind)
    #   repo (str)              — checkout path probed
    CODE_DRIFT = ("code_drift", Priority.HIGH)
```

- [ ] **Step 4: Add the routing policy entry + hook**

In `events/routing_policy.py`, in the `# ----- system health → WARN alerts` section, after the `_E.RESOURCE_PRESSURE` line:

```python
    _E.CODE_DRIFT: _Spec(Attention.WARN, ALERTS),   # hook: resolved → INFO
```

In `classify()`, after the `elif et == EventType.GATEWAY_HEALTH:` branch (keep the `elif` chain intact):

```python
    elif et == EventType.CODE_DRIFT:
        if payload.get("status") == "resolved":
            attention = Attention.INFO   # recovery — closure telemetry
            wa = "none"
```

- [ ] **Step 5: Add emoji + WhatsApp title**

In `events/formatting.py`, at the tail of `EVENT_TYPE_EMOJI` (after `TRACKER_PARTIAL_BACKLOG`):

```python
    # Agent-src code drift (2026-07-21) — the deployed detached checkout is
    # not running what main says should be running. Shuffle arrows read as
    # "the code paths crossed"; distinct from 🔃 (PR opened).
    EventType.CODE_DRIFT:               "🔀",
}
```

Then EYEBALL the whole `EVENT_TYPE_EMOJI` dict top-to-bottom for any duplicate `EventType.X:` key (attribute-access dup keys are silently shadowed and ruff cannot flag them).

In `WHATSAPP_TITLE_BY_EVENT`, after the `RESOURCE_PRESSURE` line:

```python
    EventType.CODE_DRIFT:                  "STALE CODE RUNNING",
```

- [ ] **Step 6: Run the new tests + pairing tests**

Run: `python -m pytest tests/events/test_routing_policy.py tests/events/test_formatting.py::test_event_icons_cover_all_types tests/events/test_schema.py tests/events/test_schema_contract.py tests/events/test_telegram_notifier.py -v`
Expected: ALL PASS (the pairing/coverage tests now see matched entries; `-k CodeDrift` subset passes too). If `test_telegram_notifier.py::test_topic_routing_covers_all_domain_events` fails, read its assertion and add the entry it demands (it enforces notifier-side topic coverage).

- [ ] **Step 7: Commit**

```powershell
git add events/schema.py events/routing_policy.py events/formatting.py tests/events/test_routing_policy.py
git commit -m "feat(events): add CODE_DRIFT event type with routing, emoji, WA title"
```

---

### Task 2: Sampler — `DriftSample` + `sample_code_drift` + state path

**Files:**
- Create: `events/producers/code_drift_monitor.py` (sampler half)
- Modify: `events/paths.py` (append `code_drift_state_path()`)
- Test: `tests/events/producers/test_code_drift_monitor.py`

**Interfaces:**
- Produces: `DriftSample(state, head, main, behind_count=0, ahead_count=0, dirty=False, missed_subjects=())` frozen dataclass with `.shape` property → `[state, behind_count, ahead_count]`; `sample_code_drift(repo: Optional[Path]) -> Optional[DriftSample]`; `events.paths.code_drift_state_path() -> Path`. Task 3 consumes all three.

- [ ] **Step 1: Write the failing sampler tests**

Create `tests/events/producers/test_code_drift_monitor.py`:

```python
"""Tests for events.producers.code_drift_monitor — CodeDriftMonitor.

The monitor probes the shared detached checkout (~/.hermes/agent-src) with
read-only git and emits CODE_DRIFT on the rising edge of HEAD != main.
Added 2026-07-21 after three restart cycles ran stale code (2026-07-20/21).

The edge core takes an injected sampler + wall clock; only the
sample_code_drift() unit tests below touch real git, against a throwaway
tmp_path repo.
"""

import subprocess

import pytest

from events.bus import EventBus
from events.schema import EventType, Priority
from events.producers.code_drift_monitor import (
    CodeDriftMonitor,
    DriftSample,
    sample_code_drift,
)


@pytest.fixture
def bus(tmp_path):
    return EventBus(db_path=tmp_path / "events" / "event_bus.db")


def _drift_events(bus):
    return bus.query(event_type=EventType.CODE_DRIFT)


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "commit.gpgsign=false", *args],
        check=True, capture_output=True, text=True, timeout=30,
    )


@pytest.fixture
def repo(tmp_path):
    """Throwaway repo: two commits on main, HEAD detached at the first
    (i.e. the deployed checkout LAGS main by 1 — the incident shape)."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    (repo / "a.txt").write_text("one", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "first")
    (repo / "a.txt").write_text("two", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "second landed fix")
    _git(repo, "checkout", "--detach", "HEAD~1")
    return repo


class TestSampleCodeDrift:
    def test_behind_detached_checkout(self, repo):
        s = sample_code_drift(repo)
        assert s.state == "behind"
        assert s.behind_count == 1
        assert s.ahead_count == 0
        assert s.dirty is False
        assert len(s.missed_subjects) == 1
        assert "second landed fix" in s.missed_subjects[0]

    def test_in_sync(self, repo):
        _git(repo, "checkout", "--detach", "main")
        s = sample_code_drift(repo)
        assert s.state == "in_sync"
        assert s.head == s.main

    def test_dirty_flag(self, repo):
        (repo / "a.txt").write_text("local edit", encoding="utf-8")
        assert sample_code_drift(repo).dirty is True

    def test_missing_repo_returns_none(self, tmp_path):
        assert sample_code_drift(tmp_path / "nope") is None

    def test_shape_property(self):
        s = DriftSample(state="behind", head="a" * 9, main="b" * 9,
                        behind_count=3)
        assert s.shape == ["behind", 3, 0]
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -v`
Expected: collection error — `ModuleNotFoundError: events.producers.code_drift_monitor`.

- [ ] **Step 3: Add the path helper**

Append to `events/paths.py`:

```python
def code_drift_state_path() -> Path:
    """CodeDriftMonitor episode persistence (2026-07-21).

    Holds {"alerting", "last_emit_wall", "last_shape"} so the falling-edge
    "resolved" event survives the common remediation path (FF the checkout,
    then restart the gateway). Wall-clock timestamps — same lesson as the
    notifier batch-age persistence. Cross-profile, so canonical root.
    """
    return notifications_home() / "code_drift_state.json"
```

- [ ] **Step 4: Write the sampler module**

Create `events/producers/code_drift_monitor.py`:

```python
"""CodeDriftMonitor — emits CODE_DRIFT when the deployed checkout drifts from main.

The gateway's editable install imports the WORKING TREE of the shared
checkout at ~/.hermes/agent-src, which is deliberately kept on a detached
HEAD so worktree agents can land commits onto the `main` ref via
`git branch -f`. A commit landed on main therefore does NOT run until the
checkout is fast-forwarded and the gateway restarted — on 2026-07-20/21
three restart cycles ran stale code while every session believed the fix
was live because "main tip moved".

Two local layers already surface this (laptop-monitor tray row,
events_doctor); this producer is the third: drift as an event-bus event so
it reaches Telegram when the operator is away from the machine.

Emission policy
---------------
Edge-triggered on the WALL clock (state is persisted across restarts):
fire on the rising edge of drift, fire immediately when the drift *shape*
(state, behind, ahead) changes, re-ping a sustained episode every 6 h, and
emit a single status="resolved" event on the falling edge — but only if
the episode actually alerted. Episode state lives in
~/.hermes/notifications/code_drift_state.json so the resolved ping
survives the common remediation path (FF, then restart the gateway).

Read-only git, bounded subprocess (15 s timeout). The monitor NEVER
fast-forwards — remediation is a deliberate operator action.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from events.bus import EventBus
from events.paths import code_drift_state_path
from events.schema import EventType
from events.state import load_state, save_state

logger = logging.getLogger(__name__)

# One git probe per 15 min; a sustained episode re-pings every 6 h.
DEFAULT_CHECK_INTERVAL_SECONDS = 900.0
DEFAULT_RE_ALERT_COOLDOWN_SECONDS = 6 * 3600.0
MISSED_SUBJECTS_CAP = 5

_AGENT_SRC_DEFAULT = Path.home() / ".hermes" / "agent-src"


def _agent_src_root() -> Path:
    return Path(os.getenv("HERMES_AGENT_SRC") or _AGENT_SRC_DEFAULT)


@dataclass(frozen=True)
class DriftSample:
    """Point-in-time relationship of the checkout's HEAD to refs/heads/main."""

    state: str  # "in_sync" | "behind" | "ahead" | "diverged"
    head: str
    main: str
    behind_count: int = 0
    ahead_count: int = 0
    dirty: bool = False
    missed_subjects: Tuple[str, ...] = ()

    @property
    def shape(self) -> List:
        """The identity of a drift episode: a change here re-alerts
        immediately (list, not tuple, so it round-trips through JSON)."""
        return [self.state, self.behind_count, self.ahead_count]


def _git(repo: Path, *args: str) -> Tuple[int, str]:
    """Run a read-only git command; returns (returncode, stdout)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True, text=True, timeout=15,
        )
        return proc.returncode, proc.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def sample_code_drift(repo: Optional[Path] = None) -> Optional[DriftSample]:
    """Read-only git probe of HEAD vs refs/heads/main.

    Returns None when there is nothing to evaluate (no checkout, refs
    unresolvable, git broken) — the caller treats None as a no-op so the
    poll loop never crashes and a transient git failure never fabricates
    a drift or a recovery.
    """
    repo = Path(repo) if repo is not None else _agent_src_root()
    # .git is a directory in a normal checkout and a file in a worktree.
    if not (repo / ".git").exists():
        return None

    rc_head, head = _git(repo, "rev-parse", "--verify", "HEAD")
    rc_main, main = _git(repo, "rev-parse", "--verify", "refs/heads/main")
    if rc_head != 0 or rc_main != 0:
        return None
    head, main = head.strip(), main.strip()
    dirty = bool(_git(repo, "status", "--porcelain")[1].strip())

    if head == main:
        return DriftSample(state="in_sync", head=head, main=main, dirty=dirty)

    def _count(rev_range: str) -> int:
        out = _git(repo, "rev-list", "--count", rev_range)[1].strip()
        try:
            return int(out)
        except ValueError:
            return 0

    head_behind = _git(repo, "merge-base", "--is-ancestor",
                       "HEAD", "refs/heads/main")[0] == 0
    head_ahead = _git(repo, "merge-base", "--is-ancestor",
                      "refs/heads/main", "HEAD")[0] == 0

    if head_behind:
        subjects = tuple(
            line.strip() for line in
            _git(repo, "log", "--format=%h %s", f"-{MISSED_SUBJECTS_CAP}",
                 "HEAD..refs/heads/main")[1].splitlines()
            if line.strip()
        )
        return DriftSample(
            state="behind", head=head, main=main,
            behind_count=_count("HEAD..refs/heads/main"),
            dirty=dirty, missed_subjects=subjects,
        )
    if head_ahead:
        return DriftSample(
            state="ahead", head=head, main=main,
            ahead_count=_count("refs/heads/main..HEAD"), dirty=dirty,
        )
    return DriftSample(
        state="diverged", head=head, main=main,
        behind_count=_count("HEAD..refs/heads/main"),
        ahead_count=_count("refs/heads/main..HEAD"), dirty=dirty,
    )
```

(The `CodeDriftMonitor` class is Task 3 — the module is import-complete without it for this task's tests except the class import in the test file header; add a placeholder now so the import line resolves:)

```python
class CodeDriftMonitor:  # implemented in the next commit
    pass
```

- [ ] **Step 5: Run the sampler tests**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -v`
Expected: the 5 `TestSampleCodeDrift` tests PASS.

- [ ] **Step 6: Commit**

```powershell
git add events/producers/code_drift_monitor.py events/paths.py tests/events/producers/test_code_drift_monitor.py
git commit -m "feat(events): read-only git drift sampler + state path for CodeDriftMonitor"
```

---

### Task 3: `CodeDriftMonitor` edge core with persisted episode state

**Files:**
- Modify: `events/producers/code_drift_monitor.py` (replace the placeholder class)
- Test: `tests/events/producers/test_code_drift_monitor.py` (append)

**Interfaces:**
- Consumes: `DriftSample`, `sample_code_drift`, `code_drift_state_path`, `events.state.load_state/save_state`.
- Produces: `CodeDriftMonitor(bus, *, repo_path=None, sampler=None, clock=None, state_path=None, check_interval_seconds=900.0, re_alert_cooldown_seconds=21600.0)` with `check() -> Optional[str]` and `evaluate(sample, now) -> Optional[str]`. Task 5 constructs `CodeDriftMonitor(_bus)` and calls `.check()`.

- [ ] **Step 1: Write the failing edge-core tests**

Append to `tests/events/producers/test_code_drift_monitor.py`:

```python
def behind(n=1, dirty=False):
    return DriftSample(state="behind", head="a" * 9, main="b" * 9,
                       behind_count=n, dirty=dirty,
                       missed_subjects=tuple(f"c{i} fix {i}" for i in range(min(n, 5))))


def in_sync():
    return DriftSample(state="in_sync", head="b" * 9, main="b" * 9)


def make_monitor(bus, tmp_path, **kw):
    kw.setdefault("state_path", tmp_path / "code_drift_state.json")
    kw.setdefault("check_interval_seconds", 900.0)
    return CodeDriftMonitor(bus, **kw)


class TestRisingEdge:
    def test_first_drift_emits_full_payload(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        assert m.evaluate(behind(3), now=1000.0)
        events = _drift_events(bus)
        assert len(events) == 1
        p = events[0].payload
        assert p["status"] == "drifting"
        assert p["state"] == "behind"
        assert p["behind_count"] == 3
        assert p["dirty"] is False
        assert len(p["missed_subjects"]) == 3
        assert events[0].priority is Priority.HIGH

    def test_same_shape_within_cooldown_is_silent(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(3), now=1000.0)
        assert m.evaluate(behind(3), now=1000.0 + 3600) is None
        assert len(_drift_events(bus)) == 1

    def test_in_sync_never_alerted_is_silent(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        assert m.evaluate(in_sync(), now=1000.0) is None
        assert _drift_events(bus) == []


class TestSustainedEpisode:
    def test_re_pings_after_cooldown(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(3), now=0.0)
        assert m.evaluate(behind(3), now=6 * 3600.0) is not None
        assert len(_drift_events(bus)) == 2

    def test_shape_change_bypasses_cooldown(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(3), now=0.0)
        # Two more commits land on main 10 min later: alert NOW.
        assert m.evaluate(behind(5), now=600.0) is not None
        assert _drift_events(bus)[-1].payload["behind_count"] == 5


class TestFallingEdge:
    def test_resolved_emitted_once(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(1), now=0.0)
        assert m.evaluate(in_sync(), now=60.0) is not None
        assert m.evaluate(in_sync(), now=120.0) is None
        events = _drift_events(bus)
        assert len(events) == 2
        assert events[-1].payload["status"] == "resolved"

    def test_relapse_after_resolve_fires_immediately(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path)
        m.evaluate(behind(1), now=0.0)
        m.evaluate(in_sync(), now=60.0)
        # New drift 2 min later — rising edge again, no cooldown wait.
        assert m.evaluate(behind(1), now=180.0) is not None
        assert len(_drift_events(bus)) == 3


class TestRestartSurvival:
    def test_resolved_fires_from_persisted_state(self, bus, tmp_path):
        """The common remediation is FF-then-restart: the fresh process must
        still emit the resolved ping."""
        m1 = make_monitor(bus, tmp_path)
        m1.evaluate(behind(2), now=0.0)
        # Gateway restarts onto the FF'd checkout: brand-new monitor, same
        # state file, checkout now in sync.
        m2 = make_monitor(bus, tmp_path)
        assert m2.evaluate(in_sync(), now=300.0) is not None
        assert _drift_events(bus)[-1].payload["status"] == "resolved"

    def test_restart_mid_episode_stays_quiet(self, bus, tmp_path):
        """Restart WITHOUT the FF (still drifting, same shape, inside the
        cooldown): no duplicate alert."""
        m1 = make_monitor(bus, tmp_path)
        m1.evaluate(behind(2), now=0.0)
        m2 = make_monitor(bus, tmp_path)
        assert m2.evaluate(behind(2), now=600.0) is None
        assert len(_drift_events(bus)) == 1


class TestCheckGating:
    def test_none_sample_is_noop(self, bus, tmp_path):
        m = make_monitor(bus, tmp_path,
                         sampler=lambda: None, clock=lambda: 0.0)
        assert m.check() is None
        assert _drift_events(bus) == []

    def test_check_respects_interval(self, bus, tmp_path):
        calls = []
        t = {"now": 0.0}
        m = make_monitor(
            bus, tmp_path,
            sampler=lambda: calls.append(1) or in_sync(),
            clock=lambda: t["now"],
        )
        m.check()
        t["now"] = 60.0
        m.check()          # inside the 15-min gate — no second git probe
        assert len(calls) == 1
        t["now"] = 901.0
        m.check()
        assert len(calls) == 2

    def test_sampler_exception_never_raises(self, bus, tmp_path):
        def boom():
            raise RuntimeError("git exploded")
        m = make_monitor(bus, tmp_path, sampler=boom, clock=lambda: 0.0)
        assert m.check() is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -v`
Expected: `TestSampleCodeDrift` still PASSES; every new class FAILS with `TypeError` (placeholder class takes no arguments).

- [ ] **Step 3: Implement the monitor (replace the placeholder class)**

```python
class CodeDriftMonitor:
    """Probes checkout-vs-main drift and emits CODE_DRIFT on the edge.

    Call check() from the gateway subscriber poll loop (any cadence — it
    self-gates to one git probe per ``check_interval_seconds``). Sampler,
    wall clock, and state path are injectable so the edge core is fully
    testable without git, sleeps, or live ~/.hermes I/O.
    """

    def __init__(
        self,
        bus: EventBus,
        *,
        repo_path: Optional[Path] = None,
        sampler: Optional[Callable[[], Optional[DriftSample]]] = None,
        clock: Optional[Callable[[], float]] = None,
        state_path: Optional[Path] = None,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        re_alert_cooldown_seconds: float = DEFAULT_RE_ALERT_COOLDOWN_SECONDS,
    ):
        self.bus = bus
        self._repo_path = Path(repo_path) if repo_path else None
        self._sampler = sampler or (lambda: sample_code_drift(self._repo_path))
        # WALL clock, not monotonic: last_emit is persisted across restarts.
        self._clock = clock or time.time
        self._state_path = Path(state_path) if state_path else code_drift_state_path()
        self.check_interval_seconds = check_interval_seconds
        self.re_alert_cooldown_seconds = re_alert_cooldown_seconds

        self._last_check: Optional[float] = None
        state = load_state(self._state_path, {})
        self._alerting: bool = bool(state.get("alerting"))
        last_emit = state.get("last_emit_wall")
        self._last_emit: Optional[float] = (
            float(last_emit) if isinstance(last_emit, (int, float)) else None
        )
        last_shape = state.get("last_shape")
        self._last_shape: Optional[List] = (
            list(last_shape) if isinstance(last_shape, list) else None
        )

    def check(self) -> Optional[str]:
        """Probe if the interval elapsed; emit if an edge fired.

        Swallows sampler failures — a git hiccup must never crash the
        gateway poll loop, and must not fabricate drift or recovery.
        """
        now = self._clock()
        if (self._last_check is not None
                and now - self._last_check < self.check_interval_seconds):
            return None
        self._last_check = now
        try:
            sample = self._sampler()
        except Exception:
            logger.exception("CodeDriftMonitor: sampler raised")
            return None
        if sample is None:
            return None
        return self.evaluate(sample, now)

    def evaluate(self, sample: DriftSample, now: float) -> Optional[str]:
        """Pure edge core given (sample, wall-clock now) + persisted state."""
        if sample.state == "in_sync":
            if not self._alerting:
                return None
            # Falling edge: the episode alerted, so close the loop.
            self._alerting = False
            self._last_shape = None
            self._last_emit = None  # next rising edge fires immediately
            self._save()
            return self._emit_resolved(sample)

        shape = sample.shape
        rising_edge = not self._alerting
        shape_changed = self._last_shape is not None and shape != self._last_shape
        cooldown_elapsed = (
            self._last_emit is None
            or (now - self._last_emit) >= self.re_alert_cooldown_seconds
        )
        self._alerting = True
        if not (rising_edge or shape_changed or cooldown_elapsed):
            return None

        self._last_emit = now
        self._last_shape = shape
        self._save()
        return self._emit_drift(sample)

    def _save(self) -> None:
        try:
            save_state(self._state_path, {
                "alerting": self._alerting,
                "last_emit_wall": self._last_emit,
                "last_shape": self._last_shape,
            })
        except Exception:  # pragma: no cover - defensive
            logger.exception("CodeDriftMonitor: state persist failed")

    def _repo_str(self) -> str:
        return str(self._repo_path or _agent_src_root())

    def _emit_drift(self, sample: DriftSample) -> str:
        logger.warning(
            "Code drift: checkout %s main (behind %d / ahead %d, dirty=%s) "
            "— HEAD %s vs main %s",
            sample.state, sample.behind_count, sample.ahead_count,
            sample.dirty, sample.head[:9], sample.main[:9],
        )
        return self.bus.emit(
            event_type=EventType.CODE_DRIFT,
            source="system",
            payload={
                "status": "drifting",
                "state": sample.state,
                "head": sample.head[:9],
                "main": sample.main[:9],
                "behind_count": sample.behind_count,
                "ahead_count": sample.ahead_count,
                "dirty": sample.dirty,
                "missed_subjects": list(sample.missed_subjects),
                "repo": self._repo_str(),
            },
            tags=["code", "drift", sample.state],
        )

    def _emit_resolved(self, sample: DriftSample) -> str:
        logger.info("Code drift resolved: checkout back in sync @ %s",
                    sample.main[:9])
        return self.bus.emit(
            event_type=EventType.CODE_DRIFT,
            source="system",
            payload={
                "status": "resolved",
                "head": sample.head[:9],
                "main": sample.main[:9],
                "repo": self._repo_str(),
            },
            tags=["code", "drift", "resolved"],
        )
```

- [ ] **Step 4: Run the full producer test file**

Run: `python -m pytest tests/events/producers/test_code_drift_monitor.py -v`
Expected: ALL PASS.

- [ ] **Step 5: Commit**

```powershell
git add events/producers/code_drift_monitor.py tests/events/producers/test_code_drift_monitor.py
git commit -m "feat(events): CodeDriftMonitor edge core with restart-surviving episode state"
```

---

### Task 4: Plain-language bodies — formatting helper + notifier branch + green resolved dot

**Files:**
- Modify: `events/formatting.py` (add `code_drift_body()` near the other body helpers, e.g. after `partial_backlog_body`; extend `header_dot()`)
- Modify: `events/subscribers/telegram_notifier.py` (body branch after the `TRACKER_PARTIAL_BACKLOG` branch ~line 523)
- Test: `tests/events/test_formatting.py`

**Interfaces:**
- Consumes: the CODE_DRIFT payload schema from Task 3 (`status/state/head/main/behind_count/ahead_count/dirty/missed_subjects/repo`).
- Produces: `code_drift_body(payload: dict) -> str` — the notifier delegates to it.

- [ ] **Step 1: Write the failing formatting tests**

Append to `tests/events/test_formatting.py` (match the file's existing import style — it already imports from `events.formatting`):

```python
class TestCodeDriftBody:
    def _payload(self, **kw):
        p = {
            "status": "drifting", "state": "behind",
            "head": "aaaaaaaaa", "main": "bbbbbbbbb",
            "behind_count": 3, "ahead_count": 0, "dirty": False,
            "missed_subjects": ["c1 fix one", "c2 fix two"],
            "repo": "C:/Users/diego/.hermes/agent-src",
        }
        p.update(kw)
        return p

    def test_behind_body_is_plain_language(self):
        from events.formatting import code_drift_body
        body = code_drift_body(self._payload())
        assert "LAGS main by 3 commit(s)" in body
        assert "c1 fix one" in body
        assert "merge --ff-only main" in body
        assert "restart the gateway" in body
        # No raw dict/list splat.
        assert "{" not in body and "[" not in body

    def test_dirty_flag_rendered(self):
        from events.formatting import code_drift_body
        assert "DIRTY" in code_drift_body(self._payload(dirty=True))

    def test_ahead_body(self):
        from events.formatting import code_drift_body
        body = code_drift_body(self._payload(
            state="ahead", behind_count=0, ahead_count=2, missed_subjects=[]))
        assert "AHEAD of main by 2 commit(s)" in body

    def test_diverged_body(self):
        from events.formatting import code_drift_body
        body = code_drift_body(self._payload(state="diverged"))
        assert "DIVERGED" in body

    def test_resolved_body(self):
        from events.formatting import code_drift_body
        body = code_drift_body({"status": "resolved", "head": "bbbbbbbbb",
                                "main": "bbbbbbbbb", "repo": "x"})
        assert "back in sync" in body
        assert "bbbbbbbbb" in body

    def test_resolved_header_dot_is_green(self):
        from events.formatting import header_dot, PRIORITY_EMOJI
        from events.schema import Event, EventType, Priority
        ev = Event(event_id="e", event_type=EventType.CODE_DRIFT,
                   timestamp="2026-07-21T12:00:00Z", source="system",
                   priority=Priority.HIGH,
                   payload={"status": "resolved"})
        assert header_dot(ev) == PRIORITY_EMOJI[Priority.LOW]
```

NOTE: check how `tests/events/test_formatting.py` builds an `Event` (there will be an existing helper or direct construction — copy that idiom; the `Event` constructor signature above must match `events/schema.py`, adjust field names if they differ).

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/events/test_formatting.py -k CodeDrift -v`
Expected: FAIL with `ImportError: cannot import name 'code_drift_body'`.

- [ ] **Step 3: Implement `code_drift_body` + extend `header_dot`**

In `events/formatting.py`, near the other `*_body` helpers:

```python
def code_drift_body(p: dict) -> str:
    """Plain-language CODE_DRIFT body (2026-07-21).

    The generic fallback would splat missed_subjects as a raw list; this is
    the operator's phone-facing diagnosis + remediation line.
    """
    repo = p.get("repo", "~/.hermes/agent-src")
    if p.get("status") == "resolved":
        return f"Deployed checkout back in sync with main @ {p.get('main', '?')}"

    state = p.get("state", "?")
    lines = []
    if state == "behind":
        lines.append(
            f"Deployed checkout LAGS main by {p.get('behind_count', '?')} "
            "commit(s) — landed fixes are NOT running."
        )
        for subj in (p.get("missed_subjects") or [])[:5]:
            lines.append(f"  missed: {subj}")
    elif state == "ahead":
        lines.append(
            f"Deployed checkout is AHEAD of main by {p.get('ahead_count', '?')} "
            "commit(s) — the working tree carries unlanded state."
        )
    else:
        lines.append(
            f"Deployed checkout has DIVERGED from main "
            f"(HEAD {p.get('head', '?')} vs main {p.get('main', '?')})."
        )
    if p.get("dirty"):
        lines.append("Working tree is DIRTY (uncommitted changes).")
    if state == "behind":
        lines.append(
            f"Fix: git -C {repo} merge --ff-only main, then restart the gateway."
        )
    return "\n".join(lines)
```

In `header_dot()`, extend the existing recovery-override (keep the GATEWAY_HEALTH branch as-is and add):

```python
    if event.event_type == EventType.CODE_DRIFT:
        if (event.payload or {}).get("status") == "resolved":
            return PRIORITY_EMOJI[Priority.LOW]  # 🟢 — recovery, not an alert
```

- [ ] **Step 4: Add the notifier body branch**

In `events/subscribers/telegram_notifier.py`, after the `TRACKER_PARTIAL_BACKLOG` branch (~line 523), matching the surrounding lazy-import idiom:

```python
        if et == EventType.CODE_DRIFT:
            # 2026-07-21: the generic fallback would splat missed_subjects
            # as a raw list — render the plain-language diagnosis instead.
            from events.formatting import code_drift_body
            return code_drift_body(p)
```

- [ ] **Step 5: Run formatting + notifier tests**

Run: `python -m pytest tests/events/test_formatting.py tests/events/test_telegram_notifier.py -v`
Expected: ALL PASS.

- [ ] **Step 6: Commit**

```powershell
git add events/formatting.py events/subscribers/telegram_notifier.py tests/events/test_formatting.py
git commit -m "feat(events): plain-language CODE_DRIFT bodies + green resolved dot"
```

---

### Task 5: Gateway wiring — construct, getter, poll-loop probe

**Files:**
- Modify: `events/gateway_integration.py` (import block ~line 25; module globals ~line 93; `startup()` ~line 119/130; getters ~line 337; `_subscriber_poll_loop` after the resource-pressure block ~line 759; `shutdown()` — mirror wherever `_resource_monitor` is reset)
- Test: `tests/events/test_gateway_integration.py`

**Interfaces:**
- Consumes: `CodeDriftMonitor(bus)` from Task 3 (all-defaults construction: real sampler, wall clock, canonical state path).
- Produces: `gateway_integration.get_code_drift_monitor()`.

- [ ] **Step 1: Write the failing wiring tests**

Append to `tests/events/test_gateway_integration.py` (the file already imports `inspect` and `gi`; mirror `TestResourceMonitorWiring` exactly — static assertions, NOT a real `startup()`, which is eventbus-non-hermetic and heavy):

```python
class TestCodeDriftMonitorWiring:
    """CodeDriftMonitor (2026-07-21 stale-checkout remediation) must be
    constructed at startup and probed by the poll loop. Asserted statically
    for the same reasons as TestResourceMonitorWiring above."""

    def test_getter_returns_module_global(self):
        assert gi.get_code_drift_monitor() is gi._code_drift_monitor

    def test_startup_constructs_code_drift_monitor(self):
        src = inspect.getsource(gi.startup)
        assert "_code_drift_monitor = CodeDriftMonitor(_bus)" in src

    def test_poll_loop_probes_code_drift(self):
        src = inspect.getsource(gi._subscriber_poll_loop)
        assert "_code_drift_monitor.check()" in src
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/events/test_gateway_integration.py -k CodeDrift -v`
Expected: FAIL with `AttributeError: ... has no attribute 'get_code_drift_monitor'`.

- [ ] **Step 3: Wire the monitor**

In `events/gateway_integration.py`:

1. Import, next to the other producer imports:
```python
from events.producers.code_drift_monitor import CodeDriftMonitor
```
2. Module global, next to `_resource_monitor`:
```python
_code_drift_monitor: Optional[CodeDriftMonitor] = None
```
3. In `startup()`: add `_code_drift_monitor` to the `global` statement, and next to the `_resource_monitor = ResourcePressureMonitor(_bus)` line:
```python
    _code_drift_monitor = CodeDriftMonitor(_bus)
```
4. Getter, next to `get_resource_monitor()`:
```python
def get_code_drift_monitor() -> Optional[CodeDriftMonitor]:
    """Get the code-drift monitor (checkout-vs-main probe)."""
    return _code_drift_monitor
```
5. In `_subscriber_poll_loop`, immediately after the resource-pressure block (~line 759):
```python
            # Code-drift probe — the deployed detached checkout vs the landed
            # main ref (2026-07-20/21 stale-restart incident). The monitor
            # self-gates to one read-only git sample per 15 min, so the
            # per-tick call is a clock comparison.
            if _code_drift_monitor:
                try:
                    _code_drift_monitor.check()
                except Exception:
                    logger.exception("Code drift check failed")
```
6. Grep `gateway_integration.py` for every other `_resource_monitor` reference (e.g. reset-to-None in `shutdown()`) and mirror each one for `_code_drift_monitor`.

- [ ] **Step 4: Run the wiring tests**

Run: `python -m pytest tests/events/test_gateway_integration.py -v`
Expected: ALL PASS (existing classes included).

- [ ] **Step 5: Commit**

```powershell
git add events/gateway_integration.py tests/events/test_gateway_integration.py
git commit -m "feat(events): wire CodeDriftMonitor into gateway startup + poll loop"
```

---

### Task 6: Full-suite verification + lint

**Files:** none new.

- [ ] **Step 1: Run the whole events suite**

Run: `python -m pytest tests/events -q`
Expected: 0 failures (742+ passed pre-change; new total higher). Investigate ANY failure — do not rationalize.

- [ ] **Step 2: Ruff (no cache — noqa-directive warnings hide on cache hits)**

Run: `python -m ruff check events hermes_cli tests/events --no-cache`
Expected: `All checks passed!`

- [ ] **Step 3: Re-eyeball `EVENT_TYPE_EMOJI` for duplicate `EventType.X:` keys** (final chance — dup attribute keys are silently shadowed).

- [ ] **Step 4: Commit any straggler fixes**

```powershell
git status
# commit only if steps 1-3 forced changes
```

---

## Landing (after all tasks green — separate approval)

Per the shared-checkout protocol (do NOT run as part of task execution; the executor stops after Task 6 and reports):

```powershell
# from the worktree, after review:
$sha = git rev-parse HEAD
git merge-base --is-ancestor main $sha   # must exit 0
git branch -f main $sha
```

Never `git pull`, never push. The live gateway picks the change up at the next deliberate FF of `~/.hermes/agent-src` + gateway restart.
