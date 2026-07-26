"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_diagnostics as kd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _task(**overrides):
    base = {
        "id": "t_demo00",
        "title": "demo task",
        "assignee": "demo",
        "status": "ready",
        "consecutive_failures": 0,
        "last_failure_error": None,
    }
    base.update(overrides)
    return base


def _event(kind, ts=None, **payload):
    return {
        "kind": kind,
        "created_at": int(ts if ts is not None else time.time()),
        "payload": payload or None,
    }


def _run(outcome="completed", run_id=1, error=None):
    return {
        "id": run_id,
        "outcome": outcome,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Each rule — positive + negative + clearing
# ---------------------------------------------------------------------------


def test_hallucinated_cards_fires_on_blocked_event():
    task = _task(status="ready")
    events = [
        _event("created", ts=100),
        _event("completion_blocked_hallucination", ts=200,
               phantom_cards=["t_bad1", "t_bad2"],
               verified_cards=["t_good1"]),
    ]
    # ``now=300`` keeps the synthetic event timestamps in scope without
    # tripping the stranded_in_ready rule (events are 100/200 epoch
    # which time.time() would treat as ~50yr old).
    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    halluc = [d for d in diags if d.kind == "hallucinated_cards"]
    assert len(halluc) == 1
    d = halluc[0]
    assert d.severity == "error"
    assert d.data["phantom_ids"] == ["t_bad1", "t_bad2"]
    # Generic recovery actions always available; comment action too.
    kinds = [a.kind for a in d.actions]
    assert "comment" in kinds
    assert "reassign" in kinds


def test_hallucinated_cards_clears_on_subsequent_completion():
    task = _task(status="done")
    events = [
        _event("completion_blocked_hallucination", ts=100, phantom_cards=["t_x"]),
        _event("completed", ts=200, summary="retry worked"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_prose_phantom_refs_fires_after_clean_completion():
    # Prose scan emits its event AFTER the completed event in the DB
    # path, but a subsequent clean completion clears it. Phantom id
    # must be valid hex — the scanner regex is ``t_[a-f0-9]{8,}``.
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="referenced t_bad", result_len=0),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_deadbeef99"], source="completion_summary"),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert len(diags) == 1
    assert diags[0].kind == "prose_phantom_refs"
    assert diags[0].severity == "warning"
    assert diags[0].data["phantom_refs"] == ["t_deadbeef99"]


def test_prose_phantom_refs_clears_on_later_clean_edit():
    task = _task(status="done")
    events = [
        _event("completed", ts=100, summary="bad"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_ffff0000cc"]),
        _event("edited", ts=200, fields=["result", "summary"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [])
    assert diags == []


def test_repeated_failures_fires_at_threshold_on_spawn():
    """A task with multiple spawn_failed runs gets a spawn-flavoured
    diagnostic (title mentions 'spawn', suggested action is ``doctor``).
    """
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [
        _run(outcome="spawn_failed", run_id=1),
        _run(outcome="spawn_failed", run_id=2),
        _run(outcome="spawn_failed", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.severity == "error"
    # CLI hints are what operators actually need here.
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("doctor" in s for s in suggested)


def test_repeated_failures_fires_on_timeout_loop():
    """The rule surfaces for timeout loops too — that's the point of
    unifying the counter. Suggested action is 'check logs', not
    'fix profile'."""
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [
        _run(outcome="timed_out", run_id=1),
        _run(outcome="timed_out", run_id=2),
        _run(outcome="timed_out", run_id=3),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_failures"
    assert d.data["most_recent_outcome"] == "timed_out"
    suggested = [a.label for a in d.actions if a.suggested]
    assert any("log" in s.lower() for s in suggested)


def test_repeated_failures_escalates_to_critical():
    task = _task(consecutive_failures=6, last_failure_error="boom")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert diags[0].severity == "critical"


def test_repeated_failures_below_threshold_silent():
    task = _task(consecutive_failures=1)
    assert kd.compute_task_diagnostics(task, [], []) == []


def test_repeated_failures_default_matches_dispatcher_failure_limit():
    """Default dispatcher auto-blocks at 2 failures, so diagnostics must
    also surface at 2 instead of waiting for the stale threshold of 3.
    """
    task = _task(status="blocked", consecutive_failures=2,
                 last_failure_error="elapsed 600s > limit 300s")
    runs = [_run(outcome="timed_out", run_id=1)]
    diags = kd.compute_task_diagnostics(task, [], runs)
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    d = repeated[0]
    assert d.data["failure_threshold"] == 2
    assert d.data["failure_limit"] == 2
    assert "default 5" not in d.detail
    assert "configured for 2" in d.detail


def test_repeated_failures_derives_threshold_from_kanban_failure_limit():
    task = _task(status="ready", consecutive_failures=2,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    assert kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    ) == []

    task = _task(status="blocked", consecutive_failures=4,
                 last_failure_error="Profile 'debugger' does not exist")
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 4}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 4
    assert repeated[0].data["failure_limit"] == 4


def test_repeated_failures_explicit_threshold_overrides_failure_limit():
    task = _task(status="ready", consecutive_failures=3,
                 last_failure_error="Profile 'debugger' does not exist")
    runs = [_run(outcome="spawn_failed", run_id=1)]
    diags = kd.compute_task_diagnostics(
        task, [], runs, config={"failure_limit": 5, "failure_threshold": 3}
    )
    repeated = [d for d in diags if d.kind == "repeated_failures"]
    assert len(repeated) == 1
    assert repeated[0].data["failure_threshold"] == 3
    assert repeated[0].data["failure_limit"] == 5


def test_config_from_kanban_config_preserves_explicit_diagnostics_threshold():
    cfg = kd.config_from_kanban_config({
        "failure_limit": 5,
        "diagnostics": {"failure_threshold": 3},
    })
    assert cfg["failure_threshold"] == 3
    assert cfg["failure_limit"] == 5


def test_repeated_crashes_counts_trailing_streak_only():
    task = _task(status="ready", assignee="crashy")
    runs = [
        _run(outcome="completed", run_id=1),
        _run(outcome="crashed", run_id=2, error="OOM"),
        _run(outcome="crashed", run_id=3, error="OOM again"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "repeated_crashes"
    # 2 consecutive crashes at the end → default threshold 2 → error severity.
    assert d.severity == "error"
    assert d.data["consecutive_crashes"] == 2


def test_repeated_crashes_breaks_on_recent_success():
    task = _task(status="ready", assignee="fixed")
    runs = [
        _run(outcome="crashed", run_id=1),
        _run(outcome="crashed", run_id=2),
        _run(outcome="completed", run_id=3),
    ]
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_repeated_crashes_escalates_on_many_crashes():
    task = _task(status="ready", assignee="x")
    runs = [_run(outcome="crashed", run_id=i) for i in range(1, 6)]  # 5 in a row
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert diags[0].severity == "critical"


def test_failure_rules_exempt_terminal_statuses():
    # A manual done (dashboard drag) ends no run, so the trailing crash
    # streak survives in run history — but done means done: neither
    # failure rule may keep flagging a terminal card.
    runs = [_run(outcome="crashed", run_id=1), _run(outcome="crashed", run_id=2)]
    for status in ("done", "archived"):
        task = _task(status=status, assignee="crashy", consecutive_failures=3)
        assert kd.compute_task_diagnostics(task, [], runs) == []


def test_failure_rules_exempt_running_retry():
    # Retrying a task (→ running) puts a fresh attempt in flight; its
    # in-flight run (no outcome) doesn't break the trailing crash scan,
    # so the past streak used to keep flagging over an active retry.
    # A running card must clear the failure/crash banner until this
    # attempt itself resolves.
    runs = [_run(outcome="crashed", run_id=1), _run(outcome="crashed", run_id=2)]
    task = _task(status="running", assignee="crashy", consecutive_failures=3)
    assert kd.compute_task_diagnostics(task, [], runs) == []


def test_stuck_in_blocked_fires_past_threshold():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48, reason="needs approval"),
    ]
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
    )
    assert len(diags) == 1
    d = diags[0]
    assert d.kind == "stuck_in_blocked"
    assert d.severity == "warning"
    assert d.data["age_hours"] >= 48


def test_stuck_in_blocked_silent_with_recent_comment():
    now = int(time.time())
    task = _task(status="blocked")
    events = [
        _event("blocked", ts=now - 3600 * 48),
        _event("commented", ts=now - 3600 * 2, author="human"),
    ]
    assert kd.compute_task_diagnostics(task, events, [], now=now) == []


def test_stuck_in_blocked_silent_when_not_blocked():
    task = _task(status="ready")
    events = [_event("blocked", ts=1000)]
    assert kd.compute_task_diagnostics(task, events, [], now=9999999) == []


def test_repeated_crashes_surfaces_actual_error_in_title():
    """The title should lead with the actual error text so operators
    see WHAT broke (e.g. rate-limit, auth, OOM) without opening logs.
    """
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error="openai: 429 Too Many Requests"),
        _run(outcome="crashed", run_id=2, error="openai: 429 Too Many Requests"),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert len(diags) == 1
    d = diags[0]
    assert "429" in d.title
    assert "Too Many Requests" in d.title
    # Full error in detail.
    assert "429 Too Many Requests" in d.detail


def test_repeated_crashes_no_error_fallback_title():
    task = _task(status="ready", assignee="x")
    runs = [
        _run(outcome="crashed", run_id=1, error=None),
        _run(outcome="crashed", run_id=2, error=None),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    assert "no error recorded" in diags[0].title


def test_repeated_failures_surfaces_actual_error_in_title():
    task = _task(consecutive_failures=5,
                 last_failure_error="insufficient_quota: billing limit reached")
    diags = kd.compute_task_diagnostics(task, [], [])
    assert len(diags) == 1
    d = diags[0]
    assert "insufficient_quota" in d.title or "billing limit" in d.title
    assert "insufficient_quota" in d.detail


def test_repeated_crashes_truncates_huge_tracebacks():
    """Full Python tracebacks can be tens of KB. The title stays one
    line (≤160 chars); the detail caps at 500 chars + ellipsis so the
    card doesn't explode visually."""
    huge = "Traceback (most recent call last):\n" + ("  File\n" * 500)
    task = _task(status="ready")
    runs = [
        _run(outcome="crashed", run_id=1, error=huge),
        _run(outcome="crashed", run_id=2, error=huge),
    ]
    diags = kd.compute_task_diagnostics(task, [], runs)
    d = diags[0]
    # Title only the first line, capped.
    assert "\n" not in d.title
    assert len(d.title) < 250
    # Detail contains the snippet with ellipsis.
    assert d.detail.endswith("…") or len(d.detail) < 700


# ---------------------------------------------------------------------------
# Severity sorting
# ---------------------------------------------------------------------------


def test_diagnostics_sorted_critical_first():
    """A task with both a critical (many spawn failures) and a warning
    (prose phantoms) diagnostic should list the critical one first.

    Status must be non-terminal: done/archived are exempt from the
    failure rules (done means done). ``now=300`` keeps the synthetic
    timestamps from tripping stranded_in_ready — same dodge as above."""
    task = _task(status="ready", consecutive_failures=10,
                 last_failure_error="nope")
    events = [
        _event("completed", ts=100, summary="referenced t_missing"),
        _event("suspected_hallucinated_references", ts=101,
               phantom_refs=["t_missing11"]),
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=300)
    kinds = [d.kind for d in diags]
    assert kinds[0] == "repeated_failures"  # critical
    assert "prose_phantom_refs" in kinds


# ---------------------------------------------------------------------------
# Integration — runs through real kanban_db so sqlite.Row fields work
# ---------------------------------------------------------------------------


def test_engine_works_on_sqlite_row_objects(kanban_home):
    """Regression: the rule functions must handle sqlite3.Row (which
    supports mapping access but not attribute access and isn't a dict)
    as well as dataclass Task / plain dict. The API layer passes Row
    objects directly.
    """
    conn = kb.connect()
    try:
        parent = kb.create_task(conn, title="p", assignee="w")
        real = kb.create_task(conn, title="r", assignee="x", created_by="w")
        with pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent,
                summary="with phantom", created_cards=[real, "t_deadbeef1"],
            )
        # Pull Row objects the way the API helper does.
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (parent,),
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        runs = list(conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id",
            (parent,),
        ).fetchall())
        diags = kd.compute_task_diagnostics(row, events, runs)
        assert len(diags) == 1
        assert diags[0].kind == "hallucinated_cards"
        assert "t_deadbeef1" in diags[0].data["phantom_ids"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Error-tolerance: a broken rule shouldn't 500 the whole compute call
# ---------------------------------------------------------------------------


def test_broken_rule_is_isolated(monkeypatch):
    def _bad_rule(task, events, runs, now, cfg):
        raise RuntimeError("synthetic rule bug")

    # Insert a broken rule at the front of the registry; subsequent
    # rules should still run and produce their diagnostics.
    monkeypatch.setattr(kd, "_RULES", [_bad_rule] + kd._RULES)

    task = _task(consecutive_failures=5, last_failure_error="e")
    diags = kd.compute_task_diagnostics(task, [], [])
    # The broken rule silently drops, the real one still fires.
    kinds = [d.kind for d in diags]
    assert "repeated_failures" in kinds


# ---------------------------------------------------------------------------
# stranded_in_ready
#
# Surfaces ready tasks that nobody has claimed within the threshold.
# Identity-agnostic by design: catches typo'd assignees, deleted profiles,
# down external worker pools, and misconfigured dispatchers in one rule.
# ---------------------------------------------------------------------------


def test_stranded_in_ready_fires_when_age_exceeds_threshold():
    """Default threshold = 30 min. A ready task promoted 45 min ago
    with no claim should fire as a warning."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    # 45 min = 2700s, threshold = 1800s.
    events = [_event("created", ts=now - 45 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].severity == "warning"
    assert stranded[0].data["age_seconds"] == 45 * 60
    assert stranded[0].data["assignee"] == "demo"


def test_stranded_in_ready_silent_below_threshold():
    """A ready task only 10 min old should NOT fire."""
    now = 100_000
    task = _task(status="ready", assignee="demo", claim_lock=None)
    events = [_event("created", ts=now - 10 * 60)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_non_ready_status():
    """Tasks not in ready status are out of scope (running tasks have
    their own crash / failure rules)."""
    now = 100_000
    for status in ("running", "blocked", "done", "todo", "triage"):
        task = _task(status=status, assignee="demo")
        events = [_event("created", ts=now - 6 * 3600)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        assert [d for d in diags if d.kind == "stranded_in_ready"] == [], status


def test_stranded_in_ready_skips_unassigned_tasks():
    """Empty assignee = `skipped_unassigned` on the dispatcher already.
    Don't double-flag here."""
    now = 100_000
    task = _task(status="ready", assignee="", claim_lock=None)
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_skips_claimed_tasks():
    """A live claim_lock means a worker is on it — even an old one. Don't
    second-guess: the run-level liveness signal owns that decision."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", claim_lock="run_xyz",
    )
    events = [_event("created", ts=now - 6 * 3600)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_uses_latest_ready_transition():
    """When multiple ready-transition events exist, the rule should
    age-from the most recent — a task reclaimed 20 min ago is NOT
    stranded for 6h even if it was first created 6h ago."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [
        _event("created", ts=now - 6 * 3600),       # 6 h ago
        _event("reclaimed", ts=now - 20 * 60),      # 20 min ago — wins
    ]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []


def test_stranded_in_ready_severity_escalates_with_age():
    """warning → error → critical at 2x and 6x threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    # Default threshold = 1800s.
    cases = [
        (45 * 60, "warning"),    # 1.5x → warning
        (90 * 60, "error"),      # 3x → error
        (4 * 3600, "critical"),  # 8x → critical
    ]
    for age, expected in cases:
        events = [_event("created", ts=now - age)]
        diags = kd.compute_task_diagnostics(task, events, [], now=now)
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1, f"age={age}"
        assert stranded[0].severity == expected, (
            f"age={age} expected {expected}, got {stranded[0].severity}"
        )


def test_stranded_in_ready_respects_config_override():
    """Config override changes the threshold."""
    now = 100_000
    task = _task(status="ready", assignee="demo")
    events = [_event("created", ts=now - 10 * 60)]  # 10 min
    # Default 30 min — wouldn't fire.
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    assert [d for d in diags if d.kind == "stranded_in_ready"] == []
    # Lower the threshold to 5 min — now it fires.
    diags = kd.compute_task_diagnostics(
        task, events, [], now=now,
        config={"stranded_threshold_seconds": 5 * 60},
    )
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1


def test_stranded_in_ready_falls_back_to_created_at():
    """When events have no ready-transition kind, the rule falls back
    to the task's ``created_at`` so an ancient stranded task isn't
    invisible just because its events got pruned."""
    now = 100_000
    task = _task(
        status="ready", assignee="demo", created_at=now - 4 * 3600,
    )
    # No qualifying events.
    events = [_event("commented", ts=now - 100)]
    diags = kd.compute_task_diagnostics(task, events, [], now=now)
    stranded = [d for d in diags if d.kind == "stranded_in_ready"]
    assert len(stranded) == 1
    assert stranded[0].data["age_seconds"] == 4 * 3600


def test_stranded_in_ready_works_on_real_db_row(kanban_home):
    """Round-trip through real kanban_db.connect() — confirms the rule
    works on sqlite3.Row objects, not just dicts."""
    import time as _t
    conn = kb.connect()
    try:
        # Create a task and force its created_at into the past.
        tid = kb.create_task(conn, title="stranded one", assignee="ghost")
        old_ts = int(_t.time()) - 90 * 60  # 90 min old
        conn.execute(
            "UPDATE tasks SET status = 'ready', created_at = ? WHERE id = ?",
            (old_ts, tid),
        )
        conn.commit()

        task_row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at",
            (tid,),
        ).fetchall())
        # Override created event timestamps too so age calc lines up.
        conn.execute(
            "UPDATE task_events SET created_at = ? WHERE task_id = ?",
            (old_ts, tid),
        )
        conn.commit()
        events = list(conn.execute(
            "SELECT * FROM task_events WHERE task_id = ?", (tid,),
        ).fetchall())

        diags = kd.compute_task_diagnostics(task_row, events, [])
        stranded = [d for d in diags if d.kind == "stranded_in_ready"]
        assert len(stranded) == 1
        assert stranded[0].data["assignee"] == "ghost"
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")


def test_triage_aux_unavailable_silent_without_config_context():
    """Low-level callers passing no config dict should not see this rule."""
    diags = kd.compute_task_diagnostics(_triage_task(), [], [])
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_main_model_visible():
    """Default `provider: auto` falls back to the main model — no warning."""
    config = {
        "auxiliary": {},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_silent_when_decomposer_explicit():
    """User explicitly configured decomposer → no warning, even without main."""
    config = {
        "auxiliary": {
            "kanban_decomposer": {"provider": "openrouter", "model": "qwen/qwen3"},
        },
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_unavailable_fires_auto_decompose_on_no_fallback():
    """auto_decompose=True, no decomposer, no main model → warn about decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": True},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert d.severity == "warning"
    assert "decomposer" in d.title.lower()
    assert d.data["auto_decompose"] is True
    assert d.data["primary_slot"] == "auxiliary.kanban_decomposer"
    suggested = [a for a in d.actions if a.suggested]
    assert suggested
    assert "auxiliary.kanban_decomposer" in suggested[0].payload["command"]


def test_triage_aux_unavailable_fires_auto_decompose_off_points_at_specifier():
    """auto_decompose=False → primary is specifier, not decomposer."""
    config = {
        "auxiliary": {},
        "kanban": {"auto_decompose": False},
    }
    diags = kd.compute_task_diagnostics(_triage_task(), [], [], config=config)
    triage = [d for d in diags if d.kind == "triage_aux_unavailable"]
    assert len(triage) == 1
    d = triage[0]
    assert "specifier" in d.title.lower()
    assert d.data["auto_decompose"] is False
    assert d.data["primary_slot"] == "auxiliary.triage_specifier"
    # And it should offer the manual specify command as an action
    labels = [a.label for a in d.actions]
    assert any("hermes kanban specify" in l for l in labels)


def test_triage_aux_unavailable_skips_non_triage_tasks():
    config = {"auxiliary": {}, "kanban": {"auto_decompose": True}}
    task = _task(status="todo")
    diags = kd.compute_task_diagnostics(task, [], [], config=config)
    assert [d for d in diags if d.kind == "triage_aux_unavailable"] == []


def test_triage_aux_status_recognises_auto_default_as_not_explicit():
    """Default `provider: auto` with empty fields → not 'explicit'."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": ""},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is False


def test_triage_aux_status_recognises_explicit_model_only():
    """Even with provider=auto, a non-empty model counts as explicit."""
    status = kd.triage_aux_status({
        "auxiliary": {
            "kanban_decomposer": {"provider": "auto", "model": "qwen/qwen3"},
        },
        "kanban": {},
    })
    assert status is not None
    assert status["decomposer_explicit"] is True


def test_config_from_runtime_config_carries_aux_and_model():
    cfg = kd.config_from_runtime_config({
        "kanban": {"failure_limit": 5, "auto_decompose": False},
        "auxiliary": {"kanban_decomposer": {"provider": "openrouter"}},
        "model": {"provider": "openrouter", "default": "qwen/qwen3"},
    })
    assert cfg["failure_threshold"] == 5
    assert cfg["kanban"]["auto_decompose"] is False
    assert cfg["auxiliary"]["kanban_decomposer"]["provider"] == "openrouter"
    assert cfg["model"]["default"] == "qwen/qwen3"


def test_config_from_runtime_config_handles_empty_input():
    assert kd.config_from_runtime_config(None) == {}
    assert kd.config_from_runtime_config({}) == {}


def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False


# ---------------------------------------------------------------------------
# Dispatcher reliability diagnostics
# ---------------------------------------------------------------------------


def _tick(**overrides):
    """Create a minimal dispatcher tick row dict."""
    import json as _json
    base = {
        "id": 1,
        "board": "default",
        "started_at": int(time.time()) - 120,
        "finished_at": int(time.time()) - 60,
        "reclaimed": 0,
        "promoted": 0,
        "spawned": 0,
        "skipped_nonspawnable_ids": "[]",
        "skipped_capacity_ids": "[]",
        "stale_claims_reclaimed": 0,
        "spawned_ids": "[]",
        "reclaimed_ids": "[]",
        "auto_blocked_ids": "[]",
        "error": None,
    }
    base.update(overrides)
    return base


class TestDispatcherNoRecentTick:
    """_rule_dispatcher_no_recent_tick fires when tick is stale."""

    def test_fires_when_no_ticks(self):
        """No ticks at all — dispatcher hasn't run."""
        task = _task(status="ready", assignee="worker-terra")
        diags = kd._rule_dispatcher_no_recent_tick(
            task, [], [], int(time.time()), {}, dispatcher_ticks=[],
        )
        assert len(diags) == 1
        assert diags[0].kind == "dispatcher_no_recent_tick"
        assert diags[0].severity == "critical"

    def test_fires_when_tick_stale(self):
        """Last tick older than threshold."""
        task = _task(status="ready", assignee="worker-terra")
        now_ts = int(time.time())
        stale_ts = now_ts - 200  # older than 180s threshold
        ticks = [_tick(id=1, finished_at=stale_ts)]
        diags = kd._rule_dispatcher_no_recent_tick(
            task, [], [], now_ts, {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 1
        assert diags[0].kind == "dispatcher_no_recent_tick"

    def test_no_fire_when_tick_recent(self):
        """Recent tick — no diagnostic."""
        task = _task(status="ready", assignee="worker-terra")
        now_ts = int(time.time())
        recent_ts = now_ts - 30  # within 180s
        ticks = [_tick(id=1, finished_at=recent_ts)]
        diags = kd._rule_dispatcher_no_recent_tick(
            task, [], [], now_ts, {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 0

    def test_no_fire_for_non_ready_task(self):
        """Only fires for ready tasks."""
        task = _task(status="running", assignee="worker-terra")
        diags = kd._rule_dispatcher_no_recent_tick(
            task, [], [], int(time.time()), {}, dispatcher_ticks=[],
        )
        assert len(diags) == 0

    def test_no_fire_for_unassigned_task(self):
        """Doesn't fire for tasks with no assignee."""
        task = _task(status="ready", assignee="")
        diags = kd._rule_dispatcher_no_recent_tick(
            task, [], [], int(time.time()), {}, dispatcher_ticks=[],
        )
        assert len(diags) == 0

    def test_no_fire_for_fresh_task_within_wait_window(self):
        """Fresh task (created < 2×threshold ago) is NOT flagged — normal wait."""
        now_ts = int(time.time())
        threshold = 180
        # Task created 200s ago — within 2×threshold (360s).
        task = _task(
            id="t_fresh",
            status="ready",
            assignee="worker-terra",
            created_at=now_ts - 200,
        )
        ticks = []
        diags = kd._rule_dispatcher_no_recent_tick(
            task, [], [], now_ts,
            {"dispatcher_tick_stale_seconds": threshold},
            dispatcher_ticks=ticks,
        )
        assert len(diags) == 0, "fresh task should not trigger diagnostic"

    def test_fires_for_aged_task_beyond_wait_window(self):
        """Task aged >= 2×threshold triggers diagnostic."""
        now_ts = int(time.time())
        threshold = 180
        # Task created 400s ago — beyond 2×threshold (360s).
        task = _task(
            id="t_aged",
            status="ready",
            assignee="worker-terra",
            created_at=now_ts - 400,
        )
        ticks = []
        diags = kd._rule_dispatcher_no_recent_tick(
            task, [], [], now_ts,
            {"dispatcher_tick_stale_seconds": threshold},
            dispatcher_ticks=ticks,
        )
        assert len(diags) == 1
        assert diags[0].severity == "critical"  # no ticks at all + aged


class TestDispatcherStaleClaim:
    """_rule_dispatcher_stale_claim fires when claim is expired."""

    def test_fires_when_claim_expired(self):
        now_ts = int(time.time())
        task = _task(
            status="running",
            assignee="worker-terra",
            claim_expires=now_ts - 120,
        )
        diags = kd._rule_dispatcher_stale_claim(
            task, [], [], now_ts, {},
        )
        assert len(diags) == 1
        assert diags[0].kind == "dispatcher_stale_claim"
        assert diags[0].severity == "warning"

    def test_no_fire_when_claim_valid(self):
        now_ts = int(time.time())
        task = _task(
            status="running",
            assignee="worker-terra",
            claim_expires=now_ts + 300,
        )
        diags = kd._rule_dispatcher_stale_claim(
            task, [], [], now_ts, {},
        )
        assert len(diags) == 0

    def test_no_fire_for_non_running_task(self):
        now_ts = int(time.time())
        task = _task(
            status="ready",
            assignee="worker-terra",
            claim_expires=now_ts - 120,
        )
        diags = kd._rule_dispatcher_stale_claim(
            task, [], [], now_ts, {},
        )
        assert len(diags) == 0


class TestDispatcherCapacityWait:
    """_rule_dispatcher_capacity_wait fires when this specific task is capacity-skipped."""

    def test_fires_when_capacity_skip_matches_task_id(self):
        task = _task(id="t_cap_test", status="ready", assignee="worker-terra")
        ticks = [_tick(
            skipped_capacity_ids=json.dumps([["t_cap_test", "worker-terra", 3]]),
        )]
        diags = kd._rule_dispatcher_capacity_wait(
            task, [], [], int(time.time()), {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 1
        assert diags[0].kind == "dispatcher_capacity_wait"

    def test_no_fire_when_different_task_id_skipped(self):
        """Only fires when THIS task's ID is in skipped_capacity_ids."""
        task = _task(id="t_other", status="ready", assignee="worker-terra")
        ticks = [_tick(
            skipped_capacity_ids=json.dumps([["t_cap_test", "worker-terra", 3]]),
        )]
        diags = kd._rule_dispatcher_capacity_wait(
            task, [], [], int(time.time()), {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 0, "different task ID should not trigger diagnostic"

    def test_no_fire_when_no_skips(self):
        task = _task(status="ready", assignee="worker-terra")
        ticks = [_tick(skipped_capacity_ids="[]")]
        diags = kd._rule_dispatcher_capacity_wait(
            task, [], [], int(time.time()), {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 0

    def test_no_fire_for_non_ready_task(self):
        task = _task(id="t_cap_test", status="running", assignee="worker-terra")
        ticks = [_tick(
            skipped_capacity_ids=json.dumps([["t_cap_test", "worker-terra", 3]]),
        )]
        diags = kd._rule_dispatcher_capacity_wait(
            task, [], [], int(time.time()), {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 0


class TestDispatcherNonspawnableAssignee:
    """_rule_dispatcher_nonspawnable_assignee fires for invalid assignees with exact task matching."""

    def test_fires_when_nonspawnable_matches_task_id_and_aged(self):
        now_ts = int(time.time())
        task = _task(
            id="t_ns_test",
            status="ready",
            assignee="typo-profile",
            created_at=now_ts - 500,  # older than 2*threshold (360s with default 180)
        )
        ticks = [_tick(
            skipped_nonspawnable_ids=json.dumps(["t_ns_test", "t_other_ns"]),
        )]
        diags = kd._rule_dispatcher_nonspawnable_assignee(
            task, [], [], now_ts, {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 1
        assert diags[0].kind == "dispatcher_nonspawnable_assignee"
        assert diags[0].severity == "error"

    def test_no_fire_when_different_task_id_nonspawnable(self):
        """Only fires when THIS task's ID is in skipped_nonspawnable_ids."""
        now_ts = int(time.time())
        task = _task(
            id="t_other", status="ready", assignee="worker-terra",
            created_at=now_ts - 500,
        )
        ticks = [_tick(
            skipped_nonspawnable_ids=json.dumps(["t_ns_test"]),
        )]
        diags = kd._rule_dispatcher_nonspawnable_assignee(
            task, [], [], now_ts, {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 0

    def test_no_fire_when_no_nonspawnable_skips(self):
        now_ts = int(time.time())
        task = _task(
            status="ready", assignee="worker-terra", created_at=now_ts - 300,
        )
        ticks = [_tick(skipped_nonspawnable_ids="[]")]
        diags = kd._rule_dispatcher_nonspawnable_assignee(
            task, [], [], now_ts, {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 0

    def test_no_fire_when_too_recent(self):
        """Don't fire if task created too recently (< 2*threshold)."""
        now_ts = int(time.time())
        task = _task(
            id="t_ns_test", status="ready", assignee="typo-profile",
            created_at=now_ts - 30,
        )
        ticks = [_tick(
            skipped_nonspawnable_ids=json.dumps(["t_ns_test"]),
        )]
        diags = kd._rule_dispatcher_nonspawnable_assignee(
            task, [], [], now_ts, {}, dispatcher_ticks=ticks,
        )
        assert len(diags) == 0


class TestDispatcherTickIntegration:
    """Integration: compute_task_diagnostics with dispatcher_ticks."""

    def test_dispatcher_ticks_passed_to_compute(self):
        """dispatcher_ticks are forwarded to rules that accept them."""
        now_ts = int(time.time())
        task = _task(status="ready", assignee="worker-terra")
        ticks = []  # No ticks — should trigger dispatcher_no_recent_tick
        diags = kd.compute_task_diagnostics(
            task, [], [], now=now_ts, dispatcher_ticks=ticks,
        )
        kinds = {d.kind for d in diags}
        assert "dispatcher_no_recent_tick" in kinds

    def test_dispatcher_ticks_none_is_backward_compatible(self):
        """dispatcher_ticks=None skips dispatcher rules."""
        now_ts = int(time.time())
        task = _task(status="ready", assignee="worker-terra")
        diags = kd.compute_task_diagnostics(
            task, [], [], now=now_ts, dispatcher_ticks=None,
        )
        kinds = {d.kind for d in diags}
        assert "dispatcher_no_recent_tick" not in kinds, (
            "dispatcher rules should not fire when dispatcher_ticks=None"
        )


class TestOldSchemaMigration:
    """Old dispatcher_ticks schema (integer columns) parses without error."""

    def test_old_schema_tick_with_int_columns_is_parseable(self):
        """Ticks written before JSON columns support the roll-forward parse."""
        import json as _json

        # Simulate an old-schema tick: integer counts, no JSON ID columns.
        old_tick = {
            "id": 1,
            "board": "default",
            "started_at": int(time.time()) - 120,
            "finished_at": int(time.time()) - 60,
            "reclaimed": 0,
            "promoted": 1,
            "spawned": 1,
            "stale_claims_reclaimed": 0,
            "error": None,
            # Old columns present — diagnostics parse them via _parse_json_list
            # which returns [] for non-string values, so rules degrade gracefully.
            "skipped_nonspawnable_ids": None,
            "skipped_capacity_ids": None,
            "spawned_ids": None,
            "reclaimed_ids": None,
            "auto_blocked_ids": None,
        }
        # capacity_wait rule: None→_parse_json_list→[] — no fire.
        task = _task(status="ready", assignee="worker-terra")
        diags = kd._rule_dispatcher_capacity_wait(
            task, [], [], int(time.time()), {}, dispatcher_ticks=[old_tick],
        )
        assert len(diags) == 0

        # nonspawnable rule: same — None→[] — no fire.
        diags2 = kd._rule_dispatcher_nonspawnable_assignee(
            task, [], [], int(time.time()), {}, dispatcher_ticks=[old_tick],
        )
        assert len(diags2) == 0


# ---------------------------------------------------------------------------
# fetch_diagnostics_dispatcher_ticks — unit + integration
# ---------------------------------------------------------------------------


class TestFetchDiagnosticsDispatcherTicks:
    """Unit + integration tests for fetch_diagnostics_dispatcher_ticks."""

    def test_returns_empty_when_no_ticks(self, kanban_home):
        """Empty list when no ticks exist — callers can distinguish 'no data'."""
        import sqlite3
        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        result = kd.fetch_diagnostics_dispatcher_ticks(conn)
        assert isinstance(result, list)
        assert len(result) == 0

    def test_returns_ticks_within_24h(self, kanban_home):
        """Ticks from the last 24 hours are returned."""
        import sqlite3
        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        for offset in (3600, 7200, 18000):
            conn.execute(
                """INSERT INTO dispatcher_ticks
                   (board, started_at, finished_at,
                    reclaimed, promoted, spawned,
                    skipped_nonspawnable_ids, skipped_capacity_ids,
                    stale_claims_reclaimed,
                    spawned_ids, reclaimed_ids, auto_blocked_ids, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("default", now - offset, now - offset + 1,
                 0, 1, 1, "[]", "[]", 0, "[]", "[]", "[]", None),
            )
        conn.commit()
        result = kd.fetch_diagnostics_dispatcher_ticks(conn)
        assert len(result) == 3

    def test_excludes_ticks_beyond_24h(self, kanban_home):
        """Ticks older than 24h are excluded."""
        import sqlite3
        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        conn.execute(
            """INSERT INTO dispatcher_ticks
               (board, started_at, finished_at,
                reclaimed, promoted, spawned,
                skipped_nonspawnable_ids, skipped_capacity_ids,
                stale_claims_reclaimed,
                spawned_ids, reclaimed_ids, auto_blocked_ids, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("default", now - 90000, now - 90000 + 1,
             0, 0, 0, "[]", "[]", 0, "[]", "[]", "[]", None),
        )
        conn.commit()
        result = kd.fetch_diagnostics_dispatcher_ticks(conn)
        assert len(result) == 0, "tick >24h old should be excluded"

    def test_bounded_to_last_10(self, kanban_home):
        """More than 10 ticks in 24h returns only the last 10."""
        import sqlite3
        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        for i in range(15):
            conn.execute(
                """INSERT INTO dispatcher_ticks
                   (board, started_at, finished_at,
                    reclaimed, promoted, spawned,
                    skipped_nonspawnable_ids, skipped_capacity_ids,
                    stale_claims_reclaimed,
                    spawned_ids, reclaimed_ids, auto_blocked_ids, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                ("default", now - (15 - i) * 300, now - (15 - i) * 300 + 1,
                 0, 1, 1, "[]", "[]", 0, "[]", "[]", "[]", None),
            )
        conn.commit()
        result = kd.fetch_diagnostics_dispatcher_ticks(conn)
        assert len(result) == 10, "should return at most 10 ticks"
        ids = [r["id"] for r in result]
        assert min(ids) > 5, "should return the last 10, not the first 10"

    def test_board_isolation(self, kanban_home):
        """Ticks from a different board are excluded."""
        import sqlite3
        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        conn.execute(
            """INSERT INTO dispatcher_ticks
               (board, started_at, finished_at,
                reclaimed, promoted, spawned,
                skipped_nonspawnable_ids, skipped_capacity_ids,
                stale_claims_reclaimed,
                spawned_ids, reclaimed_ids, auto_blocked_ids, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("other-board", now - 60, now - 30,
             0, 1, 1, "[]", "[]", 0, "[]", "[]", "[]", None),
        )
        conn.execute(
            """INSERT INTO dispatcher_ticks
               (board, started_at, finished_at,
                reclaimed, promoted, spawned,
                skipped_nonspawnable_ids, skipped_capacity_ids,
                stale_claims_reclaimed,
                spawned_ids, reclaimed_ids, auto_blocked_ids, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("default", now - 120, now - 90,
             0, 1, 1, "[]", "[]", 0, "[]", "[]", "[]", None),
        )
        conn.commit()
        result = kd.fetch_diagnostics_dispatcher_ticks(conn)
        assert len(result) == 1
        assert result[0]["board"] == "default"

    def test_handles_exception_gracefully(self):
        """Corrupt or missing DB returns empty list, never raises."""
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        result = kd.fetch_diagnostics_dispatcher_ticks(conn)
        assert isinstance(result, list)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Production caller integration tests
# ---------------------------------------------------------------------------


class TestDispatcherTickProductionCallers:
    """Verify production callers wire dispatcher_ticks into diagnostics."""

    def test_plugin_api_helper_passes_ticks(self, kanban_home):
        """_compute_task_diagnostics passes dispatcher_ticks through."""
        import sqlite3
        from plugins.kanban.dashboard import plugin_api

        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        conn.execute(
            """INSERT INTO tasks
               (id, title, assignee, status, created_at,
                consecutive_failures)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("t_prod_test", "prod test", "worker-terra", "ready",
             now - 300, 0),
        )
        conn.execute(
            """INSERT INTO dispatcher_ticks
               (board, started_at, finished_at,
                reclaimed, promoted, spawned,
                skipped_nonspawnable_ids, skipped_capacity_ids,
                stale_claims_reclaimed,
                spawned_ids, reclaimed_ids, auto_blocked_ids, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("default", now - 120, now - 60,
             0, 1, 1, "[]", "[]", 0, "[]", "[]", "[]", None),
        )
        conn.commit()
        result = plugin_api._compute_task_diagnostics(conn)
        assert isinstance(result, dict)
        task_diags = result.get("t_prod_test", [])
        kinds = {d["kind"] for d in task_diags}
        assert "dispatcher_no_recent_tick" not in kinds, (
            "recent tick should suppress the no-recent-tick diagnostic"
        )

    def test_plugin_api_handles_empty_ticks(self, kanban_home):
        """_compute_task_diagnostics works without any ticks."""
        import sqlite3
        from plugins.kanban.dashboard import plugin_api

        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        conn.execute(
            """INSERT INTO tasks
               (id, title, assignee, status, created_at,
                consecutive_failures)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("t_no_ticks", "no ticks", "worker-terra", "ready",
             now - 500, 0),
        )
        conn.commit()
        result = plugin_api._compute_task_diagnostics(conn)
        assert isinstance(result, dict)
        task_diags = result.get("t_no_ticks", [])
        kinds = {d["kind"] for d in task_diags}
        assert "dispatcher_no_recent_tick" in kinds, (
            "no ticks should trigger the no-recent-tick diagnostic"
        )

    def test_different_task_negative(self, kanban_home):
        """Diagnostics on task A not affected by ticks relevant to task B."""
        import sqlite3, json as _json
        from plugins.kanban.dashboard import plugin_api

        conn = sqlite3.connect(str(kanban_home / "kanban.db"))
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        for tid, name in [("t_a", "task A"), ("t_b", "task B")]:
            conn.execute(
                """INSERT INTO tasks
                   (id, title, assignee, status, created_at,
                    consecutive_failures)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (tid, name, "worker-terra", "ready", now - 500, 0),
            )
        conn.execute(
            """INSERT INTO dispatcher_ticks
               (board, started_at, finished_at,
                reclaimed, promoted, spawned,
                skipped_nonspawnable_ids, skipped_capacity_ids,
                stale_claims_reclaimed,
                spawned_ids, reclaimed_ids, auto_blocked_ids, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("default", now - 120, now - 60,
             0, 0, 0,
             "[]",
             _json.dumps([["t_b", "worker-terra", 3]]),
             0, "[]", "[]", "[]", None),
        )
        conn.commit()
        result = plugin_api._compute_task_diagnostics(conn)
        diags_b = result.get("t_b", [])
        kinds_b = {d["kind"] for d in diags_b}
        assert "dispatcher_capacity_wait" in kinds_b
        diags_a = result.get("t_a", [])
        kinds_a = {d["kind"] for d in diags_a}
        assert "dispatcher_capacity_wait" not in kinds_a, (
            "capacity wait for task B must not leak to task A"
        )
