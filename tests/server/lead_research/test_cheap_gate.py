from __future__ import annotations

import json

import pytest

from server.db import Database, now
from server.lead_research.acquisition import CheapVerification
from server.lead_research.candidates import CandidateRecord, CandidateRepository
from server.lead_research.discovery import (
    CandidateDiscoveryService,
    CheapGate,
    ResearchScope,
)
from server.lead_research.models import DiscoveryQuery
from server.lead_research.registry import ProviderRegistry
from tests.server.lead_research.test_fact_pool import fact_fixture, fact_repo


class StubFacts:
    def relevance(self, company_id, candidate, product_terms, at=None):
        del company_id, product_terms, at
        return ["ev_shared"] if candidate.data.get("shared_relevance") else []


class StubCheapVerifier:
    def verify(self, candidate, terms):
        del terms
        return CheapVerification(
            matched=bool(candidate.data.get("cheap_verification")),
            evidence_ids=list(candidate.data.get("cheap_evidence_ids", [])),
            requests=int(candidate.data.get("cheap_requests", 0)),
        )


@pytest.fixture()
def gate():
    return CheapGate(StubFacts(), StubCheapVerifier())


def candidate_fixture(**data):
    categories = data.pop("categories", [])
    return CandidateRecord(
        dataset_id="fixture",
        version="1",
        source_record_id=data.pop("source_record_id", "candidate-1"),
        company_name=data.pop("company_name", "Atlas Handel"),
        normalized_name=data.pop("normalized_name", "atlas handel"),
        country=data.pop("country", "DE"),
        domain=data.pop("domain", "atlas.example"),
        data={"categories": categories, **data},
    )


def scope_fixture(**updates):
    values = {"product_terms": ["industrial valve"]}
    values.update(updates)
    return ResearchScope(**values)


@pytest.mark.parametrize(
    ("signal", "reason", "evidence_ids"),
    [
        ({"shared_relevance": True}, "shared_relevance", ["ev_shared"]),
        ({"categories": ["industrial valve distributor"]}, "corpus_term", []),
        (
            {"cheap_verification": True, "cheap_evidence_ids": ["ev_cheap"]},
            "cheap_verification",
            ["ev_cheap"],
        ),
    ],
)
def test_gate_accepts_exactly_three_allowed_signal_classes(
    gate, signal, reason, evidence_ids,
):
    decision = gate.evaluate(
        "cmp_a", candidate_fixture(**signal), scope_fixture(),
    )

    assert decision.passed is True
    assert decision.reason == reason
    assert decision.evidence_ids == evidence_ids


def test_explicit_range_exclusion_precedes_cheap_verification(gate):
    decision = gate.evaluate(
        "cmp_a",
        candidate_fixture(
            explicit_product_ranges=["commercial bakery oven"],
            cheap_verification=True,
        ),
        scope_fixture(),
    )

    assert decision.passed is False
    assert decision.reason == "excluded_by_range"
    assert decision.requests == 0


def test_gate_names_an_unmatched_candidate_and_meters_cheap_check(gate):
    decision = gate.evaluate(
        "cmp_a", candidate_fixture(cheap_requests=1), scope_fixture(),
    )

    assert decision.passed is False
    assert decision.reason == "cheap_verification_no_scope_signal"
    assert decision.requests == 1


def test_gate_reads_validated_relevance_from_the_shared_fact_pool(fact_repo):
    fact_repo.accept(
        "cmp_a",
        fact_fixture(field="product_term", value_en="industrial valve"),
    )
    decision = CheapGate(fact_repo, StubCheapVerifier()).evaluate(
        "cmp_b",
        candidate_fixture(
            company_name="Acme Handel GmbH",
            normalized_name="acme handel gmbh",
            domain="acme-handel.example",
        ),
        scope_fixture(),
    )

    assert decision.passed is True
    assert decision.reason == "shared_relevance"
    assert decision.evidence_ids


def test_private_relevance_does_not_cross_the_gate_into_another_tenant(fact_repo):
    fact_repo.accept(
        "cmp_a",
        fact_fixture(
            field="product_term",
            value_en="industrial valve",
            source_class="customer",
            visibility="private",
        ),
    )
    decision = CheapGate(fact_repo, StubCheapVerifier()).evaluate(
        "cmp_b",
        candidate_fixture(
            company_name="Acme Handel GmbH",
            normalized_name="acme handel gmbh",
            domain="acme-handel.example",
        ),
        scope_fixture(),
    )

    assert decision.passed is False
    assert decision.reason == "cheap_verification_no_scope_signal"


def test_supply_reports_named_excluded_count_before_identity_resolution(tmp_path):
    db = Database(tmp_path / "cheap-gate.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_a", "A", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers",
        "1",
        "buyers.jsonl",
        json.dumps({
            "source_record_id": "bakery-1",
            "company_name": "Unrelated Bakery",
            "country": "DE",
            "categories": ["commercial bakery"],
        }).encode(),
    )
    service = CandidateDiscoveryService(
        db,
        ProviderRegistry([], {}),
        gate=CheapGate(StubFacts(), StubCheapVerifier()),
    )

    result = service.supply(
        "cmp_a",
        DiscoveryQuery(
            campaign_id="rc_1",
            seller_countries=["TR"],
            target_countries=["DE"],
            product_terms=["industrial valve"],
        ),
        10,
    )

    assert result.candidates == []
    assert result.counts["supplied"] == 1
    assert result.counts["cheap_verification_no_scope_signal"] == 1
    assert result.counts["passed_cheap_gate"] == 0
    assert db.one("SELECT COUNT(*) AS n FROM organizations")["n"] == 0
