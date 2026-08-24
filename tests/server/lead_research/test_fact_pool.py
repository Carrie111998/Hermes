from __future__ import annotations

import pytest

from server.db import Database, json_dump, now
from server.lead_research.facts import FactRepository
from server.lead_research.models import EvidenceSpan, ResearchFact


NOW = 2_000_000_000.0


@pytest.fixture()
def fact_repo(tmp_path):
    db = Database(tmp_path / "facts.db")
    stamp = now()
    for company_id in ("cmp_a", "cmp_b"):
        db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (company_id, company_id, "active", "{}", stamp, stamp),
        )
    for organization_id, company_id in (("org_a", "cmp_a"), ("org_b", "cmp_b")):
        db.execute(
            "INSERT INTO organizations("
            "id,company_id,display_name,normalized_name,domain,country,data,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?)",
            (organization_id, company_id, "Acme Handel GmbH", "acme handel gmbh",
             "acme-handel.example", "DE", json_dump({}), stamp, stamp),
        )
    return FactRepository(db)


def fact_fixture(**updates):
    values = {
        "organization_id": "org_a",
        "field": "buyer_role",
        "value_en": "distributor",
        "original_text": "Vertriebspartner",
        "source_language": "de",
        "derivation_kind": "translated",
        "status": "observed",
        "confidence": .95,
        "validation_basis": "exact public authority span",
        "evidence_id": "ev_1",
        "span": EvidenceSpan(original="Vertriebspartner", start=0, end=17),
        "source_class": "official",
        "visibility": "public",
        "mechanically_validated": True,
        "observed_at": NOW - 100,
        "retrieved_at": NOW - 50,
        "expires_at": NOW + 10_000,
    }
    values.update(updates)
    return ResearchFact(**values)


def test_only_validated_public_authority_promotes_to_shared(fact_repo):
    shared = fact_repo.accept("cmp_a", fact_fixture())
    registry = fact_repo.accept(
        "cmp_a",
        fact_fixture(evidence_id="ev_registry", value_en="registry value",
                     source_class="registry"),
    )
    licensed = fact_repo.accept(
        "cmp_a",
        fact_fixture(evidence_id="ev_2", value_en="licensed value",
                     source_class="licensed", visibility="licensed"),
    )
    inferred = fact_repo.accept(
        "cmp_a",
        fact_fixture(evidence_id="ev_3", value_en="unvalidated value",
                     mechanically_validated=False),
    )
    ordinary_public = fact_repo.accept(
        "cmp_a",
        fact_fixture(evidence_id="ev_public", value_en="ordinary public value",
                     source_class="public"),
    )

    assert shared.pool == registry.pool == "shared"
    assert licensed.pool == inferred.pool == ordinary_public.pool == "tenant"


def test_reusable_filters_fields_status_and_expiry(fact_repo):
    fact_repo.accept("cmp_a", fact_fixture())
    fact_repo.accept(
        "cmp_a", fact_fixture(evidence_id="ev_expired", field="store_count",
                               value_en=25, expires_at=NOW - 1),
    )
    fact_repo.accept(
        "cmp_a", fact_fixture(evidence_id="ev_unknown", value_en="unknown",
                               status="unknown", mechanically_validated=False),
    )

    facts = fact_repo.reusable("cmp_a", "org_a", {"buyer_role", "store_count"}, NOW)

    assert [(fact.field, fact.value_en) for fact in facts] == [
        ("buyer_role", "distributor")
    ]


def test_accepting_same_fact_is_idempotent(fact_repo):
    first = fact_repo.accept("cmp_a", fact_fixture())
    second = fact_repo.accept("cmp_a", fact_fixture())

    assert first.id == second.id
    assert fact_repo.db.one("SELECT COUNT(*) AS n FROM shared_facts")["n"] == 1
