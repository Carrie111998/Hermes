import json

import pytest

from tests.server.test_api_mvp import make_client
from server.lead_research.candidates import CandidateRepository
from server.lead_research.identity import IdentityResolver
from server.lead_research.models import VerificationBundle
from server.lead_research.registry import ProviderRegistry
import server.lead_research.service as service_module
from server.lead_research.service import LeadResearchService
from server.lead_research.storage import EvidenceRepository
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


def test_result_views_exports_and_claims_are_filtered_and_tenant_scoped():
    app, client, headers, _ = make_research_client()
    campaign = client.post(
        "/api/v1/research-campaigns", headers=headers, json=campaign_body(),
    ).json()
    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )
    assert started.status_code == 202

    rows = app.state.db.all(
        "SELECT id,organization_id FROM research_results "
        "WHERE company_id=? AND campaign_id=? ORDER BY id",
        (campaign["company_id"], campaign["id"]),
    )
    rejected_id = rows[0]["id"]
    app.state.db.execute(
        "UPDATE research_results SET verdict='reject',lead_id=NULL,data=? WHERE id=?",
        (json.dumps({
            "reasons": ["buyer_role"],
            "missing_evidence": ["independent_source"],
            "conflicting_claims": [],
            "source_ids": ["fixture-directory"],
        }), rejected_id),
    )

    active = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results", headers=headers,
    )
    assert active.status_code == 200
    assert active.json()
    assert {row["verdict"] for row in active.json()} <= {"strong_fit", "review"}
    assert rejected_id not in {row["id"] for row in active.json()}
    assert {
        "company_name", "verdict", "fit_score", "evidence_confidence",
        "country", "buyer_role", "source_count",
    } <= set(active.json()[0])

    rejected = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results?view=rejected",
        headers=headers,
    )
    assert rejected.status_code == 200
    assert [row["id"] for row in rejected.json()] == [rejected_id]
    assert {row["verdict"] for row in rejected.json()} == {"reject"}
    assert client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/results?view=everything",
        headers=headers,
    ).status_code == 422

    active_export = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/export", headers=headers,
    )
    rejected_export = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/export?view=rejected",
        headers=headers,
    )
    assert active_export.status_code == rejected_export.status_code == 200
    assert rejected_id not in active_export.text
    assert rejected_id in rejected_export.text
    assert 'filename="research-' in active_export.headers["content-disposition"]
    assert '-rejected.csv"' in rejected_export.headers["content-disposition"]

    result_id = active.json()[0]["id"]
    claims = client.get(
        f"/api/v1/research/results/{result_id}/claims", headers=headers,
    )
    assert claims.status_code == 200 and claims.json()
    cited = [evidence for claim in claims.json() for evidence in claim["evidence"]]
    assert cited
    assert all(item["provenance_url"].startswith("https://") for item in cited)
    assert all(item["snapshot_id"] and item["raw_hash"] for item in cited)

    other = client.post(
        "/api/v1/admin/companies", headers=headers, json={"name": "Other tenant"},
    ).json()
    other_headers = {**headers, "X-Company-ID": other["id"]}
    assert client.get(
        f"/api/v1/research/results/{result_id}/claims", headers=other_headers,
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
    first_result_ids = {
        row["id"] for row in app.state.db.all(
            "SELECT id FROM research_results WHERE campaign_id=?", (campaign["id"],)
        )
    }
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
    assert {
        row["id"] for row in app.state.db.all(
            "SELECT id FROM research_results WHERE campaign_id=?", (campaign["id"],)
        )
    } == first_result_ids
    assert app.state.db.one("SELECT COUNT(*) AS n FROM leads")["n"] == 4


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
    ("failing_ids", "expected_status", "expected_partition_status", "verified", "results"),
    [
        ({"buyer-de-2"}, "partial", "partial", 1, 1),
        ({"buyer-de-1", "buyer-de-2"}, "failed", "failed", 0, 0),
    ],
)
def test_partition_failures_preserve_candidate_diagnostics(
    failing_ids, expected_status, expected_partition_status, verified, results,
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
    )["n"] == results
    assert app.state.db.one("SELECT COUNT(*) AS n FROM organizations")["n"] == results
    assert app.state.db.one("SELECT COUNT(*) AS n FROM organization_links")["n"] == results


@pytest.mark.parametrize(
    ("stage", "expected_results"),
    [
        ("identity", 0),
        ("evidence", 0),
        ("claims", 0),
        ("eligibility", 0),
        ("scoring", 0),
        ("verdict", 0),
        ("result", 0),
        ("lead", 2),
    ],
)
def test_downstream_candidate_failures_are_bounded_and_terminal(
    monkeypatch, stage, expected_results,
):
    app, client, headers, _ = make_research_client()

    def fail(*_args, **_kwargs):
        raise RuntimeError(f"injected {stage} failure")

    if stage == "identity":
        monkeypatch.setattr(IdentityResolver, "resolve", fail)
    elif stage == "evidence":
        monkeypatch.setattr(EvidenceRepository, "save_verification", fail)
    elif stage == "claims":
        monkeypatch.setattr(LeadResearchService, "_save_claim_plan", fail)
    elif stage == "eligibility":
        monkeypatch.setattr(service_module.EligibilityService, "evaluate", fail)
    elif stage == "scoring":
        monkeypatch.setattr(service_module, "score_lead", fail)
    elif stage == "verdict":
        monkeypatch.setattr(service_module, "evaluate_verdict", fail)
    elif stage == "result":
        monkeypatch.setattr(EvidenceRepository, "upsert_result", fail)
    else:
        monkeypatch.setattr(LeadResearchService, "_upsert_lead", fail)
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()

    response = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    persisted_campaign = app.state.db.one(
        "SELECT status,run_id FROM research_campaigns WHERE id=?", (campaign["id"],)
    )
    assert persisted_campaign["status"] == "failed"
    assert app.state.db.one(
        "SELECT status FROM agent_runs WHERE id=?", (persisted_campaign["run_id"],)
    )["status"] == "failed"
    source_run = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/source-runs", headers=headers,
    ).json()[0]
    assert source_run["status"] == "failed"
    assert {
        (error["candidate_source_record_id"], error["stage"])
        for error in source_run["metrics"]["errors"]
    } == {("buyer-de-1", stage), ("buyer-de-2", stage)}
    issues = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/issues", headers=headers,
    ).json()
    assert {
        (issue["data"]["candidate_source_record_id"], issue["data"]["stage"])
        for issue in issues
    } == {("buyer-de-1", stage), ("buyer-de-2", stage)}
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM research_results WHERE campaign_id=?", (campaign["id"],)
    )["n"] == expected_results
    if stage == "lead":
        assert app.state.db.one(
            "SELECT COUNT(*) AS n FROM research_results WHERE campaign_id=? AND lead_id IS NULL",
            (campaign["id"],),
        )["n"] == expected_results


def test_diagnostic_persistence_failure_cannot_escape_candidate_boundary(monkeypatch):
    app, client, headers, _ = make_research_client()

    def fail_scoring(*_args, **_kwargs):
        raise RuntimeError("injected scoring failure")

    def fail_issue(*_args, **_kwargs):
        raise RuntimeError("injected diagnostic persistence failure")

    monkeypatch.setattr(service_module, "score_lead", fail_scoring)
    monkeypatch.setattr(LeadResearchService, "_save_processing_issue", fail_issue)
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()

    response = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    persisted_campaign = app.state.db.one(
        "SELECT status,run_id FROM research_campaigns WHERE id=?", (campaign["id"],)
    )
    assert persisted_campaign["status"] == "failed"
    assert app.state.db.one(
        "SELECT status FROM agent_runs WHERE id=?", (persisted_campaign["run_id"],)
    )["status"] == "failed"
    source_run = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/source-runs", headers=headers,
    ).json()[0]
    assert {
        (error["candidate_source_record_id"], error["stage"])
        for error in source_run["metrics"]["errors"]
    } == {("buyer-de-1", "scoring"), ("buyer-de-2", "scoring")}
    assert client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/issues", headers=headers,
    ).json() == []


@pytest.mark.parametrize("failing_update", ["campaign", "agent_run"])
def test_terminal_updates_are_attempted_independently(monkeypatch, failing_update):
    app, client, headers, _ = make_research_client()
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()
    execute = app.state.db.execute

    def selectively_fail(sql, params=()):
        is_campaign_terminal = sql.startswith(
            "UPDATE research_campaigns SET status=?,updated_at=?"
        )
        is_agent_terminal = sql.startswith(
            "UPDATE agent_runs SET status=?,output=?,completed_at=?,updated_at=?"
        )
        if (failing_update == "campaign" and is_campaign_terminal) or (
            failing_update == "agent_run" and is_agent_terminal
        ):
            raise RuntimeError(f"injected {failing_update} terminal update failure")
        return execute(sql, params)

    monkeypatch.setattr(app.state.db, "execute", selectively_fail)

    response = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert response.status_code == 202
    persisted_campaign = app.state.db.one(
        "SELECT status,run_id FROM research_campaigns WHERE id=?", (campaign["id"],)
    )
    agent_status = app.state.db.one(
        "SELECT status FROM agent_runs WHERE id=?", (persisted_campaign["run_id"],)
    )["status"]
    if failing_update == "campaign":
        assert persisted_campaign["status"] == "running"
        assert agent_status == "failed"
    else:
        assert persisted_campaign["status"] == "succeeded"
        assert agent_status == "running"


class FilteringCandidates:
    def __init__(self, db, excluded_ids):
        self.repo = CandidateRepository(db)
        self.excluded_ids = set(excluded_ids)

    def select(self, **kwargs):
        return [
            candidate for candidate in self.repo.select(**kwargs)
            if candidate.source_record_id not in self.excluded_ids
        ]


def test_refresh_removes_results_for_candidates_no_longer_selected():
    app, client, headers, _ = make_research_client()
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()
    assert client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    ).json()["status"] == "succeeded"
    app.state.lead_research.candidates = FilteringCandidates(app.state.db, {"buyer-de-2"})

    refreshed = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert refreshed.json()["status"] == "succeeded"
    assert app.state.db.one(
        "SELECT COUNT(*) AS n FROM research_results WHERE campaign_id=?", (campaign["id"],)
    )["n"] == 1
    assert len(client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json()) == 1
    assert app.state.db.one("SELECT COUNT(*) AS n FROM leads")["n"] == 2


class RejectingRefreshVerifier:
    def __init__(self, provider):
        self.provider = provider
        self.definition = provider.definition

    def discover(self, query):
        return self.provider.discover(query)

    def health(self):
        return self.provider.health()

    def verify(self, query, candidate):
        bundle = self.provider.verify(query, candidate)
        independent = bundle.sources[1]
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=[independent.model_copy(update={
                "facts": {"company_name": [candidate.company_name]},
            })],
            independent_source_count=1,
        )


def test_refresh_from_strong_fit_to_reject_hides_but_preserves_prior_leads():
    app, client, headers, _ = make_research_client()
    definition = fixture_definition()
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()
    assert client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    ).json()["status"] == "succeeded"
    app.state.lead_research.registry.providers[definition.source_id] = RejectingRefreshVerifier(
        deterministic_provider(definition)
    )

    refreshed = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert refreshed.json()["status"] == "succeeded"
    results = app.state.db.all(
        "SELECT verdict,lead_id FROM research_results WHERE campaign_id=?", (campaign["id"],)
    )
    assert len(results) == 2
    assert {row["verdict"] for row in results} == {"reject"}
    assert all(row["lead_id"] is None for row in results)
    assert client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json() == []
    assert app.state.db.one("SELECT COUNT(*) AS n FROM leads")["n"] == 2


class EvidenceIdentityVerifier:
    def __init__(self, provider):
        self.provider = provider
        self.definition = provider.definition

    def discover(self, query):
        return self.provider.discover(query)

    def health(self):
        return self.provider.health()

    def verify(self, query, candidate):
        bundle = self.provider.verify(query, candidate)
        verified_name = f"Verified {candidate.company_name} GmbH"
        verified_domain = f"verified-{candidate.source_record_id}.example.test"
        sources = []
        for source in bundle.sources:
            facts = {**source.facts, "company_name": [verified_name]}
            if source.classification == "official":
                facts["domain"] = [verified_domain]
                facts["registry_id"] = [f"REG-{candidate.source_record_id}"]
                facts.pop("country", None)
                source = source.model_copy(update={
                    "provenance_url": f"https://{verified_domain}",
                    "retrieved_via": f"https://{verified_domain}",
                })
            sources.append(source.model_copy(update={"facts": facts}))
        return VerificationBundle(
            candidate_source_record_id=candidate.source_record_id,
            sources=sources,
            independent_source_count=1,
        )


def test_organization_identity_is_written_from_verified_facts_not_candidate_hints():
    app, client, headers, _ = make_research_client()
    definition = fixture_definition()
    provider = EvidenceIdentityVerifier(deterministic_provider(definition))
    app.state.lead_research = LeadResearchService(
        app.state.db,
        registry=ProviderRegistry([definition], {definition.source_id: provider}),
    )
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()

    started = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert started.json()["status"] == "succeeded"
    atlas = app.state.db.one(
        "SELECT display_name,domain FROM organizations WHERE display_name LIKE 'Verified Atlas%'"
    )
    assert dict(atlas) == {
        "display_name": "Verified Atlas DE GmbH",
        "domain": "verified-buyer-de-1.example.test",
    }
    links = {
        row["identifier_value"] for row in app.state.db.all(
            "SELECT identifier_value FROM organization_links WHERE identifier_type='domain'"
        )
    }
    assert "verified-buyer-de-1.example.test" in links
    assert "atlas-de.example.test" not in links
    leads = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json()
    atlas_lead = next(lead for lead in leads if lead["company_name"].startswith("Verified Atlas"))
    assert atlas_lead["website"] == "verified-buyer-de-1.example.test"


@pytest.mark.parametrize("match_mode", ["verified_identifier", "candidate_hint"])
def test_existing_identity_match_refreshes_verified_facts_links_and_lead(match_mode):
    app, client, headers, company_id = make_research_client()
    definition = fixture_definition()
    provider = EvidenceIdentityVerifier(deterministic_provider(definition))
    app.state.lead_research = LeadResearchService(
        app.state.db,
        registry=ProviderRegistry([definition], {definition.source_id: provider}),
    )
    resolver = IdentityResolver(app.state.db, company_id)
    matched_domain = (
        "verified-buyer-de-1.example.test"
        if match_mode == "verified_identifier"
        else "atlas-de.example.test"
    )
    existing = resolver.resolve(
        {
            "display_name": "Legacy Atlas Name",
            "domain": matched_domain,
            "country": "DE",
            "legacy_note": "preserve me",
        },
        "legacy-verified-source",
    )
    body = campaign_body()
    body["target_countries"] = ["DE"]
    campaign = client.post("/api/v1/research-campaigns", headers=headers, json=body).json()

    response = client.post(
        f"/api/v1/research-campaigns/{campaign['id']}/start", headers=headers,
    )

    assert response.status_code == 202
    assert response.json()["status"] == "succeeded"
    organization = app.state.db.one(
        "SELECT display_name,domain,country,data FROM organizations WHERE id=?",
        (existing["organization_id"],),
    )
    assert organization["display_name"] == "Verified Atlas DE GmbH"
    assert organization["domain"] == "verified-buyer-de-1.example.test"
    assert organization["country"] == "DE"
    organization_data = json.loads(organization["data"])
    assert organization_data["legacy_note"] == "preserve me"
    assert organization_data["display_name"] == "Verified Atlas DE GmbH"
    assert organization_data["registry_id"] == "REG-buyer-de-1"
    links = {
        (row["identifier_type"], row["identifier_value"], row["organization_id"])
        for row in app.state.db.all(
            "SELECT identifier_type,identifier_value,organization_id FROM organization_links"
        )
    }
    assert (
        "domain", "verified-buyer-de-1.example.test", existing["organization_id"]
    ) in links
    assert (
        "registry_id", "DE:REG-buyer-de-1", existing["organization_id"]
    ) in links
    leads = client.get(
        f"/api/v1/research-campaigns/{campaign['id']}/leads", headers=headers,
    ).json()
    refreshed_lead = next(
        lead for lead in leads
        if lead["organization_id"] == existing["organization_id"]
    )
    assert refreshed_lead["company_name"] == "Verified Atlas DE GmbH"
    assert refreshed_lead["website"] == "verified-buyer-de-1.example.test"
    assert refreshed_lead["country"] == "DE"


def test_enabling_a_catalog_source_flips_the_catalog_row():
    """Enable/disable are shared endpoints serving two different tables.

    A catalog source must be flipped in dataset_definitions; a tenant-created
    source in data_sources. Nothing else pins which row each branch touches.
    """
    app, client, headers, company_id = make_research_client()
    source_id = fixture_definition().source_id
    assert client.get("/api/v1/data-sources/catalog", headers=headers).status_code == 200

    def enabled() -> int:
        return app.state.db.one(
            "SELECT enabled FROM dataset_definitions WHERE company_id=? AND source_id=?",
            (company_id, source_id),
        )["enabled"]

    off = client.post(f"/api/v1/data-sources/{source_id}/disable", headers=headers)
    assert off.status_code == 200, off.text
    assert enabled() == 0

    on = client.post(f"/api/v1/data-sources/{source_id}/enable", headers=headers)
    assert on.status_code == 200, on.text
    assert enabled() == 1


def test_a_tenant_connected_source_still_enables_normally():
    """The catalog branch must not swallow ordinary data_sources rows."""
    _, client, headers, _ = make_research_client()
    created = client.post("/api/v1/data-sources", headers=headers, json={
        "source_type": "manual", "name": "Internal export sheet", "enabled": False,
    })
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    response = client.post(f"/api/v1/data-sources/{source_id}/enable", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["enabled"] is True
