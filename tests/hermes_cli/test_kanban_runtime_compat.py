"""Compatibility coverage for the newer Kanban runtime API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb
from tools import kanban_tools as kanban_tools


@pytest.fixture
def isolated_kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    kb._INITIALIZED_PATHS.clear()
    return home


def _review_metadata() -> dict:
    return {
        "review_evidence": {
            "provider": "github",
            "repository": "example/repo",
            "branch": "wt/blocked-fix",
            "head_sha": "e4b9de58701f9b90c5e2329b33eec8a9f2229c07",
            "pr_url": "https://github.com/example/repo/pull/6",
            "pr_number": 6,
            "base_branch": "main",
        }
    }


@pytest.mark.parametrize("json_mode", [False, True])
def test_cli_dispatch_dry_run_accepts_default_dispatch_result(
    isolated_kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    json_mode: bool,
) -> None:
    """CLI text and JSON output must tolerate empty review-recovery fields."""
    monkeypatch.setattr(kb, "dispatch_once", lambda *args, **kwargs: kb.DispatchResult())

    result = cli._cmd_dispatch(
        argparse.Namespace(
            dry_run=True,
            json=json_mode,
            max=None,
            failure_limit=kb.DEFAULT_SPAWN_FAILURE_LIMIT,
        )
    )

    assert result == 0
    output = capsys.readouterr().out
    if json_mode:
        payload = json.loads(output)
        assert payload["review_recovered"] == []
        assert payload["review_recovery_blocked"] == []
    else:
        assert "Promoted:" in output


def test_block_task_accepts_and_persists_structured_metadata(
    isolated_kanban_home: Path,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="metadata block",
            assignee="hermes-coding",
            initial_status="running",
        )
        assert kb.block_task(
            conn,
            task_id,
            reason="waiting for independent review",
            metadata=_review_metadata(),
        )
        event = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'blocked' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()

    assert event is not None
    payload = json.loads(event["payload"])
    assert payload["review_evidence"]["pr_number"] == 6


def test_worker_kanban_block_forwards_structured_metadata(
    isolated_kanban_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with kb.connect() as conn:
        task_id = kb.create_task(
            conn,
            title="worker metadata block",
            assignee="hermes-coding",
            initial_status="running",
        )

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    result = json.loads(
        kanban_tools._handle_block(
            {
                "reason": "waiting for independent review",
                "metadata": _review_metadata(),
            }
        )
    )

    assert "error" not in result
    assert result["task_id"] == task_id
    with kb.connect() as conn:
        assert kb.get_task(conn, task_id).status == "blocked"
