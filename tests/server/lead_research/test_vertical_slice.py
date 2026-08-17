import json

import pytest

from tests.server.test_api_mvp import make_client
from server.lead_research.candidates import CandidateRepository
from server.lead_research.registry import ProviderRegistry
from server.lead_research.service import LeadResearchService
from tests.server.lead_research.fakes import deterministic_provider, fixture_definition


def make_research_client():
    app, client, headers, company_id = make_client()
    definition = fixture_definition()
    provider = deterministic_provider(definition)
    registry = ProviderRegistry([definition], {definition.source_id: provider})
    app.state.lead_research = LeadResearchService(app.state.db, registry=registry)
    candidates = [
        {
            "source_record_id": f"buyer-{country.lower()}-{index}",
            "company_name": f"{name} {country}",
            "country": country,
            "domain": f"https://{name.lower()}-{country.lower()}.example.test",
            "categories": ["household-appliances"],
            "buyer_types": ["distributor"],
        }
        for country in ("DE", "AT")
        for index, name in ((1, "Atlas"), (2, "Northstar"))
    ]
    CandidateRepository(app.state.db).import_file(
        "household-appliances",
        "2026-08",
        "candidates.jsonl",
        "\n".join(json.dumps(item) for item in candidates).encode(),
    )
    return app, client, headers, company_id


def campaign_body(name="DACH appliance distributors"):
    return {
        "name": name,
        "seller_countries": ["TR"],
        "target_countries": ["DE", "AT"],
        "sector_ids": ["household-appliances"],
        "buyer_types": ["importer", "distributor", "retailer", "wholesaler"],
        "enabled_source_ids": ["fixture-directory"],
    }


def test_research_campaign_vertical_slice_and_tenant_scope():
    app, client, headers, _ = make_research_client()
    created = client.post("/api/v1/research-campaigns", headers=headers, json=campaign_body())
    assert created.status_code == 201, created.text
    campaign = created.json()
    assert campaign["config"]["seller_countries"] == ["TR"]

    estimate = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/estimate", headers=headers,
    )
    assert estimate.status_code == 200
    assert estimate.json()["status"] == "available"
    assert estimate.json()["qualified_range"]

    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )
    assert started.status_code == 202, started.text
    assert started.json()["status"] == "succeeded"

    results = app.state.db.all(
        "SELECT verdict,lead_id FROM research_results WHERE company_id=? AND campaign_id=?",
        (campaign["company_id"], campaign["id"]),
    )
    assert results
    assert {row["verdict"] for row in results} == {"strong_fit"}
    assert all(row["lead_id"] for row in results)

    metrics = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/metrics", headers=headers,
    ).json()[0]
    assert metrics["raw_records"] >= metrics["named_candidates"] >= metrics["qualified_leads"] > 0
    assert metrics["resolved_organizations"] >= metrics["eligible_companies"] >= metrics["qualified_leads"]

    leads = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json()
    assert leads
    assert {"fit_score", "evidence_confidence", "source_ids"} <= set(leads[0])
    claims = client.get(f"/api/v1/research/leads/{leads[0]['id']}/claims", headers=headers)
    assert claims.status_code == 200 and claims.json()[0]["evidence"]

    export = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/export", headers=headers,
    )
    assert export.status_code == 200
    assert "fit_score" in export.text and "evidence_confidence" in export.text

    other = client.post("/api/v1/admin/companies", headers=headers, json={"name": "Other tenant"}).json()
    other_headers = {**headers, "X-Company-ID": other["id"]}
    assert client.get(
        f"/api/v1/research-campaigns/{campaign['id']}", headers=other_headers,
    ).status_code == 404


def test_source_lifecycle_copy_matches_behavior_and_purge_needs_exact_name():
    _, client, headers, _ = make_research_client()
    catalog = client.get("/api/v1/data-sources/catalog", headers=headers).json()
    fixture = next(item for item in catalog if item["source_id"] == "fixture-directory")
    disabled = client.post(
        "/api/v1/data-sources/fixture-directory/disable", headers=headers,
    )
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    installed = client.post(
        "/api/v1/data-sources/fixture-directory/enable", headers=headers,
    )
    assert installed.status_code == 200 and installed.json()["enabled"] is True
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=campaign_body()).json()
    assert client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    ).status_code == 202
    lead_id = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json()[0]["id"]
    rejected = client.post(
        "/api/v1/data-sources/fixture-directory/purge", headers=headers,
        json={"confirmation": "wrong"},
    )
    assert rejected.status_code == 422
    purged = client.post(
        "/api/v1/data-sources/fixture-directory/purge", headers=headers,
        json={"confirmation": fixture["display_name"]},
    )
    assert purged.status_code == 200 and purged.json()["purged"] is True
    lead = client.get(f"/api/v1/leads/{lead_id}", headers=headers).json()
    assert lead["status"] == "unqualified_after_source_removal"
    assert lead["evidence_confidence"] == 0


def test_succeeded_campaign_can_refresh_without_duplicate_runtime_state():
    app, client, headers, _ = make_research_client()
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=campaign_body()).json()
    first = client.post(f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers)
    second = client.post(f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers)
    assert first.status_code == second.status_code == 202
    metrics = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/metrics", headers=headers,
    ).json()
    assert len(metrics) == 1
    assert metrics[0]["resolved_organizations"] >= metrics[0]["eligible_companies"]
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM research_results WHERE campaign_id=?", (campaign["id"],)
    )["n"] == 4


class SelectivelyFailingVerifier:
    def __init__(self, provider, failing_ids):
        self.provider = provider
        self.definition = provider.definition
        self.failing_ids = set(failing_ids)

    def discover(self, query):
        return self.provider.discover(query)

    def health(self):
        return self.provider.health()

    def verify(self, query, candidate):
        if candidate.source_record_id in self.failing_ids:
            raise RuntimeError(f"verification unavailable for {candidate.source_record_id}")
        return self.provider.verify(query, candidate)


@pytest.mark.parametrize(
    ("failing_ids", "expected_status", "expected_partition_status", "verified"),
    [
        ({"buyer-de-2"}, "partial", "partial", 1),
        ({"buyer-de-1", "buyer-de-2"}, "failed", "failed", 0),
    ],
)
def test_partition_failures_preserve_candidate_diagnostics(
    failing_ids, expected_status, expected_partition_status, verified,
):
    app, client, headers, _ = make_research_client()
    definition = fixture_definition()
    provider = SelectivelyFailingVerifier(deterministic_provider(definition), failing_ids)
    registry = ProviderRegistry([definition], {definition.source_id: provider})
    app.state.lead_research = LeadResearchService(app.state.db, registry=registry)
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()

    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert started.status_code == 202
    assert started.json()["status"] == expected_status
    source_run = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/source-runs", headers=headers,
    ).json()[0]
    assert source_run["status"] == expected_partition_status
    assert source_run["error_category"] == "verification_error"
    assert source_run["metrics"]["verified_candidates"] == verified
    assert {
        error["candidate_source_record_id"] for error in source_run["metrics"]["errors"]
    } == failing_ids
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM research_results WHERE campaign_id=?", (campaign["id"],)
    )["n"] == 2
