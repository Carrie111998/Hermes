"""`project bind-board` must write the LINK, not only mirror the workdir.

The board's own metadata carries `project_id`, and `kanban_db.write_board_metadata`
documents what it is for: "Optional first-class Project this board is scoped to.
When set, new tasks inherit it (deterministic worktree + branch under the project's
primary repo)". `bind-board` set the project's `board_slug` and mirrored
`default_workdir`, and never wrote that field — so the bind printed success while
the effect it exists for never arrived.

Measured on one install before this fix: 11 of 11 boards had `project_id: null`
while their projects named them, and 249 tasks had piled onto shared working trees,
two of them blocking each other with "concurrent edits in shared dir".
"""

from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_cmd
from hermes_cli import projects_db as pdb


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Boards and the projects DB in tmp_path — never the operator's real ones."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(kb, "_HERMES_HOME_OVERRIDE", str(tmp_path), raising=False)
    yield


def _project(name="Web App", folders=("/tmp/webapp",)):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=list(folders))
        return pdb.get_project(pc, pid)


def _bind(proj, board):
    # Through the decorated entry point, which is what the CLI calls: the
    # @_with_project wrapper opens the DB and resolves the project itself.
    args = argparse.Namespace(board=board, project=proj.slug)
    return projects_cmd._cmd_bind_board(args)


def test_bind_board_writes_the_project_link_onto_the_board():
    proj = _project()
    kb.create_board("web")

    assert _bind(proj, "web") == 0

    meta = kb.read_board_metadata("web")
    # The field the inheritance path reads. This is the assertion the old code
    # failed: everything else about the bind already worked.
    assert meta["project_id"] == proj.id
    # And the convenience mirror it already did.
    assert meta["default_workdir"] == proj.primary_path


def test_a_project_with_no_folder_still_binds():
    """The workdir mirror may not gate the link.

    The old code returned early when `primary_path` was empty, throwing away the
    link for want of the mirror's input. A project with no folder yet still has an
    identity, and binding it is exactly how a board starts inheriting one.
    """
    proj = _project(name="Empty", folders=())
    assert not proj.primary_path
    kb.create_board("empty-board")

    assert _bind(proj, "empty-board") == 0

    meta = kb.read_board_metadata("empty-board")
    assert meta["project_id"] == proj.id
    # Nothing to mirror, so nothing is invented.
    assert not meta.get("default_workdir")


def test_a_bound_board_makes_new_tasks_inherit_the_worktree(tmp_path):
    """The point of the whole thing, asserted through task creation.

    A task created on a bound board with no workspace flags must land on a
    deterministic worktree under the project's repo — not on the shared `dir`
    workspace that 249 tasks ended up sharing.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    proj = _project(name="Router", folders=(str(repo),))
    kb.create_board("router")
    _bind(proj, "router")

    conn = kb.connect(board="router")
    try:
        #  explicitly: create_task resolves the project from the board it
        # is TOLD about, falling back to the ambient current board — not from the
        # connection. Passing it is what the CLI does, and leaving it out here
        # tested the active board instead of this one.
        tid = kb.create_task(conn, title="Add login", board="router")
        task = kb.get_task(conn, tid)
    finally:
        conn.close()

    assert task.project_id == proj.id, "the board's link must reach the task"
    assert task.workspace_kind == "worktree"
    assert task.workspace_path.startswith(str(repo))
    assert task.branch_name and not task.branch_name.startswith("wt/")


def test_unbinding_clears_the_scope_it_set():
    """Otherwise a board goes on inheriting a project it is no longer bound to.

    `write_board_metadata` takes `None` as "leave unchanged" and the empty string
    as "clear", so the clear has to be deliberate.
    """
    proj = _project()
    kb.create_board("web")
    _bind(proj, "web")
    assert kb.read_board_metadata("web")["project_id"] == proj.id

    with pdb.connect_closing() as pc:
        proj = pdb.get_project(pc, proj.id)   # reload: board_slug is set now
    assert _bind(proj, "") == 0

    assert not kb.read_board_metadata("web")["project_id"]


def test_binding_a_board_that_does_not_exist_is_a_no_op_not_a_crash():
    """The mirror was always best-effort and stays that way: the projects DB is
    the authority on the binding, and this side is a convenience on top of it."""
    proj = _project()
    assert _bind(proj, "no-such-board") == 0
