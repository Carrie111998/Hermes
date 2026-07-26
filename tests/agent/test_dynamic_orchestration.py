from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.dynamic_orchestration import (
    AttemptState,
    CandidateEvaluation,
    CredentialState,
    DecisionRelation,
    DomainValidationError,
    ErrorKind,
    ReservationState,
    ReviewState,
    RouteState,
    RouteV1,
    RuntimeErrorClassificationV1,
    TaskEnvelope,
    TaskState,
    replan_after_capacity_exhaustion,
)

FIXTURES = Path(__file__).parents[2] / "specs" / "001-dynamic-orchestration" / "fixtures" / "route-v1"


def route(**overrides: object) -> RouteV1:
    values: dict[str, object] = {
        "provider": "OpenAI", "product": "ChatGPT", "surface": "API", "account_id": "café",
        "billing_pool_id": "team-a", "quota_pool_id": "team-a", "model": "GPT-5",
        "endpoint": "HTTPS://API.EXAMPLE.COM:443/v1/", "region": "US",
    }
    values.update(overrides)
    return RouteV1.from_mapping(values)


def classification(attempted: RouteV1, quota_pool_id: str | None = None) -> RuntimeErrorClassificationV1:
    return RuntimeErrorClassificationV1(
        ErrorKind.CAPACITY_EXHAUSTED, attempted.route_id, quota_pool_id or attempted.quota_pool_id
    )


def test_route_fixtures_are_canonical_and_pool_isolation_is_stable():
    for filename in ("equivalent-unicode-case-url.json", "absent-null.json"):
        fixture = json.loads((FIXTURES / filename).read_text())
        routes = [RouteV1.from_mapping(item) for item in fixture["inputs"]]
        assert {item.route_id for item in routes} == {fixture["expected_route_id"]}
    for filename in ("different-inputs.json", "distinct-pools.json"):
        fixture = json.loads((FIXTURES / filename).read_text())
        assert [RouteV1.from_mapping(item).route_id for item in fixture["inputs"]] == fixture["expected_route_ids"]


def test_route_rejects_missing_identity_and_query_endpoint():
    with pytest.raises(DomainValidationError, match="route.identity_required"):
        route(model="")
    with pytest.raises(DomainValidationError, match="route.endpoint_invalid"):
        route(endpoint="https://api.example.com/v1?token=x")


def test_task_rejects_unaudited_model_identity_but_is_not_model_bound():
    base = {
        "task_id": "task-1", "objective": "contract", "deliverables": ["module"],
        "required_capabilities": ["filesystem.write"], "allowed_tools": ["patch"],
        "permissions": ["repository.write"], "context_limit": 1000, "privacy_classification": "internal",
        "risk_level": "medium", "effort": "E2", "budget": "subscription_only",
        "verification_level": "V2", "policy_version": "policy/v1",
    }
    with pytest.raises(DomainValidationError, match="task.unaudited_model_identity"):
        TaskEnvelope.from_mapping({**base, "model": "gpt"})
    task = TaskEnvelope.from_mapping(base)
    assert task.task_id == "task-1"
    assert not hasattr(task, "model")


def test_state_domains_are_distinct_even_when_values_overlap():
    assert type(TaskState.NEW) is not type(AttemptState.CREATED)
    assert type(RouteState.COOLDOWN) is not type(CredentialState.COOLDOWN)
    assert type(ReservationState.HELD) is not type(ReviewState.PENDING)


def test_capacity_exhaustion_replans_to_eligible_alternate_not_waiting():
    opus = route(provider="anthropic", product="claude", model="opus", quota_pool_id="anthropic-a")
    gpt = route(quota_pool_id="openai-a")
    decision = replan_after_capacity_exhaustion(
        task_id="task-1", attempt_id="attempt-1", decision_id="decision-2", failed_route=opus,
        classification=classification(opus),
        candidates=(CandidateEvaluation(opus, False, ("route_capacity_exhausted",)), CandidateEvaluation(gpt, True)),
        quality_compensation=("independent_review", "deterministic_tests"),
    )
    assert decision.relation is DecisionRelation.FALLBACK
    assert decision.selected_route_id == gpt.route_id
    assert "route_capacity_exhausted" in decision.reason_codes


def test_capacity_exhaustion_does_not_fallback_within_same_quota_pool():
    failed = route(model="gpt-a", quota_pool_id="shared-pool")
    same_pool = route(model="gpt-b", quota_pool_id="shared-pool")
    decision = replan_after_capacity_exhaustion(
        task_id="task-1", attempt_id="attempt-1", decision_id="decision-2", failed_route=failed,
        classification=classification(failed), candidates=(CandidateEvaluation(same_pool, True),),
        recheck_evidence=("shared quota pool exhausted",),
    )
    assert decision.relation is DecisionRelation.WAITING


def test_wait_requires_evidence_when_no_alternate_is_eligible():
    attempted = route()
    with pytest.raises(DomainValidationError, match="replan.recheck_evidence_required"):
        replan_after_capacity_exhaustion(task_id="task-1", attempt_id="attempt-1", decision_id="decision-2", failed_route=attempted, classification=classification(attempted), candidates=(CandidateEvaluation(attempted, False),))
    decision = replan_after_capacity_exhaustion(
        task_id="task-1", attempt_id="attempt-1", decision_id="decision-2", failed_route=attempted,
        classification=classification(attempted), candidates=(CandidateEvaluation(attempted, False),),
        recheck_evidence=("all routes rejected at decision time",),
    )
    assert decision.relation is DecisionRelation.WAITING


def test_unscoped_or_mismatched_capacity_classification_is_rejected():
    attempted = route()
    with pytest.raises(DomainValidationError, match="classification.capacity_scope_required"):
        RuntimeErrorClassificationV1(ErrorKind.CAPACITY_EXHAUSTED, "", attempted.quota_pool_id)
    with pytest.raises(DomainValidationError, match="classification.capacity_scope_required"):
        replan_after_capacity_exhaustion(task_id="task-1", attempt_id="attempt-1", decision_id="decision-2", failed_route=attempted, classification=classification(attempted, "other-pool"), candidates=())


def test_sensitive_task_and_decision_content_is_rejected():
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        TaskEnvelope.from_mapping({"prompt": "private"})
    attempted = route()
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        CandidateEvaluation(attempted, False, ("Bearer secret",))
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        replan_after_capacity_exhaustion(
            task_id="task-1", attempt_id="attempt-1", decision_id="decision-2", failed_route=attempted,
            classification=classification(attempted), candidates=(), recheck_evidence=("Bearer abc",),
        )
