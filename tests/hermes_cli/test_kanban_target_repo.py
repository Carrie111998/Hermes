"""TARGET_REPO_RESOLUTION — repo/worktree routing based on explicit `target_repo`.

Safety boundary: repository selection decides WHERE a task's worktree lands and
executes. UNKNOWN must fail closed (block before worktree creation), never fall
through to project.primary_path or an arbitrary default. Explicit target_repo
on the card is the highest-authority signal and must beat project.primary_path,
stale memory, and session history.
"""

from __future__ import annotations

import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import projects_db as pdb

# Canonical repos used by tests (absolute, non-existent paths are fine for
# resolution logic — we assert path derivation, not a real checkout).
CP = kb.CANONICAL_REPO_MAP["erp-control-plane"]
KIT = kb.CANONICAL_REPO_MAP["erp-kit"]
HGA = kb.CANONICAL_REPO_MAP["hermes-agent"]


@pytest.fixture
def kanban_conn(tmp_path):
    c = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        yield c
    finally:
        c.close()


def _make_project(repo, name="Erp"):
    with pdb.connect_closing() as pc:
        pid = pdb.create_project(pc, name=name, folders=[repo])
        return pdb.get_project(pc, pid)


def _make_task(conn, *, project=None, body="", title="t", target_repo=None):
    return kb.create_task(
        conn,
        title=title,
        body=body,
        project_id=project,
        target_repo=target_repo,
    )


""" ---- Matrix 1-2: explicit target_repo → exact repo ---- """


def test_explicit_target_repo_erp_control_plane(kanban_conn):
    tid = _make_task(kanban_conn, target_repo="erp-control-plane")
    task = kb.get_task(kanban_conn, tid)
    assert task.target_repo == "erp-control-plane"
    # Worktree anchored under the target repo, not project default (no project set).
    assert task.workspace_path == os.path.join(CP, ".worktrees", tid)


def test_explicit_target_repo_erp_kit(kanban_conn):
    tid = _make_task(kanban_conn, target_repo="erp-kit")
    task = kb.get_task(kanban_conn, tid)
    assert task.target_repo == "erp-kit"
    assert task.workspace_path == os.path.join(KIT, ".worktrees", tid)


""" ---- Matrix 3: project primary_path=erp-kit but explicit CP target → CP wins ---- """


def test_explicit_target_overrides_project_primary_path(kanban_conn):
    # project primary_path = erp-kit
    proj = _make_project(KIT)
    assert proj.primary_path == KIT
    tid = _make_task(kanban_conn, project=proj.slug, target_repo="erp-control-plane")
    task = kb.get_task(kanban_conn, tid)
    # target_repo (CP) must win over project.primary_path (erp-kit)
    assert task.target_repo == "erp-control-plane"
    assert task.workspace_path == os.path.join(CP, ".worktrees", tid)
    assert task.workspace_path != os.path.join(KIT, ".worktrees", tid)


""" ---- Matrix 4-5: legacy cards (no target_repo) via body path signals ---- """


def test_legacy_body_canonical_cp_path(kanban_conn):
    tid = _make_task(kanban_conn, body="fix control-plane: erp-control-plane/src/orchestrator.ts")
    res = kb._resolve_target_repo(kb.get_task(kanban_conn, tid))
    assert res.confidence in ("high", "medium")
    assert res.repo == CP
    assert res.source in ("component_repo_mapping", "artifact_ownership", "component_or_artifact")


def test_legacy_body_canonical_erp_path(kanban_conn):
    tid = _make_task(kanban_conn, body="work on erp-kit/app/routes/")
    res = kb._resolve_target_repo(kb.get_task(kanban_conn, tid))
    assert res.confidence in ("high", "medium")
    assert res.repo == KIT


""" ---- Matrix 6: ambiguous body → UNKNOWN (fail closed) ---- """


def test_ambiguous_body_returns_unknown(kanban_conn):
    tid = _make_task(kanban_conn, body="touch erp-kit and erp-control-plane together")
    res = kb._resolve_target_repo(kb.get_task(kanban_conn, tid))
    assert res.confidence == "UNKNOWN"
    assert res.repo is None
    # _require_resolved_target_repo must raise (block before worktree creation)
    with pytest.raises(ValueError, match="TARGET_REPO UNKNOWN"):
        kb._require_resolved_target_repo(kb.get_task(kanban_conn, tid))


""" ---- Matrix 7: stale memory/session referencing erp-kit must NOT override current CP target ---- """


def test_current_target_beats_stale_erp_kit_signal(kanban_conn):
    # Card declares target_repo=erp-control-plane; even if a stale/body signal
    # references erp-kit, the explicit metadata is highest authority.
    tid = _make_task(
        kanban_conn,
        target_repo="erp-control-plane",
        body="historical: erp-kit used to be the target; now 2026",
    )
    res = kb._resolve_target_repo(kb.get_task(kanban_conn, tid))
    assert res.confidence == "high"
    assert res.repo == CP
    assert res.source == "target_repo_metadata"


""" ---- Matrix 8: worktree target mismatch → BLOCK (fail closed) ---- """


def test_worktree_mismatch_blocks(kanban_conn, tmp_path):
    # A task explicitly targeted to erp-control-plane whose (already-created)
    # workspace lives under erp-kit must be rejected, not run on the wrong repo.
    tid = _make_task(kanban_conn, target_repo="erp-control-plane")
    task = kb.get_task(kanban_conn, tid)
    # Build a REAL git repo for the wrong location (erp-kit) and point the
    # workspace at a worktree inside it → validation must reject (mismatch).
    wrong_repo = tmp_path / "erp_kit_repo"
    wrong_repo.mkdir(parents=True)
    _run_git(wrong_repo, "init", "-q")
    _run_git(wrong_repo, "config", "user.email", "t@t")
    _run_git(wrong_repo, "config", "user.name", "t")
    wrong_wt = wrong_repo / "worktree_v2"
    _run_git(wrong_repo, "worktree", "add", "-q", str(wrong_wt), "-b", "wt/x")
    with pytest.raises(ValueError, match="DISPATCH_BLOCKED"):
        kb._validate_worktree_matches_target(task, str(wrong_wt))


def _run_git(cwd, *args):
    import subprocess
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


""" ---- Matrix 9: MULTI_REPO classification is explicit (no single forced repo) ---- """


def test_multi_repo_unknown_blocks_single_worktree(kanban_conn):
    # Body names two repos → cannot be forced into one worktree.
    tid = _make_task(kanban_conn, body="needs hermes-agent and erp-control-plane")
    res = kb._resolve_target_repo(kb.get_task(kanban_conn, tid))
    assert res.confidence == "UNKNOWN"
    # No target_repo forced; dispatch blocks rather than choose one repo.
    with pytest.raises(ValueError, match="TARGET_REPO UNKNOWN"):
        kb._require_resolved_target_repo(kb.get_task(kanban_conn, tid))


""" ---- Matrix 10: legacy single-repo project → backward-compatible primary_path fallback ---- """


def test_legacy_single_repo_project_primary_path_fallback(kanban_conn):
    # Legacy card on a single-repo project (no target_repo, no body signal):
    # must fall back to project.primary_path (backward compatible).
    proj = _make_project(CP)
    tid = _make_task(kanban_conn, project=proj.slug, body="", title="legacy")
    task = kb.get_task(kanban_conn, tid)
    assert task.target_repo is None
    assert task.workspace_path == os.path.join(CP, ".worktrees", tid)


""" ---- Exact regression: task t_7b904cdd (project=erp, primary=erp-kit, target=control-plane) ---- """


def test_historical_regression_target_erp_control_plane_over_erp_kit(kanban_conn):
    # Reproduce the real mis-route: a control-plane-targeted card (body mentions
    # control-plane) created under project erp whose primary_path is erp-kit.
    proj = _make_project(KIT)  # primary = erp-kit
    tid = _make_task(
        kanban_conn,
        project=proj.slug,
        title="TARGET_REPO_RESOLUTION",
        body="erp-control-plane: PR separado a erp-control-plane main",
    )
    task = kb.get_task(kanban_conn, tid)
    res = kb._resolve_target_repo(task)
    # The control-plane body signal must win over the erp-kit primary_path.
    assert res.confidence in ("high", "medium")
    assert res.repo == CP
    # Worktree must land in the control-plane repo, NOT erp-kit.
    assert task.workspace_path == os.path.join(CP, ".worktrees", tid)
    assert task.workspace_path != os.path.join(KIT, ".worktrees", tid)


""" ---- Schema: legacy cards keep target_repo NULL and no value is invented ---- """


def test_legacy_card_schema_target_repo_null(kanban_conn):
    tid = _make_task(kanban_conn, body="generic UI tweak")
    task = kb.get_task(kanban_conn, tid)
    assert task.target_repo is None  # no value invented for ambiguous legacy card
    res = kb._resolve_target_repo(task)
    assert res.confidence == "UNKNOWN"


"""--- target_repo inválido: NO persiste workspace_path basura (fail-closed en persistencia) ---"""


def test_invalid_target_repo_does_not_persist_junk_path(kanban_conn):
    tid = _make_task(kanban_conn, target_repo="erp-kit-experimental")
    task = kb.get_task(kanban_conn, tid)
    # Unknown target name must NOT produce a relative "erp-kit-experimental/.worktrees/..." path.
    assert task.target_repo == "erp-kit-experimental"
    # workspace_path must not be a bogus relative path derived from the name.
    if task.workspace_path is None:
        pass  # scratch / no anchor is acceptable (dispatch will fail-closed)
    else:
        assert os.path.isabs(task.workspace_path)
    # dispatch resolution fails closed on the unknown name:
    res = kb._resolve_target_repo(kb.get_task(kanban_conn, tid))
    assert res.confidence == "UNKNOWN"
    with pytest.raises(ValueError, match="TARGET_REPO UNKNOWN"):
        kb._require_resolved_target_repo(kb.get_task(kanban_conn, tid))


"""---- Non-ambiguous single canonical name in explicit metadata ---- """


def test_explicit_unknown_repo_name_fails_closed(kanban_conn):
    tid = _make_task(kanban_conn, target_repo="totally-unknown-repo")
    res = kb._resolve_target_repo(kb.get_task(kanban_conn, tid))
    assert res.confidence == "UNKNOWN"
    with pytest.raises(ValueError, match="TARGET_REPO UNKNOWN"):
        kb._require_resolved_target_repo(kb.get_task(kanban_conn, tid))


"""----- Word-boundary: hyphenated compound must NOT match canonical repo -----"""


def test_mentions_repo_hyphen_compound_no_false_positive():
    # `erp-kit-foo` must not be seen as mentioning `erp-kit` (hyphen boundary).
    assert not kb._mentions_repo("erp-kit-foo is a different repo", "erp-kit")
    assert not kb._mentions_repo("ferp-kitx", "erp-kit")
    # Standalone tokens DO match.
    assert kb._mentions_repo("touch erp-kit please", "erp-kit")
    assert kb._mentions_repo("dev/repos/erp-kit/", "erp-kit")


"""----- Backward-compat: project-anchored card silently-ambiguous resolves to project repo -----"""


def test_project_anchored_no_signal_resolves_to_project_repo(kanban_conn):
    # A legacy project-linked card with NO body repo signal must resolve to its
    # project.primary_path (not block) — backward compatibility preserved.
    import tempfile
    from pathlib import Path
    tmp = Path(tempfile.mkdtemp()) / "legacyproj"
    proj = _make_project(str(tmp))
    tid = _make_task(kanban_conn, project=proj.slug, body="generic UI tweak no repo")
    task = kb.get_task(kanban_conn, tid)
    res = kb._resolve_target_repo(task, project_primary_path=_task_project_path_helper())
    # Because project_primary_path is resolved inside the helper and passed, we
    # expect UNKNOWN at the bare call (no default); the DISPATCH path passes
    # default_anchor/project and must not raise. Assert _require with default.
    res2 = kb._require_resolved_target_repo(task)
    assert res2.confidence in ("medium", "high")
    assert res2.repo == str(tmp)


def _task_project_path_helper():
    # Used to document intent; the real path resolution happens via
    # _task_project_primary_path(task) inside _require/. Keep a trivial wrapper.
    return None