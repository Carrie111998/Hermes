"""P4-B: durable dispatcher worker ↔ child-task orchestration contract.

A durable dispatcher-spawned *parent* worker fans work out with
``kanban_create``, but before this contract it had no way to follow what it
created: ``kanban_list`` / ``kanban_unblock`` are orchestrator-only, there is
no native ``log`` or ``dispatch`` tool, and the ``hermes kanban`` CLI is not
part of a worker's tool surface. These tests pin the native, scoped
substitute:

  * ``kanban_children``      — status/heartbeat of the worker's own children
  * ``kanban_child_log``     — worker-log tail for one of its own children
  * ``kanban_child_dispatch``— a capped dispatcher tick for its own children

plus the invariants around them: interactive (Slack / plain chat) sessions and
delegate_task children never see the surface, child assignees are explicit and
deterministic, child creation is idempotent across parent retries, staged
children promote 1→2→3 under the configured concurrency cap, and a worker that
exits without a terminal call leaves a recorded reason + exit evidence rather
than a stale ``running`` card.
"""
from __future__ import annotations

import json

import pytest


# ---------------------------------------------------------------------------
# Fixtures — mirror tests/tools/test_kanban_tools.py
# ---------------------------------------------------------------------------

@pytest.fixture
def parent_worker(monkeypatch, tmp_path):
    """Isolated HERMES_HOME with a running durable parent worker card."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "parent-worker")
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from pathlib import Path as _Path
    monkeypatch.setattr(_Path, "home", lambda: tmp_path)

    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="durable parent", assignee="parent-worker")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def _create_child(**kwargs) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_create(kwargs))


def _worker_schema_names() -> set[str]:
    from tools.registry import invalidate_check_fn_cache, registry
    from toolsets import resolve_toolset

    import tools.kanban_tools  # noqa: F401  — ensure registered

    invalidate_check_fn_cache()
    schema = registry.get_definitions(set(resolve_toolset("kanban")), quiet=True)
    return {s["function"].get("name") for s in schema if "function" in s}


CHILD_TOOLS = {"kanban_children", "kanban_child_log", "kanban_child_dispatch"}


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------

def test_durable_worker_sees_child_orchestration_tools(parent_worker):
    """The confirmed defect: a durable parent worker has create/show but no
    native list/log/dispatch for the children it spawns."""
    names = _worker_schema_names()
    assert CHILD_TOOLS <= names, (
        f"durable worker is missing child-orchestration tools: "
        f"{sorted(CHILD_TOOLS - names)}"
    )
    # The pre-existing worker surface must be unchanged.
    assert {"kanban_create", "kanban_show", "kanban_complete"} <= names
    # Orchestrator-only tools stay hidden from workers.
    assert "kanban_unblock" not in names
    assert "kanban_list" not in names


def test_interactive_session_never_sees_child_orchestration_tools(
    monkeypatch, tmp_path
):
    """Interactive Slack / plain-chat sessions keep their runtime restrictions:
    no HERMES_KANBAN_TASK means no child-orchestration surface at all."""
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    names = _worker_schema_names()
    assert not (CHILD_TOOLS & names), (
        f"child-orchestration tools leaked into an interactive session: "
        f"{sorted(CHILD_TOOLS & names)}"
    )


def test_delegated_child_never_sees_child_orchestration_tools(parent_worker):
    """delegate_task children run in the worker's process with its
    HERMES_KANBAN_* env, but they are not dispatcher run owners."""
    from agent.delegation_context import delegated_child_context

    with delegated_child_context("sub-1"):
        names = _worker_schema_names()
        assert not (CHILD_TOOLS & names)

        from tools import kanban_tools as kt
        for handler in (
            kt._handle_children,
            kt._handle_child_log,
            kt._handle_child_dispatch,
        ):
            out = json.loads(handler({}))
            assert out.get("ok") is not True
            assert out.get("error")


# ---------------------------------------------------------------------------
# Deterministic assignee + idempotent creation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sentinel", ["none", "-", "null", "  ", "NONE"])
def test_child_assignee_must_be_explicit(parent_worker, sentinel):
    """A child must never land unassigned (or pseudo-assigned to a CLI
    sentinel that the dispatcher would try to spawn as `hermes -p none`)."""
    out = _create_child(title="stage 1", assignee=sentinel)
    assert out.get("ok") is not True, f"{sentinel!r} was accepted as an assignee"
    assert "assignee" in out.get("error", "")


def test_child_create_is_idempotent_across_parent_retries(parent_worker):
    """The same (parent, title, assignee, parents) tuple must resolve to one
    card, so a retried parent worker cannot double-spawn its children."""
    first = _create_child(title="stage 1", assignee="worker-a")
    second = _create_child(title="stage 1", assignee="worker-a")
    assert first.get("ok") is True, first
    assert second.get("ok") is True, second
    assert first["task_id"] == second["task_id"]

    from hermes_cli import kanban_db as kb
    conn = kb.connect()
    try:
        rows = [t for t in kb.list_tasks(conn, limit=50) if t.title == "stage 1"]
    finally:
        conn.close()
    assert len(rows) == 1, f"duplicate child cards created: {rows}"
    assert rows[0].assignee == "worker-a"

    # A distinct child is still a distinct card.
    other = _create_child(title="stage 2", assignee="worker-a")
    assert other["task_id"] != first["task_id"]


def test_explicit_idempotency_key_still_honoured(parent_worker):
    a = _create_child(title="alpha", assignee="worker-a", idempotency_key="k1")
    b = _create_child(title="beta", assignee="worker-b", idempotency_key="k1")
    assert a["task_id"] == b["task_id"]


# ---------------------------------------------------------------------------
# kanban_children — scoped status/heartbeat reads
# ---------------------------------------------------------------------------

def test_children_listing_is_scoped_to_own_children(parent_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    mine = _create_child(title="stage 1", assignee="worker-a")["task_id"]
    conn = kb.connect()
    try:
        foreign = kb.create_task(conn, title="not mine", assignee="stranger")
    finally:
        conn.close()

    out = json.loads(kt._handle_children({}))
    assert out.get("ok") is True, out
    ids = {c["id"] for c in out["children"]}
    assert ids == {mine}, f"scope leak: {ids}"
    assert out["parent_task_id"] == parent_worker
    assert foreign not in ids

    child = out["children"][0]
    for field in (
        "status", "assignee", "last_heartbeat_at", "worker_pid",
        "consecutive_failures", "latest_run", "has_log",
    ):
        assert field in child, f"missing {field} in child status row"
    assert out["counts"].get(child["status"]) == 1


def test_children_reports_heartbeat_and_run_outcome(parent_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    cid = _create_child(title="stage 1", assignee="worker-a")["task_id"]
    conn = kb.connect()
    try:
        kb.claim_task(conn, cid)
        kb.heartbeat_worker(conn, cid)
    finally:
        conn.close()

    out = json.loads(kt._handle_children({}))
    row = next(c for c in out["children"] if c["id"] == cid)
    assert row["status"] == "running"
    assert row["last_heartbeat_at"] is not None
    assert row["heartbeat_age_seconds"] is not None
    assert row["latest_run"] is not None


# ---------------------------------------------------------------------------
# kanban_child_log
# ---------------------------------------------------------------------------

def test_child_log_reads_own_child_and_refuses_foreign(parent_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    cid = _create_child(title="stage 1", assignee="worker-a")["task_id"]
    log_path = kb.worker_log_path(cid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("child stdout line\n", encoding="utf-8")

    out = json.loads(kt._handle_child_log({"child_id": cid}))
    assert out.get("ok") is True, out
    assert "child stdout line" in out["log"]

    conn = kb.connect()
    try:
        foreign = kb.create_task(conn, title="not mine", assignee="stranger")
    finally:
        conn.close()
    foreign_log = kb.worker_log_path(foreign)
    foreign_log.parent.mkdir(parents=True, exist_ok=True)
    foreign_log.write_text("secret other-task output\n", encoding="utf-8")

    denied = json.loads(kt._handle_child_log({"child_id": foreign}))
    assert denied.get("ok") is not True
    assert "secret other-task output" not in json.dumps(denied)


def test_child_log_redacts_secrets(parent_worker):
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    cid = _create_child(title="stage 1", assignee="worker-a")["task_id"]
    log_path = kb.worker_log_path(cid)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "exporting sk-ant-api03-" + "A" * 80 + "\n", encoding="utf-8"
    )
    out = json.loads(kt._handle_child_log({"child_id": cid}))
    assert out.get("ok") is True
    assert "A" * 80 not in out["log"]


# ---------------------------------------------------------------------------
# kanban_child_dispatch — staged children under the concurrency cap
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_spawn(monkeypatch):
    """Replace the real subprocess spawn with a PID-returning stub."""
    from hermes_cli import kanban_db as kb

    spawned: list[str] = []
    pid = {"next": 900000}

    def _spawn(task, workspace, *, board=None):
        spawned.append(task.id)
        pid["next"] += 1
        return pid["next"]

    monkeypatch.setattr(kb, "_default_spawn", _spawn)
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda *a, **k: None)
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: True)
    # The dispatcher refuses to spawn assignees that aren't real profiles on
    # disk; these fixtures use synthetic profile names.
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _n: True)
    # Memory pressure is host state, not a property under test.
    monkeypatch.setattr(kb, "_memory_pressure_level", lambda *a, **k: "unknown")
    return spawned


def _pin_caps(monkeypatch, *, max_spawn: int, max_in_progress: int):
    cfg = {
        "kanban": {
            "max_spawn": max_spawn,
            "max_in_progress": max_in_progress,
        }
    }
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: cfg)
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)


def test_child_dispatch_requires_children(parent_worker, monkeypatch, fake_spawn):
    from tools import kanban_tools as kt

    _pin_caps(monkeypatch, max_spawn=3, max_in_progress=3)
    out = json.loads(kt._handle_child_dispatch({}))
    assert out.get("ok") is not True
    assert fake_spawn == []


def test_child_dispatch_stages_children_1_2_3(
    parent_worker, monkeypatch, fake_spawn
):
    """Staged fan-out: stage 2 must not spawn until stage 1 is done."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _pin_caps(monkeypatch, max_spawn=4, max_in_progress=4)

    c1 = _create_child(title="stage 1", assignee="worker-a")["task_id"]
    c2 = _create_child(title="stage 2", assignee="worker-b", parents=[c1])["task_id"]
    c3 = _create_child(title="stage 3", assignee="worker-c", parents=[c2])["task_id"]

    out = json.loads(kt._handle_child_dispatch({}))
    assert out.get("ok") is True, out
    assert [s["task_id"] for s in out["spawned_children"]] == [c1]
    assert fake_spawn == [c1]

    conn = kb.connect()
    try:
        kb.complete_task(conn, c1, summary="stage 1 done")
    finally:
        conn.close()

    out = json.loads(kt._handle_child_dispatch({}))
    assert [s["task_id"] for s in out["spawned_children"]] == [c2]

    conn = kb.connect()
    try:
        kb.complete_task(conn, c2, summary="stage 2 done")
    finally:
        conn.close()

    out = json.loads(kt._handle_child_dispatch({}))
    assert [s["task_id"] for s in out["spawned_children"]] == [c3]
    assert fake_spawn == [c1, c2, c3]


def test_child_dispatch_never_exceeds_configured_concurrency(
    parent_worker, monkeypatch, fake_spawn
):
    """The cap bounds live workers board-wide, and the durable parent's own
    running card consumes one slot — dispatch must respect that, not spawn a
    child per ready card."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _pin_caps(monkeypatch, max_spawn=3, max_in_progress=3)

    ids = [
        _create_child(title=f"fan {i}", assignee=f"worker-{i}")["task_id"]
        for i in range(4)
    ]

    out = json.loads(kt._handle_child_dispatch({}))
    assert out.get("ok") is True, out
    assert len(out["spawned_children"]) == 2, out["spawned_children"]

    conn = kb.connect()
    try:
        assert kb.count_running_tasks(conn) == 3
    finally:
        conn.close()

    # A second tick must not push past the cap.
    out = json.loads(kt._handle_child_dispatch({}))
    assert out["spawned_children"] == []
    conn = kb.connect()
    try:
        assert kb.count_running_tasks(conn) == 3
        statuses = {t.id: t.status for t in kb.list_tasks(conn, limit=50)}
    finally:
        conn.close()
    assert sum(1 for t in ids if statuses[t] == "ready") == 2


def test_child_dispatch_does_not_double_claim(parent_worker, monkeypatch, fake_spawn):
    """Two ticks in a row must not spawn a second worker for a claimed child."""
    from tools import kanban_tools as kt

    _pin_caps(monkeypatch, max_spawn=4, max_in_progress=4)
    _create_child(title="stage 1", assignee="worker-a")

    json.loads(kt._handle_child_dispatch({}))
    json.loads(kt._handle_child_dispatch({}))
    assert len(fake_spawn) == 1, fake_spawn


def test_child_dispatch_dry_run_spawns_nothing(parent_worker, monkeypatch, fake_spawn):
    from tools import kanban_tools as kt

    _pin_caps(monkeypatch, max_spawn=4, max_in_progress=4)
    _create_child(title="stage 1", assignee="worker-a")

    out = json.loads(kt._handle_child_dispatch({"dry_run": True}))
    assert out.get("ok") is True, out
    assert fake_spawn == []


# ---------------------------------------------------------------------------
# Worker exit evidence
# ---------------------------------------------------------------------------

def test_child_exit_records_terminal_reason_not_stale_running(
    parent_worker, monkeypatch, fake_spawn
):
    """A child whose worker process is gone must not sit in ``running``: the
    parent must be able to read a terminal reason plus exit evidence."""
    from hermes_cli import kanban_db as kb
    from tools import kanban_tools as kt

    _pin_caps(monkeypatch, max_spawn=4, max_in_progress=4)
    cid = _create_child(title="stage 1", assignee="worker-a")["task_id"]
    json.loads(kt._handle_child_dispatch({}))
    assert fake_spawn == [cid]

    conn = kb.connect()
    try:
        pid = kb.get_task(conn, cid).worker_pid
        assert pid is not None
        # Backdate started_at past the launch grace window.
        conn.execute(
            "UPDATE tasks SET started_at = started_at - 3600 WHERE id = ?", (cid,)
        )
        conn.commit()
    finally:
        conn.close()

    # The worker process is gone, and the reaper saw a non-zero exit.
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(kb, "_classify_worker_exit", lambda _pid: ("nonzero_exit", 7))

    # dry_run so the crash accounting runs but the requeued card isn't
    # immediately respawned in the same tick — we're asserting the exit
    # evidence, not the retry policy.
    json.loads(kt._handle_child_dispatch({"dry_run": True}))

    out = json.loads(kt._handle_children({}))
    row = next(c for c in out["children"] if c["id"] == cid)
    assert row["status"] != "running", "child left stale RUNNING after worker exit"
    assert row["last_failure_error"], "no terminal reason recorded"
    assert "7" in row["last_failure_error"]

    run = row["latest_run"]
    assert run["outcome"] in {"crashed", "gave_up"}, run
    conn = kb.connect()
    try:
        kinds = {e.kind for e in kb.list_events(conn, cid)}
    finally:
        conn.close()
    assert "crashed" in kinds, kinds
