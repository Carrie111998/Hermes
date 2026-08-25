"""A lead scan has to be traceable after it finishes, without leaking a name.

Every other run type in this codebase writes `run_events`; a lead scan wrote
none. So when a real run produced 173 "leads" nobody could reconstruct where
they came from — which markets were supplied, how many candidates cleared the
floor, what the ranker did with them — without re-running it.

The other half of the contract is what the trail must *not* contain. These runs
handle a shared candidate corpus built from a customer's contact list, and a log
line is the easiest place in the system to publish a company name or an email
into a file nobody scopes by tenant. Events carry identifiers, counts,
statuses, versions and error categories. Nothing else.
"""
from __future__ import annotations

import json
import logging

import pytest

from server.db import Database, json_dump, json_load, now
from server.lead_research.candidates import CandidateRepository
from server.lead_research.models import (
    CampaignConfig,
    DatasetDefinition,
    ProviderHealth,
    VerificationBundle,
)
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from tests.server.lead_research.fakes import cited_source


CONTACT = "buyer@atlas-kitchens.example"


def _definition(source_id: str = "observed-source") -> DatasetDefinition:
    return DatasetDefinition(
        source_id=source_id, display_name=source_id, publisher="Tests",
        access_tier="public", entity_levels=["named_company"],
        capabilities=["candidate_verification"],
        emits=["company_name", "country", "buyer_role", "product_term", "locations"],
        adapter_mode="live", default_enabled=True,
    )


class Verifier:
    """Two corroborating pages, enough to qualify."""

    def __init__(self, definition, *, fail=False):
        self.definition = definition
        self.fail = fail

    def health(self):
        return ProviderHealth(status="active")

    def verify(self, query, candidate):
        del query
        if self.fail:
            raise RuntimeError("upstream 503")
        facts = {
            "company_name": [candidate.company_name],
            "country": [candidate.country],
            "buyer_role": ["distributor"],
            "product_term": ["household-appliances", "built-in ovens", "white goods"],
            "locations": [candidate.country],
        }
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[
                cited_source(
                    provenance_url=f"https://buyer.example/{candidate.source_record_id}",
                    classification="official",
                    retrieved_via=f"https://buyer.example/{candidate.source_record_id}",
                    facts=facts,
                ),
                cited_source(
                    provenance_url=f"https://registry.example/{candidate.source_record_id}",
                    classification="independent",
                    retrieved_via="https://search.example",
                    facts=facts,
                ),
            ],
            independent_source_count=1,
            requests=2,
        )


class Harness:
    def __init__(self, tmp_path):
        self.tmp_path = tmp_path
        self.company_id = "cmp_1"
        self.campaign_id = "camp_1"
        self.db: Database | None = None

    def run(self, *, fail=False, countries=("DE", "ES")) -> dict:
        self.db = Database(self.tmp_path / f"events-{fail}-{len(countries)}.db")
        stamp = now()
        self.db.execute(
            "INSERT INTO companies(id,name,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (self.company_id, "Tenant", "active", "{}", stamp, stamp),
        )
        rows = [
            {
                # A person's name and address sit in the corpus row exactly as
                # they would after a contact-list import. Nothing in the trail
                # may echo either.
                "source_record_id": f"buyer-{country.lower()}-{index}",
                "company_name": f"Atlas Kitchens {country} {index}",
                "country": country,
                "categories": ["household-appliances"],
                "buyer_types": ["distributor"],
                "primary_email": CONTACT,
                "contact_person": "Jane Roe",
            }
            for country in countries
            for index in (1, 2)
        ]
        CandidateRepository(self.db).import_file(
            "buyers", "1", "buyers.jsonl",
            "\n".join(json.dumps(row) for row in rows).encode(),
        )
        definition = _definition()
        config = CampaignConfig(
            name="Appliance buyers",
            seller_countries=["TR"],
            target_countries=list(countries),
            sector_ids=["household-appliances"],
            buyer_types=["distributor"],
            enabled_source_ids=[definition.source_id],
        )
        self.db.execute(
            "INSERT INTO research_campaigns(id,company_id,name,status,version,config,estimate,"
            "run_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (self.campaign_id, self.company_id, config.name, "draft", 1,
             json_dump(config.model_dump(mode="json")), None, None, stamp, stamp),
        )
        service = LeadResearchService(self.db, registry=ProviderRegistry(
            [definition], {definition.source_id: Verifier(definition, fail=fail)},
        ))
        return service.run(self.company_id, self.campaign_id)

    def events(self, run_id: str) -> list[dict]:
        return [
            dict(row) for row in self.db.all(
                "SELECT kind,message,data FROM run_events WHERE run_id=? ORDER BY id",
                (run_id,),
            )
        ]


@pytest.fixture()
def harness(tmp_path):
    return Harness(tmp_path)


def test_campaign_run_has_traceable_count_only_events(harness, caplog):
    with caplog.at_level(logging.INFO):
        output = harness.run()
    events = harness.events(output["run_id"])

    assert [row["kind"] for row in events] == [
        "lead_research_started",
        "lead_research_market_supplied",
        "lead_research_market_supplied",
        "lead_research_ranked",
        "lead_research_completed",
    ]
    text = "\n".join(row["data"] for row in events) + caplog.text
    assert "Atlas Kitchens" not in text
    assert "Jane Roe" not in text
    assert "@" not in text
    ranked = json_load(events[-2]["data"], {})
    assert ranked["qualified_leads"] <= 15
    assert ranked["strong_fit_pool"] >= ranked["qualified_leads"]
    assert ranked["countries_represented"] >= 1


def test_the_terminal_event_carries_status_and_the_funnel(harness):
    output = harness.run()
    completed = json_load(harness.events(output["run_id"])[-1]["data"], {})

    assert completed["status"] == "succeeded"
    assert completed["campaign_id"] == harness.campaign_id
    assert completed["qualified_leads"] == output["metrics"]["qualified_leads"]
    assert completed["provider_requests"] == output["metrics"]["provider_requests"]
    assert completed["review_candidates"] == output["metrics"]["review_candidates"]


def test_a_failed_partition_still_records_a_terminal_status_and_category(harness):
    output = harness.run(fail=True)
    events = harness.events(output["run_id"])
    completed = json_load(events[-1]["data"], {})

    assert output["status"] in {"failed", "partial"}
    assert [row["kind"] for row in events][0] == "lead_research_started"
    assert events[-1]["kind"] == "lead_research_completed"
    assert completed["status"] == output["status"]
    assert completed["failed_source_ids"] == ["observed-source"]
    assert completed["qualified_leads"] == 0
    assert "503" not in json_dump(completed), (
        "an upstream message is not an error category"
    )


def test_a_market_supply_event_names_the_market_by_iso_code_only(harness):
    output = harness.run()
    supplied = [
        json_load(row["data"], {}) for row in harness.events(output["run_id"])
        if row["kind"] == "lead_research_market_supplied"
    ]

    assert {row["country"] for row in supplied} == {"DE", "ES"}
    assert all(row["research_limit"] >= 3 for row in supplied)
    assert all(isinstance(row["supplied"], int) for row in supplied)
    assert all("company_name" not in row for row in supplied)


def test_an_unwritable_event_never_fails_the_run(harness, monkeypatch, caplog):
    """A trail is diagnostic. Losing it must not lose the campaign."""
    real_execute = Database.execute

    def selective(self, sql, params=()):
        if "run_events" in sql:
            raise RuntimeError("run_events is unavailable")
        return real_execute(self, sql, params)

    monkeypatch.setattr(Database, "execute", selective)
    with caplog.at_level(logging.ERROR):
        output = harness.run()

    assert output["status"] == "succeeded"
    assert output["metrics"]["qualified_leads"] >= 1
    assert "run event" in caplog.text
