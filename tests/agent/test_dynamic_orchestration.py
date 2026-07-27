from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from typing import Any, Callable

import pytest

from agent.dynamic_orchestration import (
    AcceptanceThresholdV1,
    AttemptState,
    AttemptStateEvent,
    AuditedModelJustification,
    CandidateEvaluation,
    CandidateScoreV1,
    CompensationEscalationV1,
    CredentialState,
    CredentialStateEvent,
    DecisionRelation,
    DomainValidationError,
    ErrorKind,
    EligibilityDisposition,
    EscalationAction,
    IndependentReviewAttestationV1,
    InitialSelectionTriggerV1,
    QualityCompensationPlanV1,
    ReservationState,
    ReservationStateEvent,
    ReviewState,
    ReviewStateEvent,
    RouteState,
    RouteStateEvent,
    RouteDecisionV1,
    RouteEligibilityFactsV1,
    RouteV1,
    RuntimeErrorClassificationV1,
    TaskEnvelope,
    TaskState,
    TaskStateEvent,
    validate_attempt_transition,
    validate_credential_transition,
    validate_reservation_transition,
    validate_review_transition,
    validate_route_transition,
    validate_task_transition,
    evaluate_route_eligibility,
    replan_after_capacity_exhaustion,
    score_eligible_candidates,
)

FIXTURES = Path(__file__).parents[2] / "specs" / "001-dynamic-orchestration" / "fixtures" / "route-v1"
UNSELECTED_ROUTE_ID = "route-v1:" + "f" * 64


def route(**overrides: object) -> RouteV1:
    values: dict[str, object] = {
        "provider": "OpenAI",
        "product": "ChatGPT",
        "surface": "API",
        "account_id": "café",
        "billing_pool_id": "team-a",
        "quota_pool_id": "team-a",
        "model": "GPT-5",
        "endpoint": "HTTPS://API.EXAMPLE.COM:443/v1/",
        "region": "US",
    }
    values.update(overrides)
    return RouteV1.from_mapping(values)


def scored_candidate(candidate_route: RouteV1, score: float = 1.0) -> CandidateEvaluation:
    return CandidateEvaluation(
        candidate_route,
        True,
        score=score,
        score_factors=("quality",),
    )


INDEPENDENT_REVIEW_ROUTE = route(
    provider="Independent",
    product="Review",
    account_id="reviewer-account",
    billing_pool_id="review-billing-pool",
    quota_pool_id="review-quota-pool",
    model="review-model",
    endpoint="https://review.example.com/v1",
)
INDEPENDENT_REVIEW_ROUTE_ID = INDEPENDENT_REVIEW_ROUTE.route_id


def classification(
    attempted: RouteV1,
    quota_pool_id: str | None = None,
    billing_pool_id: str | None = None,
) -> RuntimeErrorClassificationV1:
    return RuntimeErrorClassificationV1(
        kind=ErrorKind.CAPACITY_EXHAUSTED,
        source="typed-runtime-error",
        attempted_route_id=attempted.route_id,
        quota_pool_id=quota_pool_id or attempted.quota_pool_id,
        billing_pool_id=billing_pool_id,
        classified_at="2026-07-26T17:00:00Z",
    )


def decision_metadata() -> dict[str, object]:
    return {
        "created_at": "2026-07-26T17:00:01Z",
        "policy_version": "policy/v1",
        "router_version": "router/pure-v1",
        "capacity_view_id": "capacity-view:test",
        "effort": "E2",
        "verification": "V2",
    }


def replan(**kwargs: Any):
    verification = str(kwargs.pop("task_verification_minimum", "V0"))
    kwargs.setdefault(
        "trusted_reviewer_routes",
        {INDEPENDENT_REVIEW_ROUTE.route_id: INDEPENDENT_REVIEW_ROUTE},
    )
    kwargs.setdefault(
        "trusted_execution_routes",
        {"review-execution": INDEPENDENT_REVIEW_ROUTE},
    )
    kwargs.setdefault(
        "trusted_execution_evidence",
        {"review-execution": ("evidence:test-run",)},
    )
    kwargs.setdefault("trusted_evidence_refs", ("evidence:test-run",))
    kwargs.setdefault("trusted_threshold_results", {"evidence:test-run": True})
    kwargs.setdefault("parent_decision_id", f"parent-{kwargs.get('decision_id', 'decision')}")
    metadata = decision_metadata()
    metadata["verification"] = verification
    task_id = str(kwargs.get("task_id", "task-1"))
    task_policy_version = str(metadata["policy_version"])
    task_effort = str(metadata["effort"])
    kwargs.setdefault(
        "trusted_task",
        TaskEnvelope.from_mapping(
            task_payload(
                task_id=task_id,
                policy_version=task_policy_version,
                effort=task_effort,
                verification={
                    "minimum": verification,
                    "independent_required": False,
                    "human_gate_required": False,
                },
            )
        ),
    )
    return replan_after_capacity_exhaustion(**metadata, **kwargs)


def task_payload(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "schema_version": "task-envelope/v1",
        "task_id": "task-1",
        "objective": "contract",
        "deliverables": ["module"],
        "capabilities_required": ["filesystem.write"],
        "tools_allowed": ["patch"],
        "permissions_required": ["repository.write"],
        "context": {
            "classification": "internal",
            "max_tokens": 900,
            "token_count": 12,
            "allowed_sources": ["repository"],
        },
        "privacy": {
            "classification": "internal",
            "outbound_allowed": False,
            "retention": "ephemeral",
        },
        "risk": {
            "level": "medium",
            "reversibility": "reversible",
            "impact": "repository-only",
        },
        "effort": "E2",
        "budget": {
            "currency": "USD",
            "paid_allowed": False,
            "soft_cap": 0,
            "hard_cap": 0,
        },
        "verification": {
            "minimum": "V2",
            "independent_required": True,
            "human_gate_required": False,
        },
        "policy_version": "policy/v1",
    }
    values.update(overrides)
    return values


def valid_plan(
    decision_id: str,
    prior_route: RouteV1 | str,
    selected_route: RouteV1 | str,
    **overrides: object,
) -> QualityCompensationPlanV1:
    reviewed_execution_id = str(overrides.pop("reviewed_execution_id", "attempt-1"))
    prior_route_id = prior_route.route_id if type(prior_route) is RouteV1 else prior_route
    selected_route_id = selected_route.route_id if type(selected_route) is RouteV1 else selected_route
    values: dict[str, object] = {
        "plan_id": "plan-1",
        "decision_id": decision_id,
        "prior_route_id": prior_route_id,
        "selected_route_id": selected_route_id,
        "prior_quota_pool_id": (
            prior_route.quota_pool_id if type(prior_route) is RouteV1 else "unresolved-prior-quota"
        ),
        "prior_billing_pool_id": (
            prior_route.billing_pool_id if type(prior_route) is RouteV1 else "unresolved-prior-billing"
        ),
        "selected_quota_pool_id": (
            selected_route.quota_pool_id
            if type(selected_route) is RouteV1
            else "unresolved-selected-quota"
        ),
        "selected_billing_pool_id": (
            selected_route.billing_pool_id
            if type(selected_route) is RouteV1
            else "unresolved-selected-billing"
        ),
        "trigger_kind": "capacity_exhausted",
        "quality_delta_codes": ("different_model_family",),
        "required_verification": "V2",
        "independence_required": True,
        "required_reviewers": ("review-route-independent",),
        "review_attestations": (
            IndependentReviewAttestationV1(
                reviewer="review-route-independent",
                route_id=INDEPENDENT_REVIEW_ROUTE_ID,
                quota_pool_id="review-quota-pool",
                billing_pool_id="review-billing-pool",
                reviewed_execution_id=reviewed_execution_id,
                execution_id="review-execution",
                evidence_ref="evidence:test-run",
            ),
        ),
        "acceptance_thresholds": (
            AcceptanceThresholdV1(
                metric="focused_tests",
                operator=">=",
                value=1,
                unit="suite",
                evidence_required=True,
                evidence_ref="evidence:test-run",
                met=True,
            ),
        ),
        "escalation": CompensationEscalationV1(
            on_unmet=EscalationAction.BLOCK_DISPATCH,
            owner="policy-owner",
        ),
        "evidence_refs": ("evidence:test-run",),
        "created_at": "2026-07-26T17:00:00Z",
        "policy_version": "policy/v1",
    }
    values.update(overrides)
    return QualityCompensationPlanV1(**values)


def audit(identity_field: str) -> dict[str, str]:
    return {
        identity_field: "entity-1",
        "actor": "policy-engine",
        "timestamp": "2026-07-26T17:00:00Z",
        "reason": "contract transition",
        "correlation_id": "corr-1",
    }


@pytest.mark.parametrize("fixture_path", sorted(FIXTURES.glob("*.json")), ids=lambda path: path.name)
def test_every_route_fixture_declares_and_matches_canonical_expectations(fixture_path: Path):
    fixture = json.loads(fixture_path.read_text())
    routes = [RouteV1.from_mapping(item) for item in fixture["inputs"]]

    assert fixture["fixture_version"] == "route-v1-fixture/1"
    if "expected_canonical_json" in fixture:
        assert {item.canonical_json for item in routes} == {fixture["expected_canonical_json"]}
    if "expected_route_id" in fixture:
        assert {item.route_id for item in routes} == {fixture["expected_route_id"]}
    else:
        assert [item.route_id for item in routes] == fixture["expected_route_ids"]


def test_endpoint_path_case_is_identity_significant():
    upper = route(endpoint="https://API.EXAMPLE.COM:443/V1/Responses")
    lower = route(endpoint="https://api.example.com/v1/responses")

    assert upper.endpoint == "https://api.example.com/V1/Responses"
    assert lower.endpoint == "https://api.example.com/v1/responses"
    assert upper.route_id != lower.route_id


def test_endpoint_percent_equivalents_share_identity_and_reserved_escape_is_uppercase():
    escaped = route(endpoint="https://api.example.com/v1/%7eresponses/%2f")
    literal = route(endpoint="https://api.example.com/v1/~responses/%2F")

    assert escaped.endpoint == "https://api.example.com/v1/~responses/%2F"
    assert escaped.route_id == literal.route_id


def test_endpoint_duplicate_slashes_are_identity_significant():
    duplicate = route(endpoint="https://api.example.com/a//b")
    single = route(endpoint="https://api.example.com/a/b")
    with_parent_segment = route(endpoint="https://api.example.com/a//../b")

    assert duplicate.endpoint == "https://api.example.com/a//b"
    assert duplicate.route_id != single.route_id
    assert with_parent_segment.endpoint == "https://api.example.com/a/b"


def test_endpoint_equivalent_idna_hosts_share_identity():
    unicode_host = route(endpoint="https://café.example/v1")
    ascii_host = route(endpoint="https://xn--caf-dma.example/v1")

    assert unicode_host.endpoint == "https://xn--caf-dma.example/v1"
    assert unicode_host.route_id == ascii_host.route_id


def test_endpoint_equivalent_ipv6_spellings_share_identity():
    expanded = route(endpoint="https://[2001:0db8:0000:0000:0000:0000:0000:0001]/v1")
    compressed = route(endpoint="https://[2001:db8::1]/v1")

    assert expanded.endpoint == "https://[2001:db8::1]/v1"
    assert expanded.route_id == compressed.route_id


def test_route_rejects_unknown_canonicalization_version():
    payload = json.loads(route().canonical_json)
    payload["canonicalization_version"] = "route-v2"
    with pytest.raises(DomainValidationError, match="route.canonicalization_unknown"):
        RouteV1.from_mapping(payload)


def test_route_rejects_missing_identity_query_fragment_and_malformed_percent():
    with pytest.raises(DomainValidationError, match="route.identity_required"):
        route(model="")
    for endpoint in (
        "https://api.example.com/v1?x=1",
        "https://api.example.com/v1#frag",
        "https://api.example.com/v1/%GG",
        "https://example.com/a b",
        "https://exa%mple.com/",
        "https://example.com/\\evil",
        "https://example.com/\x01x",
    ):
        with pytest.raises(DomainValidationError, match="route.endpoint_invalid"):
            route(endpoint=endpoint)


def test_route_identity_rejects_unicode_surrogates_with_stable_domain_error():
    with pytest.raises(DomainValidationError, match="route.value_invalid"):
        route(provider="openai\ud800")


def test_route_missing_field_error_is_hash_seed_invariant():
    script = (
        "from agent.dynamic_orchestration import RouteV1, DomainValidationError\n"
        "try:\n"
        "    RouteV1.from_mapping({})\n"
        "except DomainValidationError as exc:\n"
        "    print(str(exc))\n"
    )
    outputs = []
    for seed in ("0", "1", "2", "99"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).parents[2],
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(completed.stdout.strip())
    assert outputs == ["route.identity_required: provider is required"] * 4


def test_non_string_mapping_keys_fail_with_stable_domain_errors():
    route_mapping: dict[object, object] = {}
    route_mapping.update(json.loads(route().canonical_json))
    route_mapping[7] = "unexpected"
    with pytest.raises(DomainValidationError, match="route.unexpected_field"):
        RouteV1.from_mapping(route_mapping)  # type: ignore[arg-type]

    task_mapping: dict[object, object] = {}
    task_mapping.update(task_payload())
    task_mapping[7] = "unexpected"
    with pytest.raises(DomainValidationError, match="task.unexpected_field"):
        TaskEnvelope.from_mapping(task_mapping)  # type: ignore[arg-type]

    failed = route(model="failed-key-test", quota_pool_id="failed-key-q")
    selected = route(model="selected-key-test", quota_pool_id="selected-key-q")
    plan_mapping: dict[object, object] = {}
    plan_mapping.update(asdict(valid_plan("decision-key-test", failed, selected)))
    plan_mapping[7] = "unexpected"
    with pytest.raises(DomainValidationError, match="quality.schema_invalid"):
        QualityCompensationPlanV1.from_mapping(plan_mapping)  # type: ignore[arg-type]


def test_task_is_closed_validated_and_deeply_immutable():
    justification = {
        "policy_version": "policy/v1",
        "reason": "approved exception",
        "evidence_refs": ["evidence:approval"],
        "author": "policy-owner",
        "expires_at": "2099-08-01T00:00:00Z",
    }
    source = task_payload(audited_model_justification=justification)
    deliverables = source["deliverables"]
    task = TaskEnvelope.from_mapping(source)

    assert task.deliverables == ("module",)
    assert task.capabilities_required == ("filesystem.write",)
    assert task.tools_allowed == ("patch",)
    assert task.permissions_required == ("repository.write",)
    assert task.context.max_tokens == 900
    assert task.context.token_count == 12
    assert task.schema_version == "task-envelope/v1"
    assert task.privacy.outbound_allowed is False
    assert task.risk.reversibility == "reversible"
    assert task.budget.hard_cap == 0
    assert task.verification.minimum == "V2"
    assert task.audited_model_justification is not None
    assert task.audited_model_justification.evidence_refs == ("evidence:approval",)
    assert isinstance(deliverables, list)
    deliverables.append("mutated")
    justification_evidence = justification["evidence_refs"]
    assert isinstance(justification_evidence, list)
    justification_evidence.append("mutated")
    assert task.deliverables == ("module",)
    assert task.audited_model_justification.evidence_refs == ("evidence:approval",)
    with pytest.raises(FrozenInstanceError):
        task.task_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("override", "code"),
    [
        (
            {
                "context": {
                    "classification": "internal",
                    "max_tokens": 0,
                    "token_count": 0,
                    "allowed_sources": [],
                }
            },
            "task.context_bounds_invalid",
        ),
        (
            {
                "context": {
                    "classification": "internal",
                    "max_tokens": 10,
                    "token_count": 11,
                    "allowed_sources": [],
                }
            },
            "task.context_bounds_invalid",
        ),
        ({"deliverables": ["ok", 1]}, "task.collection_invalid"),
        ({"unexpected": "value"}, "task.unexpected_field"),
    ],
)
def test_task_rejects_invalid_scalars_bounds_collections_and_unknown_fields(
    override: dict[str, object], code: str
):
    with pytest.raises(DomainValidationError, match=code):
        TaskEnvelope.from_mapping(task_payload(**override))


def test_task_rejects_unaudited_model_identity_but_valid_token_fields_are_not_sensitive():
    with pytest.raises(DomainValidationError, match="task.unaudited_model_identity"):
        TaskEnvelope.from_mapping(task_payload(model="gpt"))

    task = TaskEnvelope.from_mapping(task_payload(objective="verify token_count and max_tokens bounds"))
    assert task.task_id == "task-1"
    assert not hasattr(task, "model")


def test_task_unknown_policy_identifiers_and_secret_bearing_objective_fail_closed():
    for field_name in (
        "capabilities_required",
        "tools_allowed",
        "permissions_required",
    ):
        with pytest.raises(DomainValidationError, match="task.unknown_policy_identifier"):
            TaskEnvelope.from_mapping(
                task_payload(**{field_name: ["totally.unknown.policy.identifier"]})
            )
    for leaked_objective in (
        "password=hunter2",
        "token=private-token",
        "-----BEGIN PRIVATE KEY----- material",
    ):
        with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
            TaskEnvelope.from_mapping(task_payload(objective=leaked_objective))

    private_browser = TaskEnvelope.from_mapping(
        task_payload(
            capabilities_required=["browser.private"],
            tools_allowed=["computer-use"],
            permissions_required=["browser.private"],
            privacy={
                "classification": "private",
                "outbound_allowed": False,
                "retention": "ephemeral",
            },
            budget={
                "currency": "USD",
                "paid_allowed": True,
                "soft_cap": 10,
                "hard_cap": 20,
            },
            verification={
                "minimum": "V3",
                "independent_required": True,
                "human_gate_required": False,
            },
        )
    )
    assert private_browser.capabilities_required == ("browser.private",)


def test_classification_source_is_secret_filtered():
    attempted = route()
    for source in ("password=hunter2", "token=private-token", "-----BEGIN PRIVATE KEY-----"):
        with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
            RuntimeErrorClassificationV1(
                kind=ErrorKind.CAPACITY_EXHAUSTED,
                source=source,
                attempted_route_id=attempted.route_id,
                quota_pool_id=attempted.quota_pool_id,
                classified_at="2026-07-26T17:00:00Z",
            )


def test_task_accepts_audited_identity_without_retaining_model_fields():
    payload = task_payload(
        provider="OpenAI",
        model="GPT-5",
        audited_model_justification={
            "policy_version": "policy/v1",
            "reason": "explicit migration exception",
            "evidence_refs": ["evidence:approval-7"],
            "author": "policy-owner",
            "expires_at": "2099-08-01T00:00:00Z",
            "identity_claims": [
                ["model", "GPT-5"],
                ["provider", "OpenAI"],
            ],
        },
    )

    task = TaskEnvelope.from_mapping(payload)

    assert not hasattr(task, "provider")
    assert not hasattr(task, "model")
    assert task.audited_model_justification is not None
    assert task.audited_model_justification.identity_claims == (
        ("model", "GPT-5"),
        ("provider", "OpenAI"),
    )

    for policy_version, expires_at, expected_error in (
        ("policy/attacker", "2099-08-01T00:00:00Z", "task.justification_policy_mismatch"),
        ("policy/v1", "not-a-timestamp", "task.justification_expiry_invalid"),
        ("policy/v1", "2000-01-01T00:00:00Z", "task.justification_expired"),
    ):
        invalid = dict(payload)
        justification_source = payload["audited_model_justification"]
        assert isinstance(justification_source, dict)
        invalid_justification: dict[str, object] = dict(justification_source)
        invalid_justification["policy_version"] = policy_version
        invalid_justification["expires_at"] = expires_at
        invalid["audited_model_justification"] = invalid_justification
        with pytest.raises(DomainValidationError, match=expected_error):
            TaskEnvelope.from_mapping(invalid)


def test_candidate_and_initial_decision_contract_are_round_trippable_and_fail_closed():
    selected = route()
    candidate_payload = {
        "route_id": selected.route_id,
        "deterministic_status": "ELIGIBLE",
        "rejection_codes": [],
        "score": 1.0,
        "score_factors": ["healthy_capacity"],
    }
    candidate = CandidateEvaluation.from_mapping(candidate_payload)
    assert candidate.route_id == selected.route_id
    assert candidate.eligible is True
    assert candidate.score_factors == ("healthy_capacity",)

    decision_payload: dict[str, object] = {
        "schema_version": "route-decision/v1",
        "decision_id": "decision-initial",
        "task_id": "task-1",
        "attempt_id": "attempt-initial",
        "created_at": "2026-07-26T17:00:00Z",
        "policy_version": "policy/v1",
        "router_version": "router/v1",
        "capacity_view_id": "capacity-view-1",
        "effort": "E2",
        "verification": "V2",
        "fallback": False,
        "relation": "INITIAL",
        "candidates": [candidate_payload],
        "selected_route_id": selected.route_id,
        "trigger": {
            "schema_version": "initial-selection-trigger/v1",
            "kind": "initial_selection",
            "source": "policy-router",
            "evaluated_at": "2026-07-26T17:00:00Z",
        },
        "reason_codes": ["initial_selection"],
        "policy_status": "AUTHORIZED",
        "activation_block_reason": None,
    }
    trusted_task = TaskEnvelope.from_mapping(
        task_payload(
            verification={
                "minimum": "V2",
                "independent_required": False,
                "human_gate_required": False,
            }
        )
    )
    trusted_initial_context = {
        "trusted_task": trusted_task,
        "trusted_routes": {selected.route_id: selected},
    }
    decision = RouteDecisionV1.from_mapping(
        decision_payload,
        **trusted_initial_context,
    )
    assert decision.dispatchable is True
    assert type(decision.trigger) is InitialSelectionTriggerV1
    assert decision.candidates[0].route_id == selected.route_id

    contradictory_task = TaskEnvelope.from_mapping(
        task_payload(task_id="different-task", policy_version="policy/trusted", effort="E4")
    )
    with pytest.raises(DomainValidationError, match="decision.trusted_task_context_invalid"):
        RouteDecisionV1.from_mapping(
            decision_payload,
            trusted_task=contradictory_task,
            trusted_routes={selected.route_id: selected},
        )

    arbitrary_selection = {
        **decision_payload,
        "candidates": [],
        "selected_route_id": UNSELECTED_ROUTE_ID,
    }
    with pytest.raises(DomainValidationError, match="decision.selected_candidate_ineligible"):
        RouteDecisionV1.from_mapping(arbitrary_selection, **trusted_initial_context)

    malformed_route = {
        **decision_payload,
        "candidates": [
            {
                **candidate_payload,
                "route_id": "not-a-route-v1-id",
            }
        ],
        "selected_route_id": "not-a-route-v1-id",
    }
    with pytest.raises(DomainValidationError, match="route.identity_invalid"):
        RouteDecisionV1.from_mapping(malformed_route, **trusted_initial_context)

    untyped_trigger = {**decision_payload, "trigger": "untyped"}
    with pytest.raises(DomainValidationError, match="decision.trigger_invalid"):
        RouteDecisionV1.from_mapping(untyped_trigger, **trusted_initial_context)

    missing_status = {
        key: value
        for key, value in decision_payload.items()
        if key != "policy_status"
    }
    with pytest.raises(DomainValidationError, match="decision.persisted_status_required"):
        RouteDecisionV1.from_mapping(missing_status)


def test_candidate_audit_records_are_consistent_and_finite():
    candidate_route = route()
    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation(candidate_route, False)
    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation(candidate_route, True, ("policy_denied",))
    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation(candidate_route, True, score=float("nan"))
    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation(candidate_route, True, score=float("inf"))


def test_initial_decision_rejects_unscored_eligible_candidate():
    selected = route(model="unscored-initial", quota_pool_id="unscored-initial")
    trusted_task = TaskEnvelope.from_mapping(
        task_payload(
            task_id="task-unscored-initial",
            verification={
                "minimum": "V2",
                "independent_required": False,
                "human_gate_required": False,
            },
        )
    )

    with pytest.raises(DomainValidationError, match="decision.candidate_score_required"):
        RouteDecisionV1(
            decision_id="decision-unscored-initial",
            task_id=trusted_task.task_id,
            attempt_id="attempt-unscored-initial",
            **decision_metadata(),  # type: ignore[arg-type]
            fallback=False,
            relation=DecisionRelation.INITIAL,
            candidates=(CandidateEvaluation(selected, True),),
            selected_route_id=selected.route_id,
            trigger=InitialSelectionTriggerV1(
                "initial-selection-trigger/v1",
                "initial_selection",
                "policy-router",
                "2026-07-26T17:00:00Z",
            ),
            reason_codes=("initial_selection",),
            trusted_task=trusted_task,
            trusted_routes={selected.route_id: selected},
        )


def test_capacity_replan_rejects_unscored_eligible_candidate():
    failed = route(model="failed-unscored", quota_pool_id="failed-unscored")
    alternate = route(model="alternate-unscored", quota_pool_id="alternate-unscored")

    with pytest.raises(DomainValidationError, match="replan.candidate_score_required"):
        replan(
            task_id="task-unscored-replan",
            attempt_id="attempt-unscored-replan",
            decision_id="decision-unscored-replan",
            failed_route=failed,
            classification=classification(failed),
            candidates=(CandidateEvaluation(alternate, True),),
            task_verification_minimum="V2",
        )


ELIGIBILITY_GATES = (
    "identity_policy",
    "privacy_permission",
    "capability_tool",
    "context",
    "freshness_confidence",
    "budget",
    "breaker_cooldown",
    "concurrency_reservation",
)


def eligibility_facts(candidate_route: RouteV1, **overrides: object) -> RouteEligibilityFactsV1:
    values: dict[str, object] = {
        gate: EligibilityDisposition.PASS for gate in ELIGIBILITY_GATES
    }
    values.update(overrides)
    return RouteEligibilityFactsV1(route=candidate_route, **values)  # type: ignore[arg-type]


@pytest.mark.parametrize("gate", ELIGIBILITY_GATES)
@pytest.mark.parametrize(
    ("disposition", "suffix"),
    (
        (EligibilityDisposition.REJECT, "rejected"),
        (EligibilityDisposition.UNKNOWN, "unknown"),
    ),
)
def test_eligibility_emits_stable_fail_closed_code_for_every_gate(
    gate: str,
    disposition: EligibilityDisposition,
    suffix: str,
):
    candidate_route = route(model=f"{gate}-{suffix}", quota_pool_id=f"{gate}-{suffix}")

    (candidate,) = evaluate_route_eligibility(
        (eligibility_facts(candidate_route, **{gate: disposition}),)
    )

    assert candidate.route == candidate_route
    assert candidate.deterministic_status == "REJECTED"
    assert candidate.rejection_codes == (f"{gate}_{suffix}",)
    assert candidate.score is None
    assert candidate.score_factors == ()


def test_eligibility_records_multiple_failures_in_canonical_gate_order_and_unknown_fails_closed():
    candidate_route = route(model="multi-failure", quota_pool_id="multi-failure")
    facts = eligibility_facts(
        candidate_route,
        concurrency_reservation=EligibilityDisposition.REJECT,
        identity_policy=EligibilityDisposition.UNKNOWN,
        context=EligibilityDisposition.REJECT,
        budget=EligibilityDisposition.UNKNOWN,
    )

    (candidate,) = evaluate_route_eligibility((facts,))

    assert candidate.rejection_codes == (
        "identity_policy_unknown",
        "context_rejected",
        "budget_unknown",
        "concurrency_reservation_rejected",
    )
    assert candidate.eligible is False


def test_eligibility_is_route_sorted_permutation_invariant_and_isolated_from_mutable_input():
    routes = (
        route(model="permutation-a", quota_pool_id="permutation-a"),
        route(model="permutation-b", quota_pool_id="permutation-b"),
        route(model="permutation-c", quota_pool_id="permutation-c"),
    )
    source = [eligibility_facts(candidate_route) for candidate_route in routes]
    expected = evaluate_route_eligibility(source)

    for permutation in permutations(source):
        assert evaluate_route_eligibility(permutation) == expected
    assert tuple(candidate.route_id for candidate in expected) == tuple(
        sorted(candidate_route.route_id for candidate_route in routes)
    )

    source.reverse()
    assert evaluate_route_eligibility(source) == expected
    with pytest.raises(FrozenInstanceError):
        source[0].budget = EligibilityDisposition.REJECT  # type: ignore[misc]


def test_eligibility_rejects_duplicate_route_ids_and_untyped_dispositions():
    candidate_route = route(model="duplicate-facts", quota_pool_id="duplicate-facts")
    facts = eligibility_facts(candidate_route)
    with pytest.raises(DomainValidationError, match="eligibility.duplicate_route"):
        evaluate_route_eligibility((facts, facts))
    with pytest.raises(DomainValidationError, match="eligibility.invalid"):
        RouteEligibilityFactsV1(
            route=candidate_route,
            identity_policy="PASS",  # type: ignore[arg-type]
            privacy_permission=EligibilityDisposition.PASS,
            capability_tool=EligibilityDisposition.PASS,
            context=EligibilityDisposition.PASS,
            freshness_confidence=EligibilityDisposition.PASS,
            budget=EligibilityDisposition.PASS,
            breaker_cooldown=EligibilityDisposition.PASS,
            concurrency_reservation=EligibilityDisposition.PASS,
        )


def test_scoring_requires_exact_validated_scores_and_never_scores_rejected_candidates():
    eligible_route = route(model="score-eligible", quota_pool_id="score-eligible")
    rejected_route = route(model="score-rejected", quota_pool_id="score-rejected")
    candidates = evaluate_route_eligibility(
        (
            eligibility_facts(rejected_route, budget=EligibilityDisposition.REJECT),
            eligibility_facts(eligible_route),
        )
    )
    factors = ["quality", "healthy_capacity"]
    score = CandidateScoreV1(score=7, score_factors=factors)

    scored = score_eligible_candidates(candidates, {eligible_route.route_id: score})
    assert score_eligible_candidates(
        tuple(reversed(candidates)),
        {eligible_route.route_id: score},
    ) == scored

    factors.append("mutated")
    assert tuple(candidate.route_id for candidate in scored) == tuple(
        sorted((eligible_route.route_id, rejected_route.route_id))
    )
    eligible = next(candidate for candidate in scored if candidate.route_id == eligible_route.route_id)
    rejected = next(candidate for candidate in scored if candidate.route_id == rejected_route.route_id)
    assert eligible.route == eligible_route
    assert eligible.score == 7.0
    assert eligible.score_factors == ("quality", "healthy_capacity")
    assert rejected.score is None
    assert rejected.score_factors == ()

    invalid_maps: tuple[dict[object, object], ...] = (
        {},
        {
            eligible_route.route_id: score,
            rejected_route.route_id: CandidateScoreV1(1, ("forbidden",)),
        },
        {rejected_route.route_id: CandidateScoreV1(1, ("forbidden",))},
        {eligible_route.route_id: 7.0},
        {7: score},
    )
    for invalid in invalid_maps:
        with pytest.raises(DomainValidationError, match="score.invalid"):
            score_eligible_candidates(candidates, invalid)  # type: ignore[arg-type]


def test_score_contract_rejects_malformed_or_nonfinite_values_and_duplicate_candidates():
    for invalid_score in (True, "7", float("nan"), float("inf"), float("-inf")):
        with pytest.raises(DomainValidationError, match="score.invalid"):
            CandidateScoreV1(invalid_score, ("quality",))  # type: ignore[arg-type]
    for invalid_factors in ((), ("",), ("quality", 1)):
        with pytest.raises(DomainValidationError, match="score.invalid"):
            CandidateScoreV1(1, invalid_factors)  # type: ignore[arg-type]

    candidate_route = route(model="duplicate-score", quota_pool_id="duplicate-score")
    candidate = evaluate_route_eligibility((eligibility_facts(candidate_route),))[0]
    with pytest.raises(DomainValidationError, match="score.duplicate_candidate"):
        score_eligible_candidates(
            (candidate, candidate),
            {candidate_route.route_id: CandidateScoreV1(1, ("quality",))},
        )


@pytest.mark.parametrize(
    "invalid_identifiers",
    (
        ("user prompt contains confidential merger terms",),
        ("AWS_SECRET_ACCESS_KEY=example-secret-value",),
        ("a" * 65,),
        tuple(f"factor_{index}" for index in range(33)),
        ("quality", "quality"),
    ),
)
def test_candidate_identifiers_reject_free_text_secrets_and_unbounded_collections(
    invalid_identifiers: tuple[str, ...],
):
    candidate_route = route(model="identifier-candidate", quota_pool_id="identifier-candidate")

    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation(candidate_route, False, invalid_identifiers)
    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation(
            candidate_route,
            True,
            score=1,
            score_factors=invalid_identifiers,
        )


@pytest.mark.parametrize(
    "invalid_factors",
    (
        ("user prompt contains confidential merger terms",),
        ("AWS_SECRET_ACCESS_KEY=example-secret-value",),
        ("a" * 65,),
        tuple(f"factor_{index}" for index in range(33)),
        ("quality", "quality"),
    ),
)
def test_candidate_score_identifiers_reject_free_text_secrets_and_unbounded_collections(
    invalid_factors: tuple[str, ...],
):
    with pytest.raises(DomainValidationError, match="score.invalid"):
        CandidateScoreV1(1, invalid_factors)


def test_huge_integer_scores_are_normalized_to_domain_errors_in_all_candidate_paths():
    candidate_route = route(model="huge-score", quota_pool_id="huge-score")
    huge_score = 10**10000

    with pytest.raises(DomainValidationError, match="score.invalid"):
        CandidateScoreV1(huge_score, ("quality",))
    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation(
            candidate_route,
            True,
            score=huge_score,
            score_factors=("quality",),
        )
    with pytest.raises(DomainValidationError, match="candidate.invalid"):
        CandidateEvaluation.from_mapping(
            {
                "route_id": candidate_route.route_id,
                "deterministic_status": "ELIGIBLE",
                "rejection_codes": (),
                "score": huge_score,
                "score_factors": ("quality",),
            },
            route_context=candidate_route,
        )


def test_eligibility_and_scoring_revalidate_forged_frozen_inputs_and_reject_sensitive_factors():
    forged_route = route(model="forged-route", quota_pool_id="forged-route")
    forged_facts = eligibility_facts(forged_route)
    object.__setattr__(forged_facts.route, "endpoint", "not-an-absolute-url")
    with pytest.raises(DomainValidationError, match="route.endpoint_invalid"):
        evaluate_route_eligibility((forged_facts,))

    eligible_route = route(model="forged-score", quota_pool_id="forged-score")
    candidate = evaluate_route_eligibility((eligibility_facts(eligible_route),))[0]
    forged_score = CandidateScoreV1(1, ("quality",))
    object.__setattr__(forged_score, "score", float("nan"))
    with pytest.raises(DomainValidationError, match="score.invalid"):
        score_eligible_candidates(
            (candidate,),
            {eligible_route.route_id: forged_score},
        )

    with pytest.raises(DomainValidationError, match="score.invalid"):
        CandidateScoreV1(1, ("Bearer do-not-persist-this-value",))


def test_scored_and_rejected_evaluations_remain_persisted_contract_compatible():
    eligible_route = route(model="persisted-eligible", quota_pool_id="persisted-eligible")
    rejected_route = route(model="persisted-rejected", quota_pool_id="persisted-rejected")
    scored = score_eligible_candidates(
        evaluate_route_eligibility(
            (
                eligibility_facts(eligible_route),
                eligibility_facts(
                    rejected_route,
                    freshness_confidence=EligibilityDisposition.UNKNOWN,
                ),
            )
        ),
        {eligible_route.route_id: CandidateScoreV1(3.5, ("quality",))},
    )

    restored = tuple(
        CandidateEvaluation.from_mapping(
            asdict(candidate),
            route_context=(eligible_route if candidate.route_id == eligible_route.route_id else rejected_route),
        )
        for candidate in scored
    )

    assert restored == scored
    assert tuple(candidate.route for candidate in restored) == tuple(
        candidate.route for candidate in scored
    )


def test_full_pure_domain_eligibility_score_fallback_and_sole_wait_flow():
    trusted_task = TaskEnvelope.from_mapping(
        task_payload(task_id="task-t103-flow", objective="exercise pure routing flow")
    )
    attempted_opus = route(
        provider="anthropic",
        product="claude",
        model="claude-opus",
        account_id="anthropic-team-a",
        billing_pool_id="anthropic-team-a",
        quota_pool_id="anthropic-team-a",
    )
    gpt = route(
        provider="openai",
        product="api",
        model="gpt-5",
        account_id="openai-team-a",
        billing_pool_id="openai-team-a",
        quota_pool_id="openai-team-a",
    )
    policy_rejected = route(
        provider="zai",
        product="api",
        model="glm",
        account_id="zai-team-a",
        billing_pool_id="zai-team-a",
        quota_pool_id="zai-team-a",
    )
    facts = (
        eligibility_facts(
            attempted_opus,
            freshness_confidence=EligibilityDisposition.REJECT,
            concurrency_reservation=EligibilityDisposition.REJECT,
        ),
        eligibility_facts(gpt),
        eligibility_facts(
            policy_rejected,
            identity_policy=EligibilityDisposition.REJECT,
        ),
    )
    evaluated = evaluate_route_eligibility(reversed(facts))
    scored = score_eligible_candidates(
        evaluated,
        {gpt.route_id: CandidateScoreV1(100, ("quality", "capacity"))},
    )
    plan = valid_plan(
        "decision-t103-fallback",
        attempted_opus,
        gpt,
        reviewed_execution_id="attempt-t103-flow",
    )

    fallback = replan(
        trusted_task=trusted_task,
        task_id=trusted_task.task_id,
        attempt_id="attempt-t103-flow",
        decision_id="decision-t103-fallback",
        failed_route=attempted_opus,
        classification=classification(attempted_opus),
        candidates=scored,
        quality_compensation_plan=plan,
        task_verification_minimum="V2",
    )

    assert not hasattr(trusted_task, "model")
    assert fallback.relation is DecisionRelation.FALLBACK
    assert fallback.selected_route_id == gpt.route_id
    assert fallback.reason_codes == ("route_capacity_exhausted",)
    assert fallback.policy_status == "AUTHORIZED"
    assert fallback.dispatchable is True
    assert fallback.recheck_evidence == ()
    assert next(
        candidate for candidate in fallback.candidates if candidate.route_id == attempted_opus.route_id
    ).rejection_codes[:2] == (
        "freshness_confidence_rejected",
        "concurrency_reservation_rejected",
    )

    no_capacity = evaluate_route_eligibility(
        (
            facts[0],
            eligibility_facts(
                gpt,
                breaker_cooldown=EligibilityDisposition.REJECT,
            ),
            facts[2],
        )
    )
    rejected_only = score_eligible_candidates(no_capacity, {})
    evidence = ("breaker:openai-team-a:cooldown-until-2026-07-26T17:05:00Z",)
    waiting = replan(
        trusted_task=trusted_task,
        task_id=trusted_task.task_id,
        attempt_id="attempt-t103-flow",
        decision_id="decision-t103-wait",
        failed_route=attempted_opus,
        classification=classification(attempted_opus),
        candidates=rejected_only,
        recheck_evidence=evidence,
        task_verification_minimum="V2",
    )

    assert waiting.relation is DecisionRelation.WAITING
    assert waiting.policy_status == "WAITING_FOR_CAPACITY"
    assert waiting.selected_route_id is None
    assert waiting.dispatchable is False
    assert waiting.recheck_evidence == evidence
    assert waiting.recheck_evidence[0]


def test_valid_quality_compensation_makes_fallback_dispatchable():
    failed = route(provider="anthropic", product="claude", model="opus", quota_pool_id="anthropic-a")
    alternate = route(quota_pool_id="openai-a")
    plan = valid_plan("decision-2", failed, alternate)

    decision = replan(
        task_id="task-1",
        attempt_id="attempt-1",
        decision_id="decision-2",
        failed_route=failed,
        classification=classification(failed),
        candidates=(
            CandidateEvaluation(failed, False, ("failed_route",)),
            scored_candidate(alternate),
        ),
        quality_compensation_plan=plan,
        task_verification_minimum="V2",
    )

    assert decision.relation is DecisionRelation.FALLBACK
    assert decision.selected_route_id == alternate.route_id
    assert decision.policy_status == "AUTHORIZED"
    assert decision.activation_block_reason is None
    assert decision.dispatchable is True
    assert decision.schema_version == "route-decision/v1"
    assert decision.policy_version == "policy/v1"
    assert decision.router_version == "router/pure-v1"
    assert decision.capacity_view_id == "capacity-view:test"
    assert decision.fallback is True
    assert decision.verification == "V2"

    persisted = asdict(decision)
    trusted_task = TaskEnvelope.from_mapping(task_payload())
    trusted_reviewers = {INDEPENDENT_REVIEW_ROUTE.route_id: INDEPENDENT_REVIEW_ROUTE}
    with pytest.raises(DomainValidationError, match="decision.trusted_task_context_required"):
        RouteDecisionV1.from_mapping(persisted)

    trusted_routes = {
        failed.route_id: failed,
        alternate.route_id: alternate,
    }
    restored = RouteDecisionV1.from_mapping(
        persisted,
        trusted_routes=trusted_routes,
        trusted_task=trusted_task,
        trusted_reviewer_routes=trusted_reviewers,
        trusted_execution_routes={"review-execution": INDEPENDENT_REVIEW_ROUTE},
        trusted_execution_evidence={"review-execution": ("evidence:test-run",)},
        trusted_evidence_refs=("evidence:test-run",),
        trusted_threshold_results={"evidence:test-run": True},
    )
    assert restored == decision
    assert restored.dispatchable is True

    downgraded = asdict(decision)
    downgraded["verification"] = "V0"
    downgraded_plan = downgraded["quality_compensation_plan"]
    assert isinstance(downgraded_plan, dict)
    downgraded_plan["required_verification"] = "V0"
    with pytest.raises(DomainValidationError, match="decision.trusted_task_context_invalid"):
        RouteDecisionV1.from_mapping(
            downgraded,
            trusted_routes=trusted_routes,
            trusted_task=trusted_task,
            trusted_reviewer_routes=trusted_reviewers,
        )

    forged = asdict(decision)
    forged_plan = forged["quality_compensation_plan"]
    assert isinstance(forged_plan, dict)
    forged_plan["selected_quota_pool_id"] = "forged-unrelated-quota"
    forged_plan["selected_billing_pool_id"] = "forged-unrelated-billing"
    with pytest.raises(DomainValidationError, match="quality.route_binding_invalid"):
        RouteDecisionV1.from_mapping(
            forged,
            trusted_routes=trusted_routes,
            trusted_task=trusted_task,
            trusted_reviewer_routes=trusted_reviewers,
        )

    forged_trigger_payload = asdict(decision)
    forged_trigger = forged_trigger_payload["trigger"]
    assert isinstance(forged_trigger, dict)
    forged_trigger["attempted_route_id"] = alternate.route_id
    forged_trigger["quota_pool_id"] = alternate.quota_pool_id
    forged_trigger["billing_pool_id"] = alternate.billing_pool_id
    with pytest.raises(DomainValidationError, match="decision.trigger_route_mismatch"):
        RouteDecisionV1.from_mapping(
            forged_trigger_payload,
            trusted_routes=trusted_routes,
            trusted_task=trusted_task,
            trusted_reviewer_routes=trusted_reviewers,
        )

    unregistered = route(model="unregistered", quota_pool_id="other-q")
    typed_candidate_payload = asdict(decision)
    typed_candidate_payload["candidates"] = (
        *decision.candidates,
        CandidateEvaluation(unregistered, False, ("not_selected",)),
    )
    with pytest.raises(DomainValidationError, match="decision.trusted_route_context_required"):
        RouteDecisionV1.from_mapping(
            typed_candidate_payload,
            trusted_routes=trusted_routes,
            trusted_task=trusted_task,
            trusted_reviewer_routes=trusted_reviewers,
        )


def test_secret_bearing_nested_quality_plan_cannot_authorize_dispatch():
    failed = route(model="failed", quota_pool_id="failed-q", billing_pool_id="failed-b")
    alternate = route(model="alternate", quota_pool_id="alternate-q", billing_pool_id="alternate-b")
    plan = valid_plan("decision-sensitive-plan", failed, alternate)
    plan_payload = asdict(plan)
    thresholds = plan_payload["acceptance_thresholds"]
    assert isinstance(thresholds, tuple)
    threshold_payload = thresholds[0]
    assert isinstance(threshold_payload, dict)
    threshold_payload["value"] = "password=[REDACTED]"
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        QualityCompensationPlanV1.from_mapping(plan_payload)

    typed_plan = valid_plan("decision-sensitive-plan", failed, alternate)
    object.__setattr__(
        typed_plan.acceptance_thresholds[0],
        "value",
        "password=[REDACTED]",
    )
    decision = replan(
        task_id="task-sensitive-plan",
        attempt_id="attempt-sensitive-plan",
        decision_id="decision-sensitive-plan",
        failed_route=failed,
        classification=classification(failed),
        candidates=(
            CandidateEvaluation(failed, False, ("failed_route",)),
            scored_candidate(alternate),
        ),
        quality_compensation_plan=typed_plan,
        task_verification_minimum="V2",
    )
    assert decision.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
    assert decision.dispatchable is False

    typed_mutations: tuple[
        tuple[str, Callable[[QualityCompensationPlanV1], object], str, object], ...
    ] = (
        ("bytes secret", lambda plan: plan.acceptance_thresholds[0], "value", b"password=[REDACTED]"),
        ("schema", lambda plan: plan, "schema_version", "quality-compensation/v0"),
        ("deltas", lambda plan: plan, "quality_delta_codes", ()),
        ("owner", lambda plan: plan.escalation, "owner", ""),
        ("action", lambda plan: plan.escalation, "on_unmet", "IGNORE"),
        ("operator", lambda plan: plan.acceptance_thresholds[0], "operator", "ALWAYS"),
    )
    for _name, target, field_name, invalid_value in typed_mutations:
        adversarial_plan = valid_plan("decision-sensitive-plan", failed, alternate)
        object.__setattr__(target(adversarial_plan), field_name, invalid_value)
        blocked = replan(
            task_id="task-sensitive-plan",
            attempt_id="attempt-sensitive-plan",
            decision_id="decision-sensitive-plan",
            failed_route=failed,
            classification=classification(failed),
            candidates=(
                CandidateEvaluation(failed, False, ("failed_route",)),
                scored_candidate(alternate),
            ),
            quality_compensation_plan=adversarial_plan,
            task_verification_minimum="V2",
        )
        assert blocked.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
        assert blocked.dispatchable is False

    nested_threshold_plan = valid_plan("decision-sensitive-plan", failed, alternate)
    object.__setattr__(nested_threshold_plan.acceptance_thresholds[0], "operator", "ALWAYS")
    nested_threshold_payload = asdict(nested_threshold_plan)
    nested_threshold_payload["acceptance_thresholds"] = nested_threshold_plan.acceptance_thresholds
    with pytest.raises(DomainValidationError, match="quality.threshold_invalid"):
        QualityCompensationPlanV1.from_mapping(nested_threshold_payload)

    nested_escalation_plan = valid_plan("decision-sensitive-plan", failed, alternate)
    object.__setattr__(nested_escalation_plan.escalation, "on_unmet", "IGNORE")
    nested_escalation_payload = asdict(nested_escalation_plan)
    nested_escalation_payload["escalation"] = nested_escalation_plan.escalation
    with pytest.raises(DomainValidationError, match="quality.escalation_invalid"):
        QualityCompensationPlanV1.from_mapping(nested_escalation_payload)


def test_fallback_ranking_is_score_based_and_permutation_invariant():
    failed = route(model="failed", quota_pool_id="failed-quota", billing_pool_id="failed-billing")
    low = route(model="low", quota_pool_id="eligible-quota", billing_pool_id="eligible-billing")
    high = route(model="high", quota_pool_id="eligible-quota", billing_pool_id="eligible-billing")
    plan = valid_plan(
        "decision-score",
        failed,
        high,
        reviewed_execution_id="attempt-score",
    )

    def select(
        candidates: tuple[CandidateEvaluation, ...],
        compensation_plan: QualityCompensationPlanV1 = plan,
    ) -> str | None:
        return replan(
            task_id="task-score",
            attempt_id="attempt-score",
            decision_id="decision-score",
            failed_route=failed,
            classification=classification(failed),
            candidates=candidates,
            quality_compensation_plan=compensation_plan,
            task_verification_minimum="V2",
        ).selected_route_id

    low_candidate = CandidateEvaluation(low, True, score=1.0, score_factors=("quality",))
    high_candidate = CandidateEvaluation(high, True, score=100.0, score_factors=("quality",))
    assert select((low_candidate, high_candidate)) == high.route_id
    assert select((high_candidate, low_candidate)) == high.route_id

    tie_selected = min((low, high), key=lambda candidate_route: candidate_route.route_id)
    tie_plan = valid_plan(
        "decision-score",
        failed,
        tie_selected,
        reviewed_execution_id="attempt-score",
    )
    low_tie = CandidateEvaluation(low, True, score=10.0, score_factors=("quality",))
    high_tie = CandidateEvaluation(high, True, score=10.0, score_factors=("quality",))
    assert select((low_tie, high_tie), tie_plan) == tie_selected.route_id
    assert select((high_tie, low_tie), tie_plan) == tie_selected.route_id


def test_direct_decisions_enforce_ranking_e0_waiting_and_review_authority():
    low = route(model="direct-low", quota_pool_id="direct-low-q")
    high = route(model="direct-high", quota_pool_id="direct-high-q")
    initial_trigger = InitialSelectionTriggerV1(
        schema_version="initial-selection-trigger/v1",
        kind="initial_selection",
        source="policy-router",
        evaluated_at="2026-07-26T17:00:00Z",
    )
    direct_task = TaskEnvelope.from_mapping(
        task_payload(task_id="task-direct-ranking")
    )
    direct_values: dict[str, object] = {
        "decision_id": "decision-direct-ranking",
        "task_id": "task-direct-ranking",
        "attempt_id": "attempt-direct-ranking",
        **decision_metadata(),
        "fallback": False,
        "relation": DecisionRelation.INITIAL,
        "candidates": (
            CandidateEvaluation(low, True, score=1.0, score_factors=("quality",)),
            CandidateEvaluation(high, True, score=100.0, score_factors=("quality",)),
        ),
        "selected_route_id": low.route_id,
        "trigger": initial_trigger,
        "reason_codes": ("initial_selection",),
        "trusted_task": direct_task,
        "trusted_routes": {
            low.route_id: low,
            high.route_id: high,
        },
    }
    with pytest.raises(DomainValidationError, match="decision.selection_rank_invalid"):
        RouteDecisionV1(**direct_values)  # type: ignore[arg-type]

    direct_values["effort"] = "E0"
    direct_values["selected_route_id"] = high.route_id
    direct_values["trusted_task"] = TaskEnvelope.from_mapping(
        task_payload(task_id="task-direct-ranking", effort="E0")
    )
    with pytest.raises(DomainValidationError, match="decision.effort_route_prohibited"):
        RouteDecisionV1(**direct_values)  # type: ignore[arg-type]

    failed = route(model="waiting-failed", quota_pool_id="waiting-failed-q")
    unrelated = route(model="waiting-unrelated", quota_pool_id="waiting-unrelated-q")
    with pytest.raises(DomainValidationError, match="decision.trigger_route_mismatch"):
        RouteDecisionV1(
            decision_id="decision-waiting-mismatch",
            task_id="task-waiting-mismatch",
            attempt_id="attempt-waiting-mismatch",
            **decision_metadata(),  # type: ignore[arg-type]
            fallback=False,
            relation=DecisionRelation.WAITING,
            candidates=(CandidateEvaluation(failed, False, ("capacity",)),),
            selected_route_id=None,
            trigger=classification(unrelated),
            reason_codes=("waiting",),
            prior_route_id=failed.route_id,
            trusted_task=TaskEnvelope.from_mapping(
                task_payload(task_id="task-waiting-mismatch")
            ),
            trusted_routes={failed.route_id: failed},
            trusted_prior_route=failed,
        )

    selected = route(model="review-selected", quota_pool_id="review-selected-q")
    mismatched_review = valid_plan(
        "decision-review-binding",
        failed,
        selected,
        reviewed_execution_id="other-attempt",
    )
    blocked = replan(
        task_id="task-review-binding",
        attempt_id="actual-attempt",
        decision_id="decision-review-binding",
        failed_route=failed,
        classification=classification(failed),
        candidates=(
            CandidateEvaluation(failed, False, ("failed_route",)),
            scored_candidate(selected),
        ),
        quality_compensation_plan=mismatched_review,
        task_verification_minimum="V2",
    )
    assert blocked.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
    assert blocked.dispatchable is False

    forged_attestation = replace(
        mismatched_review.review_attestations[0],
        reviewed_execution_id="actual-attempt",
        quota_pool_id="forged-review-pool",
    )
    forged_reviewer_plan = replace(
        mismatched_review,
        review_attestations=(forged_attestation,),
    )
    forged_reviewer_decision = replan(
        task_id="task-review-binding",
        attempt_id="actual-attempt",
        decision_id="decision-review-binding",
        failed_route=failed,
        classification=classification(failed),
        candidates=(
            CandidateEvaluation(failed, False, ("failed_route",)),
            scored_candidate(selected),
        ),
        quality_compensation_plan=forged_reviewer_plan,
        task_verification_minimum="V2",
    )
    assert forged_reviewer_decision.policy_status == (
        "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
    )
    assert forged_reviewer_decision.dispatchable is False

    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        TaskEnvelope.from_mapping(task_payload(objective="inspect (github_pat_REDACTED)"))

    for objective in (
        "inspect _github_pat_REDACTED",
        "inspect _gho_REDACTED",
        "inspect _xoxb-REDACTED",
        "inspect _sk-proj-REDACTED",
    ):
        with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
            TaskEnvelope.from_mapping(task_payload(objective=objective))


def test_task_authority_binds_replan_and_quality_policy_requirements():
    failed = route(model="authority-failed", quota_pool_id="authority-failed-q")
    selected = route(model="authority-selected", quota_pool_id="authority-selected-q")
    candidates = (
        CandidateEvaluation(failed, False, ("failed_route",)),
        scored_candidate(selected),
    )
    authoritative_task = TaskEnvelope.from_mapping(
        task_payload(
            task_id="trusted-task",
            policy_version="policy/trusted",
            effort="E4",
            verification={
                "minimum": "V4",
                "independent_required": True,
                "human_gate_required": True,
            },
        )
    )
    with pytest.raises(DomainValidationError, match="replan.trusted_task_mismatch"):
        replan_after_capacity_exhaustion(
            trusted_task=authoritative_task,
            task_id="forged-task",
            attempt_id="forged-attempt",
            failed_route=failed,
            classification=classification(failed),
            candidates=candidates,
            decision_id="forged-decision",
            parent_decision_id="parent-forged-decision",
            **decision_metadata(),  # type: ignore[arg-type]
            quality_compensation_plan=None,
            trusted_reviewer_routes={
                INDEPENDENT_REVIEW_ROUTE.route_id: INDEPENDENT_REVIEW_ROUTE
            },
        )

    task_independent = TaskEnvelope.from_mapping(
        task_payload(
            task_id="task-quality-authority",
            verification={
                "minimum": "V2",
                "independent_required": True,
                "human_gate_required": False,
            },
        )
    )
    task_human = TaskEnvelope.from_mapping(
        task_payload(
            task_id="task-quality-authority",
            verification={
                "minimum": "V2",
                "independent_required": False,
                "human_gate_required": True,
            },
        )
    )
    cases = (
        (
            valid_plan(
                "decision-quality-authority",
                failed,
                selected,
                reviewed_execution_id="attempt-quality-authority",
                independence_required=False,
                required_reviewers=(),
                review_attestations=(),
            ),
            task_independent,
        ),
        (
            valid_plan(
                "decision-quality-authority",
                failed,
                selected,
                reviewed_execution_id="attempt-quality-authority",
            ),
            task_human,
        ),
        (
            valid_plan(
                "decision-quality-authority",
                failed,
                selected,
                reviewed_execution_id="attempt-quality-authority",
                human_approval_ref="approval:forged",
            ),
            task_human,
        ),
        (
            valid_plan(
                "decision-quality-authority",
                failed,
                selected,
                reviewed_execution_id="attempt-quality-authority",
                policy_version="policy/attacker",
            ),
            TaskEnvelope.from_mapping(task_payload(task_id="task-quality-authority")),
        ),
        (
            valid_plan(
                "decision-quality-authority",
                failed,
                selected,
                reviewed_execution_id="attempt-quality-authority",
                required_verification="V4",
            ),
            TaskEnvelope.from_mapping(task_payload(task_id="task-quality-authority")),
        ),
    )
    for plan, trusted_task in cases:
        decision = replan(
            trusted_task=trusted_task,
            task_id="task-quality-authority",
            attempt_id="attempt-quality-authority",
            decision_id="decision-quality-authority",
            failed_route=failed,
            classification=classification(failed),
            candidates=candidates,
            quality_compensation_plan=plan,
            task_verification_minimum="V2",
        )
        assert decision.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
        assert decision.dispatchable is False

    approved_plan = valid_plan(
        "decision-quality-authority",
        failed,
        selected,
        reviewed_execution_id="attempt-quality-authority",
        human_approval_ref="approval:trusted",
    )
    approved = replan(
        trusted_task=task_human,
        trusted_human_approval_refs={"approval:trusted": "task-quality-authority"},
        task_id="task-quality-authority",
        attempt_id="attempt-quality-authority",
        decision_id="decision-quality-authority",
        failed_route=failed,
        classification=classification(failed),
        candidates=candidates,
        quality_compensation_plan=approved_plan,
        task_verification_minimum="V2",
    )
    assert approved.policy_status == "AUTHORIZED"
    assert approved.dispatchable is True


@pytest.mark.parametrize(
    "failure_kind",
    (
        "absent",
        "invalid_schema",
        "mismatched",
        "insufficient_verification",
        "non_independent",
        "unattested",
        "unmet",
        "unenforceable",
    ),
)
def test_invalid_quality_compensation_blocks_fallback_without_hiding_candidate(failure_kind: str):
    failed = route(provider="anthropic", product="claude", model="opus", quota_pool_id="anthropic-a")
    alternate = route(quota_pool_id="openai-a")
    plan: QualityCompensationPlanV1 | dict[str, object] | None
    if failure_kind == "absent":
        plan = None
    elif failure_kind == "invalid_schema":
        plan = {"schema_version": "quality-compensation/v0"}
    elif failure_kind == "mismatched":
        plan = valid_plan("decision-2", failed, UNSELECTED_ROUTE_ID)
    elif failure_kind == "insufficient_verification":
        plan = valid_plan(
            "decision-2",
            failed,
            alternate,
            required_verification="V1",
            independence_required=False,
        )
    elif failure_kind == "non_independent":
        plan = valid_plan(
            "decision-2",
            failed,
            alternate,
            required_reviewers=(alternate.route_id,),
        )
    elif failure_kind in {"unattested", "unmet"}:
        plan = valid_plan(
            "decision-2",
            failed,
            alternate,
            acceptance_thresholds=(
                AcceptanceThresholdV1(
                    metric="focused_tests",
                    operator=">=",
                    value=1,
                    evidence_required=True,
                    evidence_ref=(
                        "evidence:test-run" if failure_kind == "unmet" else None
                    ),
                    met=False if failure_kind == "unmet" else None,
                ),
            ),
        )
    else:
        plan = {
            "schema_version": "quality-compensation/v1",
            "plan_id": "plan-bad",
            "decision_id": "decision-2",
            "prior_route_id": failed.route_id,
            "selected_route_id": alternate.route_id,
            "trigger_kind": "capacity_exhausted",
            "quality_delta_codes": ["different_model_family"],
            "required_verification": "V2",
            "independence_required": True,
            "required_reviewers": ["review-route-independent"],
            "acceptance_thresholds": [],
            "escalation": {"on_unmet": "IGNORE", "owner": "nobody"},
            "evidence_refs": ["evidence:test-run"],
            "created_at": "2026-07-26T17:00:00Z",
            "policy_version": "policy/v1",
        }

    decision = replan(
        task_id="task-1",
        attempt_id="attempt-1",
        decision_id="decision-2",
        failed_route=failed,
        classification=classification(failed),
        candidates=(scored_candidate(alternate),),
        quality_compensation_plan=plan,
        task_verification_minimum="V2",
    )

    assert decision.relation is DecisionRelation.FALLBACK
    assert decision.selected_route_id == alternate.route_id
    assert decision.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
    assert decision.activation_block_reason == "quality_compensation_insufficient"
    assert "quality_compensation_insufficient" in decision.reason_codes
    assert decision.dispatchable is False


def test_activation_blocked_fallback_cannot_claim_reservation():
    failed = route(model="opus", quota_pool_id="failed-quota")
    alternate = route(model="gpt", quota_pool_id="alternate-quota")
    blocked = replan(
        task_id="task-1",
        attempt_id="attempt-1",
        decision_id="decision-2",
        failed_route=failed,
        classification=classification(failed),
        candidates=(scored_candidate(alternate),),
        quality_compensation_plan=None,
        task_verification_minimum="V2",
    )
    assert blocked.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
    with pytest.raises(DomainValidationError, match="decision.blocked_reservation"):
        replace(
            blocked,
            reservation_id="reservation-held",
            trusted_task=TaskEnvelope.from_mapping(task_payload()),
            trusted_reviewer_routes={
                INDEPENDENT_REVIEW_ROUTE.route_id: INDEPENDENT_REVIEW_ROUTE
            },
            trusted_routes={
                failed.route_id: failed,
                alternate.route_id: alternate,
            },
            trusted_prior_route=failed,
        )


def test_quality_independence_requires_route_pool_and_execution_attestation():
    failed = route(
        model="opus",
        quota_pool_id="anthropic-quota",
        billing_pool_id="anthropic-billing",
    )
    alternate = route(quota_pool_id="openai-quota", billing_pool_id="openai-billing")
    invalid_attestations = (
        (),
        (
            IndependentReviewAttestationV1(
                "reviewer",
                alternate.route_id,
                "review-quota",
                "review-billing",
                "fallback-execution",
                "review-execution",
                "evidence:test-run",
            ),
        ),
        (
            IndependentReviewAttestationV1(
                "reviewer",
                INDEPENDENT_REVIEW_ROUTE_ID,
                failed.quota_pool_id,
                "review-billing",
                "fallback-execution",
                "review-execution",
                "evidence:test-run",
            ),
        ),
        (
            IndependentReviewAttestationV1(
                "reviewer",
                INDEPENDENT_REVIEW_ROUTE_ID,
                "review-quota",
                alternate.billing_pool_id,
                "fallback-execution",
                "review-execution",
                "evidence:test-run",
            ),
        ),
        (
            IndependentReviewAttestationV1(
                "reviewer",
                INDEPENDENT_REVIEW_ROUTE_ID,
                "review-quota",
                "review-billing",
                "fallback-execution",
                "attempt-1",
                "evidence:test-run",
            ),
        ),
        (
            IndependentReviewAttestationV1(
                "reviewer",
                INDEPENDENT_REVIEW_ROUTE_ID,
                "review-quota",
                "review-billing",
                "same-execution",
                "same-execution",
                "evidence:test-run",
            ),
        ),
    )
    for attestations in invalid_attestations:
        plan = valid_plan(
            "decision-2",
            failed,
            alternate,
            required_reviewers=("reviewer",),
            review_attestations=attestations,
        )
        decision = replan(
            task_id="task-1",
            attempt_id="attempt-1",
            decision_id="decision-2",
            failed_route=failed,
            classification=classification(failed),
            candidates=(scored_candidate(alternate),),
            quality_compensation_plan=plan,
            task_verification_minimum="V2",
        )
        assert decision.dispatchable is False
        assert decision.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"


def test_human_gate_is_fail_closed_without_external_approval_authority():
    failed = route(model="opus", quota_pool_id="failed-quota")
    alternate = route(model="gpt", quota_pool_id="alternate-quota")
    plan = valid_plan(
        "decision-2",
        failed,
        alternate,
        escalation=CompensationEscalationV1(
            on_unmet=EscalationAction.HUMAN_GATE,
            owner="human-owner",
        ),
        human_approval_ref="evidence:test-run",
    )
    decision = replan(
        task_id="task-1",
        attempt_id="attempt-1",
        decision_id="decision-2",
        failed_route=failed,
        classification=classification(failed),
        candidates=(scored_candidate(alternate),),
        quality_compensation_plan=plan,
        task_verification_minimum="V2",
    )
    assert decision.dispatchable is False
    assert decision.activation_block_reason == "quality_compensation_insufficient"


def test_capacity_exhaustion_rejects_mismatched_billing_pool_scope():
    attempted = route(billing_pool_id="billing-a", quota_pool_id="quota-a")
    with pytest.raises(DomainValidationError, match="classification.capacity_scope_required"):
        replan(
            task_id="task-1",
            attempt_id="attempt-1",
            decision_id="decision-2",
            failed_route=attempted,
            classification=classification(attempted, billing_pool_id="billing-b"),
            candidates=(),
            recheck_evidence=("pool evidence",),
        )


def test_capacity_exhaustion_does_not_fallback_within_same_quota_pool():
    failed = route(model="gpt-a", quota_pool_id="shared-pool")
    same_pool = route(model="gpt-b", quota_pool_id="shared-pool")
    decision = replan(
        task_id="task-1",
        attempt_id="attempt-1",
        decision_id="decision-2",
        failed_route=failed,
        classification=classification(failed),
        candidates=(CandidateEvaluation(same_pool, True),),
        recheck_evidence=("shared quota pool exhausted",),
    )
    assert decision.relation is DecisionRelation.WAITING
    assert decision.policy_status == "WAITING_FOR_CAPACITY"
    assert decision.dispatchable is False
    assert all(not candidate.eligible for candidate in decision.candidates)
    assert decision.candidates[0].rejection_codes == ("same_exhausted_quota_pool",)


def test_wait_requires_evidence_when_no_alternate_is_eligible():
    attempted = route()
    with pytest.raises(DomainValidationError, match="replan.recheck_evidence_required"):
        replan(
            task_id="task-1",
            attempt_id="attempt-1",
            decision_id="decision-2",
            failed_route=attempted,
            classification=classification(attempted),
            candidates=(CandidateEvaluation(attempted, False, ("policy_rejected",)),),
        )


def test_unscoped_or_mismatched_capacity_classification_is_rejected():
    attempted = route()
    with pytest.raises(DomainValidationError, match="classification.capacity_scope_required"):
        RuntimeErrorClassificationV1(
            kind=ErrorKind.CAPACITY_EXHAUSTED,
            source="typed-runtime-error",
            attempted_route_id="",
            quota_pool_id=attempted.quota_pool_id,
            classified_at="2026-07-26T17:00:00Z",
        )
    with pytest.raises(DomainValidationError, match="classification.capacity_scope_required"):
        replan(
            task_id="task-1",
            attempt_id="attempt-1",
            decision_id="decision-2",
            failed_route=attempted,
            classification=classification(attempted, "other-pool"),
            candidates=(),
        )


def test_runtime_classification_requires_route_pool_scope_not_provider_labels():
    attempted = route()
    classification_value = RuntimeErrorClassificationV1.from_mapping(
        {
            "kind": "capacity_exhausted",
            "attempted_route_id": attempted.route_id,
            "quota_pool_id": attempted.quota_pool_id,
            "billing_pool_id": attempted.billing_pool_id,
            "source": "typed-runtime-error",
            "classified_at": "2026-07-26T17:00:00Z",
        }
    )
    assert classification_value.source == "typed-runtime-error"
    assert classification_value.classified_at.endswith("Z")

    with pytest.raises(DomainValidationError, match="classification.capacity_scope_required"):
        RuntimeErrorClassificationV1.from_mapping(
            {
                "kind": "capacity_exhausted",
                "provider": "openai",
                "model": "gpt-5",
            }
        )


@pytest.mark.parametrize(
    ("validator", "event_type", "from_state", "to_state", "identity_field"),
    [
        (validate_task_transition, TaskStateEvent, TaskState.NEW, TaskState.PLANNED, "task_id"),
        (
            validate_attempt_transition,
            AttemptStateEvent,
            AttemptState.CREATED,
            AttemptState.RESERVED,
            "attempt_id",
        ),
        (
            validate_route_transition,
            RouteStateEvent,
            RouteState.DISCOVERED,
            RouteState.ELIGIBLE,
            "route_id",
        ),
        (
            validate_credential_transition,
            CredentialStateEvent,
            CredentialState.AVAILABLE,
            CredentialState.COOLDOWN,
            "credential_id",
        ),
        (
            validate_reservation_transition,
            ReservationStateEvent,
            ReservationState.PENDING,
            ReservationState.HELD,
            "reservation_id",
        ),
        (
            validate_review_transition,
            ReviewStateEvent,
            ReviewState.PENDING,
            ReviewState.IN_PROGRESS,
            "review_id",
        ),
    ],
)
def test_each_state_domain_has_an_independent_audited_legal_transition(
    validator: object,
    event_type: type[object],
    from_state: object,
    to_state: object,
    identity_field: str,
):
    event = validator(  # type: ignore[operator]
        from_state=from_state,
        to_state=to_state,
        **audit(identity_field),
    )
    assert type(event) is event_type
    assert getattr(event, identity_field) == "entity-1"
    assert event.actor == "policy-engine"  # type: ignore[attr-defined]
    assert event.timestamp.endswith("Z")  # type: ignore[attr-defined]
    assert event.reason == "contract transition"  # type: ignore[attr-defined]
    assert event.correlation_id == "corr-1"  # type: ignore[attr-defined]


def test_transition_validators_reject_illegal_edges_missing_audit_and_cross_domain_inference():
    with pytest.raises(DomainValidationError, match="state.transition_illegal"):
        validate_task_transition(
            from_state=TaskState.NEW,
            to_state=TaskState.COMPLETED,
            **audit("task_id"),
        )
    with pytest.raises(DomainValidationError, match="state.audit_metadata_required"):
        validate_attempt_transition(
            from_state=AttemptState.CREATED,
            to_state=AttemptState.RESERVED,
            **{**audit("attempt_id"), "actor": ""},
        )
    with pytest.raises(DomainValidationError, match="state.domain_mismatch"):
        validate_task_transition(
            from_state=AttemptState.CREATED,  # type: ignore[arg-type]
            to_state=TaskState.PLANNED,
            **audit("task_id"),
        )

    with pytest.raises(DomainValidationError, match="state.transition_illegal"):
        TaskStateEvent(
            task_id="task-1",
            from_state=TaskState.NEW,
            to_state=TaskState.COMPLETED,
            actor="policy-engine",
            timestamp="2026-07-26T17:00:00Z",
            reason="illegal direct construction",
            correlation_id="corr-1",
        )


def test_state_domains_are_distinct_even_when_values_overlap():
    assert type(TaskState.NEW) is not type(AttemptState.CREATED)
    assert type(RouteState.COOLDOWN) is not type(CredentialState.COOLDOWN)
    assert type(ReservationState.HELD) is not type(ReviewState.PENDING)


def test_sensitive_task_and_decision_content_is_rejected_without_token_name_false_positive():
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        TaskEnvelope.from_mapping({"prompt": "private"})
    attempted = route()
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        replan(
            task_id="task-1",
            attempt_id="attempt-1",
            decision_id="decision-2",
            failed_route=attempted,
            classification=classification(attempted),
            candidates=(),
            recheck_evidence=("Bearer abc",),
        )

    for leaked_value in (
        "password=hunter2",
        "api_key=not-safe",
        "token=not-safe",
        "prompt=private-content",
        "github_pat_exampletoken",
        "-----BEGIN PRIVATE KEY----- material",
    ):
        with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
            replan(
                task_id="task-1",
                attempt_id="attempt-1",
                decision_id="decision-2",
                failed_route=attempted,
                classification=classification(attempted),
                candidates=(),
                recheck_evidence=(leaked_value,),
            )


@pytest.mark.parametrize(
    "parser",
    (
        RouteV1.from_mapping,
        AuditedModelJustification.from_mapping,
        TaskEnvelope.from_mapping,
        InitialSelectionTriggerV1.from_mapping,
        RuntimeErrorClassificationV1.from_mapping,
        CandidateEvaluation.from_mapping,
        AcceptanceThresholdV1.from_mapping,
        CompensationEscalationV1.from_mapping,
        IndependentReviewAttestationV1.from_mapping,
        QualityCompensationPlanV1.from_mapping,
        RouteDecisionV1.from_mapping,
    ),
)
@pytest.mark.parametrize("malformed", (None, 7, [], "not-a-mapping"))
def test_public_mapping_parsers_reject_non_mappings_with_domain_errors(
    parser: Callable[[Any], object], malformed: object
):
    with pytest.raises(DomainValidationError):
        parser(malformed)


def test_endpoint_and_justification_reject_noncanonical_unicode_or_time():
    with pytest.raises(DomainValidationError, match="route.endpoint_invalid"):
        route(endpoint="https://api.example.com/\ud800")
    base = {
        "policy_version": "policy/v1",
        "reason": "audited exception",
        "evidence_refs": ("evidence:audit",),
        "author": "policy-owner",
    }
    for expires_at in (
        "2099-08-01T00:00:00+01:00",
        "2099-W31-5T00:00:00Z",
        "20990801T000000Z",
    ):
        with pytest.raises(DomainValidationError, match="task.justification_expiry_invalid"):
            TaskEnvelope.from_mapping(
                task_payload(
                    model="audited-model",
                    provider="audited-provider",
                    audited_model_justification={**base, "expires_at": expires_at},
                )
            )


def test_sensitive_scan_covers_complete_public_graphs_and_bytearray():
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        TaskEnvelope.from_mapping(
            task_payload(
                model="audited-model",
                provider="audited-provider",
                audited_model_justification={
                    "policy_version": "policy/v1",
                    "reason": "audited exception",
                    "evidence_refs": ("evidence:audit",),
                    "author": "github_pat_REDACTED",
                    "expires_at": "2099-08-01T00:00:00Z",
                },
            )
        )
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        InitialSelectionTriggerV1(
            "initial-selection-trigger/v1", "initial_selection", "policy", "gho_REDACTED"
        )
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        CompensationEscalationV1(EscalationAction.BLOCK_DISPATCH, "xoxb-REDACTED")
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        IndependentReviewAttestationV1(
            "sk-proj-REDACTED",
            INDEPENDENT_REVIEW_ROUTE_ID,
            "review-quota-pool",
            "review-billing-pool",
            "attempt-1",
            "review-execution",
            "evidence:test-run",
        )
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        validate_task_transition(
            from_state=TaskState.NEW,
            to_state=TaskState.PLANNED,
            **{**audit("task_id"), "actor": "github_pat_REDACTED"},
        )
    attempted = route()
    with pytest.raises(DomainValidationError, match="decision.sensitive_field_prohibited"):
        RuntimeErrorClassificationV1.from_mapping(
            {
                "kind": "capacity_exhausted",
                "source": "runtime",
                "attempted_route_id": attempted.route_id,
                "quota_pool_id": attempted.quota_pool_id,
                "billing_pool_id": bytearray(b"gho_REDACTED"),
                "classified_at": "2026-07-26T17:00:00Z",
            }
        )


def test_initial_route_authority_task_gates_reservation_and_e0_contract():
    selected = route(model="initial-authority")
    trigger = InitialSelectionTriggerV1(
        "initial-selection-trigger/v1",
        "initial_selection",
        "policy",
        "2026-07-26T17:00:00Z",
    )
    base: dict[str, object] = {
        "decision_id": "initial-authority-decision",
        "task_id": "initial-authority-task",
        "attempt_id": "initial-authority-attempt",
        **decision_metadata(),
        "fallback": False,
        "relation": DecisionRelation.INITIAL,
        "candidates": (CandidateEvaluation(selected, True, score=1, score_factors=("policy",)),),
        "selected_route_id": selected.route_id,
        "trigger": trigger,
        "reason_codes": (),
    }
    normal_task = TaskEnvelope.from_mapping(
        task_payload(
            task_id="initial-authority-task",
            verification={"minimum": "V2", "independent_required": False, "human_gate_required": False},
        )
    )
    with pytest.raises(DomainValidationError, match="decision.trusted_route_context_required"):
        RouteDecisionV1(**base, trusted_task=normal_task)

    independent_task = TaskEnvelope.from_mapping(task_payload(task_id="initial-authority-task"))
    independent = RouteDecisionV1(
        **base,
        trusted_task=independent_task,
        trusted_routes={selected.route_id: selected},
    )
    assert independent.policy_status == "ACTIVATION_BLOCKED_INDEPENDENT_REVIEW"
    assert independent.dispatchable is False

    human_task = TaskEnvelope.from_mapping(
        task_payload(
            task_id="initial-authority-task",
            verification={"minimum": "V2", "independent_required": False, "human_gate_required": True},
        )
    )
    with pytest.raises(DomainValidationError, match="decision.blocked_reservation"):
        RouteDecisionV1(
            **base,
            reservation_id="reservation-self-claimed",
            trusted_task=human_task,
            trusted_routes={selected.route_id: selected},
        )
    approved = RouteDecisionV1(
        **base,
        trusted_task=human_task,
        trusted_routes={selected.route_id: selected},
        trusted_human_approval_refs={"approval:trusted": "initial-authority-task"},
    )
    assert approved.dispatchable is True
    unrelated_approval = RouteDecisionV1(
        **base,
        trusted_task=human_task,
        trusted_routes={selected.route_id: selected},
        trusted_human_approval_refs={"approval:trusted": "some-other-task"},
    )
    assert unrelated_approval.dispatchable is False

    e0_task = TaskEnvelope.from_mapping(
        task_payload(
            task_id="initial-authority-task",
            effort="E0",
            verification={"minimum": "V0", "independent_required": False, "human_gate_required": False},
        )
    )
    e0_values = dict(base)
    e0_values.update(effort="E0", verification="V0", candidates=(), selected_route_id=None)
    e0 = RouteDecisionV1(**e0_values, trusted_task=e0_task, trusted_routes={})
    assert e0.policy_status == "NO_ROUTE_REQUIRED"
    assert e0.dispatchable is False


def test_quality_authority_requires_execution_evidence_threshold_and_rejects_e0_replan():
    failed = route(model="authority-failed", quota_pool_id="authority-failed-q")
    selected = route(model="authority-selected", quota_pool_id="authority-selected-q")
    plan = valid_plan(
        "quality-authority-decision",
        failed,
        selected,
        reviewed_execution_id="quality-authority-attempt",
    )
    common = {
        "task_id": "quality-authority-task",
        "attempt_id": "quality-authority-attempt",
        "decision_id": "quality-authority-decision",
        "failed_route": failed,
        "classification": classification(failed),
        "candidates": (scored_candidate(selected),),
        "quality_compensation_plan": plan,
        "task_verification_minimum": "V2",
    }
    for missing_context in (
        {"trusted_execution_routes": None},
        {"trusted_execution_evidence": None},
        {"trusted_execution_evidence": {"review-execution": ("evidence:unrelated",)}},
        {"trusted_evidence_refs": ()},
        {"trusted_threshold_results": {"evidence:test-run": False}},
    ):
        decision = replan(**common, **missing_context)
        assert decision.policy_status == "ACTIVATION_BLOCKED_QUALITY_COMPENSATION"
        assert decision.dispatchable is False

    e0_task = TaskEnvelope.from_mapping(
        task_payload(
            task_id="quality-authority-task",
            effort="E0",
            verification={"minimum": "V0", "independent_required": False, "human_gate_required": False},
        )
    )
    with pytest.raises(DomainValidationError, match="replan.effort_route_prohibited"):
        e0_common = {**common, "task_verification_minimum": "V0"}
        replan(**e0_common, trusted_task=e0_task)


def test_decision_rejects_non_mapping_optional_route_registries():
    task = TaskEnvelope.from_mapping(
        task_payload(
            task_id="registry-shape-task",
            effort="E0",
            verification={
                "minimum": "V0",
                "independent_required": False,
                "human_gate_required": False,
            },
        )
    )
    with pytest.raises(
        DomainValidationError,
        match="decision.trusted_reviewer_context_invalid",
    ):
        RouteDecisionV1(
            decision_id="registry-shape-decision",
            task_id=task.task_id,
            attempt_id="registry-shape-attempt",
            **{**decision_metadata(), "effort": "E0", "verification": "V0"},
            fallback=False,
            relation=DecisionRelation.INITIAL,
            candidates=(),
            selected_route_id=None,
            trigger=InitialSelectionTriggerV1(
                "initial-selection-trigger/v1",
                "initial_selection",
                "policy",
                "2026-07-26T17:00:00Z",
            ),
            reason_codes=(),
            trusted_task=task,
            trusted_routes={},
            trusted_reviewer_routes=[],  # type: ignore[arg-type]
        )


def test_task_justification_expiry_uses_explicit_reference_time():
    payload = task_payload(
        model="audited-model",
        audited_model_justification={
            "policy_version": "policy/v1",
            "reason": "time-bounded audited exception",
            "evidence_refs": ("evidence:approval",),
            "author": "policy-owner",
            "expires_at": "2026-07-27T00:00:00Z",
        },
    )
    accepted = TaskEnvelope.from_mapping(
        payload,
        reference_time=datetime(2026, 7, 26, 23, 59, tzinfo=timezone.utc),
    )
    assert accepted.audited_model_justification is not None

    with pytest.raises(DomainValidationError, match="task.justification_expired"):
        TaskEnvelope.from_mapping(
            payload,
            reference_time=datetime(2026, 7, 27, 0, 1, tzinfo=timezone.utc),
        )
