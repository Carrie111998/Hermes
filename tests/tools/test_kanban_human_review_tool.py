"""Restricted model-facing tool for exact-head QA advancement."""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_human_review as human_review
from hermes_cli import kanban_review_runner as runner
from hermes_cli import kanban_slack as slack


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


def _typed_snapshot() -> github.GitHubPullRequestSnapshot:
    return github.GitHubPullRequestSnapshot(
        provider="fixture",
        observation_id="qa-tool-exact-head",
        repository=REPO,
        pr_number=44,
        pr_url="https://github.com/Echlon-Bank/Echlon-Bank/pull/44",
        state="open",
        is_draft=False,
        base_ref="main",
        head_ref="ech-444",
        head_sha=HEAD,
        observed_at=int(time.time()),
    )


class _SnapshotProvider:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or _typed_snapshot()

    def read_snapshot(self, *, repository: str, pr_number: int):
        assert repository.casefold() == REPO.casefold()
        assert pr_number == 44
        return self.snapshot


def _production_config():
    return {
        "kanban": {
            "human_review": {"enabled": True},
            "review_runner": {
                "enabled": True,
                "mode": "live",
                "providers": {
                    "github": {
                        "enabled": True,
                        "delivery_enabled": True,
                        "adapter": "mcp",
                        "repositories": [REPO],
                        "reviewer_logins": ["p-echlon"],
                    },
                    "slack": {
                        "enabled": True,
                        "delivery_enabled": True,
                        "adapter": "mcp",
                        "channel_ids": ["C_REVIEW", "C_OBSERVE_ONLY"],
                        "notification_channel_id": "C_REVIEW",
                        "acknowledgement_user_ids": [
                            "U0AA6S8RX5M",
                            "U_OBSERVER",
                        ],
                    },
                },
            },
        }
    }


def _runtime_adapters():
    provider = _SnapshotProvider()
    return runner.ReviewRunnerAdapters(
        provider_timeout_seconds=1,
        reconciliation_snapshot_provider=provider,
        github_snapshot_provider=provider,
        github_delivery_transport=cast(Any, object()),
        slack_snapshot_provider=provider,
        slack_delivery_transport=cast(Any, object()),
        slack_acknowledgement_provider=cast(Any, object()),
    )


def test_kanban_list_schema_covers_every_kernel_status():
    from tools import kanban_tools as kt

    parameters = cast(dict[str, Any], kt.KANBAN_LIST_SCHEMA["parameters"])
    properties = cast(dict[str, Any], parameters["properties"])
    status_schema = cast(dict[str, Any], properties["status"])
    assert set(status_schema["enum"]) == kb.VALID_STATUSES


def test_tool_is_disabled_by_default_and_profile_scoped(monkeypatch):
    from tools import kanban_tools as kt

    monkeypatch.setenv("HERMES_PROFILE", "echlon-qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_qa")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)
    monkeypatch.setattr(kt, "load_config", lambda: {})
    assert kt._check_human_review_qa_mode() is False

    monkeypatch.setattr(kt, "load_config", _production_config)
    assert kt._check_human_review_qa_mode() is True
    monkeypatch.setenv("HERMES_PROFILE", "lookalike-qa")
    assert kt._check_human_review_qa_mode() is False


def test_tool_requires_one_explicit_slack_notification_destination(monkeypatch):
    from tools import kanban_tools as kt

    config = _production_config()
    del config["kanban"]["review_runner"]["providers"]["slack"][
        "notification_channel_id"
    ]
    monkeypatch.setenv("HERMES_PROFILE", "echlon-qa")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_qa")
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)
    monkeypatch.setattr(kt, "load_config", lambda: config)

    assert kt._check_human_review_qa_mode() is False


def test_typed_outbox_failure_rolls_back_qa_gate_and_completion(monkeypatch, tmp_path):
    db_path, implementation_id, qa_id, _ = _setup(monkeypatch, tmp_path)
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "load_config", _production_config)
    monkeypatch.setattr(
        runner,
        "_build_configured_mcp_adapters",
        lambda _config: _runtime_adapters(),
    )

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected typed outbox failure")

    monkeypatch.setattr(runner, "enqueue_human_review_gate_outboxes", fail_enqueue)
    result = json.loads(kt._handle_advance_linear_pr_after_qa(_args(implementation_id)))

    assert "provider boundary failed" in result["error"]
    with kb.connect(db_path) as conn:
        qa_task = kb.get_task(conn, qa_id)
        assert qa_task is not None
        assert qa_task.status == "running"
        assert conn.execute("SELECT COUNT(*) FROM human_review_gates").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM github_human_review_outbox"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_human_review_outbox"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status='awaiting_human'"
        ).fetchone()[0] == 0


def test_tool_uses_trusted_worker_run_and_advances_atomically(monkeypatch, tmp_path):
    db_path, implementation_id, qa_id, _ = _setup(monkeypatch, tmp_path)
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "load_config", _production_config)
    monkeypatch.setattr(
        runner,
        "_build_configured_mcp_adapters",
        lambda _config: _runtime_adapters(),
    )
    result = json.loads(kt._handle_advance_linear_pr_after_qa(_args(implementation_id)))
    assert result.get("ok") is True, result
    assert result["created"] is True
    with kb.connect(db_path) as conn:
        qa_task = kb.get_task(conn, qa_id)
        assert qa_task is not None
        assert qa_task.status == "done"
        human = kb.get_task(conn, result["task_id"])
        assert human is not None and human.status == "awaiting_human"
        gate = conn.execute(
            "SELECT qa_worker_session_id FROM human_review_gates WHERE id=?",
            (result["gate_id"],),
        ).fetchone()
        assert gate["qa_worker_session_id"] == "trusted-qa-session"
        assert conn.execute(
            "SELECT COUNT(*) FROM github_human_review_outbox WHERE gate_id=?",
            (result["gate_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_human_review_outbox WHERE gate_id=?",
            (result["gate_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT channel_id FROM slack_human_review_outbox WHERE gate_id=?",
            (result["gate_id"],),
        ).fetchone()[0] == "C_REVIEW"
        unsupported = conn.execute(
            "SELECT state FROM review_gate_deliveries "
            "WHERE gate_id=? AND channel='github_review_request'",
            (result["gate_id"],),
        ).fetchone()
        assert unsupported["state"] == "superseded"

        now = int(time.time())
        conn.execute(
            "UPDATE github_human_review_outbox SET state='sent', "
            "external_id='review-comment-1', sent_at=?, updated_at=? "
            "WHERE gate_id=?",
            (now, now, result["gate_id"]),
        )
        conn.execute(
            "UPDATE slack_human_review_outbox SET state='sent', "
            "external_message_ts='201.1', delivered_thread_ts='201.1', "
            "sent_at=?, updated_at=? WHERE gate_id=?",
            (now, now, result["gate_id"]),
        )
        conn.commit()
        assert runner.refresh_human_review_gate_from_outboxes(
            conn,
            gate_id=result["gate_id"],
            now=now,
        ) == "awaiting_human"

        slack_intent = slack.get_intent(conn, result["slack_intent_ids"][0])
        assert slack_intent is not None
        other_ack = slack.record_acknowledgement(
            conn,
            source_intent_id=slack_intent.id,
            event=slack.SlackAcknowledgementEvent(
                provider="slack",
                event_id="evt-other-user",
                channel_id=slack_intent.channel_id,
                thread_ts="201.1",
                message_ts="202.0",
                user_id="U_OBSERVER",
                source="reaction",
                value="eyes",
                observed_at=now,
            ),
            now=now,
        )
        assert runner._mark_gate_seen_from_slack(
            conn,
            intent=slack_intent,
            receipt=other_ack.receipt,
            now=now,
        ) is False
        gate_before_expected_ack = human_review.get_human_review_gate(
            conn, result["gate_id"]
        )
        assert gate_before_expected_ack is not None
        assert gate_before_expected_ack.state == "awaiting_human"

        ack = slack.record_acknowledgement(
            conn,
            source_intent_id=slack_intent.id,
            event=slack.SlackAcknowledgementEvent(
                provider="slack",
                event_id="evt-1",
                channel_id=slack_intent.channel_id,
                thread_ts="201.1",
                message_ts="202.1",
                user_id="U0AA6S8RX5M",
                source="reaction",
                value="eyes",
                observed_at=now,
            ),
            now=now,
        )
        assert runner._mark_gate_seen_from_slack(
            conn,
            intent=slack_intent,
            receipt=ack.receipt,
            now=now,
        ) is True

        gate_after_ack = human_review.get_human_review_gate(conn, result["gate_id"])
        assert gate_after_ack is not None
        pre_gate_approval = replace(
            _typed_snapshot(),
            reviews=(
                github.GitHubReview(
                    review_id="review-before-qa",
                    author_login="p-echlon",
                    head_sha=HEAD,
                    state="approved",
                    submitted_at=gate_after_ack.qa_approved_at - 1,
                ),
            ),
        )
        assert runner._ingest_github_human_decisions(
            conn,
            provider=_SnapshotProvider(pre_gate_approval),
            allowed_reviewer_logins=("p-echlon",),
            max_items=10,
            now=now,
        ) == []
        still_seen = human_review.get_human_review_gate(conn, result["gate_id"])
        assert still_seen is not None and still_seen.state == "seen"

        approved_snapshot = replace(
            _typed_snapshot(),
            reviews=(
                github.GitHubReview(
                    review_id="review-after-qa",
                    author_login="p-echlon",
                    head_sha=HEAD,
                    state="approved",
                    submitted_at=gate_after_ack.qa_approved_at + 1,
                ),
            ),
        )
        decision_results = runner._ingest_github_human_decisions(
            conn,
            provider=_SnapshotProvider(approved_snapshot),
            allowed_reviewer_logins=("p-echlon",),
            max_items=10,
            now=now,
        )
        assert decision_results[0]["outcome"] == "approved"
        final_gate = human_review.get_human_review_gate(conn, result["gate_id"])
        assert final_gate is not None and final_gate.state == "human_approved"


def test_configured_mcp_boundary_reads_exact_head_without_network_writes(
    monkeypatch,
    tmp_path,
):
    db_path, implementation_id, _qa_id, _run_id = _setup(monkeypatch, tmp_path)
    home = tmp_path / ".hermes"
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "test-github-token")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "test-slack-token")
    monkeypatch.setenv("SLACK_TEAM_ID", "T_TEST")
    (home / "config.yaml").write_text(
        """
kanban:
  human_review:
    enabled: true
  review_runner:
    enabled: true
    mode: live
    providers:
      github:
        enabled: true
        delivery_enabled: true
        adapter: mcp
        mcp_server: github
        repositories: [Echlon-Bank/Echlon-Bank]
        reviewer_logins: [p-echlon]
      slack:
        enabled: true
        delivery_enabled: true
        adapter: mcp
        mcp_server: slack
        channel_ids: [C_REVIEW, C_OBSERVE_ONLY]
        notification_channel_id: C_REVIEW
        acknowledgement_user_ids: [U0AA6S8RX5M, U_OBSERVER]
mcp_servers:
  github:
    command: fake-github
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${env:GITHUB_PERSONAL_ACCESS_TOKEN}
  slack:
    command: fake-slack
    env:
      SLACK_BOT_TOKEN: ${env:SLACK_BOT_TOKEN}
      SLACK_TEAM_ID: T_TEST
""".lstrip(),
        encoding="utf-8",
    )

    from tools import kanban_tools as kt
    from tools import mcp_tool
    from tools.registry import registry

    read_calls: list[str] = []
    network_write_calls: list[str] = []
    registered_names: list[str] = []
    write_tools = {
        "create_pull_request_review",
        "slack_post_message",
        "slack_reply_to_thread",
    }
    responses = {
        "get_pull_request": {
            "number": 44,
            "html_url": "https://github.com/Echlon-Bank/Echlon-Bank/pull/44",
            "state": "open",
            "draft": False,
            "merged": False,
            "head": {"ref": "ech-444", "sha": HEAD},
            "base": {"ref": "main"},
            "requested_reviewers": [],
            "requested_teams": [],
        },
        "get_pull_request_status": {
            "state": "success",
            "sha": HEAD,
            "total_count": 0,
            "statuses": [],
        },
        "get_pull_request_reviews": [],
        "get_pull_request_comments": [],
        "slack_get_channel_history": {"ok": True, "messages": []},
        "slack_get_thread_replies": {"ok": True, "messages": []},
    }

    def fake_register(config):
        for server_name, server_config in config.items():
            toolset = f"mcp-{server_name}"
            for provider_tool in server_config["tools"]["include"]:
                tool_name = f"mcp__{server_name}__{provider_tool}"
                assert registry.get_entry(tool_name) is None

                def handler(_args, *, _provider_tool=provider_tool, **_kwargs):
                    if _provider_tool in write_tools:
                        network_write_calls.append(_provider_tool)
                        raise AssertionError("QA advancement must not perform an MCP write")
                    read_calls.append(_provider_tool)
                    return json.dumps({"result": responses[_provider_tool]})

                registry.register(
                    name=tool_name,
                    toolset=toolset,
                    schema={
                        "name": tool_name,
                        "description": "Fake configured MCP transport",
                        "parameters": {"type": "object", "properties": {}},
                    },
                    handler=handler,
                )
                registered_names.append(tool_name)
        return list(registered_names)

    monkeypatch.setattr(mcp_tool, "register_mcp_servers", fake_register)
    try:
        result = json.loads(
            kt._handle_advance_linear_pr_after_qa(_args(implementation_id))
        )
    finally:
        for tool_name in registered_names:
            registry.deregister(tool_name)

    assert result.get("ok") is True, result
    assert sorted(read_calls) == [
        "get_pull_request",
        "get_pull_request_comments",
        "get_pull_request_reviews",
        "get_pull_request_status",
    ]
    assert network_write_calls == []
    with kb.connect(db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM human_review_gates WHERE id=?",
            (result["gate_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM github_human_review_outbox WHERE gate_id=?",
            (result["gate_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM slack_human_review_outbox WHERE gate_id=?",
            (result["gate_id"],),
        ).fetchone()[0] == 1


def test_wrong_board_db_override_is_rejected_without_writes(monkeypatch, tmp_path):
    db_path, implementation_id, qa_id, _ = _setup(monkeypatch, tmp_path)
    wrong_db = tmp_path / ".hermes" / "wrong.db"
    kb.init_db(wrong_db)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(wrong_db))
    from tools import kanban_tools as kt

    monkeypatch.setattr(kt, "load_config", _production_config)
    result = json.loads(kt._handle_advance_linear_pr_after_qa(_args(implementation_id)))
    assert "error" in result
    assert "wrong-board" in result["error"]
    with kb.connect(db_path) as conn:
        qa_task = kb.get_task(conn, qa_id)
        assert qa_task is not None
        assert qa_task.status == "running"
        assert conn.execute("SELECT COUNT(*) FROM human_review_gates").fetchone()[0] == 0
