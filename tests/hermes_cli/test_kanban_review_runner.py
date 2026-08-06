"""Tests for the bounded script-only reconciliation/outbox runner."""

from __future__ import annotations

import argparse
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_review_runner as runner


NOW = 1_800_000_000
HEAD = "a" * 40


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


class NoSnapshotProvider:
    def read_snapshot(self, *, repository: str, pr_number: int):
        del repository, pr_number
        return None


class RegisteredAdapter:
    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> github.GitHubPullRequestSnapshot:
        raise AssertionError(
            f"unexpected live snapshot call for {repository}#{pr_number}"
        )

    def find_delivery(
        self,
        *,
        idempotency_key: str,
    ) -> github.GitHubDeliveryReceipt | None:
        del idempotency_key
        return None

    def send_intent(
        self,
        intent: github.GitHubOutboxIntent,
    ) -> github.GitHubDeliveryReceipt:
        raise AssertionError(f"unexpected live adapter call for {intent.id}")


class NeverCalledSnapshotProvider:
    def __init__(self) -> None:
        self.called = False

    def read_snapshot(self, *, repository: str, pr_number: int):
        del repository, pr_number
        self.called = True
        raise AssertionError("deadline guard must stop this provider call")


def _insert_github_intent(
    conn,
    intent_id: str,
    *,
    pr_number: int = 1,
    state: str = "pending",
    attempt_count: int = 0,
    max_attempts: int = 3,
    created_at: int = NOW - 10,
    updated_at: int = NOW - 10,
    next_attempt_at: int | None = None,
):
    conn.execute(
        """
        INSERT INTO github_human_review_outbox (
            id, gate_id, repository, pr_number, head_sha, surface, operation,
            payload_json, payload_sha256, idempotency_key, state,
            attempt_count, max_attempts, next_attempt_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'issue_comment', 'create_comment',
                  '{}', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            intent_id,
            f"gate-{intent_id}",
            "nousresearch/hermes-agent",
            pr_number,
            HEAD,
            "0" * 64,
            f"github:{intent_id}",
            state,
            attempt_count,
            max_attempts,
            next_attempt_at,
            created_at,
            updated_at,
        ),
    )
    conn.commit()


def _insert_linear_ref(conn, *, issue_id: str = "linear-runner-deadline") -> None:
    conn.execute(
        """
        INSERT INTO linear_issue_coordinators (
            linear_issue_id, linear_identifier, title, issue_url,
            source_revision, snapshot_sha256, created_at, updated_at
        ) VALUES (?, 'ECH-RUNNER', 'Runner deadline fixture',
                  'https://linear.app/echlon/issue/ECH-RUNNER', 1, ?, ?, ?)
        """,
        (issue_id, "1" * 64, NOW, NOW),
    )
    conn.execute(
        """
        INSERT INTO linear_issue_pr_links (
            linear_issue_id, repository, pr_number, first_seen_revision,
            last_seen_revision, created_at, updated_at
        ) VALUES (?, 'nousresearch/hermes-agent', 1, 1, 1, ?, ?)
        """,
        (issue_id, NOW, NOW),
    )
    conn.commit()


def _live_config(**overrides: Any) -> runner.ReviewRunnerConfig:
    values: dict[str, Any] = {
        "enabled": True,
        "mode": "live",
        "github_provider_enabled": True,
    }
    values.update(overrides)
    return runner.ReviewRunnerConfig(**values)


def _registered_adapters() -> runner.ReviewRunnerAdapters:
    return runner.ReviewRunnerAdapters(
        provider_timeout_seconds=1,
        reconciliation_snapshot_provider=NoSnapshotProvider(),
        github_snapshot_provider=RegisteredAdapter(),
        github_delivery_transport=RegisteredAdapter(),
    )


def test_default_config_is_dry_run_and_every_mutating_surface_is_disabled():
    from hermes_cli.config import DEFAULT_CONFIG

    kanban_config = cast(dict[str, Any], DEFAULT_CONFIG["kanban"])
    raw = cast(dict[str, Any], kanban_config["review_runner"])
    config = runner.ReviewRunnerConfig.from_mapping(raw)

    assert config.mode == "dry-run"
    assert config.enabled is False
    assert config.gateway_enabled is False
    assert config.github_provider_enabled is False
    assert config.slack_provider_enabled is False


def test_kanban_parser_rejects_abbreviated_global_options() -> None:
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="command")
    kc.build_parser(subparsers)

    with pytest.raises(SystemExit):
        parser.parse_args(["kanban", "--boa", "default", "review-runner", "health"])


def test_dry_run_is_deterministic_and_writes_neither_audit_nor_lease(kanban_home):
    with kb.connect_closing() as conn:
        before = conn.total_changes
        first = runner.run_review_runner(
            conn,
            config=runner.ReviewRunnerConfig(),
            now=NOW,
        )
        second = runner.run_review_runner(
            conn,
            config=runner.ReviewRunnerConfig(),
            now=NOW,
        )
        after = conn.total_changes
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_runs"
        ).fetchone()[0]
        lease_count = conn.execute(
            "SELECT COUNT(*) FROM review_boundary_runner_leases"
        ).fetchone()[0]

    assert first.to_dict() == second.to_dict()
    assert first.status == "no_op"
    assert first.read_only is True
    assert first.run_id is None
    assert before == after
    assert audit_count == 0
    assert lease_count == 0


def test_active_lease_blocks_duplicate_run_and_stale_lease_is_recovered(kanban_home):
    config = runner.ReviewRunnerConfig(enabled=True, mode="shadow")
    with kb.connect_closing() as conn:
        first = runner.acquire_runner_lease(
            conn,
            owner_id="owner-a",
            now=NOW,
            lease_seconds=config.lease_seconds,
        )
        blocked = runner.run_review_runner(
            conn,
            config=config,
            now=NOW + 1,
            owner_id="owner-b",
        )
        recovered = runner.acquire_runner_lease(
            conn,
            owner_id="owner-c",
            now=first.expires_at,
            lease_seconds=config.lease_seconds,
        )

    assert first.acquired is True
    assert blocked.status == "lease_held"
    assert blocked.results == ()
    assert recovered.acquired is True
    assert recovered.stale_recovered is True
    assert recovered.previous_owner_id == "owner-a"


def test_concurrent_scheduler_lease_has_exactly_one_winner(kanban_home):
    barrier = threading.Barrier(2)

    def attempt(owner_id: str) -> runner.LeaseReceipt:
        with kb.connect_closing() as conn:
            barrier.wait(timeout=5)
            return runner.acquire_runner_lease(
                conn,
                owner_id=owner_id,
                now=NOW,
                lease_seconds=180,
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(attempt, ("scheduler-a", "scheduler-b")))

    assert sum(receipt.acquired for receipt in receipts) == 1
    assert sum(not receipt.acquired for receipt in receipts) == 1


def test_duplicate_live_invocation_processes_one_intent_once(
    kanban_home,
    monkeypatch,
):
    calls: list[str] = []

    def fake_process(conn, intent_id, **kwargs):
        del kwargs
        calls.append(intent_id)
        conn.execute(
            "UPDATE github_human_review_outbox SET state='sent' WHERE id=?",
            (intent_id,),
        )
        conn.commit()
        return github.ProcessIntentResult(intent_id, "sent", "sent", sent=True)

    monkeypatch.setattr(github, "process_intent", fake_process)
    with kb.connect_closing() as conn:
        _insert_github_intent(conn, "gho_once")
        first = runner.run_review_runner(
            conn,
            config=_live_config(),
            adapters=_registered_adapters(),
            now=NOW,
            owner_id="owner-a",
        )
        second = runner.run_review_runner(
            conn,
            config=_live_config(),
            adapters=_registered_adapters(),
            now=NOW,
            owner_id="owner-b",
        )

    assert first.status == "ok"
    assert second.status == "no_op"
    assert calls == ["gho_once"]


def test_timeout_stops_before_next_candidate_without_retry_storm(
    kanban_home,
    monkeypatch,
):
    calls: list[str] = []

    def fake_process(conn, intent_id, **kwargs):
        del kwargs
        calls.append(intent_id)
        conn.execute(
            "UPDATE github_human_review_outbox SET state='sent' WHERE id=?",
            (intent_id,),
        )
        conn.commit()
        return github.ProcessIntentResult(intent_id, "sent", "sent", sent=True)

    ticks = iter((0.0, 0.0, 0.0, 0.0, 0.0, 6.0))
    monkeypatch.setattr(github, "process_intent", fake_process)
    with kb.connect_closing() as conn:
        _insert_github_intent(conn, "gho_first", created_at=NOW - 20)
        _insert_github_intent(
            conn,
            "gho_second",
            pr_number=2,
            created_at=NOW - 10,
        )
        receipt = runner.run_review_runner(
            conn,
            config=_live_config(timeout_seconds=5, lease_seconds=10),
            adapters=_registered_adapters(),
            now=NOW,
            monotonic=lambda: next(ticks),
        )
        states = dict(
            conn.execute(
                "SELECT id, state FROM github_human_review_outbox ORDER BY id"
            ).fetchall()
        )

    assert receipt.status == "timed_out"
    assert calls == ["gho_first"]
    assert states == {"gho_first": "sent", "gho_second": "pending"}


def test_live_runner_renews_lease_with_elapsed_time(kanban_home, monkeypatch):
    renewals: list[int] = []
    ticks = iter((0.0, 0.0, 0.0, 2.0))

    def fake_renew(conn, *, owner_id, now, lease_seconds):
        del conn, owner_id, lease_seconds
        renewals.append(now)
        return True

    def fake_process(conn, intent_id, **kwargs):
        del conn, kwargs
        return github.ProcessIntentResult(intent_id, "sent", "sent", sent=True)

    monkeypatch.setattr(runner, "renew_runner_lease", fake_renew)
    monkeypatch.setattr(github, "process_intent", fake_process)
    with kb.connect_closing() as conn:
        _insert_github_intent(conn, "gho_elapsed_lease")
        receipt = runner.run_review_runner(
            conn,
            config=_live_config(timeout_seconds=10, lease_seconds=10),
            adapters=_registered_adapters(),
            now=NOW,
            monotonic=lambda: next(ticks),
        )

    assert receipt.status == "ok"
    assert renewals == [NOW + 2]


def test_reconciliation_deadline_stops_before_unbudgeted_provider_read(kanban_home):
    provider = NeverCalledSnapshotProvider()
    ticks = iter((0.0, 1.0))
    with kb.connect_closing() as conn:
        _insert_linear_ref(conn)
        before = conn.total_changes
        receipt = runner.run_review_runner(
            conn,
            config=runner.ReviewRunnerConfig(timeout_seconds=5, lease_seconds=5),
            adapters=runner.ReviewRunnerAdapters(
                provider_timeout_seconds=5,
                reconciliation_snapshot_provider=provider,
            ),
            now=NOW,
            monotonic=lambda: next(ticks),
        )
        after = conn.total_changes

    assert receipt.status == "timed_out"
    assert receipt.read_only is True
    assert receipt.errors == (
        "runner deadline exhausted before the next reconciliation read",
    )
    assert provider.called is False
    assert before == after


def test_retry_exhaustion_is_observable_and_not_selected(kanban_home):
    with kb.connect_closing() as conn:
        _insert_github_intent(
            conn,
            "gho_exhausted",
            state="retry",
            attempt_count=3,
            max_attempts=3,
        )
        health = runner.diagnose_review_runner(
            conn,
            config=_live_config(),
            now=NOW,
        )

    assert health["outbox"]["github_due"] == 0
    assert health["outbox"]["github_retry_exhausted"] == 1
    assert health["outbox"]["candidate_ids"] == []


def test_health_reports_backlog_age_and_stale_attempts(kanban_home):
    with kb.connect_closing() as conn:
        _insert_github_intent(
            conn,
            "gho_stale",
            state="attempting",
            attempt_count=1,
            created_at=NOW - 500,
            updated_at=NOW - github.ATTEMPT_LEASE_SECONDS,
        )
        health = runner.diagnose_review_runner(
            conn,
            config=_live_config(),
            now=NOW,
        )

    github_health = health["outbox"]["github"]
    assert github_health["state_counts"] == {"attempting": 1}
    assert github_health["open_count"] == 1
    assert github_health["stale_attempting"] == 1
    assert github_health["oldest_open_age_seconds"] == 500


def test_disabled_provider_is_quiet_noop_and_preserves_pending_intent(kanban_home):
    config = runner.ReviewRunnerConfig(enabled=True, mode="live")
    with kb.connect_closing() as conn:
        _insert_github_intent(conn, "gho_disabled")
        receipt = runner.run_review_runner(conn, config=config, now=NOW)
        state = conn.execute(
            "SELECT state FROM github_human_review_outbox WHERE id='gho_disabled'"
        ).fetchone()[0]

    assert receipt.status == "no_op"
    assert receipt.quiet_noop is True
    assert receipt.results == ()
    assert receipt.skipped == (
        {
            "surface": "github",
            "intent_id": "gho_disabled",
            "reason": "provider_disabled",
        },
    )
    assert state == "pending"


def test_registered_adapter_without_bounded_timeout_fails_closed(
    kanban_home,
    monkeypatch,
):
    called = False

    def unexpected_process(*args, **kwargs):
        del args, kwargs
        nonlocal called
        called = True
        raise AssertionError("provider call must not start")

    monkeypatch.setattr(github, "process_intent", unexpected_process)
    adapters = runner.ReviewRunnerAdapters(
        reconciliation_snapshot_provider=NoSnapshotProvider(),
        github_snapshot_provider=RegisteredAdapter(),
        github_delivery_transport=RegisteredAdapter(),
    )
    with kb.connect_closing() as conn:
        _insert_github_intent(conn, "gho_unbounded")
        receipt = runner.run_review_runner(
            conn,
            config=_live_config(),
            adapters=adapters,
            now=NOW,
        )

    assert receipt.status == "failed"
    assert "provider_timeout_seconds" in receipt.errors[0]
    assert called is False


def test_shadow_mode_persists_one_idempotent_audit_only(kanban_home):
    config = runner.ReviewRunnerConfig(enabled=True, mode="shadow")
    with kb.connect_closing() as conn:
        first = runner.run_review_runner(conn, config=config, now=NOW)
        second = runner.run_review_runner(conn, config=config, now=NOW + 1)
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM reconciliation_runs"
        ).fetchone()[0]
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM review_boundary_runner_leases"
            ).fetchone()[0]
            == 0
        )

    assert first.reconciliation_run_id == second.reconciliation_run_id
    assert audit_count == 1


def test_health_reports_script_only_readiness_and_restart_contract(kanban_home):
    with kb.connect_closing() as conn:
        health = runner.diagnose_review_runner(
            conn,
            config=runner.ReviewRunnerConfig(),
            now=NOW,
        )

    assert health["script_only"] is True
    assert health["llm_required"] is False
    assert health["recursive_scheduling"] is False
    assert health["readiness"] == {
        "dry_run_ready": True,
        "shadow_ready": False,
        "live_ready": False,
    }
    assert health["gateway"]["requires_gateway_restart"] is False
    assert health["gateway"]["code_deployment_requires_gateway_restart"] is True
    assert health["gateway"]["external_operator_restart_command"] == (
        "hermes gateway " + "restart"
    )
    assert health["gateway"]["post_restart_verification"] == [
        "hermes gateway status",
        "/kanban review-runner health --json",
    ]
    assert health["cron"]["job_created_by_runner"] is False
    assert health["cron"]["compatible_mode"] == "no_agent"
    assert health["cron"]["quiet_noop_stdout"] is True


def test_health_does_not_claim_ready_for_unbounded_registered_adapter(kanban_home):
    with kb.connect_closing() as conn:
        health = runner.diagnose_review_runner(
            conn,
            config=runner.ReviewRunnerConfig(enabled=True, mode="shadow"),
            adapters=runner.ReviewRunnerAdapters(
                reconciliation_snapshot_provider=NoSnapshotProvider(),
            ),
            now=NOW,
        )

    assert health["providers"]["timeout_bounded"] is False
    assert health["readiness"] == {
        "dry_run_ready": False,
        "shadow_ready": False,
        "live_ready": False,
    }


def test_cli_health_json_and_quiet_noop(kanban_home, capsys):
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)

    health_args = parser.parse_args(["kanban", "review-runner", "health", "--json"])
    assert kc.kanban_command(health_args) == 0
    health = json.loads(capsys.readouterr().out)
    assert health["readiness"]["dry_run_ready"] is True

    run_args = parser.parse_args(["kanban", "review-runner", "run", "--quiet"])
    assert kc.kanban_command(run_args) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


@pytest.mark.asyncio
async def test_gateway_runner_is_disabled_by_default(kanban_home, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"review_runner": {"gateway_enabled": False}}},
    )
    event = SimpleNamespace(
        text="/kanban review-runner health --json",
        source=SimpleNamespace(),
    )
    instance = object.__new__(GatewayRunner)

    output = await GatewayRunner._handle_kanban_command(instance, cast(Any, event))

    assert "gateway dispatch is disabled" in output.lower()
    assert "no cron job" in output.lower()


@pytest.mark.asyncio
async def test_gateway_gate_cannot_be_bypassed_by_option_abbreviation(
    kanban_home,
    monkeypatch,
):
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"review_runner": {"gateway_enabled": False}}},
    )
    called = False

    def fail_if_called(_rest: str) -> str:
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(kc, "run_slash", fail_if_called)
    event = SimpleNamespace(
        text="/kanban --boa default review-runner health",
        source=SimpleNamespace(),
    )
    instance = object.__new__(GatewayRunner)

    output = await GatewayRunner._handle_kanban_command(instance, cast(Any, event))

    assert "gateway dispatch is disabled" in output.lower()
    assert called is False


@pytest.mark.asyncio
async def test_gateway_quiet_noop_is_silent_after_explicit_opt_in(
    kanban_home,
    monkeypatch,
):
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"review_runner": {"gateway_enabled": True}}},
    )
    event = SimpleNamespace(
        text="/kanban review-runner run --quiet",
        source=SimpleNamespace(),
    )
    instance = object.__new__(GatewayRunner)

    output = await GatewayRunner._handle_kanban_command(instance, cast(Any, event))

    assert output == ""


@pytest.mark.asyncio
async def test_gateway_cannot_override_operator_runner_policy(
    kanban_home,
    monkeypatch,
):
    from gateway.run import GatewayRunner

    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"kanban": {"review_runner": {"gateway_enabled": True}}},
    )
    called = False

    def fail_if_called(_rest: str) -> str:
        nonlocal called
        called = True
        return "unexpected"

    monkeypatch.setattr(kc, "run_slash", fail_if_called)
    event = SimpleNamespace(
        text="/kanban review-runner run --mode live --retry-ceiling=20",
        source=SimpleNamespace(),
    )
    instance = object.__new__(GatewayRunner)

    output = await GatewayRunner._handle_kanban_command(instance, cast(Any, event))

    assert "policy overrides are not accepted" in output.lower()
    assert "no runner" in output.lower()
    assert called is False
