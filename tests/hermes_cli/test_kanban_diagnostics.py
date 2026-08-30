"""Tests for hermes_cli.kanban_diagnostics — rule-engine that produces
structured distress signals (diagnostics) for kanban tasks.

These tests exercise each rule in isolation using minimal in-memory
task/event/run fixtures (no DB) plus a few integration-style cases
that round-trip through the real kanban_db to make sure the rule
engine works on sqlite3.Row objects as well as dataclasses.
"""

from __future__ import annotations

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




# ---------------------------------------------------------------------------
# triage_aux_unavailable rule — auto-decompose aware
# ---------------------------------------------------------------------------


def _triage_task():
    return _task(id="t_triage1", status="triage")








def test_severity_at_or_above_uses_threshold_semantics():
    assert kd.severity_at_or_above("warning", "warning") is True
    assert kd.severity_at_or_above("error", "warning") is True
    assert kd.severity_at_or_above("critical", "warning") is True
    assert kd.severity_at_or_above("critical", "error") is True
    assert kd.severity_at_or_above("warning", "error") is False
    assert kd.severity_at_or_above("error", "critical") is False
    assert kd.severity_at_or_above("mystery", "warning") is False
    assert kd.severity_at_or_above("warning", None) is True


# ---------------------------------------------------------------------------
# role_assignee_mismatch rule
# ---------------------------------------------------------------------------


def test_role_assignee_mismatch_fires_on_common_prefixes():
    cases = [
        ("Coder: fix memory leak", "reviewer", "Coder", "coder"),
        ("Reviewer: review PR #123", "coder", "Reviewer", "reviewer"),
        ("DevOps: deploy production cluster", "architect", "DevOps", "devops"),
        ("[DevOps] deploy gateway", "reviewer", "DevOps", "devops"),
        ("Coder\uFF1Afix json crash", "reviewer", "Coder", "coder"),
        ("[Coder]: handle bytes body", "devops", "Coder", "coder"),
    ]
    now = int(time.time())
    cfg = {"profiles": ["coder", "reviewer", "devops"]}
    for title, assignee, expected_prefix, expected_role in cases:
        task = _task(title=title, assignee=assignee, status="ready")
        diags = kd.compute_task_diagnostics(task, [], [], now=now, config=cfg)
        mismatches = [d for d in diags if d.kind == "role_assignee_mismatch"]
        assert len(mismatches) == 1, f"Failed for title={title!r}, assignee={assignee!r}"
        d = mismatches[0]
        assert d.severity == "warning"
        assert d.data["role_prefix"] == expected_prefix
        assert d.data["expected_assignee"] == expected_role
        assert d.data["actual_assignee"] == assignee
        assert any(a.kind == "reassign" for a in d.actions)
        assert any(a.kind == "cli_hint" for a in d.actions)


def test_role_assignee_mismatch_does_not_fire_when_matching():
    cases = [
        ("Coder: fix memory leak", "coder"),
        ("Reviewer: review PR #123", "reviewer"),
        ("DevOps: deploy production cluster", "devops"),
        ("[DevOps] deploy gateway", "devops"),
        ("Coder\uFF1Afix json crash", "coder"),
    ]
    now = int(time.time())
    for title, assignee in cases:
        task = _task(title=title, assignee=assignee, status="ready")
        diags = kd.compute_task_diagnostics(task, [], [], now=now)
        mismatches = [d for d in diags if d.kind == "role_assignee_mismatch"]
        assert len(mismatches) == 0, f"Unexpected mismatch diagnostic for {title!r} with {assignee!r}"


def test_role_assignee_mismatch_does_not_fire_on_unrelated_prefix_or_no_prefix():
    cases = [
        ("Fix bug in parser", "reviewer"),
        ("Feature: add support for streaming", "coder"),
        ("Bug: crash on empty list", "reviewer"),
        ("http://example.com/issue/12", "coder"),
        ("123: numeric title", "coder"),
        ("Quick investigation", "devops"),
    ]
    now = int(time.time())
    for title, assignee in cases:
        task = _task(title=title, assignee=assignee, status="ready")
        diags = kd.compute_task_diagnostics(task, [], [], now=now)
        mismatches = [d for d in diags if d.kind == "role_assignee_mismatch"]
        assert len(mismatches) == 0, f"Unexpected mismatch diagnostic for non-role title {title!r}"


def test_role_assignee_mismatch_exempt_for_terminal_status():
    now = int(time.time())
    for status in ("done", "archived"):
        task = _task(title="Coder: fix bug", assignee="reviewer", status=status)
        diags = kd.compute_task_diagnostics(task, [], [], now=now)
        mismatches = [d for d in diags if d.kind == "role_assignee_mismatch"]
        assert len(mismatches) == 0, f"Terminal task ({status}) should not fire role_assignee_mismatch"


def test_role_assignee_mismatch_suppresses_suggested_reassign_for_uninstalled_role(monkeypatch):
    """When a title has a role prefix for an uninstalled/non-spawnable role (e.g. architect),
    the diagnostic fires as advisory warning but does NOT suggest reassigning to a non-spawnable target."""
    now = int(time.time())
    # Set known installed profiles to only ['coder', 'reviewer', 'devops']
    cfg = {"profiles": ["coder", "reviewer", "devops"]}
    task = _task(title="Architect: design system", assignee="reviewer", status="ready")
    diags = kd.compute_task_diagnostics(task, [], [], now=now, config=cfg)
    mismatches = [d for d in diags if d.kind == "role_assignee_mismatch"]
    assert len(mismatches) == 1
    d = mismatches[0]
    assert d.severity == "warning"
    assert d.data["role_prefix"] == "Architect"
    assert d.data["expected_assignee"] == "architect"
    # Reassign action must not be suggested when architect is not installed
    suggested_actions = [a for a in d.actions if a.suggested]
    assert len(suggested_actions) == 0, f"Uninstalled profile must not have suggested action: {suggested_actions}"


def test_role_assignee_mismatch_recognizes_installed_custom_profiles(tmp_path, monkeypatch):
    """Custom installed profiles (e.g. fable) discovered dynamically on disk
    are recognized as role prefixes and generate suggested reassignments."""
    now = int(time.time())
    # Create fake custom profile on disk
    hermes_root = tmp_path / ".hermes"
    fable_dir = hermes_root / "profiles" / "fable"
    fable_dir.mkdir(parents=True)
    (fable_dir / "config.yaml").write_text("model: custom-model\n")

    monkeypatch.setattr("hermes_constants.get_default_hermes_root", lambda: hermes_root)
    monkeypatch.setattr("hermes_constants.get_hermes_home", lambda: hermes_root)

    task = _task(title="Fable: generate world lore", assignee="reviewer", status="ready")
    # Call compute_task_diagnostics with default config (no explicit profiles passed)
    diags = kd.compute_task_diagnostics(task, [], [], now=now)
    mismatches = [d for d in diags if d.kind == "role_assignee_mismatch"]
    assert len(mismatches) == 1
    d = mismatches[0]
    assert d.data["role_prefix"] == "Fable"
    assert d.data["expected_assignee"] == "fable"
    reassign_actions = [a for a in d.actions if a.kind == "reassign" and a.suggested]
    assert len(reassign_actions) == 1
    assert reassign_actions[0].payload["suggested_assignee"] == "fable"
