"""Review stage tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import dev_executor as ex


def test_parse_review_verdict_valid():
    text = '{"verdict":"pass","blocking_findings":[],"notes":["ok"]}'
    parsed = ex.parse_review_verdict(text)
    assert parsed is not None
    assert parsed["verdict"] == "pass"


def test_parse_review_verdict_garbage_fail_closed():
    assert ex.parse_review_verdict("thanks for reviewing") is None


def test_review_gate_all_pass():
    kimi = {"verdict": "pass", "blocking_findings": [], "notes": []}
    grok = {"verdict": "pass", "blocking_findings": [], "notes": []}
    proceed, repair = ex.review_gate(True, kimi, grok)
    assert proceed is True
    assert repair is False


def test_review_gate_any_fail_needs_repair():
    kimi = {"verdict": "fail", "blocking_findings": ["bug"], "notes": []}
    grok = {"verdict": "pass", "blocking_findings": [], "notes": []}
    proceed, repair = ex.review_gate(True, kimi, grok)
    assert proceed is False
    assert repair is True


def test_kimi_grok_invocations_mocked_at_subprocess_boundary(kanban_home, tmp_path):
    from hermes_cli import kanban_db as kb

    home = kanban_home
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
    run = kb.latest_run(conn, task_id)
    repo = tmp_path / "repo"
    repo.mkdir()
    logs = tmp_path / "logs"
    meta = ex.merge_pipeline_state(
        {},
        {
            "contract": {"task_summary": "x"},
            "repo_path": str(repo),
            "logs_root": str(logs),
            "base_commit": "aaa",
            "candidate_commit": "bbb",
            "mechanical_pass": True,
        },
    )
    ex.save_run_metadata(conn, run.id, meta)
    executor = ex.DevExecutor({"enabled": True, "board": "dev", "max_attempts": 2, "tick_seconds": 15, "cursor_timeout_seconds": 1800, "verify_command_timeout": 600})
    executor._active[task_id] = ex.ActiveTask(task_id, run.id, ex.PHASE_REVIEWING)

    verdict = '{"verdict":"pass","blocking_findings":[],"notes":[]}'
    kimi_mock = MagicMock(return_value=type("P", (), {"stdout": verdict, "stderr": ""})())
    grok_mock = MagicMock(return_value=type("P", (), {"stdout": verdict, "stderr": ""})())

    with patch.object(ex, "unified_diff", return_value="diff"):
        with patch.object(ex, "hermes_chat_review", kimi_mock):
            with patch.object(ex, "resolve_cursor_agent_binary", return_value="/bin/agent"):
                with patch.object(ex, "run_subprocess", grok_mock):
                    executor._phase_reviewing(conn, task_id, run.id, meta, ex.pipeline_state(meta))

    kimi_mock.assert_called_once()
    grok_mock.assert_called_once()
    conn.close()


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home
