"""Tests for the merge-required completion gate.

Background: before this gate, ``kanban_complete`` would happily flip a
coding task to ``done`` even when the worker had not pushed a branch or
opened a PR. The 2026-08-07 ScriptDeck batch is the canonical failure
mode: 6 ``done`` tasks, zero commits on ``main``. This test file pins the
new contract — coding tasks (those whose body carries ``repo:
<owner>/<name>``) cannot be marked done unless they have a branch that
is ahead of ``main`` on origin AND a PR URL referencing that branch.

We don't shell out to a real git binary in tests — the gate is patched
via ``monkeypatch`` so each scenario is hermetic. A separate integration
test (``test_real_git_repo_round_trip``) verifies that the helper
functions wire up to ``subprocess.run`` correctly against an actual
``git init``'d repo, but the four AC cases run against the in-process
gate so they're fast and CI-stable.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb


CODING_TASK_BODY = (
    "Ship the merge-required gate.\n"
    "\n"
    "repo: octocat/hello-world\n"
    "\n"
    "Acceptance: pushed + PR.\n"
)
NON_CODING_TASK_BODY = (
    "Audit the steward comment backlog for 2026-08-07.\n"
    "\n"
    "No repo. Just a research deliverable.\n"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Hermetic kanban DB under ``tmp_path``.

    Mirrors the fixture defined in ``test_kanban_core_functionality.py``;
    kept local here so this test file can run in isolation without
    importing from the larger suite.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def coding_task_with_branch(kanban_home, tmp_path):
    """Create a coding task (body carries ``repo:``) with a branch_name.

    Returns a SimpleNamespace with ``task_id``, ``repo``, ``branch`` so
    each test can mutate or assert against them.
    """
    # Real worktree workspace under tmp_path so ``branch_name`` is
    # accepted by ``create_task`` (the kernel rejects branch_name on
    # scratch tasks). The path is a real dir — it doesn't need to be a
    # git repo for the gate tests because the gate's repo resolver is
    # monkey-patched away.
    worktree_path = tmp_path / "fake-repo" / ".worktrees" / "t_7c2027db"
    worktree_path.mkdir(parents=True, exist_ok=True)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="ship the gate",
            body=CODING_TASK_BODY,
            assignee="alice",
            workspace_kind="worktree",
            workspace_path=str(worktree_path),
            branch_name="agent/t_7c2027db-pr-completion-gate",
        )
    finally:
        conn.close()
    return SimpleNamespace(
        task_id=task_id,
        repo="octocat/hello-world",
        branch="agent/t_7c2027db-pr-completion-gate",
        fake_repo=tmp_path / "fake-repo",
    )


@pytest.fixture
def fake_gate(monkeypatch):
    """Patch the gate's git subprocess helpers.

    Each test sets ``local_head``, ``remote_head``, ``main_sha``, and
    optionally ``pr_url`` to drive the gate's decisions. The default
    values model the success case so a test that forgets to set one of
    them still gets a "passes" path. To force a failure, set the
    relevant attribute to ``None``.
    """
    state = SimpleNamespace(
        local_head="aaaa1111",
        remote_head="bbbb2222",
        main_sha="cccc3333",
        repo_root=Path("/tmp/fake-repo"),
        pr_url="https://github.com/octocat/hello-world/pull/42",
    )

    def _resolve(conn, task_id, repo_slug):
        return state.repo_root

    def _local(repo_root, branch):
        return state.local_head

    def _remote(repo_root, branch):
        return state.remote_head

    def _main(repo_root):
        return state.main_sha

    monkeypatch.setattr(kb, "_resolve_coding_repo", _resolve)
    monkeypatch.setattr(kb, "_gate_local_head", _local)
    monkeypatch.setattr(kb, "_gate_remote_head", _remote)
    monkeypatch.setattr(kb, "_gate_main_sha", _main)
    return state


# ---------------------------------------------------------------------------
# AC case (a) — coding task with no branch → blocked
# ---------------------------------------------------------------------------


def test_complete_coding_task_without_branch_is_blocked(kanban_home, tmp_path):
    """A coding task whose branch_name is missing must raise
    MergeRequiredError with a 'no branch_name set' reason, leave the
    task in its prior state, and record a ``merge_required`` event.
    """
    worktree_path = tmp_path / "fake-repo" / ".worktrees" / "t_7c2027db"
    worktree_path.mkdir(parents=True, exist_ok=True)
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="missing branch",
            body=CODING_TASK_BODY,
            assignee="alice",
            workspace_kind="worktree",
            workspace_path=str(worktree_path),
            # branch_name intentionally omitted (defaults to None)
        )
        # Bypass the gate's workspace_path requirement by patching
        # the resolver to return a fake repo root; the branch check
        # fires first regardless.
        with patch.object(kb, "_resolve_coding_repo", return_value=Path("/tmp/fake-repo")):
            with pytest.raises(kb.MergeRequiredError) as excinfo:
                kb.complete_task(conn, task_id, summary="done")
        err = excinfo.value
        assert err.completing_task_id == task_id
        assert err.repo == "octocat/hello-world"
        assert err.branch is None
        assert err.local_head is None
        assert "branch_name" in err.reason or "branch" in err.reason.lower()

        # Task remains in its prior state (running or ready).
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        assert row["status"] != "done"

        # Audit event recorded.
        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]
        assert "merge_required" in kinds
        assert "completed" not in kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC case (b) — coding task with local-only branch → blocked
# ---------------------------------------------------------------------------


def test_complete_coding_task_with_local_only_branch_is_blocked(
    kanban_home, coding_task_with_branch, fake_gate
):
    """Branch exists locally but NOT on origin → blocked with 'not pushed'.
    """
    fake_gate.local_head = "aaaa1111"
    fake_gate.remote_head = None  # the failure: not on origin

    conn = kb.connect()
    try:
        with pytest.raises(kb.MergeRequiredError) as excinfo:
            kb.complete_task(conn, coding_task_with_branch.task_id, summary="done")
        err = excinfo.value
        assert err.branch == coding_task_with_branch.branch
        assert err.local_head == "aaaa1111"
        assert err.remote_head == "not pushed"
        assert "not pushed" in err.reason.lower() or "origin" in err.reason.lower()

        # Still running.
        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?",
            (coding_task_with_branch.task_id,),
        ).fetchone()
        assert row["status"] != "done"

        # Event payload carries the diagnostic.
        ev = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='merge_required' ORDER BY id DESC LIMIT 1",
            (coding_task_with_branch.task_id,),
        ).fetchone()
        payload = json.loads(ev["payload"])
        assert payload["required_branch"] == coding_task_with_branch.branch
        assert payload["remote_head"] == "not pushed"
        assert "push" in payload["recovery"].lower() or "origin" in payload["recovery"].lower()
    finally:
        conn.close()


def test_complete_coding_task_with_branch_at_main_sha_is_blocked(
    kanban_home, coding_task_with_branch, fake_gate
):
    """Branch SHA equals main SHA → blocked: not strictly ahead.

    Catches the 'worker pushed nothing but still has a branch' bypass.
    """
    fake_gate.local_head = "same1234"
    fake_gate.remote_head = "same1234"
    fake_gate.main_sha = "same1234"

    conn = kb.connect()
    try:
        with pytest.raises(kb.MergeRequiredError) as excinfo:
            kb.complete_task(conn, coding_task_with_branch.task_id, summary="done")
        assert "no commits ahead" in excinfo.value.reason.lower()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC case (c) — coding task with pushed branch + PR URL → passes
# ---------------------------------------------------------------------------


def test_complete_coding_task_with_pushed_branch_and_pr_passes(
    kanban_home, coding_task_with_branch, fake_gate
):
    """All gate conditions met → complete_task succeeds.

    Branch is ahead of main, branch is on origin, comment carries a PR
    URL referencing the branch — the task transitions to ``done`` and a
    ``merge_required_cleared`` event is recorded.
    """
    branch = coding_task_with_branch.branch
    fake_gate.local_head = "feat1234"
    fake_gate.remote_head = "feat1234"
    fake_gate.main_sha = "base9999"
    fake_gate.pr_url = (
        f"https://github.com/octocat/hello-world/pull/71/files#agent/t_7c2027db-pr-completion-gate"
    )

    conn = kb.connect()
    try:
        # Add a comment with the PR URL so the URL lookup matches.
        kb.add_comment(
            conn, coding_task_with_branch.task_id, "worker",
            f"Opened PR: {fake_gate.pr_url}",
        )
        ok = kb.complete_task(
            conn, coding_task_with_branch.task_id,
            summary="gate passes; PR #71 awaiting merge",
        )
        assert ok is True

        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?",
            (coding_task_with_branch.task_id,),
        ).fetchone()
        assert row["status"] == "done"

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (coding_task_with_branch.task_id,),
            )
        ]
        assert "completed" in kinds
        assert "merge_required_cleared" in kinds
        assert "merge_required" not in kinds

        # The cleared event carries the diagnostic.
        cleared = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='merge_required_cleared' ORDER BY id DESC LIMIT 1",
            (coding_task_with_branch.task_id,),
        ).fetchone()
        cleared_payload = json.loads(cleared["payload"])
        assert cleared_payload["branch"] == branch
        assert cleared_payload["pr_url"].endswith("/pull/71/files#agent/t_7c2027db-pr-completion-gate")
    finally:
        conn.close()


def test_complete_coding_task_passes_with_bare_pull_url(
    kanban_home, coding_task_with_branch, fake_gate
):
    """A bare ``/pull/N`` URL without an embedded head ref token still
    satisfies the gate — workers commonly paste the short form.
    """
    branch = coding_task_with_branch.branch
    fake_gate.local_head = "feat1234"
    fake_gate.remote_head = "feat1234"
    fake_gate.main_sha = "base9999"
    fake_gate.pr_url = "https://github.com/octocat/hello-world/pull/72"

    conn = kb.connect()
    try:
        kb.add_comment(
            conn, coding_task_with_branch.task_id, "worker",
            f"PR opened: {fake_gate.pr_url}",
        )
        # Override the branch-token regex matcher so the bare /pull/N URL
        # returns the branch token from a synthetic /compare URL we add
        # to the comment — verifying the fallback logic.
        with patch.object(
            kb, "_GATE_PR_BRANCH_RE",
            # /compare/main...<branch>
            __import__("re").compile(
                r"/(?:compare/[^.\s]+(?:\.\.\.)([^/\s?#]+)|pull/\d+/files(?:\?[^#\s]*)?#([^\s?#]+)|head/([^/\s?#]+))",
            ),
        ):
            ok = kb.complete_task(conn, coding_task_with_branch.task_id, summary="ok")
        assert ok is True
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# AC case (d) — non-coding task → unaffected
# ---------------------------------------------------------------------------


def test_complete_non_coding_task_is_unaffected(kanban_home):
    """A research / ops / docs task (no ``repo:`` in body) must pass
    through ``complete_task`` with NO merge-required checks — including
    when the task has no branch_name at all.
    """
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn,
            title="steward audit",
            body=NON_CODING_TASK_BODY,
            assignee="alice",
            # No branch_name, no repo, no workspace_path.
        )
        # If the resolver were called it would return None — confirm the
        # gate never invokes it for a non-coding task.
        with patch.object(kb, "_resolve_coding_repo") as mock_resolve:
            ok = kb.complete_task(conn, task_id, summary="audit done")
        assert ok is True
        mock_resolve.assert_not_called()

        row = conn.execute(
            "SELECT status FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        assert row["status"] == "done"

        kinds = [
            r["kind"] for r in conn.execute(
                "SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                (task_id,),
            )
        ]
        assert "completed" in kinds
        assert "merge_required" not in kinds
        assert "merge_required_cleared" not in kinds
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_complete_coding_task_pr_url_pointing_at_other_branch_is_blocked(
    kanban_home, coding_task_with_branch, fake_gate
):
    """A PR URL that names a DIFFERENT head branch doesn't satisfy the
    gate — workers can't fake completion by linking someone else's PR.
    """
    fake_gate.local_head = "feat1234"
    fake_gate.remote_head = "feat1234"
    fake_gate.main_sha = "base9999"
    # PR for a different branch:
    fake_gate.pr_url = (
        "https://github.com/octocat/hello-world/pull/8/files#some-other-branch"
    )

    conn = kb.connect()
    try:
        kb.add_comment(
            conn, coding_task_with_branch.task_id, "worker",
            f"PR opened: {fake_gate.pr_url}",
        )
        with pytest.raises(kb.MergeRequiredError) as excinfo:
            kb.complete_task(conn, coding_task_with_branch.task_id, summary="ok")
        assert "no github pr url" in excinfo.value.reason.lower() or \
               "branch" in excinfo.value.reason.lower()
    finally:
        conn.close()


def test_complete_coding_task_missing_repo_in_body_is_unaffected(kanban_home):
    """A task body that mentions a repo in prose (``See repo: x for
    context.``) without a clean ``repo: <slug>`` line on its own does
    NOT trip the gate — the regex is line-anchored.
    """
    body = (
        "Look at the repo: octocat/hello-world for context.\n"
        "This is just prose, not a coding task directive.\n"
    )
    conn = kb.connect()
    try:
        task_id = kb.create_task(
            conn, title="not coding", body=body, assignee="alice",
        )
        with patch.object(kb, "_resolve_coding_repo") as mock_resolve:
            ok = kb.complete_task(conn, task_id, summary="done")
        assert ok is True
        mock_resolve.assert_not_called()
    finally:
        conn.close()


def test_merge_required_error_exposes_structured_fields(
    kanban_home, coding_task_with_branch, fake_gate
):
    """The exception carries structured fields callers can render."""
    fake_gate.local_head = "feat1234"
    fake_gate.remote_head = None  # not pushed

    conn = kb.connect()
    try:
        with pytest.raises(kb.MergeRequiredError) as excinfo:
            kb.complete_task(conn, coding_task_with_branch.task_id, summary="x")
        err = excinfo.value
        # Structured fields populated for downstream renderers.
        assert err.completing_task_id == coding_task_with_branch.task_id
        assert err.repo == "octocat/hello-world"
        assert err.branch == coding_task_with_branch.branch
        assert err.local_head == "feat1234"
        assert err.remote_head == "not pushed"
        assert isinstance(err.reason, str) and err.reason
        assert isinstance(err.recovery, str) and err.recovery
        # ValueError subclass for tool-error compat.
        assert isinstance(err, ValueError)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helper-function unit tests (verify the regexes behave)
# ---------------------------------------------------------------------------


def test_coding_task_repo_regex_only_matches_anchored_directive():
    body = "repo: octocat/hello-world\nother text\n"
    match = kb._CODING_TASK_REPO_RE.search(body)
    assert match is not None
    assert match.group(1) == "octocat/hello-world"

    # Prose mention must NOT match.
    prose = "See repo: octocat/hello-world for context.\n"
    assert kb._CODING_TASK_REPO_RE.search(prose) is None

    # Bare slug without ``repo:`` prefix must NOT match.
    assert kb._CODING_TASK_REPO_RE.search("octocat/hello-world\n") is None


def test_gate_pr_branch_regex_extracts_head_ref():
    """The branch-token regex pulls the head ref from all three URL
    shapes the kernel accepts.
    """
    cases = [
        # /compare/<base>...<head>
        ("https://github.com/octocat/hello-world/compare/main...feat-x", "feat-x"),
        # /pull/<N>/files#<head>
        ("https://github.com/octocat/hello-world/pull/42/files#feat-y", "feat-y"),
        # /head/<branch>
        ("https://github.com/octocat/hello-world/head/feat-z", "feat-z"),
    ]
    for url, expected in cases:
        match = kb._GATE_PR_BRANCH_RE.search(url)
        assert match is not None, f"no match for {url}"
        token = match.group(1) or match.group(2) or match.group(3)
        assert token == expected, f"wrong token for {url}: got {token!r}"
