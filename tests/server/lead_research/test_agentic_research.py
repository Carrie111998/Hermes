from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from server.agent_service import AgentRunService, StubRunExecutor
from server.db import Database, json_dump, now
from server.lead_research.agentic import AgenticResearchService
from server.lead_research.models import (
    AgenticResearchRequest,
    AgenticResearchResult,
    CampaignConfig,
    EvidenceSpan,
    LeadCandidate,
    ProposedFact,
    ResearchBatch,
    ResearchGap,
    ResearchGapPlan,
    ResearchPage,
)


@pytest.fixture()
def harness(tmp_path):
    db = Database(tmp_path / "agentic.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_a", "Tenant A", "active", "{}", stamp, stamp),
    )
    db.execute(
        "INSERT INTO organizations("
        "id,company_id,display_name,normalized_name,domain,country,data,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?,?)",
        ("org_1", "cmp_a", "Acme GmbH", "acme gmbh", "acme.test", "DE", "{}", stamp, stamp),
    )
    config = CampaignConfig(
        name="Agentic gaps",
        target_countries=["DE"],
        product_terms=["industrial valve"],
        enabled_source_ids=["brightdata-web"],
        enrichment={
            "enabled": True,
            "research_each_lead": True,
            "model_profile": "model-decision",
            "extractor_model_profile": "model-cheap",
        },
    )
    db.execute(
        "INSERT INTO research_campaigns("
        "id,company_id,name,status,version,config,created_at,updated_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        ("rc_1", "cmp_a", config.name, "running", 1,
         json_dump(config.model_dump(mode="json")), stamp, stamp),
    )
    runs = AgentRunService(db, StubRunExecutor())
    return db, runs, AgenticResearchService(db, runs=runs)


def candidate(**updates) -> LeadCandidate:
    values = {
        "organization_id": "org_1",
        "display_name": "Acme GmbH",
        "domain": "acme.test",
        "country": "DE",
        "qualifying_evidence": [],
        "fit_score": 12,
        "priority_band": "C",
    }
    values.update(updates)
    return LeadCandidate(**values)


def gap_plan() -> ResearchGapPlan:
    return ResearchGapPlan(
        organization_id="org_1",
        gaps=[ResearchGap(
            dimension="buyer_channel_fit",
            weight=20,
            fields=["buyer_role", "buyer_type"],
            route="agentic",
            required=True,
            agentic_fields=["buyer_role", "buyer_type"],
        )],
        batches=[ResearchBatch(
            source_hint="official_site",
            fields=["buyer_role", "buyer_type", "company_size", "website"],
            route="agentic",
        )],
    )


def test_agentic_gap_run_is_durable_and_uses_configured_models(harness):
    _, runs, agentic = harness

    ref = agentic.enqueue(
        "cmp_a",
        "rc_1",
        candidate(),
        gap_plan(),
        decision_model="model-decision",
        extractor_model="model-cheap",
    )
    row = runs.get("cmp_a", ref.run_id)

    assert row["run_type"] == "lead_research_gap"
    assert row["payload"]["decision_model"] == "model-decision"
    assert row["payload"]["extractor_model"] == "model-cheap"
    assert row["status"] == "queued"


def test_accepted_page_stores_incidental_schema_known_facts(harness):
    db, runs, agentic = harness
    ref = agentic.enqueue(
        "cmp_a", "rc_1", candidate(), gap_plan(),
        decision_model="model-decision", extractor_model="model-cheap",
    )
    content = (
        "Acme GmbH is a distributor. Company size: 120 employees. "
        "Website: acme.test"
    )

    def proposed(field, value, literal):
        start = content.index(literal)
        return ProposedFact(
            field=field,
            value_en=value,
            original_text=literal,
            source_language="en",
            derivation_kind="observed",
            confidence=.9,
            validation_basis="agent extraction pending exact-span check",
            page_id="page_1",
            span=EvidenceSpan(original=literal, start=start, end=start + len(literal)),
            observed_at=1_700_000_000.0,
        )

    output = AgenticResearchResult(
        pages=[ResearchPage(
            page_id="page_1",
            source_id="agentic-web",
            canonical_url="https://acme.test/about",
            snapshot_content=content,
            raw_hash=hashlib.sha256(content.encode()).hexdigest(),
            source_language="en",
            source_class="official",
            visibility="public",
            retrieved_at=datetime.now(timezone.utc),
        )],
        facts=[
            proposed("buyer_role", "distributor", "distributor"),
            proposed("company_size", "120 employees", "120 employees"),
            proposed("website", "acme.test", "acme.test"),
        ],
        unresolved_fields=[],
        requests_started=1,
        tokens_used=300,
        stop_reason="required_coverage",
    )
    db.execute(
        "UPDATE agent_runs SET status='succeeded',output=?,completed_at=?,updated_at=? WHERE id=?",
        (json_dump(output.model_dump(mode="json")), now(), now(), ref.run_id),
    )

    facts = agentic.accept_result("cmp_a", ref.run_id)

    assert {fact.field for fact in facts} >= {"buyer_role", "company_size", "website"}
    assert all(fact.mechanically_validated for fact in facts)


def test_terminal_veto_skips_agentic_run(harness):
    _, _, agentic = harness
    terminal = candidate(qualifying_evidence=[{
        "field": "lifecycle_status", "value": "closed", "status": "observed",
    }])

    assert agentic.enqueue_if_needed("cmp_a", "rc_1", terminal, gap_plan()) is None


def test_low_current_fit_does_not_prune_missing_weighted_research(harness):
    _, _, agentic = harness

    ref = agentic.enqueue_if_needed("cmp_a", "rc_1", candidate(fit_score=1), gap_plan())

    assert ref is not None
    assert ref.run_id.startswith("run_")


