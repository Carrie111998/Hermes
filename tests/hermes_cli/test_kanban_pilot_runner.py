"""Behavior tests for the controlled, manifest-driven Workflow v1 pilot runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_pilot_runner import (
    PilotPlan,
    PilotSafetyError,
    assert_runner_source,
    prepare_pilot,
)
from hermes_cli.kanban_execution import (
    get_workflow_controller_state,
    validate_execution_readiness,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def pilot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "pilot-test")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-b", "main", str(repo))
    _git(repo, "config", "user.email", "pilot@example.invalid")
    _git(repo, "config", "user.name", "Pilot Test")
    (repo / "src").mkdir()
    (repo / "src" / "anchor.py").write_text(
        "class Anchor:\n    pass\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "pin")
    pin = _git(repo, "rev-parse", "HEAD")
    manifest = {
        "schema": "hermes.workflow-pilot.v1",
        "campaign": {
            "repository": "example/repo",
            "issue": "257",
            "board": "pilot-test",
        },
        "source": {
            "path": str(repo),
            "pin_sha": pin,
            "worktree_root": str(repo.parent / "pilot-worktrees"),
        },
        "controls": {"concurrency": 2, "permit": "issue-257-disposable"},
        "leaves": [
            {
                "id": "alpha",
                "version": 1,
                "phase": 1,
                "branch": "pilot/alpha-v1",
                "worktree": "alpha-v1",
                "objective": "Produce alpha.",
                "allowed_paths": [".workflow-pilot/alpha.json"],
                "relevant_files": ["src/anchor.py"],
                "symbols": ["Anchor"],
                "acceptance_checks": ["python -m compileall -q src"],
            },
            {
                "id": "beta",
                "version": 1,
                "phase": 1,
                "branch": "pilot/beta-v1",
                "worktree": "beta-v1",
                "objective": "Produce beta.",
                "allowed_paths": [".workflow-pilot/beta.json"],
                "relevant_files": ["src/anchor.py"],
                "symbols": ["Anchor"],
                "acceptance_checks": ["python -m compileall -q src"],
            },
            {
                "id": "dependent",
                "version": 1,
                "phase": 2,
                "objective": "Wait for alpha.",
                "allowed_paths": [".workflow-pilot/dependent.json"],
                "relevant_files": ["src/anchor.py"],
                "symbols": ["Anchor"],
                "acceptance_checks": ["python -m compileall -q src"],
                "depends_on": ["alpha/v1", "beta/v1"],
                "dispatchable": False,
            },
        ],
    }
    return repo, pin, manifest


def test_runner_source_attestation_requires_exact_clean_head_tree(pilot):
    repo, _pin, _manifest = pilot
    tree = _git(repo, "rev-parse", "HEAD^{tree}")

    assert_runner_source(repo, tree)
    with pytest.raises(PilotSafetyError, match="reviewed source tree"):
        assert_runner_source(repo, "0" * 40)

    (repo / "unreviewed.txt").write_text("drift\n", encoding="utf-8")
    with pytest.raises(PilotSafetyError, match="not clean"):
        assert_runner_source(repo, tree)


def test_plan_rejects_phase_two_that_does_not_depend_on_both_phase_one_leaves(pilot):
    _repo, _pin, manifest = pilot
    manifest["leaves"][2]["depends_on"] = ["alpha/v1"]
    with pytest.raises(PilotSafetyError, match="every Phase 1 leaf"):
        PilotPlan.from_mapping(manifest)


def test_plan_rejects_overlapping_phase_one_paths_and_unbounded_concurrency(pilot):
    _repo, _pin, manifest = pilot
    manifest["leaves"][1]["allowed_paths"] = [".workflow-pilot/alpha.json"]
    with pytest.raises(PilotSafetyError, match="path-disjoint"):
        PilotPlan.from_mapping(manifest)
    manifest["leaves"][1]["allowed_paths"] = [".workflow-pilot/beta.json"]
    manifest["controls"]["concurrency"] = 3
    with pytest.raises(PilotSafetyError, match="concurrency"):
        PilotPlan.from_mapping(manifest)


def test_plan_rejects_non_pilot_branches_and_globbed_write_boundaries(pilot):
    _repo, _pin, manifest = pilot
    manifest["leaves"][0]["branch"] = "main"
    with pytest.raises(PilotSafetyError, match=r"pilot/\*"):
        PilotPlan.from_mapping(manifest)

    manifest["leaves"][0]["branch"] = "pilot/alpha-v1"
    manifest["leaves"][0]["allowed_paths"] = [".workflow-pilot/*.json"]
    with pytest.raises(PilotSafetyError, match="exact paths"):
        PilotPlan.from_mapping(manifest)


def test_plan_rejects_unsafe_board_slug(pilot):
    _repo, _pin, manifest = pilot
    manifest["campaign"]["board"] = "../other-board"
    with pytest.raises(PilotSafetyError, match="safe board slug"):
        PilotPlan.from_mapping(manifest)


def test_prepare_requires_manifest_board_to_match_active_database(pilot, monkeypatch):
    _repo, _pin, manifest = pilot
    plan = PilotPlan.from_mapping(manifest)
    kb.init_db()
    with kb.connect() as conn:
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "different-board")
        with pytest.raises(PilotSafetyError, match="active Kanban board"):
            prepare_pilot(conn, plan)


def test_prepare_registers_two_ready_candidates_and_one_dependency_blocked_leaf(pilot):
    repo, pin, manifest = pilot
    plan = PilotPlan.from_mapping(manifest)
    kb.init_db()
    with kb.connect() as conn:
        result = prepare_pilot(conn, plan)
        assert result.pin_sha == pin
        assert set(result.task_ids) == {"alpha/v1", "beta/v1", "dependent/v1"}
        alpha = kb.get_task(conn, result.task_ids["alpha/v1"])
        beta = kb.get_task(conn, result.task_ids["beta/v1"])
        dependent = kb.get_task(conn, result.task_ids["dependent/v1"])
        assert alpha is not None and alpha.workspace_path
        assert beta is not None and beta.workspace_path
        assert dependent is not None
        assert Path(alpha.workspace_path).name == "alpha-v1"
        assert Path(beta.workspace_path).name == "beta-v1"
        assert alpha.branch_name == "pilot/alpha-v1"
        assert beta.branch_name == "pilot/beta-v1"
        assert _git(Path(alpha.workspace_path), "rev-parse", "HEAD") == pin
        assert _git(Path(beta.workspace_path), "rev-parse", "HEAD") == pin
        _git(Path(alpha.workspace_path), "checkout", "-b", "pilot/unapproved")
        assert (
            "branch_mismatch" in validate_execution_readiness(conn, alpha.id).blockers
        )
        assert (
            "dependencies_not_done"
            in validate_execution_readiness(conn, dependent.id).blockers
        )
        state = get_workflow_controller_state(conn)
        assert not state.dispatch_enabled and not state.broker_ready
    assert _git(repo, "branch", "--list", "pilot/*").splitlines()


def test_prepare_is_idempotent_and_refuses_source_or_permit_drift(pilot):
    _repo, _pin, manifest = pilot
    plan = PilotPlan.from_mapping(manifest)
    kb.init_db()
    with kb.connect() as conn:
        first = prepare_pilot(conn, plan)
        second = prepare_pilot(conn, plan)
        assert first.task_ids == second.task_ids
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3

    changed = json.loads(json.dumps(manifest))
    changed["controls"]["permit"] = "different-permit"
    with kb.connect() as conn, pytest.raises(PilotSafetyError, match="permit"):
        prepare_pilot(conn, PilotPlan.from_mapping(changed))
