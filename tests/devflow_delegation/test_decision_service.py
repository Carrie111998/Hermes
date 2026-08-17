"""Tests for the reusable authoritative DDP decision service."""

from __future__ import annotations

from dataclasses import replace

import pytest

from devflow_delegation.contract import parse_request
from devflow_delegation.decision_service import (
    DdpDecisionExpired,
    DdpDecisionService,
    DdpDecisionTelemetryError,
    DdpDecisionUnauthorized,
)
from devflow_delegation.ledger import DelegationLedger
from devflow_delegation.lifecycle import transition


def _request():
    return parse_request(
        {
            "schema_version": "3.0",
            "type": "DEVFLOW_WORK_REQUEST",
            "idempotency_key": "test:decision-service:v1",
            "source": {"agent": "tester", "kind": "explicit", "finding_id": "DS-1"},
            "kind": "feature",
            "title": "Decision service fixture",
            "problem_statement": "Exercise shared authoritative decisions.",
            "evidence": [{"kind": "manual", "ref": "test", "summary": "fixture"}],
            "target": {"repo": "hermes", "subsystem": "test"},
            "severity": "medium",
            "priority": "P2",
            "confidence": 0.9,
            "acceptance_criteria": ["n/a"],
            "safety_notes": [],
        }
    )


def _triaged(ledger: DelegationLedger) -> str:
    request = _request()
    ledger.insert_request(request)
    transition(ledger, None, request.request_id, "TRIAGED", actor="triage")
    return request.request_id


def test_stage_requires_bound_actor_rationale_and_triaged_request(tmp_path) -> None:
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _triaged(ledger)
    service = DdpDecisionService(ledger=ledger, bus=None)

    with pytest.raises(DdpDecisionUnauthorized, match="actor"):
        service.stage(request_id=request_id, decision="approve", actor="", rationale="reviewed")
    with pytest.raises(DdpDecisionUnauthorized, match="rationale"):
        service.stage(request_id=request_id, decision="approve", actor="operator", rationale="")
    with pytest.raises(DdpDecisionUnauthorized, match="decision"):
        service.stage(request_id=request_id, decision="merge", actor="operator", rationale="reviewed")
    with pytest.raises(DdpDecisionUnauthorized, match="unknown"):
        service.stage(request_id="missing", decision="approve", actor="operator", rationale="reviewed")

    assert ledger.get_request(request_id)["state"] == "TRIAGED"
    assert ledger.human_decision_for_request(request_id) is None


def test_stage_returns_immutable_actor_bound_300_second_confirmation(tmp_path) -> None:
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _triaged(ledger)
    now = [10.0]
    service = DdpDecisionService(ledger=ledger, bus=None, monotonic=lambda: now[0])

    staged = service.stage(
        request_id=request_id,
        decision="approve",
        actor="operator-a",
        rationale="acceptance evidence reviewed",
    )

    assert staged.request_id == request_id
    assert staged.decision == "approve"
    assert staged.target_state == "PLANNED"
    assert staged.expires_at_monotonic == 310.0
    assert request_id in staged.immutable_summary
    assert "Decision service fixture" in staged.immutable_summary
    assert staged.confirmation_token
    with pytest.raises((AttributeError, TypeError)):
        staged.decision = "decline"  # type: ignore[misc]
    assert ledger.get_request(request_id)["state"] == "TRIAGED"


def test_confirm_is_one_time_atomic_and_emits_after_commit(tmp_path) -> None:
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _triaged(ledger)

    class Bus:
        states: list[str] = []

        def emit(self, **_kwargs) -> None:
            self.states.append(ledger.get_request(request_id)["state"])

    bus = Bus()
    service = DdpDecisionService(ledger=ledger, bus=bus)
    staged = service.stage(
        request_id=request_id,
        decision="approve",
        actor="operator-a",
        rationale="reviewed",
    )

    assert service.confirm(staged=staged, actor="operator-a") == "committed"
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for_request(request_id)["decision"] == "approve"
    assert [row["to_state"] for row in ledger.transitions_for(request_id)] == [
        "TRIAGED",
        "PLANNED",
    ]
    assert bus.states == ["PLANNED"]
    assert service.confirm(staged=staged, actor="operator-a") == "already_decided"
    assert len(ledger.transitions_for(request_id)) == 2


def test_confirm_fails_closed_for_actor_decision_token_expiry_and_restart(tmp_path) -> None:
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _triaged(ledger)
    now = [10.0]
    service = DdpDecisionService(ledger=ledger, bus=None, monotonic=lambda: now[0])
    staged = service.stage(
        request_id=request_id,
        decision="decline",
        actor="operator-a",
        rationale="not ready",
    )

    with pytest.raises(DdpDecisionUnauthorized, match="actor"):
        service.confirm(staged=staged, actor="operator-b")
    with pytest.raises(DdpDecisionUnauthorized, match="token"):
        service.confirm(staged=replace(staged, confirmation_token="wrong"), actor="operator-a")
    with pytest.raises(DdpDecisionUnauthorized, match="decision"):
        service.confirm(
            staged=replace(staged, decision="approve", target_state="PLANNED"),
            actor="operator-a",
        )
    with pytest.raises(DdpDecisionUnauthorized, match="unknown"):
        DdpDecisionService(ledger=ledger, bus=None).confirm(staged=staged, actor="operator-a")

    now[0] = 311.0
    with pytest.raises(DdpDecisionExpired, match="expired"):
        service.confirm(staged=staged, actor="operator-a")
    assert ledger.get_request(request_id)["state"] == "TRIAGED"
    assert ledger.human_decision_for_request(request_id) is None


def test_request_wide_winner_and_expected_state_guard_are_authoritative(tmp_path) -> None:
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _triaged(ledger)
    approve_service = DdpDecisionService(ledger=ledger, bus=None)
    decline_service = DdpDecisionService(ledger=ledger, bus=None)
    approve = approve_service.stage(
        request_id=request_id,
        decision="approve",
        actor="operator-a",
        rationale="ready",
    )
    decline = decline_service.stage(
        request_id=request_id,
        decision="decline",
        actor="operator-b",
        rationale="not ready",
    )

    assert approve_service.confirm(staged=approve, actor="operator-a") == "committed"
    assert decline_service.confirm(staged=decline, actor="operator-b") == "already_decided"
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for_request(request_id)["decision"] == "approve"

    ledger2 = DelegationLedger(tmp_path / "race.db")
    raced_request = _triaged(ledger2)
    raced_service = DdpDecisionService(ledger=ledger2, bus=None)
    raced = raced_service.stage(
        request_id=raced_request,
        decision="approve",
        actor="operator-a",
        rationale="ready",
    )
    assert ledger2.record_human_decision(
        raced_request,
        "other",
        "decline",
        "concurrent decision",
        "other-token",
    )
    transition(ledger2, None, raced_request, "DECLINED", actor="other")

    assert raced_service.confirm(staged=raced, actor="operator-a") == "already_decided"
    assert ledger2.get_request(raced_request)["state"] == "DECLINED"


def test_telemetry_failure_keeps_durable_decision_and_consumes_token(tmp_path) -> None:
    class RaisingBus:
        def emit(self, **_kwargs) -> None:
            raise RuntimeError("telemetry unavailable")

    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _triaged(ledger)
    service = DdpDecisionService(ledger=ledger, bus=RaisingBus())
    staged = service.stage(
        request_id=request_id,
        decision="approve",
        actor="operator-a",
        rationale="ready",
    )

    with pytest.raises(DdpDecisionTelemetryError, match="committed"):
        service.confirm(staged=staged, actor="operator-a")
    assert ledger.get_request(request_id)["state"] == "PLANNED"
    assert ledger.human_decision_for_request(request_id)["decision"] == "approve"
    assert service.confirm(staged=staged, actor="operator-a") == "already_decided"


def test_confirmation_never_invokes_executor(tmp_path, monkeypatch) -> None:
    ledger = DelegationLedger(tmp_path / "ledger.db")
    request_id = _triaged(ledger)
    service = DdpDecisionService(ledger=ledger, bus=None)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("decision service must not invoke executor")

    monkeypatch.setattr("devflow_delegation.executor.run_executor_tick", forbidden)
    staged = service.stage(
        request_id=request_id,
        decision="approve",
        actor="operator-a",
        rationale="ready",
    )
    assert service.confirm(staged=staged, actor="operator-a") == "committed"
