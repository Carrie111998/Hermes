"""Matching an identity when no source supplies an identifier.

Resolution used to key on a domain or a registry id only. Nothing emits a
registry id, and a domain arrives only from an official-classified page — TED
carries one for 40 of 201 rows and classifies every source independent — so a
company whose evidence named it but did not link it got a brand-new
organization on every run. That duplicated the company, duplicated its lead,
and hid the tenant's own prior claims from the run that needed them.
"""
from __future__ import annotations

import pytest

from server.db import Database
from server.lead_research.identity import IdentityResolver


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path / "identity.db")
    database.initialize()
    database.execute(
        "INSERT INTO companies(id,name,created_at,updated_at) VALUES(?,?,?,?)",
        ("cmp_1", "Tenant", 0.0, 0.0),
    )
    database.execute(
        "INSERT INTO companies(id,name,created_at,updated_at) VALUES(?,?,?,?)",
        ("cmp_2", "Other tenant", 0.0, 0.0),
    )
    return database


def _payload(**overrides) -> dict:
    return {"display_name": "Pro Horeca SRL", "country": "RO", **overrides}


def test_the_same_company_verified_twice_without_a_domain_stays_one_identity(db):
    """The regression this file exists for."""
    resolver = IdentityResolver(db, "cmp_1")

    first = resolver.resolve(_payload(), "ted")
    second = resolver.resolve(_payload(), "ted")

    assert second["organization_id"] == first["organization_id"]
    assert second["created"] is False
    assert second["matched_by"] == "name_country"
    assert db.one("SELECT COUNT(*) AS n FROM organizations")["n"] == 1


def test_a_later_run_that_learns_a_domain_links_the_identity_it_already_had(db):
    """Name matching has to bootstrap toward a strong identifier, not replace it."""
    resolver = IdentityResolver(db, "cmp_1")
    first = resolver.resolve(_payload(), "ted")

    second = resolver.resolve(_payload(domain="https://prohoreca.ro"), "brightdata-web")

    assert second["organization_id"] == first["organization_id"]
    links = db.all(
        "SELECT identifier_type,identifier_value FROM organization_links WHERE company_id='cmp_1'"
    )
    assert [(row["identifier_type"], row["identifier_value"]) for row in links] == [
        ("domain", "prohoreca.ro")
    ]
    # And the domain is now the match, not the name.
    third = resolver.resolve(
        {"display_name": "Renamed Pro Horeca", "country": "RO", "domain": "prohoreca.ro"},
        "brightdata-web",
    )
    assert third["organization_id"] == first["organization_id"]
    assert third["matched_by"] == "domain"


def test_two_companies_sharing_a_name_in_one_market_are_not_merged(db):
    """Different verified domains mean two companies, whatever they are called."""
    resolver = IdentityResolver(db, "cmp_1")

    first = resolver.resolve(_payload(domain="https://prohoreca.ro"), "brightdata-web")
    second = resolver.resolve(_payload(domain="https://pro-horeca-cluj.ro"), "brightdata-web")

    assert second["organization_id"] != first["organization_id"]
    assert second["created"] is True
    assert db.one("SELECT COUNT(*) AS n FROM organizations")["n"] == 2


def test_the_same_name_in_another_market_is_another_company(db):
    resolver = IdentityResolver(db, "cmp_1")

    romanian = resolver.resolve(_payload(country="RO"), "ted")
    czech = resolver.resolve(_payload(country="CZ"), "ted")

    assert czech["organization_id"] != romanian["organization_id"]


def test_a_name_with_no_country_never_matches(db):
    """A name without a market is not an identity, so it must not claim one."""
    resolver = IdentityResolver(db, "cmp_1")
    resolver.resolve(_payload(), "ted")

    unplaced = resolver.resolve({"display_name": "Pro Horeca SRL"}, "ted")

    assert unplaced["created"] is True
    assert db.one("SELECT COUNT(*) AS n FROM organizations")["n"] == 2


def test_the_candidate_row_can_supply_the_market_the_verifier_did_not(db):
    """A hint may locate an identity; it still never becomes a stored fact.

    Verifiers routinely name a company without stating its country — TED's
    fixture shape and any snippet that omits the country do exactly this —
    while the corpus row always carries a validated ISO code.
    """
    resolver = IdentityResolver(db, "cmp_1")
    first = resolver.resolve(_payload(country=None), "ted")

    second = resolver.resolve(
        {"display_name": "Pro Horeca SRL"}, "ted",
        matching_hints={"country": "RO", "display_name": "Pro Horeca", "domain": None},
    )

    assert second["organization_id"] == first["organization_id"]
    assert second["matched_by"] == "name_country"
    stored = db.one("SELECT country FROM organizations WHERE id=?", (first["organization_id"],))
    assert stored["country"] is None, "a hint country must not be stored as a verified fact"


def test_a_recorded_market_still_blocks_a_cross_market_match(db):
    """The permissive rule applies only while the stored market is unknown."""
    resolver = IdentityResolver(db, "cmp_1")
    romanian = resolver.resolve(_payload(country="RO"), "ted")

    czech = resolver.resolve(
        {"display_name": "Pro Horeca SRL"}, "ted",
        matching_hints={"country": "CZ", "display_name": "Pro Horeca", "domain": None},
    )

    assert czech["organization_id"] != romanian["organization_id"]


def test_another_tenants_identity_is_never_matched(db):
    IdentityResolver(db, "cmp_2").resolve(_payload(), "ted")

    mine = IdentityResolver(db, "cmp_1").resolve(_payload(), "ted")

    assert mine["created"] is True
    assert db.one(
        "SELECT COUNT(*) AS n FROM organizations WHERE company_id='cmp_1'"
    )["n"] == 1


def test_country_case_does_not_split_an_identity(db):
    resolver = IdentityResolver(db, "cmp_1")
    first = resolver.resolve(_payload(country="RO"), "ted")

    second = resolver.resolve(_payload(country="ro"), "ted")

    assert second["organization_id"] == first["organization_id"]


def test_a_matched_identity_keeps_its_prior_facts(db):
    """A refresh merges verified facts; it must not erase what was there."""
    resolver = IdentityResolver(db, "cmp_1")
    resolver.resolve(_payload(registry_id="REG-1"), "ted")

    resolver.resolve(_payload(domain="https://prohoreca.ro"), "brightdata-web")

    organization = db.one("SELECT data FROM organizations WHERE company_id='cmp_1'")
    from server.db import json_load
    data = json_load(organization["data"], {})
    assert data["registry_id"] == "REG-1"
    assert data["domain"] == "prohoreca.ro"
