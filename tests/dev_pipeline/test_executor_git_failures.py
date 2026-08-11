"""Git failure handling boundary tests for dev-pipeline executor."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import dev_executor as ex
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _executor_cfg() -> dict:
    return {
        "enabled": True,
        "board": "dev",
        "max_attempts": 2,
        "tick_seconds": 15,
        "cursor_timeout_seconds": 1800,
        "verify_command_timeout": 600,
    }


def _dev_block_kinds(conn, task_id: str) -> list[str]:
    return [
        (ev.payload or {}).get("block_kind")
        for ev in kb.list_events(conn, task_id)
        if ev.kind == "dev_blocked"
    ]


def _completed_proc(rc: int = 0, stdout: str = "", stderr: str = ""):
    return type("P", (), {"returncode": rc, "stdout": stdout, "stderr": stderr})()


def _init_git_repo(path: Path, label: str) -> Path:
    path.mkdir()
    commands = [
        ["git", "init", "-b", "main"],
        ["git", "config", "user.email", "probe@example.invalid"],
        ["git", "config", "user.name", "Probe"],
    ]
    for command in commands:
        subprocess.run(command, cwd=path, check=True, capture_output=True, text=True)
    (path / "identity.txt").write_text(label, encoding="utf-8")
    subprocess.run(
        ["git", "add", "identity.txt"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "commit", "-m", label],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def test_clone_repo_reuses_existing_matching_origin(tmp_path):
    source = _init_git_repo(tmp_path / "source", "source")
    dest = tmp_path / "dest"
    ok, _ = ex.clone_repo(str(source), dest, "main")
    assert ok is True

    reused, detail = ex.clone_repo(str(source), dest, "main")

    assert reused is True
    assert detail == str(dest)


def test_clone_repo_rejects_existing_mismatched_origin(tmp_path):
    intended = _init_git_repo(tmp_path / "intended", "intended")
    stale = _init_git_repo(tmp_path / "stale", "stale")
    dest = tmp_path / "dest"
    ok, _ = ex.clone_repo(str(stale), dest, "main")
    assert ok is True

    reused, detail = ex.clone_repo(str(intended), dest, "main")

    assert reused is False
    assert "origin does not match" in detail
    assert (dest / "identity.txt").read_text(encoding="utf-8") == "stale"


def test_clone_repo_rejects_existing_non_git_residue(tmp_path):
    source = _init_git_repo(tmp_path / "source", "source")
    dest = tmp_path / "dest"
    dest.mkdir()
    (dest / "residue.txt").write_text("keep", encoding="utf-8")

    reused, detail = ex.clone_repo(str(source), dest, "main")

    assert reused is False
    assert "not a git worktree" in detail
    assert (dest / "residue.txt").read_text(encoding="utf-8") == "keep"


def test_clone_repo_https_fallback_checkout_failure(tmp_path):
    dest = tmp_path / "repo"
    calls: list[list[str]] = []

    def git_fn(args, *, cwd=None, **_kw):
        calls.append(list(args))
        if args[:2] == ["clone", "--branch"]:
            return _completed_proc(1, stderr="branch missing")
        if args[:1] == ["clone"] and len(args) == 3:
            return _completed_proc(0)
        if args[:1] == ["checkout"]:
            return _completed_proc(1, stderr="checkout blew up")
        return _completed_proc(0)

    ok, err = ex.clone_repo(
        "https://github.com/org/r.git",
        dest,
        "feature-x",
        git_fn=git_fn,
    )
    assert ok is False
    assert "checkout blew up" in err
    assert any(args[:1] == ["checkout"] for args in calls)


def test_clone_repo_local_checkout_failure(tmp_path):
    dest = tmp_path / "repo"

    def git_fn(args, *, cwd=None, **_kw):
        if args[:1] == ["clone"]:
            return _completed_proc(0)
        if args[:1] == ["checkout"]:
            return _completed_proc(1, stderr="local checkout failed")
        return _completed_proc(0)

    with patch.object(ex, "is_local_git_repo", return_value=True):
        ok, err = ex.clone_repo("/tmp/local.git", dest, "dev", git_fn=git_fn)
    assert ok is False
    assert "local checkout failed" in err


def test_ensure_dev_branch_base_checkout_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git_fn(args, *, cwd=None, **_kw):
        if args[:2] == ["checkout", "main"]:
            return _completed_proc(1, stderr="missing main")
        return _completed_proc(0, stdout="abc123\n")

    with patch.object(ex, "git_command", side_effect=git_fn):
        result, err = ex.ensure_dev_branch(repo, "t1", "main")
    assert result is None
    assert "missing main" in err


def test_ensure_dev_branch_dev_branch_checkout_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    def git_fn(args, *, cwd=None, **_kw):
        if args[:2] == ["checkout", "main"]:
            return _completed_proc(0)
        if args[:2] == ["checkout", "-B"]:
            return _completed_proc(1, stderr="cannot create branch")
        if args[:1] == ["rev-parse"]:
            return _completed_proc(0, stdout="deadbeef\n")
        return _completed_proc(0)

    with patch.object(ex, "git_command", side_effect=git_fn):
        result, err = ex.ensure_dev_branch(repo, "t1", "main")
    assert result is None
    assert "cannot create branch" in err


def _setup_verifying_task(conn, tmp_path) -> tuple[str, int, dict]:
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "fix bug"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    pipeline_run = ex.start_pipeline_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": ex.PHASE_VERIFYING,
                "contract": {"task_summary": "x", "acceptance_commands": ["true"]},
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
            },
        ),
    )
    meta = ex.load_run_metadata(conn, pipeline_run)
    return task_id, pipeline_run, meta


@pytest.mark.parametrize(
    "failure_mode,expected_detail",
    [
        ("candidate_clone", "verify candidate clone failed"),
        ("candidate_checkout", "verify candidate checkout failed"),
    ],
)
def test_verifying_candidate_git_failure_blocks(
    kanban_home, tmp_path, failure_mode, expected_detail
):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_verifying_task(conn, tmp_path)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_VERIFYING)

    def git_fn(args, *, cwd=None, **_kw):
        if failure_mode == "candidate_clone" and args[:1] == ["clone"]:
            return _completed_proc(1, stderr="clone failed hard")
        if (
            failure_mode == "candidate_checkout"
            and args[:1] == ["checkout"]
            and args[1] == "bbb"
        ):
            return _completed_proc(1, stderr="bad ref")
        return _completed_proc(0)

    with patch.object(ex, "git_head_sha", return_value="bbb"):
        with patch.object(ex, "git_command", side_effect=git_fn):
            with patch.object(ex, "run_verification") as mock_verify:
                with patch.object(ex, "block_dev_task") as mock_block:
                    executor._phase_verifying(
                        conn, task_id, run_id, meta, ex.pipeline_state(meta)
                    )
                    mock_verify.assert_not_called()
                    mock_block.assert_called_once()
                    assert mock_block.call_args[0][2] == "infra_broken"
                    assert expected_detail in mock_block.call_args[0][3]
    assert task_id not in executor._active
    conn.close()


def test_verifying_missing_candidate_blocks(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_verifying_task(conn, tmp_path)
    meta = ex.merge_pipeline_state(meta, {"candidate_commit": ""})
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_VERIFYING)

    with patch.object(ex, "git_head_sha", return_value=None):
        with patch.object(ex, "run_verification") as mock_verify:
            with patch.object(ex, "block_dev_task") as mock_block:
                executor._phase_verifying(
                    conn,
                    task_id,
                    run_id,
                    meta,
                    ex.pipeline_state(meta),
                )
                mock_verify.assert_not_called()
                mock_block.assert_called_once()
                assert mock_block.call_args[0][2] == "infra_broken"
    assert task_id not in executor._active
    conn.close()


def test_verifying_workspace_head_mismatch_blocks_before_acceptance(
    kanban_home, tmp_path
):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_verifying_task(conn, tmp_path)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_VERIFYING)

    with patch.object(ex, "git_head_sha", return_value="different"):
        with patch.object(ex, "git_command") as git_mock:
            with patch.object(ex, "run_verification") as verify_mock:
                with patch.object(ex, "block_dev_task") as block_mock:
                    executor._phase_verifying(
                        conn, task_id, run_id, meta, ex.pipeline_state(meta)
                    )

    git_mock.assert_not_called()
    verify_mock.assert_not_called()
    block_mock.assert_called_once()
    assert block_mock.call_args[0][2] == "infra_broken"
    assert "does not match" in block_mock.call_args[0][3]
    assert task_id not in executor._active
    conn.close()


@pytest.mark.parametrize(
    "failure_mode,expected_detail",
    [
        ("baseline_clone", "verify baseline clone failed"),
        ("baseline_checkout", "verify baseline checkout failed"),
    ],
)
def test_verifying_baseline_git_failure_blocks(
    kanban_home, tmp_path, failure_mode, expected_detail
):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id, meta = _setup_verifying_task(conn, tmp_path)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_VERIFYING)

    fail_result = ex.CommandResult(
        command="true",
        exit_code=1,
        output_path=tmp_path / "fail.log",
    )
    clone_calls = {"count": 0}

    def git_fn(args, *, cwd=None, **_kw):
        if args[:1] == ["clone"]:
            clone_calls["count"] += 1
            if failure_mode == "baseline_clone" and clone_calls["count"] == 2:
                return _completed_proc(1, stderr="baseline clone failed")
        if (
            failure_mode == "baseline_checkout"
            and args[:1] == ["checkout"]
            and args[1] == "aaa"
        ):
            return _completed_proc(1, stderr="baseline checkout failed")
        return _completed_proc(0)

    with patch.object(ex, "git_head_sha", return_value="bbb"):
        with patch.object(ex, "git_command", side_effect=git_fn):
            with patch.object(
                ex, "run_verification", return_value=[fail_result]
            ) as mock_verify:
                with patch.object(ex, "block_dev_task") as mock_block:
                    executor._phase_verifying(
                        conn, task_id, run_id, meta, ex.pipeline_state(meta)
                    )
                    assert mock_verify.call_count == 1
                    mock_block.assert_called_once()
                    assert mock_block.call_args[0][2] == "infra_broken"
                    assert expected_detail in mock_block.call_args[0][3]
    assert task_id not in executor._active
    conn.close()


def test_preparing_ensure_dev_branch_failure_blocks(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"repo": "/tmp/r", "branch": "main", "task": "do thing"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run = kb.latest_run(conn, task_id)
    executor = ex.DevExecutor(_executor_cfg())
    executor._active[task_id] = ex.ActiveTask(task_id, run.id, ex.PHASE_PREPARING)

    with patch.object(ex, "clone_repo", return_value=(True, "/tmp/r")):
        with patch.object(
            ex, "ensure_dev_branch", return_value=(None, "checkout main failed")
        ):
            with patch.object(ex, "block_dev_task") as mock_block:
                executor._phase_preparing(conn, task_id, run.id, {}, {})
                mock_block.assert_called_once()
                assert mock_block.call_args[0][2] == "infra_broken"
                assert "checkout main failed" in mock_block.call_args[0][3]
    assert task_id not in executor._active
    conn.close()
