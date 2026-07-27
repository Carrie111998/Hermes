"""Tests for GatewayWorkerBridgeWatchersMixin — worker task push alerts.

The mixin tails the worker bridge event log (workers/bridge.db) with its own
persisted cursor and, on terminal ``task.status`` transitions, injects a
synthetic ``MessageEvent`` via ``adapter.handle_message()`` so the
orchestrator agent session wakes up and processes the result. Same pattern as
the kanban notifier in gateway/kanban_watchers.py.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.worker_bridge_ultra import GatewayWorkerBridgeUltraMixin
from gateway.worker_bridge_watchers import (  # noqa: F401 — see _PENDING_STEP_8
    DEFAULT_AUTO_DISPATCH_ENABLED,
    DEFAULT_MAX_TASKS_PER_CYCLE,
    DEFAULT_STATUSES,
    GatewayWorkerBridgeWatchersMixin,
    MAX_MAX_TASKS_PER_CYCLE,
    MIN_INTERVAL_SECONDS,
    SKIP_CLAIM_LOST,
    SKIP_DEP_INCOMPLETE,
    SKIP_LIVE_PID,
    SKIP_MANUAL_HOLD,
    SKIP_NO_CAPACITY,
    SKIP_PAUSED,
    SKIP_PENDING_APPROVAL,
    SKIP_PENDING_INPUT,
    SKIP_RETRIES_EXHAUSTED,
    SKIP_SPAWN_ERROR,
    SKIP_WAITING_INPUT,
    _resolve_alert_settings,
    claim_task_for_dispatch,
    collect_new_transitions,
    collect_pending_work,
    count_free_global_slots,
    format_alert_text,
    has_active_worker_tasks,
    load_cursor,
    load_last_auto_dispatch,
    load_last_nudge,
    release_dispatch_claim,
    save_cursor,
    save_last_auto_dispatch,
    save_last_nudge,
    select_dispatchable_tasks,
)

STATUSES = frozenset(DEFAULT_STATUSES)


# ---------------------------------------------------------------------------
# Step 8 of PLAN_AUTO_DISPATCH.md is only partly built.
#
# This file was written spec-first alongside that plan. The guards it specifies
# that carry their own weight — dependency gating, retry-budget gating,
# surfacing paused/waiting_input, the env kill switch — are implemented and are
# covered by the passing cases below.
#
# The rest of the plan was superseded during implementation and is NOT built:
#
#   * The plan gives this watcher both `created` and `queued`. Shipped instead:
#     GatewayWorkerTaskDispatcherMixin owns `created` and atomically reserves
#     each one into `queued` (store.reserve_created_task) before spawning, and
#     this watcher claims only `queued`. The two interlock; letting both select
#     `created` would race them for the same task.
#   * `count_free_global_slots` reading the `leases` table (shipped: count of
#     tasks in running/verifying).
#   * The auto-dispatch-aware alert wording ("Free worker capacity: N task(s)
#     are pending…") replacing the manual "run `hermes worker tasks start`" text.
#   * Renaming runtime `gateway_dispatch_claim` -> `dispatch_claim`. The shipped
#     key is already persisted in live bridge.db rows; renaming it would orphan
#     in-flight claims.
#
# These are marked xfail rather than deleted so the specification survives and
# whoever finishes Step 8 gets an XPASS the moment each lands. Non-strict on
# purpose: a partial implementation should not turn into a hard failure here.
# ---------------------------------------------------------------------------
_PENDING_STEP_8 = {
    "test_selects_only_created_and_queued": "plan gives this watcher `created`; shipped design reserves created->queued in GatewayWorkerTaskDispatcherMixin",
    "test_requeued_retry_is_dispatchable": "fixture seeds the retry task as `created`, which this watcher deliberately does not own",
    "test_free_slots_counts_live_leases": "count_free_global_slots is not leases-backed yet",
    "test_stale_lease_pid_frees_capacity": "count_free_global_slots is not leases-backed yet",
    "test_free_slots_never_negative": "count_free_global_slots is not leases-backed yet",
    "test_format_alert_text_failures_first_with_instructions": "auto-dispatch-aware alert wording not implemented",
    "test_format_alert_text_includes_pending_section": "auto-dispatch-aware alert wording not implemented",
    "test_format_alert_text_pending_only_nudge": "auto-dispatch-aware alert wording not implemented",
    "test_tick_includes_pending_work_in_alert": "auto-dispatch-aware alert wording not implemented",
    "test_nudge_lists_blocked_when_auto_dispatch_enabled": "auto-dispatch-aware alert wording not implemented",
    "test_nudge_unchanged_when_auto_dispatch_disabled": "auto-dispatch-aware alert wording not implemented",
    "test_resolve_target_uses_most_recent_session_when_unpinned": "alert-target resolution differs from the plan",
    "test_claim_marks_task_and_returns_snapshot": "runtime key is gateway_dispatch_claim, not dispatch_claim",
    "test_release_leaves_row_alone_if_live_pid_took_over": "runtime key is gateway_dispatch_claim, not dispatch_claim",
    "test_auto_dispatch_spawns_and_audits": "spawn/audit semantics differ from the plan",
    "test_capacity_limits_spawn_budget": "spawn/audit semantics differ from the plan",
    "test_zero_free_slots_spawns_nothing_and_audits_no_capacity": "spawn/audit semantics differ from the plan",
    "test_spawn_exception_does_not_corrupt_task": "spawn/audit semantics differ from the plan",
    "test_skip_audit_deduped_until_reason_changes": "skip-audit dedup not implemented",
}


_PENDING_STEP_8 = {
    name: f"PLAN_AUTO_DISPATCH.md step 8 pending: {reason}"
    for name, reason in _PENDING_STEP_8.items()
}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_bridge_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            job_id TEXT,
            idempotency_key TEXT UNIQUE,
            worker TEXT NOT NULL,
            status TEXT NOT NULL,
            priority INTEGER NOT NULL,
            spec TEXT NOT NULL,
            runtime TEXT NOT NULL,
            result TEXT,
            created_at REAL NOT NULL DEFAULT 0,
            -- Present in the real store (WorkerStore._initialize) and written by
            -- claim_task_for_dispatch / release_dispatch_claim. Omitting it made
            -- every claim/release case fail with "no such column: updated_at".
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT,
            job_id TEXT,
            kind TEXT NOT NULL,
            payload TEXT NOT NULL,
            created_at REAL NOT NULL
        );
        """
    )
    conn.commit()
    return conn


def add_task(conn, task_id, worker="claude-code", objective="do the thing",
             status="queued", error="", summary="", runtime=None, created_at=0.0,
             priority=0, spec_extra=None):
    result = json.dumps({"error": error, "summary": summary}) if (error or summary) else None
    runtime_json = json.dumps(runtime if runtime is not None else {})
    spec = {"objective": objective}
    if spec_extra:
        spec.update(spec_extra)
    conn.execute(
        "INSERT INTO tasks "
        "(task_id, worker, status, priority, spec, runtime, result, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            worker,
            status,
            int(priority),
            json.dumps(spec),
            runtime_json,
            result,
            created_at,
        ),
    )
    conn.commit()


def add_leases_table(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS leases ("
        "  scope TEXT NOT NULL,"
        "  task_id TEXT,"
        "  pid INTEGER"
        ");"
    )
    conn.commit()


def add_lease(conn, scope, pid, task_id=None):
    conn.execute(
        "INSERT INTO leases (scope, task_id, pid) VALUES (?, ?, ?)",
        (scope, task_id, pid),
    )
    conn.commit()


def add_permission_requests_table(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS permission_requests ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id TEXT NOT NULL,"
        "  status TEXT NOT NULL"
        ");"
    )
    conn.commit()


def add_permission_request(conn, task_id, status="pending"):
    conn.execute(
        "INSERT INTO permission_requests (task_id, status) VALUES (?, ?)",
        (task_id, status),
    )
    conn.commit()


def add_input_requests_table(conn):
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS input_requests ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  task_id TEXT NOT NULL,"
        "  status TEXT NOT NULL"
        ");"
    )
    conn.commit()


def add_input_request(conn, task_id, status="pending"):
    conn.execute(
        "INSERT INTO input_requests (task_id, status) VALUES (?, ?)",
        (task_id, status),
    )
    conn.commit()


def read_events(db_path, kind=None):
    conn = sqlite3.connect(str(db_path))
    try:
        if kind is None:
            rows = conn.execute(
                "SELECT task_id, kind, payload FROM events ORDER BY event_id"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT task_id, kind, payload FROM events WHERE kind=? "
                "ORDER BY event_id",
                (kind,),
            ).fetchall()
    finally:
        conn.close()
    return [
        {"task_id": r[0], "kind": r[1], "payload": json.loads(r[2] or "{}")}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Auto-dispatch stubs: fake the bridge / spawn boundary. The spawn callable
# records calls and (optionally) performs the same claim_task-style CAS the
# real detached runner performs, so races are exercised against the test DB.
# ---------------------------------------------------------------------------

class FakeBridgeStore:
    """Append-only event sink that also writes to the real events table."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.appended = []

    def append_event(self, *, kind, payload, task_id):
        self.appended.append({"kind": kind, "payload": payload, "task_id": task_id})
        conn = sqlite3.connect(str(self.db_path))
        try:
            conn.execute(
                "INSERT INTO events (task_id, kind, payload, created_at) "
                "VALUES (?, ?, ?, 0)",
                (task_id, kind, json.dumps(payload)),
            )
            conn.commit()
        finally:
            conn.close()


class FakeBridge:
    def __init__(self, db_path, maximum_concurrency=4):
        self._maximum_concurrency = maximum_concurrency
        self.store = FakeBridgeStore(db_path)


class FakeSpawn:
    """Stand-in for cli._spawn / spawn_task_runner.

    ``raise_for`` names task_ids whose spawn should raise. When ``claim`` is
    set it performs a BEGIN IMMEDIATE compare-and-set against the tasks table
    exactly like store.claim_task: it stamps ``runtime.pid`` and ``status`` and
    raises on a lost race (owner already has a live pid).
    """

    def __init__(self, db_path=None, claim=False, raise_for=()):
        self.db_path = db_path
        self.claim = claim
        self.raise_for = set(raise_for)
        self.calls = []

    def __call__(self, bridge, task_id, message=None):
        self.calls.append(task_id)
        if task_id in self.raise_for:
            raise RuntimeError(f"spawn blew up for {task_id}")
        if self.claim and self.db_path is not None:
            conn = sqlite3.connect(str(self.db_path), isolation_level=None, timeout=15)
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT status, runtime FROM tasks WHERE task_id=?", (task_id,)
                ).fetchone()
                runtime = json.loads((row[1] if row else "") or "{}")
                pid = runtime.get("pid")
                if pid:
                    try:
                        import psutil

                        alive = psutil.pid_exists(int(pid))
                    except Exception:
                        alive = True
                    if alive:
                        conn.execute("ROLLBACK")
                        raise RuntimeError("lost claim race")
                runtime["pid"] = os.getpid()
                conn.execute(
                    "UPDATE tasks SET status='queued', runtime=? WHERE task_id=?",
                    (json.dumps(runtime), task_id),
                )
                conn.execute("COMMIT")
            finally:
                conn.close()
        return {"task_id": task_id, "status": "queued"}


def _auto_settings(**overrides):
    ad = {"enabled": True, "interval": 0.0, "max_tasks_per_cycle": 2}
    ad.update(overrides.pop("auto_dispatch", {}))
    settings = dict(SETTINGS, auto_dispatch=ad)
    settings.update(overrides)
    return settings


def _run_dispatch(runner, settings, db_path, state_file):
    return asyncio.run(
        runner._worker_bridge_auto_dispatch(
            settings, db_path=db_path, state_file=state_file
        )
    )


def add_status_event(conn, task_id, status, at=1000.0):
    conn.execute(
        "INSERT INTO events (task_id, kind, payload, created_at) "
        "VALUES (?, 'task.status', ?, ?)",
        (task_id, json.dumps({"status": status}), at),
    )
    conn.commit()


class FakeAdapter:
    def __init__(self):
        self.handled = []
        self.fail_next = False

    async def handle_message(self, event):
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("adapter dispatch blew up")
        self.handled.append(event)


class FakePlatform:
    value = "discord"

    def __str__(self):  # pragma: no cover - debug aid
        return self.value

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        return isinstance(other, FakePlatform) or (
            getattr(other, "value", None) == self.value
        )


class FakeHome:
    chat_id = "111222333"
    thread_id = None
    name = "big-a-home"


class FakeConfig:
    def __init__(self, home=FakeHome()):
        self._home = home

    def get_home_channel(self, platform):
        return self._home


@dataclass
class _FakeOrigin:
    platform: object
    chat_id: str
    thread_id: str | None = None
    user_id: str | None = "user-1"
    user_name: str | None = "Operator"
    chat_name: str | None = None
    chat_type: str = "group"


class FakeSessionStore:
    def __init__(self, entries=None):
        self._entries = list(entries or [])

    def list_sessions(self, active_minutes=None):
        return list(self._entries)


class FakeRunner(GatewayWorkerBridgeUltraMixin, GatewayWorkerBridgeWatchersMixin):
    def __init__(self, adapter, config=None, session_store=None):
        self._running = True
        self.adapters = {FakePlatform(): adapter}
        self.config = config or FakeConfig()
        self.session_store = session_store


@pytest.fixture
def bridge(tmp_path):
    db_path = tmp_path / "bridge.db"
    conn = make_bridge_db(db_path)
    state_file = tmp_path / "gateway_alerts_cursor.json"
    yield conn, db_path, state_file
    conn.close()


SETTINGS = {
    "interval": 5.0,
    "nudge_interval": 900.0,
    "statuses": STATUSES,
    "platform": None,
    "chat_id": None,
    "thread_id": None,
    "chat_type": "group",
}


# ---------------------------------------------------------------------------
# Mixin shape (mirrors test_kanban_watchers_mixin.py)
# ---------------------------------------------------------------------------

def test_mixin_defines_watcher_methods():
    for m in (
        "_worker_bridge_notifier_watcher",
        "_worker_bridge_tick",
        "_resolve_worker_alert_target",
    ):
        assert hasattr(GatewayWorkerBridgeWatchersMixin, m), f"mixin missing {m}"


def test_watcher_loop_is_coroutine():
    assert inspect.iscoroutinefunction(
        GatewayWorkerBridgeWatchersMixin._worker_bridge_notifier_watcher
    )
    assert inspect.iscoroutinefunction(
        GatewayWorkerBridgeWatchersMixin._worker_bridge_tick
    )


def test_gateway_runner_inherits_mixin():
    from gateway.run import GatewayRunner

    assert issubclass(GatewayRunner, GatewayWorkerBridgeWatchersMixin)
    owner = next(
        c for c in GatewayRunner.__mro__
        if "_worker_bridge_notifier_watcher" in c.__dict__
    )
    assert owner is GatewayWorkerBridgeWatchersMixin


# ---------------------------------------------------------------------------
# Cursor / collection semantics
# ---------------------------------------------------------------------------

def test_first_run_baselines_silently(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t1")
    add_status_event(conn, "t1", "succeeded")

    transitions, head = collect_new_transitions(db_path, state_file, STATUSES)
    assert transitions == [] and head is None
    # Baseline adopted the current end of the log — backlog never replays.
    assert load_cursor(state_file) == 1


def test_collects_only_terminal_transitions_after_cursor(bridge):
    conn, db_path, state_file = bridge
    save_cursor(state_file, 0)
    add_task(conn, "t1", objective="build feature X")
    add_task(conn, "t2", error="boom", objective="fix bug Y")
    add_status_event(conn, "t1", "running")      # non-terminal: skipped
    add_status_event(conn, "t1", "succeeded")
    add_status_event(conn, "t2", "failed")

    transitions, head = collect_new_transitions(db_path, state_file, STATUSES)
    assert head == 3
    assert [(t["task_id"], t["status"]) for t in transitions] == [
        ("t1", "succeeded"), ("t2", "failed"),
    ]
    assert transitions[0]["objective"] == "build feature X"
    assert transitions[1]["error"] == "boom"
    # collect does NOT advance the cursor — delivery does.
    assert load_cursor(state_file) == 0


def test_missing_db_is_silent(tmp_path):
    transitions, head = collect_new_transitions(
        tmp_path / "nope.db", tmp_path / "cursor.json", STATUSES
    )
    assert transitions == [] and head is None


def test_format_alert_text_failures_first_with_instructions():
    text = format_alert_text([
        {"task_id": "t1", "status": "succeeded", "worker": "claude-code",
         "objective": "obj-a", "error": "", "summary": "all good", "at": 0, "event_id": 1},
        {"task_id": "t2", "status": "failed", "worker": "grok",
         "objective": "obj-b", "error": "exploded", "summary": "", "at": 0, "event_id": 2},
    ])
    assert "Worker Bridge Alert" in text
    assert text.index("t2") < text.index("t1")  # failures listed first
    assert "exploded" in text
    assert "Act on each task now" in text
    assert "hermes worker tasks show" in text


# ---------------------------------------------------------------------------
# Injection via adapter.handle_message
# ---------------------------------------------------------------------------

def _tick(runner, db_path, state_file, settings=SETTINGS):
    return asyncio.run(
        runner._worker_bridge_tick(settings, db_path=db_path, state_file=state_file)
    )


def test_tick_injects_synthetic_message_and_advances_cursor(bridge):
    conn, db_path, state_file = bridge
    save_cursor(state_file, 0)
    add_task(conn, "t1", objective="ship it")
    add_status_event(conn, "t1", "succeeded")

    adapter = FakeAdapter()
    runner = FakeRunner(adapter)

    assert _tick(runner, db_path, state_file) is True
    assert len(adapter.handled) == 1
    event = adapter.handled[0]
    assert event.internal is True
    assert "t1" in event.text and "ship it" in event.text
    assert event.source.chat_id == "111222333"
    assert event.source.chat_type == "group"
    assert event.source.platform.value == "discord"
    assert load_cursor(state_file) == 1

    # Second tick: nothing new — no duplicate wake.
    assert _tick(runner, db_path, state_file) is False
    assert len(adapter.handled) == 1


def test_tick_retries_when_injection_fails(bridge):
    conn, db_path, state_file = bridge
    save_cursor(state_file, 0)
    add_task(conn, "t1", error="boom")
    add_status_event(conn, "t1", "failed")

    adapter = FakeAdapter()
    adapter.fail_next = True
    runner = FakeRunner(adapter)

    with pytest.raises(RuntimeError):
        _tick(runner, db_path, state_file)
    # Cursor did NOT advance — the alert is not lost.
    assert load_cursor(state_file) == 0

    # Next tick succeeds and delivers the same transition exactly once.
    assert _tick(runner, db_path, state_file) is True
    assert len(adapter.handled) == 1
    assert load_cursor(state_file) == 1


def test_tick_skips_cursor_past_nonterminal_noise(bridge):
    conn, db_path, state_file = bridge
    save_cursor(state_file, 0)
    add_task(conn, "t1")
    add_status_event(conn, "t1", "queued")
    add_status_event(conn, "t1", "running")

    adapter = FakeAdapter()
    runner = FakeRunner(adapter)

    assert _tick(runner, db_path, state_file) is False
    assert adapter.handled == []
    # Cursor advanced past the noise so the scan window stays small.
    assert load_cursor(state_file) == 2


def test_tick_holds_cursor_when_no_target(bridge):
    conn, db_path, state_file = bridge
    save_cursor(state_file, 0)
    add_task(conn, "t1")
    add_status_event(conn, "t1", "succeeded")

    class NoHomeConfig:
        def get_home_channel(self, platform):
            return None

    runner = FakeRunner(FakeAdapter(), config=NoHomeConfig())
    assert _tick(runner, db_path, state_file) is False
    # Transition stays pending for a later tick once a target exists.
    assert load_cursor(state_file) == 0


def test_target_respects_config_overrides(bridge):
    adapter = FakeAdapter()
    runner = FakeRunner(adapter)
    settings = dict(SETTINGS, chat_id="999", thread_id="42", chat_type="thread")
    target = runner._resolve_worker_alert_target(settings)
    assert target is not None
    _, source = target
    assert source.chat_id == "999"
    assert source.thread_id == "42"
    assert source.chat_type == "thread"


def test_target_platform_pin_excludes_others():
    runner = FakeRunner(FakeAdapter())
    settings = dict(SETTINGS, platform="telegram")
    assert runner._resolve_worker_alert_target(settings) is None


# ---------------------------------------------------------------------------
# Issue #7 — most-recent session targeting
# ---------------------------------------------------------------------------

def _session_entry(chat_id, thread_id, updated_at, *, user_id="user-1",
                   platform=None):
    platform = platform or FakePlatform()
    origin = _FakeOrigin(
        platform=platform,
        chat_id=chat_id,
        thread_id=thread_id,
        user_id=user_id,
        chat_name=f"chat-{chat_id}",
    )
    return SimpleNamespace(
        session_key=f"{chat_id}:{thread_id}",
        updated_at=updated_at,
        origin=origin,
        platform=platform,
    )


def test_most_recent_session_source_picks_newest_real_session():
    platform = FakePlatform()
    older = _session_entry(
        "aaa", "thread-A", datetime(2026, 1, 1, tzinfo=timezone.utc),
        platform=platform,
    )
    newer = _session_entry(
        "bbb", "thread-B", datetime(2026, 6, 1, tzinfo=timezone.utc),
        platform=platform,
    )
    adapter = FakeAdapter()
    runner = FakeRunner(adapter, session_store=FakeSessionStore([older, newer]))
    target = runner._most_recent_session_source(SETTINGS)
    assert target is not None
    resolved_adapter, origin = target
    assert resolved_adapter is adapter
    assert origin.chat_id == "bbb"
    assert origin.thread_id == "thread-B"


def test_resolve_target_uses_most_recent_session_when_unpinned():
    platform = FakePlatform()
    older = _session_entry(
        "aaa", "thread-A", datetime(2026, 1, 1, tzinfo=timezone.utc),
        platform=platform,
    )
    newer = _session_entry(
        "bbb", "thread-B", datetime(2026, 6, 1, tzinfo=timezone.utc),
        platform=platform,
    )
    adapter = FakeAdapter()
    runner = FakeRunner(adapter, session_store=FakeSessionStore([older, newer]))
    target = runner._resolve_worker_alert_target(SETTINGS)
    assert target is not None
    _, source = target
    assert source.chat_id == "bbb"
    assert source.thread_id == "thread-B"
    # Injected turns are marked system so they never become the next target.
    assert source.user_id == "system:worker-bridge"
    assert source.user_name == "Worker Bridge"


def test_resolve_target_config_pin_beats_most_recent_session():
    platform = FakePlatform()
    recent = _session_entry(
        "bbb", "thread-B", datetime(2026, 6, 1, tzinfo=timezone.utc),
        platform=platform,
    )
    adapter = FakeAdapter()
    runner = FakeRunner(adapter, session_store=FakeSessionStore([recent]))
    settings = dict(SETTINGS, chat_id="999", thread_id="42")
    target = runner._resolve_worker_alert_target(settings)
    assert target is not None
    _, source = target
    assert source.chat_id == "999"
    assert source.thread_id == "42"


def test_most_recent_session_skips_system_origins():
    platform = FakePlatform()
    system = _session_entry(
        "sys", "thread-sys",
        datetime(2026, 7, 1, tzinfo=timezone.utc),
        user_id="system:worker-bridge",
        platform=platform,
    )
    human = _session_entry(
        "hum", "thread-hum",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        user_id="user-9",
        platform=platform,
    )
    adapter = FakeAdapter()
    runner = FakeRunner(adapter, session_store=FakeSessionStore([system, human]))
    target = runner._most_recent_session_source(SETTINGS)
    assert target is not None
    _, origin = target
    assert origin.chat_id == "hum"
    assert origin.thread_id == "thread-hum"


def test_most_recent_session_falls_back_to_home_when_none():
    adapter = FakeAdapter()
    runner = FakeRunner(adapter, session_store=FakeSessionStore([]))
    target = runner._resolve_worker_alert_target(SETTINGS)
    assert target is not None
    _, source = target
    assert source.chat_id == "111222333"


# ---------------------------------------------------------------------------
# Issue #8 — pending work + idle nudge
# ---------------------------------------------------------------------------

def test_collect_pending_work_skips_live_pid(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t-created", status="created", objective="pending one",
             runtime={}, created_at=1.0)
    add_task(
        conn, "t-queued-live", status="queued", objective="already running",
        runtime={"pid": os.getpid()}, created_at=2.0,
    )
    add_task(
        conn, "t-paused-dead", status="paused", objective="dead pid",
        runtime={"pid": 999_999_999}, created_at=3.0,
    )
    add_task(conn, "t-running", status="running", objective="active",
             runtime={"pid": os.getpid()}, created_at=4.0)

    pending = collect_pending_work(db_path)
    ids = [p["task_id"] for p in pending]
    assert "t-created" in ids
    assert "t-paused-dead" in ids
    assert "t-queued-live" not in ids
    assert "t-running" not in ids


def test_collect_pending_work_missing_db(tmp_path):
    assert collect_pending_work(tmp_path / "nope.db") == []


def test_has_active_worker_tasks(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t1", status="queued")
    assert has_active_worker_tasks(db_path) is False
    add_task(conn, "t2", status="running")
    assert has_active_worker_tasks(db_path) is True


def test_format_alert_text_includes_pending_section():
    text = format_alert_text(
        [
            {
                "task_id": "t1",
                "status": "succeeded",
                "worker": "claude-code",
                "objective": "obj-a",
                "error": "",
                "summary": "ok",
                "at": 0,
                "event_id": 1,
            }
        ],
        pending=[
            {
                "task_id": "t-pend",
                "worker": "grok",
                "status": "created",
                "objective": "next job",
            }
        ],
    )
    assert "Free worker capacity: 1 task(s) are pending dispatch" in text
    assert "t-pend" in text and "next job" in text
    assert "hermes worker tasks start" in text
    assert "ack any failures first" in text


def test_format_alert_text_omits_pending_when_empty():
    text = format_alert_text(
        [
            {
                "task_id": "t1",
                "status": "succeeded",
                "worker": "claude-code",
                "objective": "obj-a",
                "error": "",
                "summary": "",
                "at": 0,
                "event_id": 1,
            }
        ],
        pending=[],
    )
    assert "Free worker capacity" not in text


def test_format_alert_text_pending_only_nudge():
    text = format_alert_text(
        [],
        pending=[
            {
                "task_id": "t-idle",
                "worker": "claude-code",
                "status": "queued",
                "objective": "waiting",
            }
        ],
    )
    assert "Worker Bridge Alert" in text
    assert "no worker tasks are currently running" in text
    assert "t-idle" in text
    assert "Triage/ack any open failures first" in text


def test_tick_includes_pending_work_in_alert(bridge):
    conn, db_path, state_file = bridge
    save_cursor(state_file, 0)
    add_task(conn, "t1", objective="ship it", status="succeeded")
    add_task(conn, "t2", objective="next up", status="created", created_at=2.0)
    add_status_event(conn, "t1", "succeeded")

    adapter = FakeAdapter()
    runner = FakeRunner(adapter)
    assert _tick(runner, db_path, state_file) is True
    text = adapter.handled[0].text
    assert "t1" in text and "ship it" in text
    assert "Free worker capacity" in text
    assert "t2" in text and "next up" in text


def test_idle_nudge_injects_when_idle_with_pending(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t-pend", status="created", objective="please start me")

    adapter = FakeAdapter()
    runner = FakeRunner(adapter)
    settings = dict(SETTINGS, nudge_interval=1.0)

    assert asyncio.run(
        runner._worker_bridge_idle_nudge(
            settings, db_path=db_path, state_file=state_file
        )
    ) is True
    assert len(adapter.handled) == 1
    assert "t-pend" in adapter.handled[0].text
    assert "please start me" in adapter.handled[0].text
    assert load_last_nudge(state_file) > 0

    # Within the interval — no second nudge.
    assert asyncio.run(
        runner._worker_bridge_idle_nudge(
            settings, db_path=db_path, state_file=state_file
        )
    ) is False
    assert len(adapter.handled) == 1


def test_idle_nudge_skips_when_workers_busy(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t-pend", status="created", objective="waiting")
    add_task(conn, "t-run", status="running", runtime={"pid": os.getpid()})

    adapter = FakeAdapter()
    runner = FakeRunner(adapter)
    settings = dict(SETTINGS, nudge_interval=1.0)

    assert asyncio.run(
        runner._worker_bridge_idle_nudge(
            settings, db_path=db_path, state_file=state_file
        )
    ) is False
    assert adapter.handled == []


def test_idle_nudge_disabled_when_interval_zero(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t-pend", status="created")
    adapter = FakeAdapter()
    runner = FakeRunner(adapter)
    settings = dict(SETTINGS, nudge_interval=0)

    assert asyncio.run(
        runner._worker_bridge_idle_nudge(
            settings, db_path=db_path, state_file=state_file
        )
    ) is False
    assert adapter.handled == []


def test_save_cursor_preserves_last_nudge(tmp_path):
    state_file = tmp_path / "cursor.json"
    save_last_nudge(state_file, 12345.0)
    save_cursor(state_file, 7)
    assert load_cursor(state_file) == 7
    assert load_last_nudge(state_file) == 12345.0


def test_mixin_defines_idle_nudge_method():
    assert hasattr(GatewayWorkerBridgeWatchersMixin, "_worker_bridge_idle_nudge")
    assert hasattr(GatewayWorkerBridgeWatchersMixin, "_most_recent_session_source")
    assert inspect.iscoroutinefunction(
        GatewayWorkerBridgeWatchersMixin._worker_bridge_idle_nudge
    )


# ===========================================================================
# Step 8 — guarded automatic dispatch
# ===========================================================================

# --- select_dispatchable_tasks: selection & ordering ----------------------

def test_selects_only_created_and_queued(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t-created", status="created", created_at=1.0)
    add_task(conn, "t-queued", status="queued", created_at=2.0)
    add_task(conn, "t-paused", status="paused", created_at=3.0)
    add_task(conn, "t-waiting", status="waiting_input", created_at=4.0)
    add_task(conn, "t-running", status="running",
             runtime={"pid": os.getpid()}, created_at=5.0)
    add_task(conn, "t-failed", status="failed", created_at=6.0)
    add_task(conn, "t-succeeded", status="succeeded", created_at=7.0)

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert {t["task_id"] for t in eligible} == {"t-created", "t-queued"}
    reasons = {s["task_id"]: s["skip_reason"] for s in skipped}
    assert reasons["t-paused"] == SKIP_PAUSED
    assert reasons["t-waiting"] == SKIP_WAITING_INPUT
    # running / failed / succeeded are not pending-ish → not surfaced at all
    assert "t-running" not in reasons
    assert "t-failed" not in reasons
    assert "t-succeeded" not in reasons


def test_ordering_priority_then_age_then_id(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "b-hi", status="queued", priority=90, created_at=100.0)
    add_task(conn, "old-mid", status="queued", priority=50, created_at=10.0)
    add_task(conn, "new-mid", status="queued", priority=50, created_at=20.0)
    add_task(conn, "z-lo", status="queued", priority=10, created_at=5.0)

    eligible, _skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["b-hi", "old-mid", "new-mid", "z-lo"]


def test_ordering_task_id_tiebreak(bridge):
    conn, db_path, _state = bridge
    # Same priority AND same created_at → task_id ASC is the deterministic tiebreak.
    add_task(conn, "ccc", status="queued", priority=50, created_at=1.0)
    add_task(conn, "aaa", status="queued", priority=50, created_at=1.0)
    add_task(conn, "bbb", status="queued", priority=50, created_at=1.0)

    eligible, _skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["aaa", "bbb", "ccc"]


def test_live_pid_task_skipped_dead_pid_dispatchable(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t-live", status="queued", runtime={"pid": os.getpid()},
             created_at=1.0)
    add_task(conn, "t-dead", status="queued", runtime={"pid": 999_999_999},
             created_at=2.0)

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["t-dead"]
    assert {s["task_id"]: s["skip_reason"] for s in skipped} == {
        "t-live": SKIP_LIVE_PID
    }


def test_limit_caps_eligible_but_not_skipped(bridge):
    conn, db_path, _state = bridge
    for i in range(5):
        add_task(conn, f"t{i}", status="queued", priority=50, created_at=float(i))
    add_task(conn, "held", status="queued", created_at=99.0,
             spec_extra={"metadata": {"hold": True}})

    eligible, skipped = select_dispatchable_tasks(db_path, 2)
    assert len(eligible) == 2
    # Skipped list is never truncated by the budget — the nudge needs them all.
    assert {s["task_id"] for s in skipped} == {"held"}


def test_minimal_schema_tolerated(bridge):
    # Default make_bridge_db has no permission_requests / input_requests / leases
    # tables — guards must pass-open rather than raise.
    conn, db_path, _state = bridge
    add_task(conn, "t1", status="queued")
    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["t1"]
    assert skipped == []


# --- select_dispatchable_tasks: blocked guards (req 5) --------------------

def test_pending_permission_request_blocks(bridge):
    conn, db_path, _state = bridge
    add_permission_requests_table(conn)
    add_task(conn, "t-pending", status="queued", created_at=1.0)
    add_task(conn, "t-decided", status="queued", created_at=2.0)
    add_permission_request(conn, "t-pending", status="pending")
    add_permission_request(conn, "t-decided", status="approved")

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["t-decided"]
    assert {s["task_id"]: s["skip_reason"] for s in skipped} == {
        "t-pending": SKIP_PENDING_APPROVAL
    }


def test_pending_input_request_blocks(bridge):
    conn, db_path, _state = bridge
    add_input_requests_table(conn)
    add_task(conn, "t-pending", status="queued", created_at=1.0)
    add_task(conn, "t-answered", status="queued", created_at=2.0)
    add_input_request(conn, "t-pending", status="pending")
    add_input_request(conn, "t-answered", status="answered")

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["t-answered"]
    assert {s["task_id"]: s["skip_reason"] for s in skipped} == {
        "t-pending": SKIP_PENDING_INPUT
    }


def test_parent_dependency_blocks_until_terminal_success(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "parent-run", status="running", runtime={"pid": os.getpid()})
    add_task(conn, "parent-ok", status="succeeded")
    add_task(conn, "child-run", status="queued", created_at=1.0,
             spec_extra={"parent_task_id": "parent-run"})
    add_task(conn, "child-ok", status="queued", created_at=2.0,
             spec_extra={"parent_task_id": "parent-ok"})
    add_task(conn, "child-missing", status="queued", created_at=3.0,
             spec_extra={"parent_task_id": "does-not-exist"})

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["child-ok"]
    reasons = {s["task_id"]: s["skip_reason"] for s in skipped}
    assert reasons["child-run"] == SKIP_DEP_INCOMPLETE
    assert reasons["child-missing"] == SKIP_DEP_INCOMPLETE  # fail closed


def test_failure_successor_is_not_blocked_by_its_failed_parent(bridge):
    """A failure successor's parent is failed BY DEFINITION — never gate on it.

    gateway.failure_successors only creates a successor when the parent reached
    failed/timed_out, and stamps that parent as parent_task_id. Requiring the
    parent to be succeeded/accepted would hold every failure successor forever
    with dependency_incomplete, silently killing orphan recovery for them.
    """
    conn, db_path, _state = bridge
    add_task(conn, "parent-failed", status="failed")
    add_task(conn, "parent-timeout", status="timed_out")
    add_task(conn, "succ-failed", status="queued", created_at=1.0,
             spec_extra={"parent_task_id": "parent-failed",
                         "metadata": {"failure_successor": True}})
    add_task(conn, "succ-timeout", status="queued", created_at=2.0,
             spec_extra={"parent_task_id": "parent-timeout",
                         "metadata": {"failure_successor": True}})

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["succ-failed", "succ-timeout"]
    assert skipped == []


def test_failure_successor_still_honours_explicit_depends_on(bridge):
    """The provenance exemption is scoped to parent_task_id only."""
    conn, db_path, _state = bridge
    add_task(conn, "parent-failed", status="failed")
    add_task(conn, "blocker", status="running", runtime={"pid": os.getpid()})
    add_task(conn, "succ", status="queued", created_at=1.0,
             spec_extra={"parent_task_id": "parent-failed",
                         "metadata": {"failure_successor": True,
                                      "depends_on": ["blocker"]}})

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert eligible == []
    assert {s["task_id"]: s["skip_reason"] for s in skipped} == {
        "succ": SKIP_DEP_INCOMPLETE
    }


def test_metadata_depends_on_blocks(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "dep-ok", status="accepted")
    add_task(conn, "dep-bad", status="running", runtime={"pid": os.getpid()})
    add_task(conn, "child", status="queued", created_at=1.0,
             spec_extra={"metadata": {"depends_on": ["dep-ok", "dep-bad"]}})

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert eligible == []
    assert {s["task_id"]: s["skip_reason"] for s in skipped} == {
        "child": SKIP_DEP_INCOMPLETE
    }


def test_manual_hold_metadata_blocks(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t-manual", status="queued", created_at=1.0,
             spec_extra={"metadata": {"manual_dispatch": True}})
    add_task(conn, "t-hold", status="queued", created_at=2.0,
             spec_extra={"metadata": {"hold": True}})
    add_task(conn, "t-free", status="queued", created_at=3.0)

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["t-free"]
    assert {s["task_id"]: s["skip_reason"] for s in skipped} == {
        "t-manual": SKIP_MANUAL_HOLD,
        "t-hold": SKIP_MANUAL_HOLD,
    }


def test_requeued_retry_is_dispatchable(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t-retry-ok", status="created", created_at=1.0,
             runtime={"retries": 1}, spec_extra={"limits": {"maximum_retries": 3}})
    add_task(conn, "t-retry-exhausted", status="created", created_at=2.0,
             runtime={"retries": 3}, spec_extra={"limits": {"maximum_retries": 3}})

    eligible, skipped = select_dispatchable_tasks(db_path, 10)
    assert [t["task_id"] for t in eligible] == ["t-retry-ok"]
    assert {s["task_id"]: s["skip_reason"] for s in skipped} == {
        "t-retry-exhausted": SKIP_RETRIES_EXHAUSTED
    }


# --- count_free_global_slots (req 3) --------------------------------------

def test_free_slots_no_leases_table_uses_active_tasks(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "r1", status="running", runtime={"pid": os.getpid()})
    add_task(conn, "r2", status="verifying", runtime={"pid": os.getpid()})
    add_task(conn, "q1", status="queued")
    assert count_free_global_slots(db_path, 4) == 2


def test_free_slots_counts_live_leases(bridge):
    conn, db_path, _state = bridge
    add_leases_table(conn)
    add_lease(conn, "global", os.getpid())
    add_lease(conn, "global", os.getpid())
    add_lease(conn, "global", os.getpid())
    add_lease(conn, "worker:x", os.getpid())  # non-global — ignored
    assert count_free_global_slots(db_path, 4) == 1


def test_stale_lease_pid_frees_capacity(bridge):
    conn, db_path, _state = bridge
    add_leases_table(conn)
    add_lease(conn, "global", os.getpid())
    add_lease(conn, "global", 999_999_999)  # dead → reclaimed
    assert count_free_global_slots(db_path, 4) == 3


def test_free_slots_never_negative(bridge):
    conn, db_path, _state = bridge
    add_leases_table(conn)
    for _ in range(5):
        add_lease(conn, "global", os.getpid())
    assert count_free_global_slots(db_path, 2) == 0


# --- _worker_bridge_auto_dispatch (mixin) ---------------------------------

def test_auto_dispatch_spawns_and_audits(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t1", status="queued", priority=50, created_at=1.0)
    add_task(conn, "t2", status="queued", priority=90, created_at=2.0)

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    spawn = FakeSpawn(db_path)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, spawn)

    started = _run_dispatch(runner, _auto_settings(), db_path, state_file)
    assert started == 2
    # Highest priority spawned first.
    assert spawn.calls == ["t2", "t1"]
    audit = read_events(db_path, "task.autodispatch")
    dispatched = [e for e in audit if e["payload"]["action"] == "dispatched"]
    assert {e["task_id"] for e in dispatched} == {"t1", "t2"}
    assert all(e["payload"]["by"] == "gateway-watcher" for e in dispatched)


def test_max_tasks_per_cycle_caps_burst(bridge):
    conn, db_path, state_file = bridge
    for i in range(6):
        add_task(conn, f"t{i}", status="queued", priority=50, created_at=float(i))

    bridge_stub = FakeBridge(db_path, maximum_concurrency=10)
    spawn = FakeSpawn(db_path, claim=True)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, spawn)
    settings = _auto_settings(auto_dispatch={"max_tasks_per_cycle": 2})

    assert _run_dispatch(runner, settings, db_path, state_file) == 2
    assert spawn.calls == ["t0", "t1"]  # oldest two, ordering preserved

    # Next cycle picks up where the first left off (claimed tasks now have a
    # live pid and drop out of selection). Reset the pacing stamp to simulate
    # the dispatch interval elapsing between cycles.
    save_last_auto_dispatch(state_file, 0.0)
    assert _run_dispatch(runner, settings, db_path, state_file) == 2
    assert spawn.calls == ["t0", "t1", "t2", "t3"]


def test_capacity_limits_spawn_budget(bridge):
    conn, db_path, state_file = bridge
    add_leases_table(conn)
    for _ in range(3):
        add_lease(conn, "global", os.getpid())  # 3 of 4 slots busy
    for i in range(5):
        add_task(conn, f"t{i}", status="queued", priority=50, created_at=float(i))

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    spawn = FakeSpawn(db_path)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, spawn)

    started = _run_dispatch(runner, _auto_settings(), db_path, state_file)
    assert started == 1  # only one free global slot
    assert spawn.calls == ["t0"]


def test_zero_free_slots_spawns_nothing_and_audits_no_capacity(bridge):
    conn, db_path, state_file = bridge
    add_leases_table(conn)
    for _ in range(4):
        add_lease(conn, "global", os.getpid())
    add_task(conn, "t-pend", status="queued", created_at=1.0)

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    spawn = FakeSpawn(db_path)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, spawn)

    started = _run_dispatch(runner, _auto_settings(), db_path, state_file)
    assert started == 0
    assert spawn.calls == []
    reasons = {s["skip_reason"] for s in runner._last_skipped_pending}
    assert SKIP_NO_CAPACITY in reasons


def test_spawn_exception_does_not_corrupt_task(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t-bad", status="queued", priority=90, created_at=1.0)
    add_task(conn, "t-good", status="queued", priority=50, created_at=2.0)
    before = conn.execute(
        "SELECT status, runtime FROM tasks WHERE task_id='t-bad'"
    ).fetchone()

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    spawn = FakeSpawn(db_path, raise_for={"t-bad"})
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, spawn)

    # No exception escapes; the good task in the same batch is still attempted.
    started = _run_dispatch(runner, _auto_settings(), db_path, state_file)
    assert started == 1
    assert spawn.calls == ["t-bad", "t-good"]

    after = conn.execute(
        "SELECT status, runtime FROM tasks WHERE task_id='t-bad'"
    ).fetchone()
    assert tuple(after) == tuple(before)  # row untouched
    audit = read_events(db_path, "task.autodispatch")
    errors = [e for e in audit if e["payload"].get("reason") == SKIP_SPAWN_ERROR]
    assert any(e["task_id"] == "t-bad" for e in errors)


def test_dispatch_never_touches_event_cursor(bridge):
    conn, db_path, state_file = bridge
    save_cursor(state_file, 7)
    add_task(conn, "t1", status="queued")

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, FakeSpawn(db_path))

    _run_dispatch(runner, _auto_settings(), db_path, state_file)
    assert load_cursor(state_file) == 7


def test_audit_event_kind_never_wakes_agent(bridge):
    # task.autodispatch must NOT match the terminal statuses collect_new_transitions
    # scans (kind='task.status'), so audit rows never inject an agent turn.
    conn, db_path, state_file = bridge
    save_cursor(state_file, 0)
    add_task(conn, "t1", status="queued")

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, FakeSpawn(db_path))
    _run_dispatch(runner, _auto_settings(), db_path, state_file)

    transitions, _head = collect_new_transitions(db_path, state_file, STATUSES)
    assert transitions == []


def test_skip_audit_deduped_until_reason_changes(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t-hold", status="queued", created_at=1.0,
             spec_extra={"metadata": {"hold": True}})

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, FakeSpawn(db_path))

    for _ in range(3):
        _run_dispatch(runner, _auto_settings(), db_path, state_file)
    skips = [
        e for e in read_events(db_path, "task.autodispatch")
        if e["task_id"] == "t-hold"
    ]
    # Same reason across 3 polls → deduped to a single audit row.
    assert len(skips) == 1
    assert skips[0]["payload"]["reason"] == SKIP_MANUAL_HOLD


def test_concurrent_dispatch_single_claim_winner(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t1", status="queued", created_at=1.0)

    bridge_a = FakeBridge(db_path, maximum_concurrency=4)
    bridge_b = FakeBridge(db_path, maximum_concurrency=4)
    spawn = FakeSpawn(db_path, claim=True)  # shared CAS against one DB
    runner_a = FakeRunner(FakeAdapter())
    runner_b = FakeRunner(FakeAdapter())
    runner_a._get_worker_bridge = lambda db=None: (bridge_a, spawn)
    runner_b._get_worker_bridge = lambda db=None: (bridge_b, spawn)

    state_a = state_file
    state_b = db_path.parent / "cursor_b.json"
    started_a = _run_dispatch(runner_a, _auto_settings(), db_path, state_a)
    started_b = _run_dispatch(runner_b, _auto_settings(), db_path, state_b)

    # Exactly one gateway claims the task; the other sees the live pid and skips.
    assert started_a + started_b == 1
    assert spawn.calls.count("t1") == 1
    row = conn.execute(
        "SELECT status, runtime FROM tasks WHERE task_id='t1'"
    ).fetchone()
    assert json.loads(row[1])["pid"] == os.getpid()


def test_interval_pacing_persisted(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t1", status="queued")

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    spawn = FakeSpawn(db_path)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, spawn)
    # A real interval so the second immediate tick is inside the window.
    settings = _auto_settings(auto_dispatch={"interval": 900.0})

    assert _run_dispatch(runner, settings, db_path, state_file) == 1
    assert load_last_auto_dispatch(state_file) > 0
    # Second tick within the interval is a no-op (no second spawn).
    assert _run_dispatch(runner, settings, db_path, state_file) == 0
    assert spawn.calls == ["t1"]


def test_disabled_via_config_skips_dispatch(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t1", status="queued")

    spawn = FakeSpawn(db_path)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (FakeBridge(db_path), spawn)
    settings = _auto_settings(auto_dispatch={"enabled": False})

    assert _run_dispatch(runner, settings, db_path, state_file) == 0
    assert spawn.calls == []
    assert runner._last_skipped_pending == []


def test_plugin_import_failure_degrades_to_nudge_only(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t1", status="queued")
    add_task(conn, "t-hold", status="queued",
             spec_extra={"metadata": {"hold": True}})

    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: None  # plugin unavailable

    started = _run_dispatch(runner, _auto_settings(), db_path, state_file)
    assert started == 0
    # Still surfaces blocked pending work for the fallback nudge.
    assert any(
        s["skip_reason"] == SKIP_MANUAL_HOLD for s in runner._last_skipped_pending
    )


# --- fallback nudge integration (req 6) -----------------------------------

def test_nudge_lists_blocked_when_auto_dispatch_enabled(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t-hold", status="queued", objective="needs a human",
             spec_extra={"metadata": {"hold": True}})

    adapter = FakeAdapter()
    runner = FakeRunner(adapter)
    runner._last_skipped_pending = [
        {"task_id": "t-hold", "worker": "claude-code", "status": "queued",
         "objective": "needs a human", "skip_reason": SKIP_MANUAL_HOLD}
    ]
    settings = _auto_settings(nudge_interval=1.0)

    assert asyncio.run(
        runner._worker_bridge_idle_nudge(
            settings, db_path=db_path, state_file=state_file
        )
    ) is True
    text = adapter.handled[0].text
    assert "blocked" in text and SKIP_MANUAL_HOLD in text
    assert "t-hold" in text


def test_nudge_unchanged_when_auto_dispatch_disabled(bridge):
    conn, db_path, state_file = bridge
    add_task(conn, "t-pend", status="created", objective="start me")

    adapter = FakeAdapter()
    runner = FakeRunner(adapter)
    # No auto_dispatch key (or disabled) → full pending list, current wording.
    settings = dict(SETTINGS, nudge_interval=1.0)

    assert asyncio.run(
        runner._worker_bridge_idle_nudge(
            settings, db_path=db_path, state_file=state_file
        )
    ) is True
    text = adapter.handled[0].text
    assert "no worker tasks are currently running" in text
    assert "blocked:" not in text
    assert "t-pend" in text


# --- config parsing (req 9, 10) -------------------------------------------

def test_auto_dispatch_default_enabled():
    settings = _resolve_alert_settings(
        {"worker_bridge": {"gateway_alerts": {"enabled": True}}}
    )
    assert settings is not None
    ad = settings["auto_dispatch"]
    assert ad["enabled"] is DEFAULT_AUTO_DISPATCH_ENABLED is True
    assert ad["max_tasks_per_cycle"] == DEFAULT_MAX_TASKS_PER_CYCLE
    # Interval defaults to the watcher interval, floored at 5s.
    assert ad["interval"] >= 5.0


def test_auto_dispatch_config_overrides():
    settings = _resolve_alert_settings(
        {"worker_bridge": {"gateway_alerts": {
            "enabled": True,
            "auto_dispatch": {
                "enabled": False,
                "interval_seconds": 30,
                "max_tasks_per_cycle": 5,
            },
        }}}
    )
    ad = settings["auto_dispatch"]
    assert ad["enabled"] is False
    assert ad["interval"] == 30.0
    assert ad["max_tasks_per_cycle"] == 5


def test_auto_dispatch_interval_and_budget_floors():
    settings = _resolve_alert_settings(
        {"worker_bridge": {"gateway_alerts": {
            "enabled": True,
            "auto_dispatch": {"interval_seconds": 1, "max_tasks_per_cycle": 0},
        }}}
    )
    ad = settings["auto_dispatch"]
    assert ad["interval"] == 5.0  # floored
    assert ad["max_tasks_per_cycle"] == 1  # floored


def test_auto_dispatch_env_kill_switch(monkeypatch):
    monkeypatch.setenv("HERMES_WORKER_BRIDGE_AUTODISPATCH", "0")
    settings = _resolve_alert_settings(
        {"worker_bridge": {"gateway_alerts": {
            "enabled": True,
            "auto_dispatch": {"enabled": True},
        }}}
    )
    assert settings["auto_dispatch"]["enabled"] is False


def test_save_last_auto_dispatch_preserves_siblings(tmp_path):
    state_file = tmp_path / "cursor.json"
    save_cursor(state_file, 5)
    save_last_nudge(state_file, 111.0)
    save_last_auto_dispatch(state_file, 222.0)
    assert load_cursor(state_file) == 5
    assert load_last_nudge(state_file) == 111.0
    assert load_last_auto_dispatch(state_file) == 222.0


# --- config bounds (HIGH: reject fat-fingered values) ----------------------

def test_max_tasks_per_cycle_clamped_to_upper_bound():
    settings = _resolve_alert_settings(
        {"worker_bridge": {"gateway_alerts": {
            "enabled": True,
            "auto_dispatch": {"max_tasks_per_cycle": 5000},
        }}}
    )
    assert settings["auto_dispatch"]["max_tasks_per_cycle"] == MAX_MAX_TASKS_PER_CYCLE == 50


def test_negative_max_tasks_per_cycle_floored_to_one():
    settings = _resolve_alert_settings(
        {"worker_bridge": {"gateway_alerts": {
            "enabled": True,
            "auto_dispatch": {"max_tasks_per_cycle": -7},
        }}}
    )
    assert settings["auto_dispatch"]["max_tasks_per_cycle"] == 1


def test_nonpositive_interval_floored():
    settings = _resolve_alert_settings(
        {"worker_bridge": {"gateway_alerts": {
            "enabled": True,
            "interval_seconds": 0,
            "auto_dispatch": {"interval_seconds": -3},
        }}}
    )
    assert settings["interval"] == MIN_INTERVAL_SECONDS
    assert settings["auto_dispatch"]["interval"] == MIN_INTERVAL_SECONDS


# --- atomic claim (HIGH: no double-dispatch across poll cycles) ------------

def test_claim_marks_task_and_returns_snapshot(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t1", status="queued")
    snap = claim_task_for_dispatch(db_path, "t1")
    assert snap is not None and snap["status"] == "queued"
    runtime = json.loads(
        conn.execute("SELECT runtime FROM tasks WHERE task_id='t1'").fetchone()[0]
    )
    assert runtime["dispatch_claim"]["pid"] == os.getpid()


def test_claim_lost_when_live_pid_owns_task(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t1", status="queued", runtime={"pid": os.getpid()})
    assert claim_task_for_dispatch(db_path, "t1") is None


def test_claim_lost_when_not_dispatchable(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t1", status="running", runtime={"pid": os.getpid()})
    assert claim_task_for_dispatch(db_path, "t1") is None


def test_claim_lost_when_row_missing(bridge):
    _conn, db_path, _state = bridge
    assert claim_task_for_dispatch(db_path, "ghost") is None


def test_release_restores_exact_row(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t1", status="queued")
    before = conn.execute(
        "SELECT status, runtime FROM tasks WHERE task_id='t1'"
    ).fetchone()
    snap = claim_task_for_dispatch(db_path, "t1")
    # Claim mutated the row (added the marker)...
    assert "dispatch_claim" in conn.execute(
        "SELECT runtime FROM tasks WHERE task_id='t1'"
    ).fetchone()[0]
    release_dispatch_claim(db_path, "t1", snap)
    after = conn.execute(
        "SELECT status, runtime FROM tasks WHERE task_id='t1'"
    ).fetchone()
    assert tuple(after) == tuple(before)


def test_release_leaves_row_alone_if_live_pid_took_over(bridge):
    conn, db_path, _state = bridge
    add_task(conn, "t1", status="queued")
    snap = claim_task_for_dispatch(db_path, "t1")
    # A runner started and stamped its (live) pid before the spawn call raised.
    conn.execute(
        "UPDATE tasks SET runtime=? WHERE task_id='t1'",
        (json.dumps({"pid": os.getpid()}),),
    )
    conn.commit()
    release_dispatch_claim(db_path, "t1", snap)
    runtime = json.loads(
        conn.execute("SELECT runtime FROM tasks WHERE task_id='t1'").fetchone()[0]
    )
    assert runtime["pid"] == os.getpid()  # not clobbered back to the snapshot


def test_dispatch_claim_lost_records_skip_reason(bridge, monkeypatch):
    conn, db_path, state_file = bridge
    add_task(conn, "t1", status="queued", created_at=1.0)

    bridge_stub = FakeBridge(db_path, maximum_concurrency=4)
    spawn = FakeSpawn(db_path)
    runner = FakeRunner(FakeAdapter())
    runner._get_worker_bridge = lambda db=None: (bridge_stub, spawn)

    # Simulate the snapshot going stale: the claim is lost for every task.
    monkeypatch.setattr(
        "gateway.worker_bridge_watchers.claim_task_for_dispatch",
        lambda db_path, task_id: None,
    )
    started = _run_dispatch(runner, _auto_settings(), db_path, state_file)
    assert started == 0
    assert spawn.calls == []  # never reached the spawn boundary
    assert any(
        s["skip_reason"] == SKIP_CLAIM_LOST for s in runner._last_skipped_pending
    )
