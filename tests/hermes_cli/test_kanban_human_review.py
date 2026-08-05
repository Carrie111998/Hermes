"""Human-review gate kernel tests.

These tests exercise the exact-head QA transition against an isolated Kanban
SQLite database. Network delivery is intentionally outside this module.
"""
from __future__ import annotations

import concurrent.futures
import threading
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_human_review as hr


BOARD = "echlon-linear-fixes"
REPO = "Echlon-Bank/Echlon-Bank"
HEAD = "a" * 40


@pytest.fixture
def workflow(tmp_path: Path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    db_path = home / "kanban" / "boards" / BOARD / "kanban.db"
    kb.init_db(db_path)

    with kb.connect(db_path) as conn:
        implementation_id = kb.create_task(
            conn,
            title="opaque implementation task title",
            assignee="echlon-coder",
            board=BOARD,
        )
        implementation = kb.claim_task(conn, implementation_id, claimer="coder:test")
        assert implementation is not None
        assert kb.complete_task(
            conn,
            implementation_id,
            summary="Implemented and verified the exact PR head.",
            metadata={
                "linear_issue_id": "ECH-999",
                "linear_issue_url": "https://linear.app/echlon/issue/ECH-999/test",
                "repo": REPO,
                "pr_number": 123,
                "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/123",
                "pr_base": "main",
                "branch": "ech-999-human-gate",
                "pr_head_sha": HEAD,
                "changed_files": ["app.py"],
                "tests_or_checks_run": ["pytest -q"],
                "created_child_ids": [],
            },
        )
        qa_id = kb.create_task(
            conn,
            title="opaque QA task title",
            assignee="echlon-qa",
            parents=[implementation_id],
            created_by="echlon-coder",
            board=BOARD,
        )
        qa = kb.claim_task(conn, qa_id, claimer="qa:test")
        assert qa is not None
        qa_run_id = qa.current_run_id
        assert qa_run_id is not None

    return {
        "db_path": db_path,
        "implementation_id": implementation_id,
        "qa_id": qa_id,
        "qa_run_id": qa_run_id,
    }


def _packet(implementation_id: str, **overrides):
    packet = {
        "schema_version": 1,
        "board": BOARD,
        "gate_kind": "srdja_pr_review",
        "reviewer_principal": "github:p-echlon",
        "notification_principal": "slack:U0AA6S8RX5M",
        "human_assignee": "srdja",
        "linear_issue_id": "ECH-999",
        "linear_title": "Test exact-head human gate",
        "linear_issue_url": "https://linear.app/echlon/issue/ECH-999/test",
        "repo": REPO,
        "pr_number": 123,
        "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/123",
        "base_branch": "main",
        "head_branch": "ech-999-human-gate",
        "approved_head_sha": HEAD,
        "implementation_task_id": implementation_id,
        "qa_verdict": "APPROVE_FOR_SRDJA_REVIEW",
        "qa_attempt_count": 0,
        "coder_correction_attempt_count": 0,
        "changed_files": ["app.py"],
        "claimed_fix_summary": "Adds the human-review gate.",
        "tests_or_checks_run": [{"command": "pytest -q", "outcome": "passed"}],
        "verification_output": ["1 passed"],
        "regression_checks": ["existing review claim path unchanged"],
        "blockers": [],
        "known_risks": [],
        "unchecked_items": ["live delivery disabled"],
        "external_side_effects": "none",
        "requires_srdja_review": True,
        "merge_policy": "human_only",
        "coderabbit": {
            "status": "skipped",
            "disposition": "QA reviewed the exact head independently",
            "actionable_count": 0,
            "unresolved_count": 0,
        },
    }
    packet.update(overrides)
    return packet


def _snapshot(**overrides):
    snapshot = {
        "source": "github_readback",
        "verified_at": int(time.time()),
        "repo": REPO,
        "pr_number": 123,
        "pr_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/123",
        "state": "OPEN",
        "is_draft": False,
        "base_branch": "main",
        "head_branch": "ech-999-human-gate",
        "head_sha": HEAD,
    }
    snapshot.update(overrides)
    return snapshot


def _advance(conn, workflow, packet=None, snapshot=None):
    return hr.advance_linear_pr_after_qa(
        conn,
        qa_task_id=workflow["qa_id"],
        expected_run_id=workflow["qa_run_id"],
        approval_packet=packet or _packet(workflow["implementation_id"]),
        pr_snapshot=snapshot or _snapshot(),
        worker_session_id="qa-session-1",
        board=BOARD,
    )


def test_schema_is_additive_and_indexes_exact_and_active_gate_identity(workflow):
    with kb.connect(workflow["db_path"]) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    assert {"human_review_gates", "review_gate_deliveries"} <= tables
    assert {
        "uq_human_review_gate_exact_head",
        "uq_human_review_gate_active_pr",
        "uq_review_gate_delivery_channel",
    } <= indexes


def test_atomic_advance_creates_non_dispatchable_gate_outbox_and_completes_qa(workflow):
    with kb.connect(workflow["db_path"]) as conn:
        result = _advance(conn, workflow)

        assert result.created is True
        qa = kb.get_task(conn, workflow["qa_id"])
        human = kb.get_task(conn, result.task_id)
        assert qa is not None and qa.status == "done"
        assert human is not None and human.status == "awaiting_human"
        assert human.assignee == "srdja"
        assert result.task_id in kb.child_ids(conn, workflow["qa_id"])
        assert kb.claim_task(conn, result.task_id) is None
        assert kb.claim_review_task(conn, result.task_id) is None

        gate = hr.get_human_review_gate(conn, result.gate_id)
        assert gate is not None
        assert gate.qa_task_id == workflow["qa_id"]
        assert gate.qa_run_id == workflow["qa_run_id"]
        assert gate.qa_worker_session_id == "qa-session-1"
        assert gate.approved_head_sha == HEAD
        assert len(gate.approval_packet_sha256) == 64
        assert gate.state == "pending_delivery"

        deliveries = hr.list_gate_deliveries(conn, result.gate_id)
        assert [(d.channel, d.state) for d in deliveries] == [
            ("github_comment", "pending"),
            ("github_review_request", "pending"),
            ("slack", "pending"),
        ]
        completed = [e for e in kb.list_events(conn, workflow["qa_id"]) if e.kind == "completed"]
        assert completed[-1].payload["verified_cards"] == [result.task_id]
        assert completed[-1].payload["approval_packet_sha256"] == gate.approval_packet_sha256


def test_existing_review_claim_semantics_are_unchanged(workflow):
    with kb.connect(workflow["db_path"]) as conn:
        review_id = kb.create_task(conn, title="agent review", assignee="reviewer", board=BOARD)
        conn.execute("UPDATE tasks SET status='review' WHERE id=?", (review_id,))
        claimed = kb.claim_review_task(conn, review_id, claimer="review:test")
        assert claimed is not None
        assert claimed.status == "running"


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda p, s: p.update(qa_verdict="BLOCK_RETURN_TO_ECHLON_CODER"), "qa_verdict"),
        (lambda p, s: p.update(implementation_task_id="t_deadbeef"), "lineage"),
        (lambda p, s: s.update(head_sha="b" * 40), "head"),
        (lambda p, s: s.update(state="CLOSED"), "OPEN"),
        (lambda p, s: s.update(is_draft=True), "draft"),
        (lambda p, s: s.update(verified_at=1), "stale"),
        (lambda p, s: s.update(source="fake_github"), "trusted"),
    ],
)
def test_validation_failure_leaves_qa_running_and_writes_nothing(workflow, mutation, error):
    packet = _packet(workflow["implementation_id"])
    snapshot = _snapshot()
    mutation(packet, snapshot)
    with kb.connect(workflow["db_path"]) as conn:
        with pytest.raises(ValueError, match=error):
            _advance(conn, workflow, packet=packet, snapshot=snapshot)
        assert kb.get_task(conn, workflow["qa_id"]).status == "running"
        assert conn.execute("SELECT COUNT(*) FROM human_review_gates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM review_gate_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE status='awaiting_human'").fetchone()[0] == 0


def test_wrong_run_and_untrusted_profile_are_rejected(workflow):
    with kb.connect(workflow["db_path"]) as conn:
        with pytest.raises(ValueError, match="current run"):
            hr.advance_linear_pr_after_qa(
                conn,
                qa_task_id=workflow["qa_id"],
                expected_run_id=workflow["qa_run_id"] + 1,
                approval_packet=_packet(workflow["implementation_id"]),
                pr_snapshot=_snapshot(),
                board=BOARD,
            )
        conn.execute(
            "UPDATE task_runs SET profile='lookalike-qa' WHERE id=?",
            (workflow["qa_run_id"],),
        )
        with pytest.raises(ValueError, match="echlon-qa"):
            _advance(conn, workflow)
        assert kb.get_task(conn, workflow["qa_id"]).status == "running"


def test_two_concurrent_advances_converge_on_one_gate_task_and_outbox(workflow):
    barrier = threading.Barrier(2)

    def run_once():
        with kb.connect(workflow["db_path"]) as conn:
            barrier.wait(timeout=5)
            return _advance(conn, workflow)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _i: run_once(), range(2)))

    assert {r.gate_id for r in results} == {results[0].gate_id}
    assert {r.task_id for r in results} == {results[0].task_id}
    assert sorted(r.created for r in results) == [False, True]
    with kb.connect(workflow["db_path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM human_review_gates").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE status='awaiting_human'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM review_gate_deliveries").fetchone()[0] == 3


def test_partial_failure_rolls_back_gate_task_link_outbox_and_qa_completion(workflow, monkeypatch):
    original = hr._insert_gate_deliveries

    def fail_after_first(conn, **kwargs):
        original(conn, channels=("github_comment",), **kwargs)
        raise RuntimeError("injected delivery insert failure")

    monkeypatch.setattr(hr, "_insert_gate_deliveries", fail_after_first)
    with kb.connect(workflow["db_path"]) as conn:
        with pytest.raises(RuntimeError, match="injected"):
            _advance(conn, workflow)
        assert kb.get_task(conn, workflow["qa_id"]).status == "running"
        assert kb.child_ids(conn, workflow["qa_id"]) == []
        assert conn.execute("SELECT COUNT(*) FROM human_review_gates").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM review_gate_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE status='awaiting_human'").fetchone()[0] == 0


def test_coderabbit_actionable_findings_cannot_be_silently_approved(workflow):
    unresolved = _packet(
        workflow["implementation_id"],
        coderabbit={
            "status": "actionable",
            "disposition": "ignored",
            "actionable_count": 2,
            "unresolved_count": 1,
        },
    )
    with kb.connect(workflow["db_path"]) as conn:
        with pytest.raises(ValueError, match="CodeRabbit.*unresolved"):
            _advance(conn, workflow, packet=unresolved)


def test_coderabbit_skipped_and_rate_limited_require_explicit_disposition(workflow):
    packet = _packet(
        workflow["implementation_id"],
        coderabbit={
            "status": "rate_limited",
            "disposition": "",
            "actionable_count": 0,
            "unresolved_count": 0,
        },
    )
    with kb.connect(workflow["db_path"]) as conn:
        with pytest.raises(ValueError, match="disposition"):
            _advance(conn, workflow, packet=packet)
        result = _advance(
            conn,
            workflow,
            packet=_packet(
                workflow["implementation_id"],
                coderabbit={
                    "status": "rate_limited",
                    "disposition": "QA completed an independent exact-head review",
                    "actionable_count": 0,
                    "unresolved_count": 0,
                },
            ),
        )
        assert result.created is True
