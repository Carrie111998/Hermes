"""Which companies a campaign leaves alone.

Verifying one candidate costs three Web Unlocker fetches, so cost matters — but
per-candidate cost is bounded by `select(limit=...)`, and skipping an identity
means it is absent from the run's rebuilt results. Only closure survives that
trade: you never want to contact a dissolved company, so losing it from the
output is the point rather than a loss.

Validation state cannot live on the candidate corpus — that table is immutable
and shared across tenants — so it is read back from tenant claims here.
"""
from __future__ import annotations

import pytest

from server.db import Database, json_dump, new_id, now
from server.lead_research.service import LeadResearchService


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "incremental.db")


@pytest.fixture()
def service(db):
    return LeadResearchService(db)


def _tenant(db, company_id: str) -> None:
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (company_id, company_id, "active", "{}", now(), now()),
    )


def _organization(db, company_id: str, name: str, country: str) -> str:
    _tenant(db, company_id)
    organization_id = new_id("org")
    db.execute(
        "INSERT INTO organizations(id,company_id,display_name,normalized_name,country,"
        "data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (organization_id, company_id, name, name, country, "{}", now(), now()),
    )
    return organization_id


def _claim(db, company_id, organization_id, field, value, *, age_days=0.0, campaign_id=None):
    db.execute(
        "INSERT INTO feature_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("claim"), company_id, campaign_id, organization_id, field, "observed",
         json_dump(value), 0.9, "observed", "[]", "{}", now() - age_days * 86400),
    )


def test_a_closed_company_is_never_researched_again(db, service):
    organization_id = _organization(db, "cmp_1", "dead trading", "AE")
    _claim(db, "cmp_1", organization_id, "lifecycle_status", "closed")

    assert service._settled_identities("cmp_1") == ({("dead trading", "AE")}, 1)


def test_a_later_operating_claim_reopens_a_wrongly_closed_company(db, service):
    """Closure must not be a one-way door: a false positive has to be undoable."""
    organization_id = _organization(db, "cmp_1", "alive trading", "AE")
    _claim(db, "cmp_1", organization_id, "lifecycle_status", "closed", age_days=5)
    _claim(db, "cmp_1", organization_id, "lifecycle_status", "operating")

    assert service._settled_identities("cmp_1") == (set(), 0)


def test_closure_is_seen_whichever_campaign_recorded_it(db, service):
    """Production writes claims with a campaign id; the tenant owns closure."""
    organization_id = _organization(db, "cmp_1", "dead trading", "AE")
    _claim(db, "cmp_1", organization_id, "lifecycle_status", "closed", campaign_id="camp_old")

    assert service._settled_identities("cmp_1") == ({("dead trading", "AE")}, 1)


def test_a_validated_company_is_still_offered_to_a_later_campaign(db, service):
    """The regression this file exists for.

    Skipping already-validated identities emptied every campaign after the
    first: the skip was tenant-wide, a run rebuilds its results from scratch,
    and claims written by an earlier campaign are never cleared. A second
    campaign therefore selected nothing and reported zero leads.
    """
    organization_id = _organization(db, "cmp_1", "known buyer", "SA")
    _claim(db, "cmp_1", organization_id, "country", "SA", campaign_id="camp_old")
    _claim(db, "cmp_1", organization_id, "buyer_role", "distributor", campaign_id="camp_old")

    assert service._settled_identities("cmp_1") == (set(), 0)


def test_another_tenants_closure_never_skips_your_candidates(db, service):
    organization_id = _organization(db, "cmp_other", "dead trading", "AE")
    _claim(db, "cmp_other", organization_id, "lifecycle_status", "closed")

    assert service._settled_identities("cmp_1") == (set(), 0)


def test_no_organizations_means_nothing_to_skip(db, service):
    assert service._settled_identities("cmp_1") == (set(), 0)
