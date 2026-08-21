"""Work that grew with the tenant instead of with the batch.

Two accidental scans. Finding a lead by organization read every lead row and
decoded every JSON payload, once per qualifying candidate — 396 candidates
against 173 leads on the measured run, growing with the square of the tenant.
And candidate selection fetched superseded corpus rows, decoded them and
term-matched them before discarding them, so a tenant holding a corrected corpus
beside its original paid twice for every row to use half of them.

These tests pin the shape of the work, not its speed: a timing assertion would
be flaky on a loaded machine and would not say what went wrong.
"""
from __future__ import annotations

import json

import pytest

from server.db import Database, json_dump, now
from server.lead_research.candidates import CandidateRepository
from server.lead_research.models import (
    CampaignConfig,
    DatasetDefinition,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService


class CountingDatabase:
    """Wraps a Database and records every statement executed through it."""

    def __init__(self, inner):
        self.inner = inner
        self.queries: list[str] = []

    def _record(self, sql):
        self.queries.append(" ".join(str(sql).split()))

    def all(self, sql, params=()):
        self._record(sql)
        return self.inner.all(sql, params)

    def one(self, sql, params=()):
        self._record(sql)
        return self.inner.one(sql, params)

    def execute(self, sql, params=()):
        self._record(sql)
        return self.inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def matching(self, fragment: str) -> int:
        return sum(1 for sql in self.queries if fragment in sql)


class SimpleVerifier:
    def __init__(self, definition):
        self.definition = definition

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[VerificationSource(
                provenance_url=f"https://registry.example/{candidate.source_record_id}",
                raw_hash="e" * 64,
                classification="independent",
                retrieved_via="https://search.example",
                facts={
                    "company_name": [candidate.company_name],
                    "country": [candidate.country],
                    "buyer_role": ["distributor"],
                },
            )],
            independent_source_count=1,
            requests=1,
        )


def _definition() -> DatasetDefinition:
    return DatasetDefinition(
        source_id="simple-source",
        display_name="Simple source",
        publisher="Tests",
        access_tier="public",
        entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        emits=["company_name", "country", "buyer_role"],
        adapter_mode="live",
        default_enabled=True,
    )


@pytest.fixture()
def harness(tmp_path):
    db = Database(tmp_path / "shape.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_1", "Tenant", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers", "1", "candidates.jsonl",
        b"\n".join(
            json.dumps({
                "source_record_id": f"buyer-de-{index:02d}",
                "company_name": f"Buyer {index} DE",
                "country": "DE",
                "categories": ["household-appliances"],
            }).encode()
            for index in range(6)
        ),
    )
    return db


def _run(db, *, campaign_id="camp_1", verify_workers=1, reuse=False):
    definition = _definition()
    config = CampaignConfig(
        name="German appliance distributors",
        target_countries=["DE"],
        sector_ids=["household-appliances"],
        buyer_types=["distributor"],
        enabled_source_ids=[definition.source_id],
        refresh={"schedule": "monthly", "reuse_public_cache": reuse},
    )
    stamp = now()
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, "cmp_1", config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    service = LeadResearchService(
        db,
        registry=ProviderRegistry([definition], {definition.source_id: SimpleVerifier(definition)}),
        verify_workers=verify_workers,
    )
    return service.run("cmp_1", campaign_id)


# ── the lead lookup ───────────────────────────────────────────────────────────

def test_the_lead_table_is_read_once_per_run_not_once_per_candidate(harness):
    """The regression this file exists for.

    Six candidates all qualify here. Every one of them used to trigger its own
    full read of the lead table.
    """
    counting = CountingDatabase(harness)

    result = _run(counting)

    assert result["metrics"]["qualified_leads"] == 6
    assert counting.matching("FROM leads WHERE company_id=?") == 1


def test_two_candidates_resolving_to_one_company_still_share_a_lead(harness):
    """The index has to stay current inside the run, not just at its start.

    A lead inserted by an earlier candidate is not in the snapshot the run began
    with, so an index built once and never updated would insert a duplicate.
    """
    definition = _definition()

    class OneCompany(SimpleVerifier):
        """Every candidate turns out to be the same company."""

        def verify(self, query, candidate):
            bundle = super().verify(query, candidate)
            return VerificationBundle(
                candidate_source_record_id=candidate.source_record_id,
                sources=[bundle.sources[0].model_copy(update={"facts": {
                    "company_name": ["One Company GmbH"],
                    "country": ["DE"],
                    "buyer_role": ["distributor"],
                }})],
                independent_source_count=1,
                requests=1,
            )

    config = CampaignConfig(
        name="One company", target_countries=["DE"],
        sector_ids=["household-appliances"], buyer_types=["distributor"],
        enabled_source_ids=[definition.source_id],
        refresh={"schedule": "monthly", "reuse_public_cache": False},
    )
    stamp = now()
    harness.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("camp_1", "cmp_1", config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    LeadResearchService(
        harness,
        registry=ProviderRegistry([definition], {definition.source_id: OneCompany(definition)}),
        verify_workers=1,
    ).run("cmp_1", "camp_1")

    assert harness.one("SELECT COUNT(*) AS n FROM leads WHERE company_id='cmp_1'")["n"] == 1


def test_a_rerun_updates_the_lead_it_already_created(harness):
    _run(harness)
    _run(harness, campaign_id="camp_2")

    assert harness.one("SELECT COUNT(*) AS n FROM leads WHERE company_id='cmp_1'")["n"] == 6


# ── candidate selection ───────────────────────────────────────────────────────

def test_a_superseded_corpus_version_is_not_fetched_at_all(harness):
    """Filtered in the query, not in the loop.

    A superseded row used to be fetched, JSON-decoded and term-matched before
    being thrown away.
    """
    repo = CandidateRepository(harness)
    repo.import_file(
        "buyers", "2", "candidates.jsonl",
        b"\n".join(
            json.dumps({
                "source_record_id": f"buyer-de-{index:02d}",
                "company_name": f"Buyer {index} DE v2",
                "country": "DE",
                "categories": ["household-appliances"],
            }).encode()
            for index in range(6)
        ),
    )
    counting = CountingDatabase(harness)

    selected = CandidateRepository(counting).select(
        countries=["DE"], product_terms=[], limit=10,
    )

    assert {record.version for record in selected} == {"2"}
    selection = next(
        sql for sql in counting.queries if "FROM candidate_records WHERE" in sql
    )
    assert "version=?" in selection, "the version filter never reached the query"


def test_selection_still_honours_country_terms_limit_and_exclusions(harness):
    """The predicate moved; nothing else about selection may change."""
    repo = CandidateRepository(harness)

    assert len(repo.select(countries=["DE"], product_terms=[], limit=3)) == 3
    assert repo.select(countries=["AT"], product_terms=[], limit=10) == []
    assert len(repo.select(
        countries=["DE"], product_terms=["household-appliances"], limit=10,
    )) == 6
    assert repo.select(countries=["DE"], product_terms=["nonsense"], limit=10) == []
    assert len(repo.select(
        countries=["DE"], product_terms=[], limit=10,
        exclude={("buyer 0 de", "DE"), ("buyer 1 de", "DE")},
    )) == 4


def test_selection_before_any_import_returns_nothing_rather_than_everything(tmp_path):
    """An empty corpus has no current versions, so the predicate would be empty."""
    db = Database(tmp_path / "empty.db")

    assert CandidateRepository(db).select(countries=["DE"], product_terms=[], limit=5) == []


# ── the reuse lookup added with the evidence cache ────────────────────────────

def test_evidence_reuse_reads_once_per_run_on_an_index(harness):
    counting = CountingDatabase(harness)
    _run(counting, reuse=True)
    counting.queries.clear()

    _run(counting, campaign_id="camp_2", reuse=True)

    # Matched on the reuse projection specifically. The other statements against
    # this table are `save_evidence` existence checks, which are one point lookup
    # per evidence row on the table's UNIQUE constraint — an N+1 in count, but
    # each O(log n) and free next to a fetch, so deliberately left alone.
    assert counting.matching("payload,retrieved_at FROM evidence_records") == 1
    plan = [
        dict(row).get("detail") for row in harness.all(
            "EXPLAIN QUERY PLAN SELECT source_id,source_record_id,provenance_url,raw_hash,"
            "payload,retrieved_at FROM evidence_records WHERE company_id=? AND withdrawn_at IS NULL "
            "AND source_id IN (?) AND retrieved_at>=? ORDER BY retrieved_at",
            ("cmp_1", "simple-source", 0.0),
        )
    ]
    assert any("ix_research_evidence_reuse" in str(detail) for detail in plan), plan
