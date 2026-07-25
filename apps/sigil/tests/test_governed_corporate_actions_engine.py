from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest

from sigil.corporate_actions import (
    CorporateActionDisposition,
    CorporateActionEvent,
    CorporateActionKind,
    CorporateActionQuality,
    CorporateActionStatus,
    CorporateActionValidationError,
    GovernedCorporateActionsInput,
    GovernedCorporateActionsPolicy,
    compare_corporate_actions_packages,
    construct_governed_corporate_actions_package,
    inspect_provenance,
    list_adjustment_instructions,
    list_conflicts,
    list_events,
    list_quality_reasons,
    list_readiness_blockers,
    list_sources,
    verify_package_identity,
)


AS_OF = "2026-07-24T20:00:00Z"
AS_OF_EPOCH = int(
    datetime.fromisoformat(AS_OF.replace("Z", "+00:00"))
    .astimezone(timezone.utc)
    .timestamp()
)


def event(**overrides) -> CorporateActionEvent:
    values = {
        "action_id": "split-001",
        "instrument_id": "AAPL",
        "kind": CorporateActionKind.FORWARD_SPLIT,
        "status": CorporateActionStatus.ANNOUNCED,
        "announced_at": "2026-07-20T12:00:00Z",
        "effective_at": "2026-08-01T00:00:00Z",
        "source_id": "issuer-primary",
        "evidence_references": ("evidence://issuer/split-001",),
        "ratio_numerator": "4",
        "ratio_denominator": "1",
    }
    values.update(overrides)
    return CorporateActionEvent(**values)


def policy(**overrides) -> GovernedCorporateActionsPolicy:
    values = {
        "permitted_sources": ("issuer-primary", "exchange-primary"),
    }
    values.update(overrides)
    return GovernedCorporateActionsPolicy(**values)


def request(
    governed_policy: GovernedCorporateActionsPolicy,
    *events: CorporateActionEvent,
    **overrides,
) -> GovernedCorporateActionsInput:
    values = {
        "instrument_id": "AAPL",
        "as_of": AS_OF,
        "as_of_epoch_seconds": AS_OF_EPOCH,
        "events": tuple(events),
        "policy_identity": governed_policy.policy_identity,
    }
    values.update(overrides)
    return GovernedCorporateActionsInput(**values)


def test_constructs_immutable_analytical_package() -> None:
    governed_policy = policy()
    package = construct_governed_corporate_actions_package(
        request(governed_policy, event()),
        governed_policy,
    )

    assert package.quality is CorporateActionQuality.ACCEPTABLE
    assert package.disposition is CorporateActionDisposition.REVIEW_REQUIRED
    assert package.analytical_only is True
    assert package.authorizes_trading is False
    assert package.mutates_positions is False
    assert verify_package_identity(package)
    with pytest.raises(FrozenInstanceError):
        package.instrument_id = "MSFT"


def test_sorting_and_identity_are_input_order_independent() -> None:
    governed_policy = policy()
    first = event()
    second = event(
        action_id="symbol-001",
        kind=CorporateActionKind.SYMBOL_CHANGE,
        effective_at="2026-08-02T00:00:00Z",
        new_symbol="AAPLX",
    )

    package_a = construct_governed_corporate_actions_package(
        request(governed_policy, first, second),
        governed_policy,
    )
    package_b = construct_governed_corporate_actions_package(
        request(governed_policy, second, first),
        governed_policy,
    )

    assert package_a.package_identity == package_b.package_identity
    assert package_a.provenance.request_identity == (
        package_b.provenance.request_identity
    )


def test_two_sources_produce_verified_quality() -> None:
    governed_policy = policy()
    package = construct_governed_corporate_actions_package(
        request(
            governed_policy,
            event(),
            event(
                action_id="split-002",
                source_id="exchange-primary",
                evidence_references=("evidence://exchange/split-002",),
            ),
        ),
        governed_policy,
    )

    assert package.quality is CorporateActionQuality.VERIFIED
    assert list_sources(package) == (
        "exchange-primary",
        "issuer-primary",
    )


def test_split_emits_ratio_instruction() -> None:
    governed_policy = policy()
    package = construct_governed_corporate_actions_package(
        request(governed_policy, event()),
        governed_policy,
    )

    instructions = list_adjustment_instructions(package)
    assert len(instructions) == 1
    assert instructions[0].adjustment_type == "share_ratio_adjustment"
    assert instructions[0].ratio_numerator == "4"
    assert instructions[0].ratio_denominator == "1"
    assert instructions[0].analytical_only is True
    assert instructions[0].requires_human_review is True


def test_cash_dividend_requires_amount_and_currency() -> None:
    governed_policy = policy()
    bad = event(
        kind=CorporateActionKind.CASH_DIVIDEND,
        ratio_numerator=None,
        ratio_denominator=None,
        cash_amount="0.25",
        currency=None,
    )

    with pytest.raises(
        CorporateActionValidationError,
        match="currency is required",
    ):
        construct_governed_corporate_actions_package(
            request(governed_policy, bad),
            governed_policy,
        )


def test_cash_dividend_emits_cash_entitlement() -> None:
    governed_policy = policy()
    dividend = event(
        kind=CorporateActionKind.CASH_DIVIDEND,
        ratio_numerator=None,
        ratio_denominator=None,
        cash_amount="0.25",
        currency="USD",
        record_at="2026-07-28T00:00:00Z",
        ex_at="2026-07-29T00:00:00Z",
        payment_at="2026-08-15T00:00:00Z",
    )
    package = construct_governed_corporate_actions_package(
        request(governed_policy, dividend),
        governed_policy,
    )

    instruction = package.adjustment_instructions[0]
    assert instruction.adjustment_type == "cash_entitlement"
    assert instruction.cash_amount == "0.25"
    assert instruction.currency == "USD"


def test_merger_requires_target_instrument() -> None:
    governed_policy = policy()
    merger = event(
        kind=CorporateActionKind.MERGER,
        ratio_numerator=None,
        ratio_denominator=None,
    )

    with pytest.raises(
        CorporateActionValidationError,
        match="target_instrument_id is required",
    ):
        construct_governed_corporate_actions_package(
            request(governed_policy, merger),
            governed_policy,
        )


def test_symbol_change_requires_new_symbol() -> None:
    governed_policy = policy()
    symbol_change = event(
        kind=CorporateActionKind.SYMBOL_CHANGE,
        ratio_numerator=None,
        ratio_denominator=None,
    )

    with pytest.raises(
        CorporateActionValidationError,
        match="new_symbol is required",
    ):
        construct_governed_corporate_actions_package(
            request(governed_policy, symbol_change),
            governed_policy,
        )


def test_duplicate_action_ids_are_rejected() -> None:
    governed_policy = policy()

    with pytest.raises(
        CorporateActionValidationError,
        match="duplicate action_id",
    ):
        construct_governed_corporate_actions_package(
            request(governed_policy, event(), event()),
            governed_policy,
        )


def test_conflicting_effective_events_are_blocked() -> None:
    governed_policy = policy()
    first = event()
    second = event(
        action_id="split-conflict",
        ratio_numerator="3",
        ratio_denominator="1",
        source_id="exchange-primary",
    )
    package = construct_governed_corporate_actions_package(
        request(governed_policy, first, second),
        governed_policy,
    )

    assert package.quality is CorporateActionQuality.REJECTED
    assert package.disposition is CorporateActionDisposition.BLOCKED
    assert list_conflicts(package) == ("split-001", "split-conflict")
    assert "conflicting-action:split-001" in list_readiness_blockers(
        package
    )


def test_policy_can_reject_conflicts_immediately() -> None:
    governed_policy = policy(reject_conflicting_effective_events=True)
    first = event()
    second = event(
        action_id="split-conflict",
        ratio_numerator="3",
        ratio_denominator="1",
        source_id="exchange-primary",
    )

    with pytest.raises(
        CorporateActionValidationError,
        match="conflicting effective",
    ):
        construct_governed_corporate_actions_package(
            request(governed_policy, first, second),
            governed_policy,
        )


def test_cancelled_event_emits_no_instruction() -> None:
    governed_policy = policy()
    cancelled = event(status=CorporateActionStatus.CANCELLED)
    package = construct_governed_corporate_actions_package(
        request(governed_policy, cancelled),
        governed_policy,
    )

    assert package.adjustment_instructions == ()
    assert package.quality is CorporateActionQuality.REJECTED
    assert "no-active-corporate-actions" in package.readiness_blockers


def test_disallowed_source_is_rejected() -> None:
    governed_policy = policy()

    with pytest.raises(
        CorporateActionValidationError,
        match="source is not permitted",
    ):
        construct_governed_corporate_actions_package(
            request(
                governed_policy,
                event(source_id="unknown-source"),
            ),
            governed_policy,
        )


def test_disallowed_kind_is_rejected() -> None:
    governed_policy = policy(
        permitted_kinds=(CorporateActionKind.CASH_DIVIDEND,)
    )

    with pytest.raises(
        CorporateActionValidationError,
        match="kind is not permitted",
    ):
        construct_governed_corporate_actions_package(
            request(governed_policy, event()),
            governed_policy,
        )


def test_evidence_is_required() -> None:
    governed_policy = policy()

    with pytest.raises(
        CorporateActionValidationError,
        match="evidence references are required",
    ):
        construct_governed_corporate_actions_package(
            request(
                governed_policy,
                event(evidence_references=()),
            ),
            governed_policy,
        )


def test_ratio_must_be_positive() -> None:
    governed_policy = policy()

    with pytest.raises(
        CorporateActionValidationError,
        match="greater than zero",
    ):
        construct_governed_corporate_actions_package(
            request(
                governed_policy,
                event(ratio_denominator="0"),
            ),
            governed_policy,
        )


def test_future_announcement_is_rejected() -> None:
    governed_policy = policy()

    with pytest.raises(
        CorporateActionValidationError,
        match="after the request as_of",
    ):
        construct_governed_corporate_actions_package(
            request(
                governed_policy,
                event(announced_at="2026-07-25T00:00:00Z"),
            ),
            governed_policy,
        )


def test_policy_identity_mismatch_is_rejected() -> None:
    first_policy = policy()
    second_policy = policy(require_evidence_references=False)

    with pytest.raises(
        CorporateActionValidationError,
        match="policy mismatch",
    ):
        construct_governed_corporate_actions_package(
            request(first_policy, event()),
            second_policy,
        )


def test_comparison_detects_added_removed_and_changed_events() -> None:
    governed_policy = policy()
    before = construct_governed_corporate_actions_package(
        request(
            governed_policy,
            event(),
            event(
                action_id="remove-me",
                effective_at="2026-08-03T00:00:00Z",
            ),
        ),
        governed_policy,
    )
    after = construct_governed_corporate_actions_package(
        request(
            governed_policy,
            event(ratio_numerator="5"),
            event(
                action_id="add-me",
                kind=CorporateActionKind.SYMBOL_CHANGE,
                effective_at="2026-08-04T00:00:00Z",
                new_symbol="AAPLX",
                ratio_numerator=None,
                ratio_denominator=None,
            ),
        ),
        governed_policy,
    )

    comparison = compare_corporate_actions_packages(before, after)
    assert comparison.added_action_ids == ("add-me",)
    assert comparison.removed_action_ids == ("remove-me",)
    assert comparison.changed_action_ids == ("split-001",)


def test_comparison_requires_same_instrument() -> None:
    governed_policy = policy()
    first = construct_governed_corporate_actions_package(
        request(governed_policy, event()),
        governed_policy,
    )
    other_event = event(instrument_id="MSFT")
    other_request = request(
        governed_policy,
        other_event,
        instrument_id="MSFT",
    )
    second = construct_governed_corporate_actions_package(
        other_request,
        governed_policy,
    )

    with pytest.raises(
        CorporateActionValidationError,
        match="same instrument",
    ):
        compare_corporate_actions_packages(first, second)


def test_audit_helpers_are_read_only() -> None:
    governed_policy = policy()
    package = construct_governed_corporate_actions_package(
        request(governed_policy, event()),
        governed_policy,
    )

    assert list_events(package) == package.events
    assert list_quality_reasons(package) == package.quality_reasons
    assert list_readiness_blockers(package) == package.readiness_blockers
    assert inspect_provenance(package) == package.provenance


def test_tampering_breaks_identity_verification() -> None:
    governed_policy = policy()
    package = construct_governed_corporate_actions_package(
        request(governed_policy, event()),
        governed_policy,
    )
    assert verify_package_identity(package)

    object.__setattr__(
        package,
        "quality_reasons",
        ("tampered",),
    )

    assert not verify_package_identity(package)
