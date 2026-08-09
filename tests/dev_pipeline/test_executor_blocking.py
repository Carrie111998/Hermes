"""Post-attempt blocking and writer-fencing tests."""

from __future__ import annotations

import json
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


def _setup_pipeline_task(conn, tmp_path, *, phase: str = ex.PHASE_REVIEWING) -> tuple[str, int]:
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    task_id = kb.create_task(
        conn,
        title="t",
        body=json.dumps({"task": "implement x"}),
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    kb._end_run(
        conn,
        task_id,
        outcome="completed",
        summary="attempt done",
        metadata={"dev_pipeline": {"candidate_commit": "bbb"}},
    )
    pipeline_run = ex.start_new_run(
        conn,
        task_id,
        metadata=ex.merge_pipeline_state(
            {},
            {
                "phase": phase,
                "contract": {"task_summary": "x", "acceptance_commands": ["true"]},
                "repo_path": str(repo),
                "logs_root": str(logs),
                "base_commit": "aaa",
                "candidate_commit": "bbb",
                "mechanical_pass": True,
            },
        ),
    )
    return task_id, pipeline_run


def test_review_unavailable_blocks_after_attempt_end(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id = _setup_pipeline_task(conn, tmp_path)
    meta = ex.load_run_metadata(conn, run_id)
    executor = ex.DevExecutor(
        {
            "enabled": True,
            "board": "dev",
            "max_attempts": 2,
            "tick_seconds": 15,
            "cursor_timeout_seconds": 1800,
            "verify_command_timeout": 600,
        }
    )
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    with patch.object(ex, "unified_diff", return_value="safe diff"):
        with patch.object(ex, "hermes_chat_review") as mock_kimi:
            mock_kimi.return_value = type(
                "P", (), {"stdout": "garbage", "stderr": ""}
            )()
            with patch.object(ex, "resolve_cursor_agent_binary", return_value=None):
                executor._phase_reviewing(
                    conn, task_id, run_id, meta, ex.pipeline_state(meta)
                )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    conn.close()


def test_secret_in_diff_blocks_after_attempt_end(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id = _setup_pipeline_task(conn, tmp_path, phase=ex.PHASE_PUBLISHING)
    meta = ex.load_run_metadata(conn, run_id)
    executor = ex.DevExecutor(
        {
            "enabled": True,
            "board": "dev",
            "max_attempts": 2,
            "tick_seconds": 15,
            "cursor_timeout_seconds": 1800,
            "verify_command_timeout": 600,
        }
    )
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_PUBLISHING)

    secret_diff = "+token ghp_abcdefghijklmnopqrstuvwxyz1234567890\n"
    with patch.object(ex, "publish_pr", return_value=(False, "findings", "secret_in_diff")):
        executor._phase_publishing(
            conn, task_id, run_id, meta, ex.pipeline_state(meta)
        )

    task = kb.get_task(conn, task_id)
    assert task is not None
    assert task.status == "blocked"
    conn.close()


def test_reviewing_secret_scan_before_writing_artifacts(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id, run_id = _setup_pipeline_task(conn, tmp_path)
    meta = ex.load_run_metadata(conn, run_id)
    logs = Path(ex.pipeline_state(meta)["logs_root"])
    executor = ex.DevExecutor(
        {
            "enabled": True,
            "board": "dev",
            "max_attempts": 2,
            "tick_seconds": 15,
            "cursor_timeout_seconds": 1800,
            "verify_command_timeout": 600,
        }
    )
    executor._active[task_id] = ex.ActiveTask(task_id, run_id, ex.PHASE_REVIEWING)

    with patch.object(
        ex, "unified_diff", return_value="+ghp_secretleak123456789012345678901234\n"
    ):
        with patch.object(ex, "hermes_chat_review") as mock_kimi:
            executor._phase_reviewing(
                conn, task_id, run_id, meta, ex.pipeline_state(meta)
            )
            mock_kimi.assert_not_called()

    assert (logs / "secret-scan-quarantine.json").is_file()
    assert not (logs / "review-kimi.raw").exists()
    conn.close()


def test_spawn_refuses_when_other_task_unit_active(kanban_home, tmp_path):
    kb.create_board("dev")
    conn = kb.connect(board="dev")
    task_id = kb.create_task(
        conn,
        title="t",
        body='{"task":"x"}',
        workspace_kind="scratch",
        board="dev",
    )
    kb.claim_task(conn, task_id, claimer="dev-executor")
    run1 = kb.latest_run(conn, task_id)
    run2 = ex.start_new_run(
        conn, task_id, metadata={"dev_pipeline": {"phase": "RUNNING"}}
    )
    meta = ex.load_run_metadata(conn, run2)
    executor = ex.DevExecutor(
        {
            "enabled": True,
            "board": "dev",
            "max_attempts": 2,
            "tick_seconds": 15,
            "cursor_timeout_seconds": 1800,
            "verify_command_timeout": 600,
        }
    )

    active_unit = ex.unit_name(task_id, run1.id)

    def fake_active(unit):
        return unit == active_unit, "active"

    with patch.object(executor, "_is_active", side_effect=fake_active):
        with patch.object(ex, "systemd_run_attempt") as mock_run:
            executor._spawn_attempt(
                conn, task_id, run2, meta, ex.pipeline_state(meta)
            )
            mock_run.assert_not_called()
    conn.close()
