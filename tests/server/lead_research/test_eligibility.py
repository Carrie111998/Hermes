"""What the buyer-role gate accepts.

`EligibilityService` intersects the roles a campaign asks for with the roles
observed for a company. Verifiers and campaigns do not share one vocabulary —
TED can only state "public procurement supplier", while a campaign asks in its
sector's terms — so a bare intersection rejected every candidate for a reason
that had nothing to do with the company. These tests pin the bridge, and pin
that it stops short of roles the evidence does not support.
"""
from __future__ import annotations

import pytest

from server.lead_research.models import CampaignConfig
from server.lead_research.qualification import EligibilityService, satisfies_buyer_role


def _config(buyer_types: list[str]) -> CampaignConfig:
    return CampaignConfig(
        name="Eligibility fixture",
        target_countries=["RO"],
        sector_ids=["household-appliances"],
        buyer_types=buyer_types,
        enabled_source_ids=["ted"],
    )


def _candidate(buyer_types: list[str], **overrides) -> dict:
    return {
        "organization_id": "org_1",
        "country": "RO",
        "sector_ids": ["household-appliances"],
        "buyer_types": buyer_types,
        # The default policy asks for one independent source, so every fixture
        # that is testing something else has to satisfy it. Coverage has its
        # own tests in test_eligibility_policy.py.
        "independent_domain_count": 1,
        **overrides,
    }


def test_a_matching_role_passes_directly():
    assert satisfies_buyer_role({"distributor"}, {"distributor", "importer"})


def test_role_spelling_does_not_decide_eligibility():
    """Sectors write "procurement_organization"; prose writes it with a space."""
    assert satisfies_buyer_role({"Procurement Organization"}, {"procurement_organization"})


@pytest.mark.parametrize(
    "requested", ["distributor", "importer", "retailer", "wholesaler", "procurement_organization"]
)
def test_a_public_procurement_supplier_answers_any_reselling_role(requested):
    """Winning a public supply contract proves the company resells these goods."""
    assert satisfies_buyer_role({"public procurement supplier"}, {requested})


@pytest.mark.parametrize("requested", ["brand", "manufacturer"])
def test_a_public_procurement_supplier_is_not_a_maker(requested):
    """The bridge must not invent ownership or production it cannot evidence."""
    assert not satisfies_buyer_role({"public procurement supplier"}, {requested})


def test_an_unknown_role_never_qualifies_by_accident():
    assert not satisfies_buyer_role({"landlord"}, {"distributor"})


def test_nothing_observed_cannot_satisfy_the_gate():
    assert not satisfies_buyer_role(set(), {"distributor"})


def test_the_sector_brief_qualifies_a_ted_verified_company():
    """The end-to-end shape of the trap.

    A customer brief sends the sector's own roles, and TED-derived rows carry
    "public procurement supplier". Before the bridge this intersection was
    empty, so a search over a procurement corpus qualified nothing at all.
    """
    config = _config(["importer", "distributor", "retailer", "brand", "wholesaler"])
    gate = EligibilityService().evaluate(_candidate(["public procurement supplier"]), config)

    assert gate.eligible
    assert gate.gates["buyer_role"] == "pass"


def test_a_company_with_no_observed_role_is_rejected_not_assumed():
    config = _config(["importer", "distributor"])
    gate = EligibilityService().evaluate(_candidate([]), config)

    assert not gate.eligible
    assert gate.reasons == ["buyer_role"]
