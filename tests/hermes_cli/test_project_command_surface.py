"""``hermes project`` is one registered command, and its registry cannot drift.

Commit 9. The verbs already existed as an argparse subtree, but the command was
absent from ``COMMAND_REGISTRY`` — the single definition that produces the CLI
help entry, tab-completion, the gateway command list, the Telegram bot menu and
the Slack slash map. So ``hermes project`` worked in a terminal and was
invisible everywhere else.

The load-bearing test here is not "the entry exists" (a snapshot) but
"the registry and the argparse tree describe the same verbs" (a contract).
"""

import argparse
import io
import contextlib

import pytest

from hermes_cli import projects_cmd
from hermes_cli import kanban_db as kb
from hermes_cli.commands import (
    COMMANDS,
    COMMANDS_BY_CATEGORY,
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    slack_subcommand_map,
    telegram_bot_commands,
)


def _def(name="project"):
    return next((c for c in COMMAND_REGISTRY if c.name == name), None)


def _root_parser():
    root = argparse.ArgumentParser(prog="hermes")
    sub = root.add_subparsers(dest="command")
    projects_cmd.build_parser(sub)
    return root


def _argparse_actions() -> set:
    """Every action name the real ``hermes project`` parser accepts."""
    root = _root_parser()
    project = root._subparsers._group_actions[0].choices["project"]
    action = project._subparsers._group_actions[0]
    return set(action.choices)


def _run(argv, tmp_path=None):
    root = _root_parser()
    args = root.parse_args(argv)
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = projects_cmd.projects_command(args)
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def test_project_is_registered():
    assert _def() is not None, "hermes project must have a CommandDef"


def test_project_reaches_every_generated_surface():
    d = _def()
    assert "/project" in COMMANDS
    assert "/project" in COMMANDS_BY_CATEGORY[d.category]
    assert "project" in GATEWAY_KNOWN_COMMANDS
    assert "project" in {n for n, _ in telegram_bot_commands()}
    assert "project" in slack_subcommand_map()


def test_the_registry_and_the_parser_describe_the_same_verbs():
    """The anti-drift contract: neither side may gain a verb alone."""
    declared = set(_def().subcommands)
    actual = _argparse_actions()
    assert declared == actual, (
        f"only in the registry: {sorted(declared - actual)}; "
        f"only in argparse: {sorted(actual - declared)}"
    )


def test_the_gate_verbs_are_advertised():
    declared = set(_def().subcommands)
    for verb in ("approve-plan", "reject-plan", "plan-show", "status"):
        assert verb in declared


def test_project_is_not_cli_only():
    """A human at a gate is often not at the terminal that owns the board."""
    d = _def()
    assert not d.cli_only
    assert not d.gateway_only


def test_project_obeys_the_registry_invariants():
    d = _def()
    assert "-" not in d.name                      # Telegram rejects hyphens
    assert len(d.name) <= 32 and d.name.islower()  # Slack slash limits
    assert d.description and d.category
    names = [c.name for c in COMMAND_REGISTRY]
    assert names.count("project") == 1
    aliases = [a for c in COMMAND_REGISTRY for a in c.aliases]
    assert "project" not in aliases


# ---------------------------------------------------------------------------
# The new read-only verbs
# ---------------------------------------------------------------------------

@pytest.fixture
def gated_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "board.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ship the thing", assignee="pm")
        kb.ensure_pm_project(conn, project_id="proj-1", name="Proj One")
        kb.submit_plan(conn, project_id="proj-1", body="first draft",
                       proposed_by="pm")
        plan = kb.submit_plan(conn, project_id="proj-1",
                              body="1. do the work\n2. review it",
                              proposed_by="pm")
        assert plan["revision"] == 2
        assert kb.park_for_plan_approval(
            conn, tid, project_id="proj-1", revision=2) is True
    finally:
        conn.close()
    return tid


def test_plan_show_displays_the_plan_from_the_database(gated_task):
    code, out, _err = _run(["project", "plan-show", gated_task])
    assert code == 0
    assert "1. do the work" in out
    assert "2. review it" in out
    assert "proj-1" in out
    assert gated_task in out


def test_plan_show_never_touches_the_approval_broker(gated_task, monkeypatch):
    """Displaying a plan is not a decision, so it must not mint anything."""
    from hermes_cli import approval_broker as ab

    def _boom(*a, **k):
        raise AssertionError("plan-show must not reach the approval broker")

    monkeypatch.setattr(ab, "for_plan_decision", _boom)
    code, _out, _err = _run(["project", "plan-show", gated_task])
    assert code == 0


def test_plan_show_leaves_the_gate_exactly_as_it_found_it(gated_task):
    conn = kb.connect()
    try:
        before = dict(conn.execute(
            "SELECT status, gate_state FROM tasks WHERE id = ?",
            (gated_task,)).fetchone())
    finally:
        conn.close()
    _run(["project", "plan-show", gated_task])
    conn = kb.connect()
    try:
        after = dict(conn.execute(
            "SELECT status, gate_state FROM tasks WHERE id = ?",
            (gated_task,)).fetchone())
        approvals = conn.execute("SELECT COUNT(*) c FROM pm_approvals").fetchone()["c"]
    finally:
        conn.close()
    assert after == before
    assert approvals == 0


def test_plan_show_on_an_ungated_task_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "b2.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="ordinary", assignee="coder")
    finally:
        conn.close()
    code, _out, err = _run(["project", "plan-show", tid])
    assert code == 1
    assert "not awaiting plan approval" in err


def test_plan_show_on_a_missing_task_fails_cleanly(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "b3.db"))
    kb.init_db()
    code, _out, err = _run(["project", "plan-show", "t_nope"])
    assert code == 1
    assert "no such task" in err


def test_status_lists_a_gated_task(gated_task):
    code, out, _err = _run(["project", "status"])
    assert code == 0
    assert gated_task in out
    assert "proj-1" in out


def test_status_on_an_empty_board_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "b4.db"))
    kb.init_db()
    code, out, _err = _run(["project", "status"])
    assert code == 0
    assert "no tasks are waiting" in out.lower()


def test_status_reports_but_never_releases_a_gate(gated_task):
    _run(["project", "status"])
    conn = kb.connect()
    try:
        row = conn.execute("SELECT status, gate_state FROM tasks WHERE id = ?",
                           (gated_task,)).fetchone()
        approvals = conn.execute("SELECT COUNT(*) c FROM pm_approvals").fetchone()["c"]
    finally:
        conn.close()
    assert row["gate_state"] == "plan"
    assert row["status"] == "scheduled"
    assert approvals == 0


# ---------------------------------------------------------------------------
# Regression: commit 9 adds no approval authority
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("verb", ["approve-plan", "reject-plan"])
def test_the_gate_verbs_still_display_then_fail_closed(gated_task, verb):
    code, out, err = _run(["project", verb, gated_task])
    assert "1. do the work" in out, "the plan is still displayed"
    assert code == 3, "and the decision still fails closed"
    assert "approval" in err.lower() or "surface" in err.lower()
    conn = kb.connect()
    try:
        row = conn.execute("SELECT status, gate_state FROM tasks WHERE id = ?",
                           (gated_task,)).fetchone()
        approvals = conn.execute("SELECT COUNT(*) c FROM pm_approvals").fetchone()["c"]
    finally:
        conn.close()
    assert row["gate_state"] == "plan"
    assert approvals == 0


# ---------------------------------------------------------------------------
# Drafting a plan is not approving one
# ---------------------------------------------------------------------------

@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "plans.db"))
    kb.init_db()
    return tmp_path


def test_plan_records_a_revision_and_bumps_the_project(board):
    code, out, _err = _run(["project", "plan", "proj-x", "--body", "step one"])
    assert code == 0
    assert "revision 1" in out
    code, out, _err = _run(["project", "plan", "proj-x", "--body", "step two"])
    assert code == 0
    assert "revision 2" in out
    conn = kb.connect()
    try:
        assert kb.get_plan(conn, "proj-x")["revision"] == 2
        assert kb.get_plan(conn, "proj-x", 1)["body"] == "step one"
        row = conn.execute(
            "SELECT plan_revision FROM pm_projects WHERE id = ?", ("proj-x",)
        ).fetchone()
        assert row["plan_revision"] == 2
    finally:
        conn.close()


def test_plan_touches_no_task_and_no_gate(board):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="unrelated", assignee="coder")
        before = dict(conn.execute(
            "SELECT status, gate_state FROM tasks WHERE id = ?", (tid,)).fetchone())
    finally:
        conn.close()
    _run(["project", "plan", "proj-y", "--body", "a plan"])
    conn = kb.connect()
    try:
        after = dict(conn.execute(
            "SELECT status, gate_state FROM tasks WHERE id = ?", (tid,)).fetchone())
        approvals = conn.execute("SELECT COUNT(*) c FROM pm_approvals").fetchone()["c"]
    finally:
        conn.close()
    assert after == before
    assert approvals == 0


def test_plan_says_approval_is_elsewhere(board):
    _code, out, _err = _run(["project", "plan", "proj-z", "--body", "x"])
    assert "not available from this CLI" in out


def test_plan_reads_a_body_from_a_file(board, tmp_path):
    f = tmp_path / "plan.md"
    f.write_text("line one\nline two\n")
    code, _out, _err = _run(["project", "plan", "proj-f", "--file", str(f)])
    assert code == 0
    conn = kb.connect()
    try:
        assert "line two" in kb.get_plan(conn, "proj-f")["body"]
    finally:
        conn.close()


@pytest.mark.parametrize("argv,expected", [
    (["project", "plan", "proj-e"], 2),                       # no body
    (["project", "plan", "proj-e", "--body", "   "], 2),      # blank body
    (["project", "plan", "proj-e", "--body", "x", "--file", "f"], 2),
])
def test_plan_refuses_incoherent_input(board, argv, expected):
    code, _out, err = _run(argv)
    assert code == expected
    assert err.strip()


def test_plan_show_by_project_and_by_revision(board):
    _run(["project", "plan", "proj-r", "--body", "first"])
    _run(["project", "plan", "proj-r", "--body", "second"])
    code, out, _err = _run(["project", "plan-show", "proj-r"])
    assert code == 0 and "second" in out and "Revision : 2" in out
    code, out, _err = _run(["project", "plan-show", "proj-r", "--revision", "1"])
    assert code == 0 and "first" in out and "Revision : 1" in out


def test_plan_show_on_an_unknown_project_fails_cleanly(board):
    code, _out, err = _run(["project", "plan-show", "nope"])
    assert code == 1
    assert "no plan" in err


def test_a_stale_revision_cannot_be_the_gate_target(board):
    """submit_plan bumping the project is what makes a superseded plan
    unapprovable — release_plan_gate refuses any non-current revision."""
    conn = kb.connect()
    try:
        kb.ensure_pm_project(conn, project_id="proj-s")
        kb.submit_plan(conn, project_id="proj-s", body="old")
        tid = kb.create_task(conn, title="t", assignee="pm")
        assert kb.park_for_plan_approval(
            conn, tid, project_id="proj-s", revision=1) is True
        kb.submit_plan(conn, project_id="proj-s", body="new")   # supersedes
        row = conn.execute(
            "SELECT plan_revision FROM pm_projects WHERE id = ?", ("proj-s",)
        ).fetchone()
        assert row["plan_revision"] == 2
    finally:
        conn.close()
    # The gate still points at revision 1, which is no longer current.
    code, out, _err = _run(["project", "plan-show", tid])
    assert code == 0
    assert "Revision : 1" in out


def test_project_is_curated_onto_slack_via_hermes_not_by_displacement():
    """Slack's slash cap truncates in registry order, so a new command can
    silently push the last one off the end. `project` is routed through
    `/hermes project …` instead, and `insights` keeps its native slash."""
    from hermes_cli.commands import (
        _SLACK_VIA_HERMES_ONLY,
        slack_native_slashes,
        telegram_bot_commands,
    )

    assert "project" in _SLACK_VIA_HERMES_ONLY
    native = {n for n, _d, _h in slack_native_slashes()}
    assert "project" not in native
    assert "insights" in native, "an existing command must not be displaced"
    # Still reachable on Slack through the /hermes router, and still native
    # on Telegram.
    assert "project" in slack_subcommand_map()
    assert "project" in {n for n, _ in telegram_bot_commands()}
