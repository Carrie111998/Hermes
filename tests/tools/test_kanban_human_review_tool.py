"""Restricted model-facing tool for exact-head QA advancement."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


BOARD = "echlon-linear-fixes"
REPO = "Echlon-Bank/Echlon-Bank"
HEAD = "c" * 40


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb._INITIALIZED_PATHS.clear()
    db_path = kb.board_dir(BOARD) / "kanban.db"
    kb.init_db(db_path)
    with kb.connect(db_path) as conn:
        implementation_id = kb.create_task(
            conn,
            title="implementation",
            assignee="echlon-coder",
            board=BOARD,
        )
        kb.claim_task(conn, implementation_id, claimer="coder")
        assert kb.complete_task(
            conn,
            implementation_id,
            summary="done",
            metadata={
                "linear_issue_id": "ECH-444",
                "repo": REPO,
                "pr_number": 44,
                "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/44",
                "pr_base": "main",
                "branch": "ech-444",
                "pr_head_sha": HEAD,
                "changed_files": ["app.py"],
            },
        )
        qa_id = kb.create_task(
            conn,
            title="QA",
            assignee="echlon-qa",
            parents=[implementation_id],
            created_by="echlon-coder",
            board=BOARD,
        )
        qa = kb.claim_task(conn, qa_id, claimer="qa")
        assert qa is not None and qa.current_run_id is not None
        run_id = qa.current_run_id
    monkeypatch.setenv("HERMES_PROFILE", "echlon-qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", qa_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)
    monkeypatch.setenv("HERMES_SESSION_ID", "trusted-qa-session")
    return db_path, implementation_id, qa_id, run_id


def _args(implementation_id: str):
    return {
        "board": BOARD,
        "approval_packet": {
            "schema_version": 1,
            "board": BOARD,
            "gate_kind": "srdja_pr_review",
            "reviewer_principal": "github:p-echlon",
            "notification_principal": "slack:U0AA6S8RX5M",
            "human_assignee": "srdja",
            "linear_issue_id": "ECH-444",
            "linear_title": "Exact-head review",
            "linear_issue_url": "https://linear.app/echlon/issue/ECH-444/test",
            "repo": REPO,
            "pr_number": 44,
            "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/44",
            "base_branch": "main",
            "head_branch": "ech-444",
            "approved_head_sha": HEAD,
            "implementation_task_id": implementation_id,
            "qa_verdict": "APPROVE_FOR_SRDJA_REVIEW",
            "qa_attempt_count": 0,
            "coder_correction_attempt_count": 0,
            "changed_files": ["app.py"],
            "claimed_fix_summary": "Adds review gate.",
            "tests_or_checks_run": [{"command": "pytest", "outcome": "passed"}],
            "verification_output": ["passed"],
            "regression_checks": [],
            "blockers": [],
            "known_risks": [],
            "unchecked_items": ["live delivery disabled"],
            "external_side_effects": "none",
            "requires_srdja_review": True,
            "merge_policy": "human_only",
            "coderabbit": {
                "status": "skipped",
                "disposition": "QA independently reviewed exact head",
                "actionable_count": 0,
                "unresolved_count": 0,
            },
        },
    }


def _trusted_snapshot(_packet):
    return {
        "source": "github_readback",
        "verified_at": int(time.time()),
        "repo": REPO,
        "pr_number": 44,
        "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/44",
        "state": "OPEN",
        "is_draft": False,
        "base_branch": "main",
        "head_branch": "ech-444",
        "head_sha": HEAD,
    }


def test_tool_is_disabled_by_default_and_profile_scoped(monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "echlon-qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_qa")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)
    monkeypatch.setattr(kt, "load_config", lambda: {})
    assert kt._check_human_review_qa_mode() is False

    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {"kanban": {"human_review": {"enabled": True}}},
    )
    assert kt._check_human_review_qa_mode() is False
    from hermes_cli import kanban_human_review as hr
    monkeypatch.setattr(hr, "_PR_SNAPSHOT_PROVIDER", _trusted_snapshot)
    assert kt._check_human_review_qa_mode() is True
    monkeypatch.setenv("HERMES_PROFILE", "lookalike-qa")
    assert kt._check_human_review_qa_mode() is False


def test_tool_uses_trusted_worker_run_and_advances_atomically(monkeypatch, tmp_path):
    db_path, implementation_id, qa_id, _ = _setup(monkeypatch, tmp_path)
    from tools import kanban_tools as kt

    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {"kanban": {"human_review": {"enabled": True}}},
    )
    from hermes_cli import kanban_human_review as hr
    monkeypatch.setattr(hr, "_PR_SNAPSHOT_PROVIDER", _trusted_snapshot)
    result = json.loads(kt._handle_advance_linear_pr_after_qa(_args(implementation_id)))
    assert result["ok"] is True
    assert result["created"] is True
    with kb.connect(db_path) as conn:
        assert kb.get_task(conn, qa_id).status == "done"
        human = kb.get_task(conn, result["task_id"])
        assert human is not None and human.status == "awaiting_human"
        gate = conn.execute(
            "SELECT qa_worker_session_id FROM human_review_gates WHERE id=?",
            (result["gate_id"],),
        ).fetchone()
        assert gate["qa_worker_session_id"] == "trusted-qa-session"


def test_wrong_board_db_override_is_rejected_without_writes(monkeypatch, tmp_path):
    db_path, implementation_id, qa_id, _ = _setup(monkeypatch, tmp_path)
    wrong_db = tmp_path / ".hermes" / "wrong.db"
    kb.init_db(wrong_db)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(wrong_db))
    from tools import kanban_tools as kt

    monkeypatch.setattr(
        kt,
        "load_config",
        lambda: {"kanban": {"human_review": {"enabled": True}}},
    )
    from hermes_cli import kanban_human_review as hr
    monkeypatch.setattr(hr, "_PR_SNAPSHOT_PROVIDER", _trusted_snapshot)
    result = json.loads(kt._handle_advance_linear_pr_after_qa(_args(implementation_id)))
    assert "error" in result
    assert "wrong-board" in result["error"]
    with kb.connect(db_path) as conn:
        assert kb.get_task(conn, qa_id).status == "running"
        assert conn.execute("SELECT COUNT(*) FROM human_review_gates").fetchone()[0] == 0
