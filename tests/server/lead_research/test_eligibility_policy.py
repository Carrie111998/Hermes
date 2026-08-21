"""The campaign's eligibility policy has to be the policy that runs.

Every switch here is one the campaign editor already renders. They were
collected, stored, and ignored: `EligibilityService` hardcoded its gates and
never read `config.eligibility`, so "Require official domain" did nothing and
"Require buyer role" could not be turned off. These tests pin each switch to a
visible change in outcome, which is the only thing that makes the control real.
"""
from __future__ import annotations

from server.lead_research.models import CampaignConfig
from server.lead_research.qualification import EligibilityService


def _config(**overrides) -> CampaignConfig:
    eligibility = {
        "require_resolved_identity": True, "require_official_domain": False,
        "require_target_presence": True, "require_buyer_role": True,
        "exclude_inactive": True, "minimum_independent_sources": 1,
        **overrides.pop("eligibility", {}),
    }
    return CampaignConfig(
        name="Policy fixture",
        target_countries=["RO"],
        sector_ids=["household-appliances"],
        buyer_types=["distributor"],
        enabled_source_ids=["ted"],
        eligibility=eligibility,
        **overrides,
    )


def _candidate(**overrides) -> dict:
    return {
        "organization_id": "org_1",
        "country": "RO",
        "domain": "buyer.example",
        "sector_ids": ["household-appliances"],
        "buyer_types": ["distributor"],
        "official_domains": ["buyer.example"],
        "independent_domain_count": 1,
        **overrides,
    }


def _evaluate(candidate: dict, config: CampaignConfig):
    return EligibilityService().evaluate(candidate, config)


def test_the_default_policy_qualifies_a_fully_evidenced_company():
    assert _evaluate(_candidate(), _config()).eligible


def test_requiring_an_official_domain_rejects_a_company_without_one():
    config = _config(eligibility={"require_official_domain": True})

    assert not _evaluate(_candidate(official_domains=[]), config).eligible
    assert _evaluate(_candidate(official_domains=[]), _config()).eligible, (
        "the same company must still qualify when the switch is off"
    )


def test_the_independent_source_minimum_is_enforced_at_its_configured_value():
    strict = _config(eligibility={"minimum_independent_sources": 2})

    assert not _evaluate(_candidate(independent_domain_count=1), strict).eligible
    assert _evaluate(_candidate(independent_domain_count=2), strict).eligible


def test_a_zero_minimum_switches_the_source_gate_off_rather_than_failing_it():
    config = _config(eligibility={"minimum_independent_sources": 0})
    gate = _evaluate(_candidate(independent_domain_count=0), config)

    assert gate.eligible
    assert gate.gates["independent_sources"] == "not_required"


def test_turning_the_buyer_role_gate_off_stops_it_rejecting():
    config = _config(eligibility={"require_buyer_role": False})
    gate = _evaluate(_candidate(buyer_types=[]), config)

    assert gate.eligible
    assert gate.gates["buyer_role"] == "not_required"


def test_turning_the_geography_gate_off_admits_an_out_of_market_company():
    config = _config(eligibility={"require_target_presence": False})

    assert not _evaluate(_candidate(country="PL"), _config()).eligible
    assert _evaluate(_candidate(country="PL"), config).eligible


def test_an_unresolved_identity_is_rejected_unless_the_switch_says_otherwise():
    assert not _evaluate(_candidate(organization_id=None), _config()).eligible
    assert _evaluate(
        _candidate(organization_id=None),
        _config(eligibility={"require_resolved_identity": False}),
    ).eligible


def test_a_company_observed_closed_is_rejected_while_exclude_inactive_is_on():
    """Closure also has to reject here, not only skip selection.

    A company that closed since the last run is still selected — the skip set is
    built from prior claims — so this gate is where the new evidence lands.
    """
    assert not _evaluate(_candidate(lifecycle_status="closed"), _config()).eligible
    assert _evaluate(
        _candidate(lifecycle_status="closed"),
        _config(eligibility={"exclude_inactive": False}),
    ).eligible


def test_an_operating_company_passes_the_lifecycle_gate():
    assert _evaluate(_candidate(lifecycle_status="operating"), _config()).eligible


def test_an_excluded_domain_is_rejected_however_it_is_written():
    """The list is hand-maintained, so it is compared on the normalized host."""
    config = _config(exclusions={"domains": ["https://WWW.Buyer.example/contact"]})

    gate = _evaluate(_candidate(), config)

    assert not gate.eligible
    assert gate.reasons == ["exclusion_list"]


def test_an_excluded_company_id_is_rejected():
    config = _config(exclusions={"company_ids": ["org_1"]})

    assert not _evaluate(_candidate(), config).eligible


def test_an_unrelated_exclusion_entry_changes_nothing():
    config = _config(exclusions={"domains": ["someone-else.example"], "company_ids": ["org_9"]})

    assert _evaluate(_candidate(), config).eligible


def test_compliance_reports_unknown_rather_than_claiming_a_screening_nobody_ran():
    """No sanctions source is configured; "pass" would assert a check happened."""
    gate = _evaluate(_candidate(), _config())

    assert gate.gates["compliance"] == "unknown"
    assert gate.eligible, "an unknown compliance state must not block"


def test_a_sanctioned_candidate_still_fails_once_something_marks_it():
    assert not _evaluate(_candidate(sanctioned=True), _config()).eligible


def test_a_switched_off_gate_stays_visible_in_the_record():
    """The gate map is the record of why a company qualified.

    A gate that vanishes when it is switched off is indistinguishable from one
    that passed, which makes a stored verdict unreadable after a policy change.
    """
    gate = _evaluate(_candidate(), _config(eligibility={
        "require_buyer_role": False, "require_target_presence": False,
    }))

    assert gate.gates["buyer_role"] == "not_required"
    assert gate.gates["target_geography"] == "not_required"
