"""Which companies a rerun leaves alone.

Verifying one candidate costs three Web Unlocker fetches, so a campaign that
re-researches everything it already settled is the dominant cost in the system.
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


def _claim(db, company_id, organization_id, field, value, *, age_days=0.0):
    db.execute(
        "INSERT INTO feature_claims VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id("claim"), company_id, None, organization_id, field, "observed",
         json_dump(value), 0.9, "observed", "[]", "{}", now() - age_days * 86400),
    )


def test_a_closed_company_is_never_researched_again(db, service):
    organization_id = _organization(db, "cmp_1", "dead trading", "AE")
    _claim(db, "cmp_1", organization_id, "lifecycle_status", "closed")

    skip, closed, validated = service._settled_identities("cmp_1", 30)

    assert skip == {("dead trading", "AE")}
    assert (closed, validated) == (1, 0)


def test_a_later_operating_claim_reopens_a_wrongly_closed_company(db, service):
    """Closure must not be a one-way door: a false positive has to be undoable."""
    organization_id = _organization(db, "cmp_1", "alive trading", "AE")
    _claim(db, "cmp_1", organization_id, "lifecycle_status", "closed", age_days=5)
    _claim(db, "cmp_1", organization_id, "lifecycle_status", "operating")

    skip, closed, _ = service._settled_identities("cmp_1", 30)

    assert skip == set()
    assert closed == 0


def test_a_freshly_validated_company_is_skipped(db, service):
    organization_id = _organization(db, "cmp_1", "known buyer", "SA")
    _claim(db, "cmp_1", organization_id, "country", "SA")
    _claim(db, "cmp_1", organization_id, "buyer_role", "distributor")

    skip, closed, validated = service._settled_identities("cmp_1", 30)

    assert skip == {("known buyer", "SA")}
    assert (closed, validated) == (0, 1)


def test_stale_validation_is_researched_again(db, service):
    organization_id = _organization(db, "cmp_1", "stale buyer", "SA")
    _claim(db, "cmp_1", organization_id, "country", "SA", age_days=90)
    _claim(db, "cmp_1", organization_id, "buyer_role", "distributor", age_days=90)

    assert service._settled_identities("cmp_1", 30)[0] == set()


def test_partial_validation_is_researched_again(db, service):
    """Country alone is not enough to call a company settled."""
    organization_id = _organization(db, "cmp_1", "half known", "IQ")
    _claim(db, "cmp_1", organization_id, "country", "IQ")

    assert service._settled_identities("cmp_1", 30)[0] == set()


def test_another_tenants_validation_never_skips_your_candidates(db, service):
    organization_id = _organization(db, "cmp_other", "known buyer", "SA")
    _claim(db, "cmp_other", organization_id, "country", "SA")
    _claim(db, "cmp_other", organization_id, "buyer_role", "distributor")

    assert service._settled_identities("cmp_1", 30) == (set(), 0, 0)


def test_no_organizations_means_nothing_to_skip(db, service):
    assert service._settled_identities("cmp_1", 30) == (set(), 0, 0)
