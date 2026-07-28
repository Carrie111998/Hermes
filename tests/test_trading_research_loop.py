from __future__ import annotations

import argparse
import contextlib
import io
import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.trading_research_loop import (
    CircuitBreakerOpen,
    DuplicateExperiment,
    InvalidTransition,
    PromotionBlocked,
    SafetyViolation,
    StateConflict,
    TradingResearchLoopError,
    TradingResearchLoopStore,
    build_parser,
    decompose_hypothesis,
    experiment_fingerprint,
    format_topic_update,
    reconstruct_from_kanban,
    trading_loop_command,
    topic_for_role,
)


def _open(store: TradingResearchLoopStore, **overrides):
    limits = {
        "max_iterations": 5,
        "max_experiments": 20,
        "max_consecutive_failures": 2,
        "max_wall_clock_hours": 24,
        "max_cost_budget": 10.0,
        "approval_ttl_seconds": 3600,
    }
    limits.update(overrides)
    return store.open_loop(
        goal="Trouver une stratégie spot long-only BTC/USDC vérifiable",
        scope={"symbols": ["BTC/USDC"], "timeframes": ["5m"], "market": "spot"},
        limits=limits,
        loop_id="loop-pilot",
        now=1_000,
    )


def _trusted_producer(_state, _role, _task_id):
    """Explicit test seam; production verifies the official Kanban task."""
    return True


def test_state_survives_restart_and_writes_required_layout(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    state = _open(store)
    store.transition(state["loop_id"], "researching", reason="manual start", now=1_001)

    restarted = TradingResearchLoopStore(tmp_path)
    loaded = restarted.load("loop-pilot")

    assert loaded["status"] == "researching"
    assert loaded["iteration"] == 0
    assert loaded["experiment_count"] == 0
    assert loaded["consecutive_failures"] == 0
    assert loaded["limits"]["max_iterations"] == 5
    assert loaded["telegram_threads"] == {
        "orchestrator": 1,
        "researcher": 10,
        "developer": 11,
        "backtester": 13,
        "reviewer": 16,
        "benchmark": 68,
        "risk": 106,
        "kronos": 1490,
    }
    loop_dir = tmp_path / "loop-pilot"
    assert (loop_dir / "state.json").is_file()
    assert (loop_dir / "hypotheses.json").is_file()
    assert (loop_dir / "experiments.jsonl").is_file()
    assert (loop_dir / "approvals.jsonl").is_file()
    assert (loop_dir / "rejected_hypotheses.jsonl").is_file()
    assert (loop_dir / "artifacts").is_dir()


def test_bounded_iteration_stops_without_busy_loop(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store, max_iterations=2)

    store.begin_iteration("loop-pilot", now=1_001)
    store.begin_iteration("loop-pilot", now=1_002)
    with pytest.raises(CircuitBreakerOpen):
        store.begin_iteration("loop-pilot", now=1_003)

    state = store.load("loop-pilot")
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "max_iterations_reached"
    assert state["iteration"] == 2


def test_duplicate_experiment_fingerprint_is_stable_and_rejected(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store)
    definition_a = {"symbol": "BTC/USDC", "params": {"slow": 50, "fast": 10}}
    definition_b = {"params": {"fast": 10, "slow": 50}, "symbol": "BTC/USDC"}

    assert experiment_fingerprint(definition_a) == experiment_fingerprint(definition_b)
    store.register_experiment("loop-pilot", definition_a, outcome="candidate", now=1_001)
    with pytest.raises(DuplicateExperiment):
        store.register_experiment("loop-pilot", definition_b, outcome="candidate", now=1_002)

    assert store.load("loop-pilot")["experiment_count"] == 1


def test_failure_limit_opens_loop_circuit_breaker(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store)

    store.record_failure("loop-pilot", "worker timeout", now=1_001)
    with pytest.raises(CircuitBreakerOpen):
        store.record_failure("loop-pilot", "worker timeout", now=1_002)

    state = store.load("loop-pilot")
    assert state["status"] == "blocked"
    assert state["stop_reason"] == "max_consecutive_failures_reached"
    assert state["consecutive_failures"] == 2


def test_kronos_execution_or_lower_mae_never_replaces_strategy_evidence(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store)
    for status in ("researching", "developing", "backtesting", "reviewing"):
        store.transition("loop-pilot", status, reason="test lifecycle", now=1_010)
    candidate = {
        "hypothesis_id": "h-kronos",
        "artifact_sha256": "a" * 64,
        "metrics": {
            "kronos_executed": True,
            "kronos_mae": 1.0,
            "last_close_mae": 2.0,
            "kronos_rmse": 1.5,
            "last_close_rmse": 2.5,
        },
        "evidence": ["artifacts/kronos.json"],
    }

    with pytest.raises(PromotionBlocked, match="incomplete"):
        store.request_promotion("loop-pilot", candidate, now=1_100)

    assert store.load("loop-pilot")["status"] == "blocked"


def test_promotion_requires_fresh_scoped_approval_and_matching_artifact(tmp_path: Path):
    store = TradingResearchLoopStore(
        tmp_path,
        approval_verifier=lambda attestation: "human:owner" if attestation == "signed-owner" else None,
        producer_verifier=_trusted_producer,
    )
    _open(store)
    backtest_artifact = store.write_artifact(
        "loop-pilot", "backtest.json", b'{"fee_aware":true}', evidence_type="backtest",
        producer_role="backtester", producer_task_id="task-backtest", now=1_005
    )
    review_artifact = store.write_artifact(
        "loop-pilot", "review.json", b'{"independent":true}', evidence_type="review",
        producer_role="reviewer", producer_task_id="task-review", now=1_006
    )
    candidate_artifact = store.write_artifact(
        "loop-pilot", "candidate.json", b'{"strategy":"ema"}', evidence_type="candidate",
        producer_role="developer", producer_task_id="task-development", now=1_007
    )
    for status in ("researching", "developing", "backtesting", "reviewing"):
        store.transition("loop-pilot", status, reason="test lifecycle", now=1_010)
    candidate = {
        "hypothesis_id": "h-1",
        "artifact_path": candidate_artifact["path"],
        "artifact_sha256": candidate_artifact["sha256"],
        "metrics": {
            "net_profit_pct": 3.0,
            "max_drawdown_pct": 4.0,
            "profit_factor": 1.4,
            "trades": 100,
            "baseline_beaten": True,
            "kronos_executed": False,
        },
        "evidence": [backtest_artifact["path"], review_artifact["path"]],
    }
    store.request_promotion("loop-pilot", candidate, now=1_100)

    with pytest.raises(PromotionBlocked, match="approval"):
        store.promote("loop-pilot", candidate, now=1_101)

    request_id = store.load("loop-pilot")["promotion_request_id"]
    store.record_approval(
        "loop-pilot",
        attestation="signed-owner",
        request_id=request_id,
        approved=True,
        now=1_104,
    )
    promoted = store.promote("loop-pilot", candidate, now=1_105)
    assert promoted["status"] == "stopped"
    assert promoted["stop_reason"] == "promoted_with_human_approval"


def test_rejection_requires_counterproposal_and_is_persisted(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store)
    hypothesis = {"id": "h-bad", "claim": "RSI seul suffit"}

    with pytest.raises(ValueError, match="counterproposal"):
        store.reject_hypothesis("loop-pilot", hypothesis, reason="weak", counterproposal="", now=1_001)

    store.reject_hypothesis(
        "loop-pilot",
        hypothesis,
        reason="baseline non battue",
        counterproposal="Tester un filtre de tendance et des frais réels",
        now=1_002,
    )
    rows = [json.loads(line) for line in (tmp_path / "loop-pilot" / "rejected_hypotheses.jsonl").read_text().splitlines()]
    assert rows[-1]["hypothesis"]["id"] == "h-bad"
    assert rows[-1]["counterproposal"].startswith("Tester")


def test_transition_whitelist_and_optimistic_revision(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    state = _open(store)
    with pytest.raises(InvalidTransition):
        store.transition("loop-pilot", "promoting", reason="skip gates", now=1_001)
    with pytest.raises(StateConflict):
        store.transition(
            "loop-pilot", "researching", reason="stale writer", expected_revision=state["revision"] + 1, now=1_002
        )


def test_blocked_loop_cannot_continue_work_until_explicit_recovery(tmp_path: Path):
    store = TradingResearchLoopStore(
        tmp_path,
        approval_verifier=lambda attestation: (
            "human:owner" if isinstance(attestation, dict) and attestation.get("signature") == "signed-owner" else None
        ),
    )
    _open(store)
    store.transition("loop-pilot", "blocked", reason="operator review", now=1_001)

    with pytest.raises(CircuitBreakerOpen, match="blocked"):
        store.begin_iteration("loop-pilot", now=1_002)
    with pytest.raises(CircuitBreakerOpen, match="blocked"):
        store.register_experiment("loop-pilot", {"id": "e-1"}, outcome="pending", now=1_003)

    with pytest.raises(InvalidTransition, match="authenticated recover"):
        store.transition("loop-pilot", "researching", reason="human recovery", now=1_004)

    recovered = store.recover(
        "loop-pilot",
        attestation={"signature": "signed-owner", "id": "first", "issued_at": 1_004, "expires_at": 1_100},
        now=1_005,
    )
    assert recovered["status"] == "researching"
    assert recovered["consecutive_failures"] == 0
    assert recovered["history"][-1]["event"] == "recovered"
    assert recovered["history"][-1]["actor"] == "human:owner"


def test_recovery_rejects_missing_invalid_expired_and_replayed_attestations(tmp_path: Path):
    def verifier(attestation):
        if isinstance(attestation, dict) and attestation.get("signature") == "signed-owner":
            return "human:owner"
        return None

    store = TradingResearchLoopStore(tmp_path, approval_verifier=verifier)
    _open(store)
    store.transition("loop-pilot", "blocked", reason="operator review", now=1_001)

    with pytest.raises(PromotionBlocked, match="authenticated"):
        store.recover("loop-pilot", attestation=None, now=1_002)
    with pytest.raises(PromotionBlocked, match="authenticated"):
        store.recover("loop-pilot", attestation={"signature": "forged"}, now=1_003)
    with pytest.raises(PromotionBlocked, match="expired"):
        store.recover(
            "loop-pilot",
            attestation={"signature": "signed-owner", "id": "old", "issued_at": 1_000, "expires_at": 1_003},
            now=1_004,
        )
    with pytest.raises(PromotionBlocked, match="invalid recovery attestation"):
        store.recover(
            "loop-pilot",
            attestation={"signature": "signed-owner", "id": "missing-issued", "expires_at": 1_100},
            now=1_004,
        )
    with pytest.raises(PromotionBlocked, match="validity window"):
        store.recover(
            "loop-pilot",
            attestation={"signature": "signed-owner", "id": "too-long", "issued_at": 1_000, "expires_at": 1_301},
            now=1_004,
        )

    attestation = {
        "signature": "signed-owner", "id": "fresh", "issued_at": 1_000, "expires_at": 1_100,
    }
    recovered = store.recover("loop-pilot", attestation=attestation, now=1_005)
    audit = recovered["history"][-1]
    assert audit["event"] == "recovered"
    assert audit["actor"] == "human:owner"
    assert len(audit["attestation_fingerprint"]) == 64

    store.transition("loop-pilot", "blocked", reason="second review", now=1_006)
    with pytest.raises(PromotionBlocked, match="already been used"):
        store.recover("loop-pilot", attestation=attestation, now=1_007)


def test_cli_recovery_accepts_distinct_fresh_attestations_for_later_blocks(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="command", required=True))

    def invoke(*arguments):
        args = root.parse_args(["trading-loop", *arguments])
        with contextlib.redirect_stdout(io.StringIO()):
            assert trading_loop_command(args) == 0

    invoke("open", "--goal", "recovery regression", "--symbol", "BTC/USDC", "--loop-id", "recover-twice")
    invoke("transition", "recover-twice", "blocked", "--reason", "first review")
    invoke(
        "recover", "recover-twice", "--human-confirmation", "RECOVER recover-twice",
    )
    invoke("transition", "recover-twice", "blocked", "--reason", "second review")
    invoke(
        "recover", "recover-twice", "--human-confirmation", "RECOVER recover-twice",
    )

    state = TradingResearchLoopStore(tmp_path / "loops" / "trading-research").load("recover-twice")
    recovered = [item for item in state["history"] if item["event"] == "recovered"]
    assert state["status"] == "researching"
    assert len(recovered) == 2
    assert recovered[0]["attestation_fingerprint"] != recovered[1]["attestation_fingerprint"]


def test_hypothesis_registry_is_durable_and_rejects_duplicate_ids(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store)
    hypothesis = {"id": "h-1", "claim": "EMA fee-aware beats hold"}
    store.register_hypothesis("loop-pilot", hypothesis, now=1_001)

    with pytest.raises(ValueError, match="already exists"):
        store.register_hypothesis("loop-pilot", hypothesis, now=1_002)

    rows = json.loads(
        (tmp_path / "loop-pilot" / "hypotheses.json").read_text(encoding="utf-8")
    )
    assert rows == [{**hypothesis, "registered_at": 1_001}]


def test_corrupt_checkpoint_fails_closed(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store)
    (tmp_path / "loop-pilot" / "state.json").write_text("{not-json", encoding="utf-8")

    with pytest.raises(TradingResearchLoopError, match="corrupt"):
        TradingResearchLoopStore(tmp_path).load("loop-pilot")


def test_iteration_compare_and_swap_prevents_stale_worker_write(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    state = _open(store)
    store.begin_iteration("loop-pilot", expected_revision=state["revision"], now=1_001)

    with pytest.raises(StateConflict):
        store.begin_iteration("loop-pilot", expected_revision=state["revision"], now=1_002)

    assert store.load("loop-pilot")["iteration"] == 1


def test_forbidden_live_trading_actions_are_rejected(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store)
    for action in (
        "place_order BTCUSDC",
        "submit market order",
        "use exchange API key to trade",
        "transfer funds to exchange",
        "buy BTC with real funds",
        "BUY BTC WITH REAL FUNDS",
        "submit an order",
        "submit-order",
        "submit_order",
        "submit.order",
        "submit/order",
        "place-order BTCUSDC",
        "place order",
        "place_order",
        "place/order",
        "placeOrder",
        "execute:market:order",
        "ｐｌａｃｅ　ｏｒｄｅｒ",
        "cancel an order",
        "withdraw funds",
        "transfer assets",
    ):
        with pytest.raises(SafetyViolation):
            store.assert_safe_research_action(action)


def test_artifact_producer_must_be_an_attached_official_kanban_task(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    store = TradingResearchLoopStore(tmp_path / "loops")
    _open(store)

    with pytest.raises(SafetyViolation, match="official Kanban producer"):
        store.write_artifact(
            "loop-pilot", "forged.json", b"forged", evidence_type="review",
            producer_role="reviewer", producer_task_id="made-up-task", now=1_001,
        )


def test_kanban_helpers_reject_non_official_board_connection(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect(kb.kanban_db_path("counterfeit-board"))
    try:
        with pytest.raises(ValueError, match="official trading-research"):
            decompose_hypothesis(
                conn, loop_id="loop-pilot", hypothesis={"id": "h-1"},
                board="counterfeit-board",
            )
        with pytest.raises(ValueError, match="not official board trading-research database"):
            reconstruct_from_kanban(conn, "loop-pilot")
    finally:
        conn.close()


def test_cli_exposes_end_to_end_governance_ingress():
    root = argparse.ArgumentParser()
    parser = build_parser(root.add_subparsers(dest="command", required=True))
    commands = parser._subparsers._group_actions[0].choices
    assert {
        "open", "show", "transition", "iterate", "recover", "hypothesis",
        "decompose", "experiment", "artifact", "request-promotion", "approve", "promote",
    }.issubset(commands)


@pytest.mark.parametrize(
    "command",
    ("recover", "hypothesis", "decompose", "experiment", "artifact",
     "request-promotion", "approve", "promote"),
)
def test_governance_cli_commands_expose_help_and_reject_missing_parameters(command):
    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="command", required=True))
    with pytest.raises(SystemExit) as help_exit:
        root.parse_args(["trading-loop", command, "--help"])
    assert help_exit.value.code == 0
    with pytest.raises(SystemExit) as missing_exit:
        root.parse_args(["trading-loop", command])
    assert missing_exit.value.code == 2


def test_governance_cli_rejects_unsafe_loop_identifier():
    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="command", required=True))
    with pytest.raises(SystemExit) as invalid_exit:
        root.parse_args(["trading-loop", "show", "../other-loop"])
    assert invalid_exit.value.code == 2


def test_topic_mapping_and_message_are_specialized():
    assert topic_for_role("kronos") == 1490
    assert topic_for_role("risk") == 106
    message = format_topic_update(
        loop_id="loop-pilot", role="reviewer", status="blocked", summary="baseline non battue"
    )
    assert "loop-pilot" in message
    assert "blocked" in message
    assert "baseline non battue" in message
    assert "ordre" not in message.lower()


def test_kanban_decomposition_uses_official_board_and_is_idempotent(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = kb.kanban_db_path("trading-research")
    conn = kb.connect(db)
    try:
        ids = decompose_hypothesis(
            conn,
            loop_id="loop-pilot",
            hypothesis={"id": "h-1", "claim": "EMA fee-aware"},
            board="trading-research",
        )
        again = decompose_hypothesis(
            conn,
            loop_id="loop-pilot",
            hypothesis={"id": "h-1", "claim": "EMA fee-aware"},
            board="trading-research",
        )
        assert ids == again
        assert set(ids) == {"parent", "research", "development", "backtest", "review"}
        tasks = {key: kb.get_task(conn, task_id) for key, task_id in ids.items()}
        assert tasks["research"].assignee == "researcher"
        assert tasks["development"].assignee == "developer"
        assert tasks["backtest"].assignee == "tester"
        assert tasks["review"].assignee == "reviewer"
        assert tasks["backtest"].max_retries == 2
        assert tasks["review"].skills == ["trading-research-loop", "trading-strategy-research"]
        assert all("loop_id" in (task.body or "") for task in tasks.values())
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("field", "counterfeit"),
    (("loop_type", "Counterfeit"), ("topic_id", 999999), ("live_trading", True)),
)
def test_kanban_tasks_with_counterfeit_governance_metadata_are_rejected(
    tmp_path: Path, monkeypatch, field, counterfeit
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    conn = kb.connect(kb.kanban_db_path("trading-research"))
    try:
        ids = decompose_hypothesis(
            conn, loop_id="loop-pilot", hypothesis={"id": "h-1", "claim": "EMA"}
        )
        task = kb.get_task(conn, ids["review"])
        marker = "TRADING_RESEARCH_LOOP_METADATA="
        prefix, raw_metadata = task.body.split(marker, 1)
        metadata = json.loads(raw_metadata)
        metadata[field] = counterfeit
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?",
            (
                prefix + marker + json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                task.id,
            ),
        )
        conn.commit()
        store = TradingResearchLoopStore(tmp_path / "loops")
        _open(store)
        with pytest.raises(SafetyViolation, match="official producer"):
            store.attach_kanban_tasks("loop-pilot", {"review": task.id})
    finally:
        conn.close()


def test_cli_executes_complete_governance_cycle(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    root = argparse.ArgumentParser()
    build_parser(root.add_subparsers(dest="command", required=True))

    def invoke(*arguments):
        args = root.parse_args(["trading-loop", *arguments])
        with contextlib.redirect_stdout(io.StringIO()):
            assert trading_loop_command(args) == 0

    loop_id = "cli-cycle"
    hypothesis = {"id": "h-1", "claim": "fee-aware EMA baseline"}
    hypothesis_file = tmp_path / "hypothesis.json"
    hypothesis_file.write_text(json.dumps(hypothesis), encoding="utf-8")
    invoke(
        "open", "--loop-id", loop_id, "--goal", "Test one bounded hypothesis",
        "--symbol", "BTC/USDC", "--timeframe", "5m",
    )
    invoke("hypothesis", loop_id, str(hypothesis_file))
    invoke("decompose", loop_id, str(hypothesis_file))

    store = TradingResearchLoopStore()
    state = store.load(loop_id)
    assert state["scope"]["timeframes"] == ["5m"]
    for status in ("researching", "developing", "backtesting", "reviewing"):
        invoke("transition", loop_id, status, "--reason", "CLI integration test")

    evidence_specs = (
        ("backtest.json", "backtest", "backtester", state["kanban_tasks"]["backtest"], b"backtest"),
        ("review.json", "review", "reviewer", state["kanban_tasks"]["review"], b"review"),
        ("candidate.json", "candidate", "developer", state["kanban_tasks"]["development"], b"candidate"),
    )
    for filename, evidence_type, role, task_id, payload in evidence_specs:
        source = tmp_path / filename
        source.write_bytes(payload)
        invoke(
            "artifact", loop_id, str(source), "--type", evidence_type,
            "--producer-role", role, "--producer-task-id", task_id,
        )

    artifacts = {item["evidence_type"]: item for item in store.load(loop_id)["artifacts"]}
    candidate = {
        "hypothesis_id": "h-1",
        "artifact_path": artifacts["candidate"]["path"],
        "artifact_sha256": artifacts["candidate"]["sha256"],
        "evidence": [artifacts["backtest"]["path"], artifacts["review"]["path"]],
        "metrics": {
            "net_profit_pct": 1.0, "max_drawdown_pct": 2.0,
            "profit_factor": 1.1, "trades": 30, "baseline_beaten": True,
        },
    }
    candidate_file = tmp_path / "candidate-request.json"
    candidate_file.write_text(json.dumps(candidate), encoding="utf-8")
    invoke("request-promotion", loop_id, str(candidate_file))
    invoke("approve", loop_id, "--human-confirmation", f"APPROVE {loop_id}")
    invoke("promote", loop_id, str(candidate_file))
    final_state = store.load(loop_id)
    assert final_state["status"] == "stopped"
    assert final_state["stop_reason"] == "promoted_with_human_approval"


def test_state_can_be_reconstructed_from_kanban_metadata(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = kb.kanban_db_path("trading-research")
    conn = kb.connect(db)
    try:
        expected = decompose_hypothesis(
            conn,
            loop_id="loop-pilot",
            hypothesis={"id": "h-1", "claim": "EMA fee-aware"},
            board="trading-research",
        )
        rebuilt = reconstruct_from_kanban(conn, "loop-pilot")
        assert rebuilt["loop_id"] == "loop-pilot"
        assert rebuilt["kanban_board"] == "trading-research"
        assert rebuilt["kanban_tasks"] == expected
        assert rebuilt["hypotheses"][0]["id"] == "h-1"
    finally:
        conn.close()


def test_approval_is_authenticated_strict_boolean_scoped_and_revocable(tmp_path: Path):
    store = TradingResearchLoopStore(
        tmp_path,
        approval_verifier=lambda token: "human:owner" if token == "signed" else None,
        producer_verifier=_trusted_producer,
    )
    _open(store)
    backtest = store.write_artifact(
        "loop-pilot", "bt.json", b"bt", evidence_type="backtest",
        producer_role="backtester", producer_task_id="bt-1", now=1_001,
    )
    review = store.write_artifact(
        "loop-pilot", "rv.json", b"rv", evidence_type="review",
        producer_role="reviewer", producer_task_id="rv-1", now=1_002,
    )
    artifact = store.write_artifact(
        "loop-pilot", "candidate.json", b"candidate", evidence_type="candidate",
        producer_role="developer", producer_task_id="dev-1", now=1_003,
    )
    for status in ("researching", "developing", "backtesting", "reviewing"):
        store.transition("loop-pilot", status, reason="test", now=1_010)
    candidate = {
        "hypothesis_id": "h-1", "artifact_path": artifact["path"],
        "artifact_sha256": artifact["sha256"], "evidence": [backtest["path"], review["path"]],
        "metrics": {"net_profit_pct": 1.0, "max_drawdown_pct": 2.0,
                    "profit_factor": 1.1, "trades": 30, "baseline_beaten": True},
    }
    state = store.request_promotion("loop-pilot", candidate, now=1_100)
    request_id = state["promotion_request_id"]
    with pytest.raises(PromotionBlocked, match="authenticated"):
        store.record_approval("loop-pilot", attestation="forged", request_id=request_id, approved=True, now=1_101)
    with pytest.raises(PromotionBlocked, match="boolean"):
        store.record_approval("loop-pilot", attestation="signed", request_id=request_id, approved="false", now=1_102)
    with pytest.raises(PromotionBlocked, match="request id"):
        store.record_approval("loop-pilot", attestation="signed", request_id="old", approved=True, now=1_103)
    store.record_approval("loop-pilot", attestation="signed", request_id=request_id, approved=True, now=1_104)
    store.record_approval("loop-pilot", attestation="signed", request_id=request_id, approved=False, now=1_105)
    with pytest.raises(PromotionBlocked, match="latest human decision"):
        store.promote("loop-pilot", candidate, now=1_106)


def test_artifacts_are_immutable_and_cost_inputs_fail_closed(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path, producer_verifier=_trusted_producer)
    _open(store, max_cost_budget=1.0)
    store.write_artifact(
        "loop-pilot", "proof.json", b"one", evidence_type="other",
        producer_role="researcher", producer_task_id="r-1", now=1_001,
    )
    with pytest.raises(ValueError, match="immutable"):
        store.write_artifact(
            "loop-pilot", "proof.json", b"two", evidence_type="other",
            producer_role="researcher", producer_task_id="r-1", now=1_002,
        )
    for value in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError, match="cost"):
            store.register_experiment("loop-pilot", {"cost": repr(value)}, outcome="x", cost=value, now=1_003)
    exact = store.register_experiment(
        "loop-pilot", {"id": "exact"}, outcome="x", cost=1.0, now=1_004
    )
    assert exact["cost"] == 1.0
    assert store.load("loop-pilot")["status"] == "open"


def test_cost_over_limit_is_blocked_and_stop_is_persisted(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path)
    _open(store, max_cost_budget=1.0)
    with pytest.raises(CircuitBreakerOpen, match="reaches cost budget"):
        store.register_experiment(
            "loop-pilot", {"id": "over"}, outcome="x", cost=1.01, now=1_004
        )
    state = store.load("loop-pilot")
    assert state["status"] == "stopped"
    assert state["stop_reason"] == "max_cost_budget_reached"


def test_artifact_directory_symlink_escape_is_rejected(tmp_path: Path):
    store = TradingResearchLoopStore(tmp_path, producer_verifier=_trusted_producer)
    _open(store)
    artifacts = tmp_path / "loop-pilot" / "artifacts"
    artifacts.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        artifacts.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(SafetyViolation, match="artifact directory"):
        store.write_artifact(
            "loop-pilot", "escape.json", b"escape", evidence_type="other",
            producer_role="researcher", producer_task_id="r-1", now=1_001,
        )
    assert not (outside / "escape.json").exists()


def test_decomposition_blocks_unsafe_text_and_wrong_board_connection(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    wrong = kb.connect(tmp_path / "wrong.db")
    try:
        with pytest.raises(ValueError, match="official board"):
            decompose_hypothesis(wrong, loop_id="loop-pilot", hypothesis={"id": "h-1", "claim": "EMA"})
    finally:
        wrong.close()
    conn = kb.connect(kb.kanban_db_path("trading-research"))
    try:
        with pytest.raises(SafetyViolation):
            decompose_hypothesis(
                conn, loop_id="loop-pilot",
                hypothesis={"id": "h-live", "claim": "submit market order on BTC live"},
            )
    finally:
        conn.close()
