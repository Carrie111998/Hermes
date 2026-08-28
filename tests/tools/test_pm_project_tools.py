"""PM tools: `project_ensure` and `plan_submit` in the kanban toolset.

Commit 10. A PM agent can now create a project row and draft plan revisions
from a tool call instead of a shell-out. What it still cannot do — and what
most of this file pins — is cross a gate: drafting a plan is not approving one,
and no tool here writes `pm_approvals`, changes a task status, or clears
`gate_state`.

The tools are orchestrator-scoped: a dispatcher-spawned task worker must not
see them, because a worker closing its own task has no business authoring the
project's plan.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def orchestrator(monkeypatch, tmp_path):
    """A profile with the kanban toolset and no owned task — the PM surface."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "pm")
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return tmp_path


def _call(name, args):
    import tools.kanban_tools  # noqa: F401  (registers the tools)
    from tools.registry import registry

    entry = registry.get(name) if hasattr(registry, "get") else None
    handler = getattr(entry, "handler", None) if entry else None
    if handler is None:                       # registry shape fallback
        handler = registry._tools[name].handler   # type: ignore[attr-defined]
    return handler(args)


def _rows(sql, params=()):
    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Registration and containment
# ---------------------------------------------------------------------------

PM_TOOLS = ("project_ensure", "plan_submit")


@pytest.mark.parametrize("name", PM_TOOLS)
def test_the_pm_tools_are_registered_in_the_kanban_toolset(name):
    import tools.kanban_tools  # noqa: F401
    from tools.registry import registry

    names = {t.name for t in registry.all_tools()} if hasattr(
        registry, "all_tools") else set(registry._tools)  # type: ignore[attr-defined]
    assert name in names
    entry = registry._tools[name]              # type: ignore[attr-defined]
    assert entry.toolset == "kanban"


@pytest.mark.parametrize("name", PM_TOOLS)
def test_the_pm_tools_are_hidden_from_a_task_worker(monkeypatch, tmp_path, name):
    """A dispatcher-spawned worker closes its own task; it does not plan."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    monkeypatch.setenv("HERMES_SESSION_SOURCE", "kanban")

    import tools.kanban_tools  # noqa: F401
    from tools.registry import invalidate_check_fn_cache, registry

    invalidate_check_fn_cache()
    entry = registry._tools[name]              # type: ignore[attr-defined]
    assert entry.check_fn is not None
    assert entry.check_fn() is False


@pytest.mark.parametrize("name", PM_TOOLS)
def test_the_pm_tools_are_hidden_from_a_plain_chat_session(monkeypatch, tmp_path, name):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    import tools.kanban_tools  # noqa: F401
    from tools.registry import invalidate_check_fn_cache, registry

    invalidate_check_fn_cache()
    entry = registry._tools[name]              # type: ignore[attr-defined]
    assert entry.check_fn() is False, "no kanban toolset ⇒ no PM tools"


# ---------------------------------------------------------------------------
# project_ensure
# ---------------------------------------------------------------------------

def test_project_ensure_creates_the_row(orchestrator):
    out = _call("project_ensure", {"project_id": "proj-1", "name": "Proj One"})
    assert "proj-1" in out
    rows = _rows("SELECT id, name, plan_revision FROM pm_projects")
    assert rows == [{"id": "proj-1", "name": "Proj One", "plan_revision": 0}]


def test_project_ensure_is_idempotent_and_does_not_rewrite_the_name(orchestrator):
    _call("project_ensure", {"project_id": "proj-1", "name": "Original"})
    _call("project_ensure", {"project_id": "proj-1", "name": "Renamed"})
    rows = _rows("SELECT id, name FROM pm_projects")
    assert rows == [{"id": "proj-1", "name": "Original"}], (
        "plan_revision is checked against this row at gate release; a silent "
        "rename around it would be surprising"
    )


@pytest.mark.parametrize("args", [{}, {"project_id": ""}, {"project_id": "   "}])
def test_project_ensure_refuses_an_empty_id(orchestrator, args):
    out = _call("project_ensure", args)
    assert "project_id" in out.lower()
    assert _rows("SELECT id FROM pm_projects") == []


# ---------------------------------------------------------------------------
# plan_submit
# ---------------------------------------------------------------------------

def test_plan_submit_records_revisions_in_order(orchestrator):
    _call("project_ensure", {"project_id": "p"})
    first = _call("plan_submit", {"project_id": "p", "body": "step one"})
    second = _call("plan_submit", {"project_id": "p", "body": "step two"})
    assert "1" in first and "2" in second
    rows = _rows("SELECT revision, body FROM pm_plans ORDER BY revision")
    assert [r["revision"] for r in rows] == [1, 2]
    assert rows[0]["body"] == "step one"
    assert _rows("SELECT plan_revision FROM pm_projects")[0]["plan_revision"] == 2


def test_plan_submit_requires_the_project_to_exist(orchestrator):
    out = _call("plan_submit", {"project_id": "missing", "body": "x"})
    assert "project_ensure" in out, "the error must name the tool that fixes it"
    assert _rows("SELECT id FROM pm_plans") == []


@pytest.mark.parametrize("body", ["", "   ", "\n\t "])
def test_plan_submit_refuses_an_empty_body(orchestrator, body):
    _call("project_ensure", {"project_id": "p"})
    out = _call("plan_submit", {"project_id": "p", "body": body})
    assert "body" in out.lower()
    assert _rows("SELECT id FROM pm_plans") == []


def test_plan_submit_records_who_proposed_it(orchestrator):
    _call("project_ensure", {"project_id": "p"})
    _call("plan_submit", {"project_id": "p", "body": "a plan"})
    row = _rows("SELECT proposed_by FROM pm_plans")[0]
    assert row["proposed_by"], "a display label, not an identity boundary"


# ---------------------------------------------------------------------------
# Neither tool can cross a gate
# ---------------------------------------------------------------------------

def test_neither_tool_writes_an_approval_or_moves_a_task(orchestrator):
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gated", assignee="pm")
        kb.ensure_pm_project(conn, project_id="p")
        kb.submit_plan(conn, project_id="p", body="v1")
        assert kb.park_for_plan_approval(
            conn, tid, project_id="p", revision=1) is True
        before = dict(conn.execute(
            "SELECT status, gate_state FROM tasks WHERE id = ?", (tid,)).fetchone())
    finally:
        conn.close()

    _call("project_ensure", {"project_id": "p"})
    _call("plan_submit", {"project_id": "p", "body": "v2"})

    after = _rows("SELECT status, gate_state FROM tasks WHERE id = ?", (tid,))[0]
    assert after == before, "a gated task must be untouched"
    assert _rows("SELECT COUNT(*) c FROM pm_approvals")[0]["c"] == 0
    assert _rows("SELECT COUNT(*) c FROM task_runs WHERE task_id = ?",
                 (tid,))[0]["c"] == 0


def test_a_new_revision_makes_the_gated_one_stale_rather_than_approved(orchestrator):
    """Superseding is the PM's power; releasing is not."""
    from hermes_cli import kanban_db as kb

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="gated", assignee="pm")
        kb.ensure_pm_project(conn, project_id="p")
        kb.submit_plan(conn, project_id="p", body="v1")
        kb.park_for_plan_approval(conn, tid, project_id="p", revision=1)
    finally:
        conn.close()

    _call("plan_submit", {"project_id": "p", "body": "v2"})

    assert _rows("SELECT plan_revision FROM pm_projects")[0]["plan_revision"] == 2
    assert _rows("SELECT gate_state FROM tasks WHERE id = ?", (tid,))[0][
        "gate_state"] == "plan"
    assert _rows("SELECT COUNT(*) c FROM pm_approvals")[0]["c"] == 0


def test_a_delegated_child_cannot_use_the_pm_tools(orchestrator, monkeypatch):
    monkeypatch.setenv("HERMES_DELEGATED_CHILD", "1")
    import tools.kanban_tools as kt
    from tools.registry import invalidate_check_fn_cache, registry

    invalidate_check_fn_cache()
    for name in PM_TOOLS:
        entry = registry._tools[name]          # type: ignore[attr-defined]
        assert entry.check_fn() is False
    # and even a direct call is refused at the write boundary
    monkeypatch.setattr(kt, "_is_delegated_child_context", lambda: True)
    out = _call("project_ensure", {"project_id": "p"})
    assert "delegat" in out.lower() or "not available" in out.lower()
    assert _rows("SELECT id FROM pm_projects") == []


def test_the_tool_output_is_machine_readable(orchestrator):
    _call("project_ensure", {"project_id": "p"})
    out = _call("plan_submit", {"project_id": "p", "body": "x"})
    payload = json.loads(out)
    assert payload["project_id"] == "p"
    assert payload["revision"] == 1


def test_the_pm_tools_never_leak_into_an_ordinary_chat_schema(monkeypatch, tmp_path):
    """Every model tool ships on every API call, so a leak here is paid for
    by every user of every profile. The kanban toolset gate is what keeps the
    footprint at zero for everyone else."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    import tools.kanban_tools  # noqa: F401
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("hermes-cli")), quiet=True)
    names = {s["function"].get("name") for s in schema if "function" in s}
    assert not (names & set(PM_TOOLS)), f"PM tools leaked: {names & set(PM_TOOLS)}"


def test_the_pm_tools_appear_for_a_profile_with_the_kanban_toolset(monkeypatch, tmp_path):
    import tools.kanban_tools as kt
    from tools.registry import invalidate_check_fn_cache, registry

    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(kt, "_profile_has_kanban_toolset", lambda: True)
    monkeypatch.setattr(kt, "_is_delegated_child_context", lambda: False)
    invalidate_check_fn_cache()
    for name in PM_TOOLS:
        assert registry._tools[name].check_fn() is True  # type: ignore[attr-defined]
