from __future__ import annotations

from server.db import Database, now
from server.lead_research.gaps import GapPlanner, gap_query, weighted_dimensions
from server.lead_research.models import (
    CampaignConfig,
    CompanyProfileVersion,
    CompanyResearchProfile,
    LeadCandidate,
    ScoringProfile,
    ScoringWeights,
    SourceCapability,
    StoredFact,
)
from server.lead_research.search_cache import (
    SearchAttemptRepository, query_scope, research_query_hash,
)
from tests.server.lead_research.test_fact_pool import fact_fixture


def profile_version() -> CompanyProfileVersion:
    return CompanyProfileVersion(
        id="cpv_1",
        company_id="cmp_a",
        version=1,
        status="confirmed",
        profile=CompanyResearchProfile(
            identity={"name": "Seller", "website": "https://seller.example"},
            seller_countries=["TR"],
            products=[{
                "id": "prod_1",
                "name": "Industrial valve",
                "english_name": "industrial valve",
                "sector_ids": ["industrial-machinery"],
            }],
            market_preferences={"target_countries": ["DE"]},
        ),
        created_by="usr_1",
        confirmed_by="usr_1",
        created_at=1.0,
        confirmed_at=2.0,
    )


def campaign(weights: dict[str, int] | None = None) -> CampaignConfig:
    scoring = ScoringProfile(
        weights=ScoringWeights(**weights) if weights else ScoringWeights(),
    )
    return CampaignConfig(
        name="Gap plan",
        target_countries=["DE"],
        sector_ids=["industrial-machinery"],
        product_terms=["industrial valve"],
        enabled_source_ids=["public-web"],
        scoring=scoring,
    )


def candidate(domain: str | None = "acme.test") -> LeadCandidate:
    return LeadCandidate(
        organization_id="org_1",
        domain=domain,
        display_name="Acme GmbH",
        country="DE",
        qualifying_evidence=[],
    )


def capability(fields: set[str], source_id: str = "registry") -> SourceCapability:
    return SourceCapability(
        source_id=source_id,
        candidate_discovery=False,
        emitted_fields=frozenset(fields),
        access_class="public",
        authority="registry",
        executable=True,
    )


def stored_fact(field: str) -> StoredFact:
    fact = fact_fixture(field=field, organization_id="org_1")
    return StoredFact(
        **fact.model_dump(),
        id=f"sf_{field}",
        pool="shared",
        shared_organization_id="sorg_1",
    )


def test_plan_covers_every_nonzero_weight_even_without_a_structured_source():
    plan = GapPlanner().plan(
        profile_version(),
        campaign(weights={
            "product_sector_fit": 40,
            "buyer_channel_fit": 30,
            "buying_intent": 0,
            "market_coverage": 0,
            "commercial_scale": 30,
            "trade_activity": 0,
            "contactability": 0,
        }),
        candidate(),
        reusable_facts=[],
        capabilities=[capability({"product_sector_fit"})],
    )

    assert {gap.dimension for gap in plan.gaps} == {
        "product_sector_fit", "buyer_channel_fit", "commercial_scale",
    }
    assert plan.for_dimension("product_sector_fit").route == "structured"
    assert plan.for_dimension("buyer_channel_fit").route == "agentic"
    assert plan.for_dimension("commercial_scale").route == "agentic"


def test_plan_batches_fields_that_can_be_read_from_one_official_page():
    plan = GapPlanner().plan(
        profile_version(), campaign(), candidate(), reusable_facts=[], capabilities=[],
    )

    official = [batch for batch in plan.batches if batch.source_hint == "official_site"]

    assert len(official) == 1
    assert set(official[0].fields) >= {"product_range", "company_size", "buyer_role"}


def test_fresh_reusable_fact_closes_only_its_own_dimension():
    plan = GapPlanner().plan(
        profile_version(),
        campaign(),
        candidate(),
        reusable_facts=[stored_fact("buyer_role")],
        capabilities=[],
    )

    buyer = plan.for_dimension("buyer_channel_fit")
    product = plan.for_dimension("product_sector_fit")
    assert buyer.route == "reuse"
    assert buyer.fields == []
    assert product.route == "agentic"


def test_weighted_dimensions_is_stable_and_drops_zero_weight_entries():
    weights = ScoringWeights(
        product_sector_fit=40,
        buyer_channel_fit=30,
        buying_intent=0,
        market_coverage=0,
        commercial_scale=30,
        trade_activity=0,
        contactability=0,
    )

    assert weighted_dimensions(weights) == [
        ("product_sector_fit", 40),
        ("buyer_channel_fit", 30),
        ("commercial_scale", 30),
    ]


def test_fresh_negative_attempt_suppresses_only_that_field_until_retry(tmp_path):
    db = Database(tmp_path / "gap-negative.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_a", "A", "active", "{}", stamp, stamp),
    )
    attempts = SearchAttemptRepository(db)
    profile = profile_version()
    cfg = campaign()
    lead = candidate()
    query = gap_query(profile, cfg, lead, "buyer_role", "agentic")
    attempts.record_empty(
        query_scope(query),
        research_query_hash(query, "agentic"),
        stamp + 3600,
        organization_id=query.organization_id,
        field=query.field,
        source_id="agentic",
        attempted_at=stamp,
    )

    plan = GapPlanner(search_attempts=attempts, at=stamp).plan(
        profile, cfg, lead, reusable_facts=[], capabilities=[],
    )

    buyer = plan.for_dimension("buyer_channel_fit")
    assert buyer.suppressed_fields == ["buyer_role"]
    assert "buyer_role" not in buyer.agentic_fields
    assert "buyer_type" in buyer.agentic_fields
