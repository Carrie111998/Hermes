"""What a run spent, measured rather than estimated.

`agent_runs.cost` was inserted as 0 and never updated, and nothing anywhere
counted an outbound request — while the plan reasoned about 90 requests versus
16,500 and the whole cost model of the system rests on Web Unlocker fetches. A
run could not say what it had spent.

A provider reports its own spend, because only it knows: one `verify` is zero
fetches for a local corpus and up to four for a web verifier.
"""
from __future__ import annotations

import json

import httpx
import pytest

from server.db import Database, json_dump, now
from server.lead_research.candidates import CandidateRecord, CandidateRepository
from server.lead_research.models import (
    CampaignConfig,
    DatasetDefinition,
    DiscoveryQuery,
    ProviderHealth,
    VerificationBundle,
    VerificationSource,
)
from server.lead_research.providers.bright_data import BrightDataVerifier
from server.lead_research.providers.corpus import CorpusProvider
from server.lead_research.providers.ted import TedVerifier
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService


def _query(**overrides) -> DiscoveryQuery:
    return DiscoveryQuery(**{
        "campaign_id": "camp_1", "seller_countries": ["TR"], "target_countries": ["DE"],
        "sector_ids": ["household-appliances"], "hs_codes": [], "buyer_types": ["distributor"],
        **overrides,
    })


def _candidate(**overrides) -> CandidateRecord:
    return CandidateRecord(**{
        "dataset_id": "buyers", "version": "1", "source_record_id": "buyer-de-1",
        "company_name": "Atlas Handel", "normalized_name": "atlas handel",
        "country": "DE", "domain": "atlas.example", "data": {},
        **overrides,
    })


# ── Bright Data: every fetch is a paid request ────────────────────────────────

def _unlocker(pages: list[str]) -> tuple[BrightDataVerifier, list[str]]:
    fetched: list[str] = []
    remaining = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(json.loads(request.content)["url"])
        return httpx.Response(
            200, text=remaining.pop(0) if remaining else "no content here"
        )

    verifier = BrightDataVerifier(
        "key", "zone", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    return verifier, fetched


def test_bright_data_reports_one_request_per_page_it_fetched():
    body = "Atlas Handel is a distributor of household-appliances in DE."
    verifier, fetched = _unlocker([body, body, body, body])

    bundle = verifier.verify(_query(), _candidate())

    assert bundle.requests == len(fetched)
    assert bundle.requests == 4, "one official page plus three searches"


def test_a_candidate_with_no_domain_costs_one_request_less():
    """No known website means no official page to fetch."""
    body = "Atlas Handel is a distributor of household-appliances in DE."
    verifier, fetched = _unlocker([body, body, body])

    bundle = verifier.verify(_query(), _candidate(domain=None))

    assert bundle.requests == len(fetched) == 3


def test_an_abstention_still_reports_what_it_spent():
    """The pages were fetched whether or not anything in them was usable.

    A bundle that reported 0 here would make the runs that found nothing look
    like the cheapest ones.

    No candidate domain, because a reachable official page always yields at
    least a `domain` fact — so that is the only shape that truly abstains.
    """
    verifier, fetched = _unlocker(["nothing relevant", "still nothing", "no"])

    bundle = verifier.verify(_query(), _candidate(domain=None))

    assert bundle.sources == []
    assert bundle.requests == len(fetched) > 0


# ── TED: one search, two calls when it backs off ──────────────────────────────

def _ted(responses: list[httpx.Response]) -> TedVerifier:
    remaining = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return remaining.pop(0)

    return TedVerifier(client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_ted_reports_one_request_for_one_search():
    verifier = _ted([httpx.Response(200, json={"notices": []})])

    assert verifier.verify(_query(), _candidate()).requests == 1


def test_a_rate_limited_retry_is_a_second_request():
    """429 is routine on TED, and the backoff call is a real request."""
    verifier = _ted([
        httpx.Response(429),
        httpx.Response(200, json={"notices": []}),
    ])

    assert verifier.verify(_query(), _candidate()).requests == 2


def test_a_candidate_with_no_searchable_name_costs_nothing():
    verifier = _ted([])

    bundle = verifier.verify(
        _query(), _candidate(company_name="///", normalized_name="")
    )

    assert bundle.requests == 0


# ── the corpus spends nothing ─────────────────────────────────────────────────

def test_a_local_corpus_reports_no_requests():
    bundle = CorpusProvider().verify(_query(), _candidate(
        data={"provenance_url": "https://registry.example/atlas"},
    ))

    assert bundle.sources and bundle.requests == 0


# ── what a run reports ────────────────────────────────────────────────────────

class SpendingVerifier:
    def __init__(self, definition, cost=3):
        self.definition = definition
        self.cost = cost

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[VerificationSource(
                provenance_url=f"https://registry.example/{candidate.source_record_id}",
                raw_hash="c" * 64,
                classification="independent",
                retrieved_via="https://search.example",
                facts={
                    "company_name": [candidate.company_name],
                    "country": [candidate.country],
                    "buyer_role": ["distributor"],
                },
            )],
            independent_source_count=1,
            requests=self.cost,
        )


@pytest.fixture()
def harness(tmp_path):
    db = Database(tmp_path / "metering.db")
    stamp = now()
    db.execute(
        "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
        ("cmp_1", "Tenant", "active", "{}", stamp, stamp),
    )
    CandidateRepository(db).import_file(
        "buyers", "1", "candidates.jsonl",
        b"\n".join(
            json.dumps({
                "source_record_id": f"buyer-de-{index}",
                "company_name": f"Buyer {index} DE",
                "country": "DE",
                "categories": ["household-appliances"],
            }).encode()
            for index in (1, 2)
        ),
    )
    return db


def _run(db, verifier, definition, campaign_id="camp_1", **config_overrides):
    config = CampaignConfig(**{
        "name": "German appliance distributors",
        "target_countries": ["DE"],
        "sector_ids": ["household-appliances"],
        "buyer_types": ["distributor"],
        "enabled_source_ids": [definition.source_id],
        **config_overrides,
    })
    stamp = now()
    db.execute(
        "INSERT INTO research_campaigns VALUES(?,?,?,?,?,?,?,?,?,?)",
        (campaign_id, "cmp_1", config.name, "draft", 1,
         json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
    )
    service = LeadResearchService(
        db, registry=ProviderRegistry([definition], {definition.source_id: verifier})
    )
    return service, service.run("cmp_1", campaign_id)


def _definition() -> DatasetDefinition:
    return DatasetDefinition(
        source_id="spending-source",
        display_name="Spending source",
        publisher="Tests",
        access_tier="public",
        entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        emits=["company_name", "country", "buyer_role"],
        adapter_mode="live",
        default_enabled=True,
    )


def test_a_run_reports_the_requests_it_spent(harness):
    """The regression this file exists for."""
    definition = _definition()

    _, result = _run(harness, SpendingVerifier(definition, cost=3), definition)

    assert result["metrics"]["provider_requests"] == 6, "two candidates at three each"


def test_spend_is_reported_per_partition(harness):
    definition = _definition()

    _run(harness, SpendingVerifier(definition, cost=4), definition)

    partition = harness.one(
        "SELECT metrics FROM campaign_partitions WHERE campaign_id='camp_1'"
    )
    assert json.loads(partition["metrics"])["provider_requests"] == 8


def test_a_reused_rerun_reports_no_spend(harness):
    """The saving from evidence reuse has to be visible, or it is a claim.

    This is the number that says whether the cache is working.
    """
    definition = _definition()
    service, first = _run(harness, SpendingVerifier(definition), definition)

    second = service.run("cmp_1", "camp_1")

    assert first["metrics"]["provider_requests"] > 0
    assert second["metrics"]["provider_requests"] == 0


def test_a_cancelled_run_reports_only_what_it_had_already_spent(harness):
    definition = _definition()
    service, _ = _run(
        harness, SpendingVerifier(definition, cost=3), definition,
        refresh={"schedule": "monthly", "reuse_public_cache": False},
    )
    harness.execute(
        "UPDATE research_campaigns SET status='cancelled' WHERE id='camp_1'"
    )

    result = service.run("cmp_1", "camp_1")

    assert result["status"] == "cancelled"
    assert result["metrics"].get("provider_requests", 0) == 0


def test_the_tenant_rollup_keeps_requests_and_model_spend_apart(harness, tmp_path):
    """Requests and tokens are different units; one summed number means nothing."""
    from server.routes.operations import admin_costs
    from types import SimpleNamespace

    definition = _definition()
    _run(harness, SpendingVerifier(definition, cost=3), definition)

    rows = admin_costs(SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db=harness))), None)

    row = next(item for item in rows if item["company_id"] == "cmp_1")
    assert row["provider_requests"] == 6
    assert row["provider_requests_metered"] is True
    # Model spend is still not measured, and still says so rather than claiming 0.
    assert row["metering_enabled"] is False
